"""ToolRegistry: plugin discovery, hot-reload watcher, risk-gated calls, promotion.

Plugin layout (under ``plugins_dir``)::

    active/<name>/{manifest.json, tool.json, impl.py, fixtures.json}
    inbox/<name>/{manifest.json, tool.json, impl.py, fixtures.json}
    _archive/<name>_<timestamp>/...

``impl.py`` must expose ``def run(args: dict, ctx: dict) -> dict``.
``manifest.json``: {name, version, description, trust}  trust ∈ trusted|untrusted.
``tool.json``: JSON-schema-ish description of the tool's arguments.
``fixtures.json``: list of {name, args, expect} offline test cases.

Hot reload: :meth:`watch` starts a 1s polling thread; on manifest/impl change the
plugin is re-imported under a version/hash-qualified module name (cache-bust).
The last 3 loaded versions are kept; a failed reload rolls back to the previous
working version and is recorded in ``tool_versions`` (status=rolled_back).

Risk gate: :meth:`call` on an ``untrusted`` tool whose cumulative human
confirmations are below ``confirm_threshold`` (default K=3) consults
``easyagent.decisions`` ``risk_gate`` (lazy import; any failure defaults to
requiring a human). If P(risky) >= threshold (0.5) it raises
:exc:`ApprovalRequired` instead of running the tool.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any


class ApprovalRequired(Exception):
    """Raised when an untrusted tool call needs human approval first."""

    def __init__(self, tool_name: str, args: dict | None = None):
        self.tool_name = tool_name
        self.args = args or {}
        super().__init__(
            f"human approval required before calling untrusted tool '{tool_name}'"
        )


@dataclass
class ToolDef:
    """A loaded tool: metadata + the imported impl module (or shim)."""

    name: str
    version: str
    description: str
    trust: str  # trusted | untrusted
    module: Any = field(repr=False)
    tool_json: dict = field(default_factory=dict, repr=False)
    manifest_path: str = ""
    loaded_key: str = ""

    def run(self, args: dict, ctx: dict | None = None) -> dict:
        return self.module.run(args or {}, ctx or {})


def _read_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _content_key(plugin_dir: str) -> str:
    """Hash of manifest.json + tool.json + impl.py (cache-bust on any change)."""
    h = hashlib.sha256()
    for fname in ("manifest.json", "tool.json", "impl.py"):
        p = os.path.join(plugin_dir, fname)
        if os.path.exists(p):
            with open(p, "rb") as f:
                h.update(f.read())
        h.update(b"\x00")
    return h.hexdigest()[:12]


def _match_expect(result: Any, expect: dict) -> tuple[bool, str]:
    """Check one fixture's ``expect`` dict against the tool result."""
    if not isinstance(result, dict):
        result = {"result": result}
    for key, want in (expect or {}).items():
        if key == "ok":
            if bool(result.get("ok")) != bool(want):
                return False, f"expect ok={want}, got {result.get('ok')!r}"
        elif key == "stdout_contains":
            if str(want) not in str(result.get("stdout", "")):
                return False, f"stdout missing {want!r}"
        elif key == "stderr_contains":
            if str(want) not in str(result.get("stderr", "")):
                return False, f"stderr missing {want!r}"
        elif key == "equals" and isinstance(want, dict):
            for k, v in want.items():
                if result.get(k) != v:
                    return False, f"equals: {k} expected {v!r}, got {result.get(k)!r}"
        elif key == "result_contains":
            blob = json.dumps(result, ensure_ascii=False, default=str)
            if str(want) not in blob:
                return False, f"result missing {want!r}"
        else:  # unknown key: plain equality against the result field
            if result.get(key) != want:
                return False, f"{key} expected {want!r}, got {result.get(key)!r}"
    return True, ""


class ToolRegistry:
    """Registry of tools loaded from the plugins directory."""

    KEEP_VERSIONS = 3

    def __init__(
        self,
        plugins_dir: str,
        store: Any = None,
        confirm_threshold: int = 3,
        watch_interval: float = 1.0,
        decisions: Any = None,
    ):
        self.plugins_dir = os.path.abspath(plugins_dir)
        self.active_dir = os.path.join(self.plugins_dir, "active")
        self.inbox_dir = os.path.join(self.plugins_dir, "inbox")
        self.archive_dir = os.path.join(self.plugins_dir, "_archive")
        self.store = store
        self.confirm_threshold = confirm_threshold
        self.watch_interval = watch_interval
        # Optional injected decisions provider (tests / MissionRunner share
        # one instance); falls back to decisions.make_provider() when unset.
        self.decisions = decisions
        self._lock = threading.RLock()
        self._tools: dict[str, ToolDef] = {}
        self._versions: dict[str, list[ToolDef]] = {}  # newest first, ≤ KEEP_VERSIONS
        self._keys: dict[str, str] = {}  # name -> content key of loaded version
        self._confirmations: dict[str, int] = {}
        self._last_fail_key: dict[str, str] = {}  # name -> content key of last recorded rollback
        self._watch_thread: threading.Thread | None = None
        self._watch_stop = threading.Event()

    # ------------------------------------------------------------------ loading
    def _load_impl_module(self, name: str, plugin_dir: str, key: str):
        impl_path = os.path.join(plugin_dir, "impl.py")
        if not os.path.exists(impl_path):
            raise FileNotFoundError(f"{impl_path} missing")
        # Drop any cached bytecode: pyc invalidation is (second-mtime, size),
        # so rapid same-size edits within one second would load stale code.
        try:
            pyc = importlib.util.cache_from_source(impl_path)
            if os.path.exists(pyc):
                os.remove(pyc)
        except Exception:
            pass
        module_name = f"easyagent_plugin_{name}_{key}".replace("-", "_")
        spec = importlib.util.spec_from_file_location(module_name, impl_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot build import spec for {impl_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # noqa: S102 - plugins are local code under review
        if not callable(getattr(module, "run", None)):
            raise TypeError(f"{impl_path} must expose def run(args, ctx)")
        return module

    def _load_plugin(self, name: str, plugin_dir: str | None = None) -> ToolDef:
        plugin_dir = plugin_dir or os.path.join(self.active_dir, name)
        manifest = _read_json(os.path.join(plugin_dir, "manifest.json"))
        tool_json = {}
        tool_json_path = os.path.join(plugin_dir, "tool.json")
        if os.path.exists(tool_json_path):
            tool_json = _read_json(tool_json_path)
        key = f"{manifest.get('version', '0.0.0')}@{_content_key(plugin_dir)}"
        module = self._load_impl_module(name, plugin_dir, key)
        return ToolDef(
            name=manifest.get("name", name),
            version=str(manifest.get("version", "0.0.0")),
            description=manifest.get("description", ""),
            trust=manifest.get("trust", "untrusted"),
            module=module,
            tool_json=tool_json,
            manifest_path=os.path.join(plugin_dir, "manifest.json"),
            loaded_key=key,
        )

    def _record(self, name: str, version: str, manifest: dict, status: str,
                extra: dict | None = None) -> None:
        if self.store is None:
            return
        try:
            payload = dict(manifest)
            if extra:
                payload.update(extra)
            self.store.record_tool_version(
                name, version, json.dumps(payload, ensure_ascii=False), status
            )
        except Exception:
            pass  # version bookkeeping must never break the hot path

    def _register(self, tooldef: ToolDef, status: str = "active") -> None:
        with self._lock:
            prev = self._tools.get(tooldef.name)
            if prev is not None:
                hist = self._versions.setdefault(tooldef.name, [])
                hist.insert(0, prev)
                del hist[self.KEEP_VERSIONS :]
            self._tools[tooldef.name] = tooldef
            self._keys[tooldef.name] = tooldef.loaded_key
        self._record(tooldef.name, tooldef.version,
                     {"description": tooldef.description, "trust": tooldef.trust,
                      "loaded_key": tooldef.loaded_key}, status)

    # ------------------------------------------------------------------ discover
    def discover(self, plugins_dir: str | None = None) -> list[str]:
        """Scan ``active/*/`` and (re)load every plugin found. Returns names."""
        active = os.path.join(os.path.abspath(plugins_dir or self.plugins_dir), "active")
        loaded: list[str] = []
        if not os.path.isdir(active):
            return loaded
        for name in sorted(os.listdir(active)):
            plugin_dir = os.path.join(active, name)
            manifest_path = os.path.join(plugin_dir, "manifest.json")
            if not (os.path.isdir(plugin_dir) and os.path.exists(manifest_path)):
                continue
            try:
                tooldef = self._load_plugin(name, plugin_dir)
            except Exception:
                continue  # broken plugin at startup: skip, watcher may pick it up later
            self._register(tooldef)
            loaded.append(tooldef.name)
        return loaded

    # ------------------------------------------------------------------ watcher
    def watch(self) -> threading.Thread:
        """Start the 1s polling hot-reload watcher thread (daemon)."""
        with self._lock:
            if self._watch_thread and self._watch_thread.is_alive():
                return self._watch_thread
            self._watch_stop.clear()
            self._watch_thread = threading.Thread(
                target=self._watch_loop, name="tool-registry-watch", daemon=True
            )
            self._watch_thread.start()
            return self._watch_thread

    def stop_watch(self) -> None:
        self._watch_stop.set()
        t = self._watch_thread
        if t and t.is_alive():
            t.join(timeout=2.0)

    def _watch_loop(self) -> None:
        while not self._watch_stop.wait(self.watch_interval):
            try:
                self._poll_once()
            except Exception:
                pass

    def _poll_once(self) -> None:
        if not os.path.isdir(self.active_dir):
            return
        seen: set[str] = set()
        for name in sorted(os.listdir(self.active_dir)):
            plugin_dir = os.path.join(self.active_dir, name)
            manifest_path = os.path.join(plugin_dir, "manifest.json")
            if not (os.path.isdir(plugin_dir) and os.path.exists(manifest_path)):
                continue
            seen.add(name)
            key = f"{_read_json(manifest_path).get('version', '0.0.0')}@{_content_key(plugin_dir)}"
            with self._lock:
                current_key = self._keys.get(name)
            if current_key is None:
                self._reload(name, plugin_dir)  # brand-new plugin dir
            elif key != current_key:
                self._reload(name, plugin_dir)  # changed -> hot reload
        with self._lock:
            for name in [n for n in self._tools if n not in seen]:
                del self._tools[name]
                self._keys.pop(name, None)

    def _reload(self, name: str, plugin_dir: str) -> None:
        """Reload one plugin; on failure keep the previous version (rollback)."""
        try:
            tooldef = self._load_plugin(name, plugin_dir)
        except Exception as exc:
            with self._lock:
                prev = self._tools.get(name)
                fail_key = f"{_content_key(plugin_dir)}"
                if self._last_fail_key.get(name) == fail_key:
                    return  # same broken content: don't spam tool_versions
                self._last_fail_key[name] = fail_key
            self._record(name, getattr(prev, "version", "?"),
                         {"error": f"{type(exc).__name__}: {exc}"}, "rolled_back")
            return
        with self._lock:
            self._last_fail_key.pop(name, None)  # healthy again: reset dedup
        self._register(tooldef)

    # ------------------------------------------------------------------ access
    def get(self, name: str) -> ToolDef | None:
        with self._lock:
            return self._tools.get(name)

    def list_tools(self) -> list[dict]:
        with self._lock:
            tools = list(self._tools.values())
        return [
            {"name": t.name, "version": t.version,
             "description": t.description, "trust": t.trust,
             "schema": (t.tool_json or {}).get("schema") or {}}
            for t in tools
        ]

    def versions(self, name: str) -> list[dict]:
        """Metadata of retained versions, newest first (≤ KEEP_VERSIONS)."""
        with self._lock:
            hist = [self._tools[name]] + self._versions.get(name, []) if name in self._tools else []
        return [{"version": t.version, "loaded_key": t.loaded_key} for t in hist]

    def confirmations(self, name: str) -> int:
        return self._confirmations.get(name, 0)

    def record_confirmation(self, name: str) -> int:
        """Record one human confirmation for a tool. Returns the new count."""
        with self._lock:
            self._confirmations[name] = self._confirmations.get(name, 0) + 1
            return self._confirmations[name]

    def register_tool(self, name: str, run_fn, trust: str = "trusted",
                      description: str = "", version: str = "0.0.0",
                      schema: dict | None = None) -> ToolDef:
        """Programmatic registration (used for built-in tools)."""
        tooldef = ToolDef(
            name=name, version=version, description=description, trust=trust,
            module=SimpleNamespace(run=run_fn), loaded_key=f"{version}@builtin",
            tool_json={"schema": schema or {}},
        )
        self._register(tooldef)
        return tooldef

    # ------------------------------------------------------------------ risk gate
    def _risk_gate(self, tool_name: str, args: dict) -> bool:
        """True = risky, needs a human. Fail-closed: any error → require human."""
        try:
            decisions = importlib.import_module("easyagent.decisions")
            provider = self.decisions
            if provider is None:
                provider = decisions.make_provider()
            # risk_gate is a module-level function in easyagent.decisions,
            # not a method on the provider.
            return bool(decisions.risk_gate(tool_name, args or {}, provider=provider))
        except Exception:
            return True

    def call(self, name: str, args: dict | None = None, ctx: dict | None = None) -> dict:
        """Run a tool, enforcing the untrusted-tool risk gate."""
        tool = self.get(name)
        if tool is None:
            raise KeyError(f"unknown tool: {name}")
        if (tool.trust == "untrusted"
                and self.confirmations(name) < self.confirm_threshold):
            if self._risk_gate(name, args or {}):
                raise ApprovalRequired(name, args or {})
        return tool.run(args or {}, ctx or {})

    def call_approved(self, name: str, args: dict | None = None,
                      ctx: dict | None = None) -> dict:
        """Run a tool after a human approved it (records the confirmation)."""
        self.record_confirmation(name)
        tool = self.get(name)
        if tool is None:
            raise KeyError(f"unknown tool: {name}")
        return tool.run(args or {}, ctx or {})

    # ------------------------------------------------------------------ promote
    def promote(self, name: str) -> dict:
        """Run inbox fixtures offline; on success move to active/ and register.

        Returns {"ok": True, ...} or {"ok": False, "failures"/"reason": ...}.
        Failures stay in inbox and are recorded as status=rejected.
        """
        src = os.path.join(self.inbox_dir, name)
        manifest_path = os.path.join(src, "manifest.json")
        fixtures_path = os.path.join(src, "fixtures.json")
        if not os.path.isdir(src) or not os.path.exists(manifest_path):
            return {"ok": False, "reason": f"no inbox plugin named '{name}'"}
        try:
            manifest = _read_json(manifest_path)
            fixtures = _read_json(fixtures_path) if os.path.exists(fixtures_path) else []
        except Exception as exc:
            return {"ok": False, "reason": f"bad manifest/fixtures: {exc}"}
        version = str(manifest.get("version", "0.0.0"))
        key = f"{version}@{_content_key(src)}"
        try:
            module = self._load_impl_module(name, src, f"promote_{key}")
        except Exception as exc:
            self._record(name, version, dict(manifest),
                         "rejected", {"reason": f"impl load failed: {exc}"})
            return {"ok": False, "reason": f"impl.py failed to load: {exc}"}

        failures: list[dict] = []
        for fx in fixtures or []:
            fx_name = fx.get("name", "?")
            try:
                result = module.run(fx.get("args") or {}, {})
            except Exception as exc:
                failures.append({"fixture": fx_name,
                                 "error": f"{type(exc).__name__}: {exc}"})
                continue
            ok, why = _match_expect(result, fx.get("expect") or {})
            if not ok:
                failures.append({"fixture": fx_name, "error": why, "result": result})
        if failures:
            self._record(name, version, dict(manifest),
                         "rejected", {"failures": failures})
            return {"ok": False, "failures": failures}

        dst = os.path.join(self.active_dir, name)
        try:
            os.makedirs(self.active_dir, exist_ok=True)
            if os.path.exists(dst):
                os.makedirs(self.archive_dir, exist_ok=True)
                stamp = time.strftime("%Y%m%d%H%M%S")
                shutil.move(dst, os.path.join(self.archive_dir, f"{name}_{stamp}"))
            shutil.move(src, dst)
            tooldef = self._load_plugin(name, dst)
        except Exception as exc:
            return {"ok": False, "reason": f"activation failed: {exc}"}
        self._register(tooldef, status="promoted")
        return {"ok": True, "name": tooldef.name, "version": tooldef.version,
                "fixtures_passed": len(fixtures or [])}


# ------------------------------------------------- module-level glue (server.py)
_default_registry: "ToolRegistry | None" = None


def _default_plugins_dir() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "plugins")


def get_default_registry() -> "ToolRegistry":
    """Process-wide default registry (used by server.py entrypoints)."""
    global _default_registry
    if _default_registry is None:
        store = None
        try:
            from .store import Store
            db = os.environ.get(
                "EASYAGENT_DB",
                os.path.join(os.path.dirname(_default_plugins_dir()), "data", "easyagent.db"),
            )
            os.makedirs(os.path.dirname(db), exist_ok=True)
            store = Store(db)
        except Exception:
            store = None
        _default_registry = ToolRegistry(_default_plugins_dir(), store=store)
        _default_registry.discover()
        try:
            from .tools import register_builtins
            register_builtins(_default_registry)
        except Exception:
            pass
    return _default_registry


def list_tools() -> list[dict]:
    """Module-level entrypoint for server.py: list registered tools."""
    return get_default_registry().list_tools()


def promote(name: str) -> dict:
    """Module-level entrypoint for server.py: promote a plugin from inbox."""
    return get_default_registry().promote(name)
