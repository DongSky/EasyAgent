"""Versioned extension host. Trusted processes and isolated pure JS share one protocol."""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import os
from pathlib import Path, PurePosixPath
import sys
import time
import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from jsonschema import validate

from .components import digest, local_platform
from .contracts import ModelResult
from .extension_contracts import ExtensionInstall, ExtensionManifest, ExtensionPackage
from .plugins import bounded_read
from .store import Conflict, encode


def build_package(manifest, files, *, publisher=None, signing_key=None):
    body = {
        "format": "easyagent.extension.v1",
        "manifest": ExtensionManifest.model_validate(manifest).model_dump(),
        "files": files,
        "publisher": publisher,
    }
    checksum = digest(body)
    signature = base64.b64encode(signing_key.sign(checksum.encode())).decode() if signing_key else None
    return ExtensionPackage(**body, digest=checksum, signature=signature)


def validate_package(raw, publishers):
    package = ExtensionPackage.model_validate(raw)
    if digest(package.model_dump(exclude={"digest", "signature"})) != package.digest:
        raise ValueError("extension package digest mismatch")
    if len(package.model_dump_json().encode()) > 1_500_000 or len(package.files) > 100:
        raise ValueError("extension package exceeds size/file limit")
    for name, content in package.files.items():
        path = PurePosixPath(name)
        if (
            path.is_absolute()
            or ".." in path.parts
            or "\\" in name
            or ":" in name
            or str(path) != name
            or not name
            or "\x00" in content
        ):
            raise ValueError("extension files must be portable relative text paths")
    manifest = package.manifest
    if manifest.entrypoint not in package.files:
        raise ValueError("extension entrypoint missing")
    for view in manifest.views:
        if view.entrypoint and view.entrypoint not in package.files:
            raise ValueError("UI entrypoint missing")
    for name, value in manifest.lock.items():
        if name not in package.files or digest(package.files[name]) != value:
            raise ValueError("dependency lock/file digest mismatch")
    if package.signature:
        if not package.publisher or package.publisher not in publishers:
            raise PermissionError("unknown extension publisher")
        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(publishers[package.publisher])).verify(
                base64.b64decode(package.signature), package.digest.encode()
            )
        except Exception as exc:
            raise PermissionError("invalid extension signature") from exc
    return package


class ExtensionProvider:
    def __init__(self, host, package, contribution):
        self.host, self.package, self.contribution = host, package, contribution

    async def generate(self, request, model):
        if self.contribution.stream_handler:
            from .model_streaming import MODEL_OBSERVER

            cursor = None
            total = 0
            try:
                for _ in range(4096):
                    page = await self.host.call(
                        self.package,
                        self.contribution.stream_handler,
                        {
                            "request": request.model_dump() if cursor is None else None,
                            "model": model,
                            "cursor": cursor,
                        },
                    )
                    if not isinstance(page, dict):
                        raise ValueError("invalid model stream page")
                    total += len(encode(page).encode())
                    if total > 4_000_000:
                        raise ValueError("model stream exceeds byte limit")
                    for delta in page.get("deltas", []):
                        if not isinstance(delta, dict) or delta.get("type") not in (
                            "text_delta",
                            "tool_delta",
                            "usage",
                        ):
                            raise ValueError("invalid model delta")
                        if observer := MODEL_OBSERVER.get():
                            await observer(delta)
                    if page.get("done"):
                        return ModelResult.model_validate(page["result"])
                    cursor = page.get("cursor")
                    if not isinstance(cursor, str) or not cursor:
                        raise ValueError("model stream missing continuation cursor")
                raise ValueError("model stream did not finish")
            finally:
                if cursor and self.contribution.cancel_handler:
                    await self.host.call(self.package, self.contribution.cancel_handler, {"cursor": cursor})
        result = await self.host.call(
            self.package, self.contribution.handler, {"request": request.model_dump(), "model": model}
        )
        return ModelResult.model_validate(result)


class ExtensionHost:
    def __init__(self, hub):
        self.hub, self.store = hub, hub.store
        self.packages, self.active, self.owners = {}, {}, {}
        self.provider_versions = {}
        self.processes = {}
        self.mutation_lock = asyncio.Lock()
        self.root = Path(self.store.path).parent / "extensions"
        with self.store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS extension_packages(
                  id TEXT NOT NULL,revision INTEGER NOT NULL,package TEXT NOT NULL,grants TEXT NOT NULL,
                  PRIMARY KEY(id,revision));
                CREATE TABLE IF NOT EXISTS extension_active(id TEXT PRIMARY KEY,revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS extension_state(
                  id TEXT PRIMARY KEY,revision INTEGER NOT NULL,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS extension_events(
                  sequence INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL,kind TEXT NOT NULL,
                  value TEXT NOT NULL,created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS extension_settings(id TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS extension_generation_state(
                  id TEXT NOT NULL,generation INTEGER NOT NULL,revision INTEGER NOT NULL,value TEXT NOT NULL,
                  PRIMARY KEY(id,generation));
                CREATE TABLE IF NOT EXISTS extension_generation_settings(
                  id TEXT NOT NULL,generation INTEGER NOT NULL,value TEXT NOT NULL,PRIMARY KEY(id,generation));
            """)
            rows = db.execute("SELECT * FROM extension_packages ORDER BY revision").fetchall()
            active = dict(db.execute("SELECT id,revision FROM extension_active"))
        for row in rows:
            p = ExtensionPackage.model_validate_json(row["package"])
            with self.store.connect() as db:
                db.execute(
                    "INSERT OR IGNORE INTO extension_generation_state SELECT id,?,revision,value FROM extension_state WHERE id=?",
                    (p.manifest.revision, p.manifest.id),
                )
                db.execute(
                    "INSERT OR IGNORE INTO extension_generation_settings SELECT id,?,value FROM extension_settings WHERE id=?",
                    (p.manifest.revision, p.manifest.id),
                )
            self.packages[p.manifest.id, p.manifest.revision] = p
            self.register(p, latest=False)
        for name, revision in active.items():
            self.publish(self.packages[name, revision])

    def publishers(self):
        return {r["key"]: r["value"] for r in self.store.memory_search("extension-publishers")}

    def event(self, name, kind, value):
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO extension_events(id,kind,value,created) VALUES(?,?,?,?)",
                (name, kind, encode(value), time.time()),
            )

    def events(self, after=0, name=None):
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT * FROM extension_events WHERE sequence>? AND (? IS NULL OR id=?) ORDER BY sequence LIMIT 500",
                (after, name, name),
            ).fetchall()
        return [dict(r) | {"value": json.loads(r["value"])} for r in rows]

    def snapshot(self):
        return dict(self.active)

    def preview(self, options):
        req = ExtensionInstall.model_validate(options)
        p = validate_package(req.package, self.publishers())
        m = p.manifest
        missing = []
        if local_platform() not in m.platforms:
            missing.append("platform: " + local_platform())
        missing += ["permission: " + x for x in set(m.permissions) - set(req.grants)]
        if m.runtime not in ("javascript", "wasm") and req.trust_digest != p.digest:
            missing.append("trusted process requires explicit trust_digest")
        for name, revision in m.dependencies.items():
            if self.active.get(name) != revision:
                missing.append(f"dependency: {name}@{revision}")
        for name, revision in self.active.items():
            dependency = self.packages[name, revision].manifest.dependencies.get(m.id)
            if dependency is not None and dependency != m.revision:
                missing.append("dependent extension pins another version: " + name)
        with self.store.connect() as db:
            has_generation = db.execute(
                "SELECT 1 FROM extension_generation_state WHERE id=? AND generation=?", (m.id, m.revision)
            ).fetchone()
        if has_generation:
            validate(self.state(m.id, m.revision)["value"], m.state_schema)
            validate(self.settings(m.id, m), m.settings_schema)
        elif m.id in self.active and self.active[m.id] not in m.migrations:
            validate(self.state(m.id)["value"], m.state_schema)
            validate(self.settings(m.id, self.packages[m.id, self.active[m.id]].manifest), m.settings_schema)
        for name in m.required_services:
            if name in getattr(self.hub, "extension_host_services", set()):
                if name not in m.permissions:
                    missing.append("host service permission: " + name)
                continue
            if "tool:" + name in m.permissions:
                try:
                    self.hub.tools.spec(name, m.service_versions.get(name))
                    if self.hub.tools.revision(name) is not None and name not in m.service_versions:
                        missing.append("pin service_versions for: " + name)
                except ValueError:
                    missing.append("tool service unavailable: " + name)
                continue
            owner = self.owners.get(name)
            if not owner or owner not in m.dependencies or self.active.get(owner) != m.dependencies[owner]:
                missing.append("service dependency: " + name)
        for action in m.tools + m.services + m.commands:
            if action.spec.name in self.hub.tools.entries and self.owners.get(action.spec.name) != m.id:
                raise Conflict("contribution already registered: " + action.spec.name)
        for provider in m.providers:
            if provider.alias in self.hub.models.bindings and self.owners.get(provider.alias) != m.id:
                raise Conflict("model already registered: " + provider.alias)
        if old := self.packages.get((m.id, m.revision)):
            if old.digest != p.digest:
                raise Conflict("extension revision already exists with different content")
        return {
            "id": m.id,
            "revision": m.revision,
            "digest": p.digest,
            "manifest": m.model_dump(),
            "missing": missing,
            "ready": not missing,
            "signed": bool(p.signature),
            "execution_started": False,
        }

    async def install(self, options):
        async with self.mutation_lock:
            return await self._install(options)

    async def _install(self, options):
        req = ExtensionInstall.model_validate(options)
        preview = self.preview(req)
        if not preview["ready"]:
            raise PermissionError("; ".join(preview["missing"]))
        p, m = req.package, req.package.manifest
        # Validate candidate while the old generation remains active. Hooks are allowed
        # to initialize only after the caller explicitly grants this package's permissions.
        candidate = await self.generation(p)
        await self.call(p, "lifecycle.activate", {"candidate": True}, initial=candidate)
        with self.store.transaction() as db:
            existing = db.execute(
                "SELECT package FROM extension_packages WHERE id=? AND revision=?", (m.id, m.revision)
            ).fetchone()
            if existing and ExtensionPackage.model_validate_json(existing[0]).digest != p.digest:
                raise Conflict("extension revision changed during installation")
            db.execute(
                "INSERT OR IGNORE INTO extension_packages VALUES(?,?,?,?)",
                (m.id, m.revision, p.model_dump_json(), encode(req.grants)),
            )
            db.execute(
                "INSERT OR IGNORE INTO extension_generation_state VALUES(?,?,0,?)",
                (m.id, m.revision, encode(candidate["state"])),
            )
            db.execute(
                "INSERT OR IGNORE INTO extension_generation_settings VALUES(?,?,?)",
                (m.id, m.revision, encode(candidate["settings"])),
            )
            if req.activate:
                db.execute("INSERT OR REPLACE INTO extension_active VALUES(?,?)", (m.id, m.revision))
        self.packages[m.id, m.revision] = p
        self.register(p, latest=req.activate)
        self.event(m.id, "installed", {"revision": m.revision, "digest": p.digest})
        return {**preview, "installed": True, "active": req.activate}

    def register(self, p, latest=False):
        m = p.manifest
        for action in m.tools + m.services + m.commands:

            async def handler(arguments, context, p=p, action=action):
                return await self.call(p, action.handler, arguments, context=context)

            self.owners[action.spec.name] = m.id
            self.hub.tools.versions[action.spec.name, m.revision] = (action.spec, handler)
        from .models import ModelBinding

        for provider in m.providers:
            self.owners[provider.alias] = m.id
            self.provider_versions[provider.alias, m.revision] = ModelBinding(
                ExtensionProvider(self, p, provider), provider.model, set(provider.capabilities)
            )
        if latest:
            self.publish(p)

    def publish(self, p):
        m = p.manifest
        # Retire names removed by an upgrade without deleting historical handlers.
        for name, owner in list(self.owners.items()):
            if owner == m.id:
                self.hub.tools.entries.pop(name, None)
                self.hub.tools.latest.pop(name, None)
                self.hub.models.bindings.pop(name, None)
        for a in m.tools + m.services + m.commands:
            self.hub.tools.entries[a.spec.name] = self.hub.tools.versions[a.spec.name, m.revision]
            self.hub.tools.latest[a.spec.name] = m.revision
        for provider in m.providers:
            self.hub.models.bindings[provider.alias] = self.provider_versions[provider.alias, m.revision]
        self.active[m.id] = m.revision
        self.hub.skills.extension_entries[m.id] = dict(m.skills)

    async def activate(self, name, revision):
        async with self.mutation_lock:
            return await self._activate(name, revision)

    async def _activate(self, name, revision):
        p = self.packages[name, revision]
        with self.store.connect() as db:
            grants = json.loads(
                db.execute(
                    "SELECT grants FROM extension_packages WHERE id=? AND revision=?", (name, revision)
                ).fetchone()[0]
            )
        preview = self.preview({"package": p, "grants": grants, "trust_digest": p.digest})
        if not preview["ready"]:
            raise Conflict("; ".join(preview["missing"]))
        for dep, rev in p.manifest.dependencies.items():
            if self.active.get(dep) != rev:
                raise Conflict("activate required dependency first: " + dep)
        await self.call(p, "lifecycle.activate", {"candidate": True})
        with self.store.connect() as db:
            db.execute("INSERT OR REPLACE INTO extension_active VALUES(?,?)", (name, revision))
        self.publish(p)
        self.event(name, "activated", {"revision": revision})
        return {"id": name, "revision": revision, "active": True}

    async def disable(self, name):
        async with self.mutation_lock:
            return await self._disable(name)

    async def _disable(self, name):
        if hasattr(self.hub,"backends") and any(b["extension"]==name for b in self.hub.backends.snapshot().values()):
            raise Conflict("select another backend before disabling this extension")
        for other, rev in self.active.items():
            if name in self.packages[other, rev].manifest.dependencies:
                raise Conflict("active extension depends on this package: " + other)
        rev = self.active[name]
        await self.call(self.packages[name, rev], "lifecycle.dispose", {})
        if worker := self.processes.pop(self.packages[name, rev].digest, None):
            await worker.close()
        with self.store.connect() as db:
            db.execute("DELETE FROM extension_active WHERE id=?", (name,))
        self.active.pop(name)
        for alias, owner in self.owners.items():
            if owner == name:
                self.hub.tools.entries.pop(alias, None)
                self.hub.tools.latest.pop(alias, None)
                self.hub.models.bindings.pop(alias, None)
        self.hub.skills.extension_entries.pop(name, None)
        self.event(name, "disabled", {})
        return {"id": name, "active": False, "historical_versions_retained": True}

    def catalog(self):
        return [
            {
                "id": name,
                "revision": rev,
                "active": self.active.get(name) == rev,
                "digest": p.digest,
                "manifest": p.manifest.model_dump(),
            }
            for (name, rev), p in sorted(self.packages.items())
        ]

    async def generation(self, package):
        m = package.manifest
        with self.store.connect() as db:
            existing = db.execute(
                "SELECT 1 FROM extension_generation_state WHERE id=? AND generation=?", (m.id, m.revision)
            ).fetchone()
        if existing:
            return {"state": self.state(m.id, m.revision)["value"], "settings": self.settings(m.id, m)}
        candidate = {"state": m.initial_state, "settings": m.settings}
        if m.id in self.active:
            previous = self.packages[m.id, self.active[m.id]].manifest
            candidate = {"state": self.state(m.id)["value"], "settings": self.settings(m.id, previous)}
            if handler := m.migrations.get(previous.revision):
                candidate = await self.call(
                    package, handler, {"from_revision": previous.revision, **candidate}, initial=candidate
                )
                if not isinstance(candidate, dict) or set(candidate) != {"state", "settings"}:
                    raise ValueError("migration must return state and settings")
        validate(candidate["state"], m.state_schema)
        validate(candidate["settings"], m.settings_schema)
        return candidate

    def state(self, name, generation=None):
        generation = generation or self.active.get(name)
        with self.store.connect() as db:
            row = db.execute(
                "SELECT revision,value FROM extension_generation_state WHERE id=? AND generation=?",
                (name, generation),
            ).fetchone()
        return {"revision": row[0], "value": json.loads(row[1])} if row else {"revision": 0, "value": {}}

    def set_state(self, name, revision, value, manifest=None):
        manifest = manifest or self.packages[name, self.active[name]].manifest
        validate(value, manifest.state_schema)
        if len(encode(value).encode()) > 100_000:
            raise ValueError("extension state exceeds 100 KB")
        with self.store.transaction() as db:
            changed = db.execute(
                "UPDATE extension_generation_state SET value=?,revision=revision+1 WHERE id=? AND generation=? AND revision=?",
                (encode(value), name, manifest.revision, revision),
            )
            if not changed.rowcount:
                raise Conflict("extension state revision changed")
            db.execute(
                "INSERT INTO extension_events(id,kind,value,created) VALUES(?,?,?,?)",
                (
                    name,
                    "state.changed",
                    encode({"generation": manifest.revision, "revision": revision + 1, "value": value}),
                    time.time(),
                ),
            )
        return {"revision": revision + 1, "value": value}

    def settings(self, name, manifest):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT value FROM extension_generation_settings WHERE id=? AND generation=?",
                (name, manifest.revision),
            ).fetchone()
        return json.loads(row[0]) if row else manifest.settings

    def set_settings(self, name, value):
        manifest = self.packages[name, self.active[name]].manifest
        validate(value, manifest.settings_schema)
        with self.store.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO extension_generation_settings VALUES(?,?,?)",
                (name, manifest.revision, encode(value)),
            )
        return {"saved": True}

    async def uninstall(self, name, revision):
        async with self.mutation_lock:
            if self.active.get(name) == revision:
                raise Conflict("disable the active version before uninstalling")
            for package in self.packages.values():
                if package.manifest.dependencies.get(name) == revision:
                    raise Conflict("installed extension depends on this revision")
            # Retain versions referenced by any durable run, including completed audit history.
            with self.store.transaction() as db:
                for row in db.execute("SELECT spec FROM runs"):
                    if json.loads(row[0]).get("metadata", {}).get("extensions", {}).get(name) == revision:
                        raise Conflict("durable run history references this version; retained for replay")
                for row in db.execute("SELECT body FROM definition_versions WHERE kind='workflow'"):
                    if json.loads(row[0]).get("metadata", {}).get("extensions", {}).get(name) == revision:
                        raise Conflict("saved workflow references this version; retain it for execution")
                db.execute("DELETE FROM extension_packages WHERE id=? AND revision=?", (name, revision))
                db.execute(
                    "DELETE FROM extension_generation_state WHERE id=? AND generation=?", (name, revision)
                )
                db.execute(
                    "DELETE FROM extension_generation_settings WHERE id=? AND generation=?", (name, revision)
                )
            p = self.packages.pop((name, revision))
            for a in p.manifest.tools + p.manifest.services + p.manifest.commands:
                self.hub.tools.versions.pop((a.spec.name, revision), None)
            for a in p.manifest.providers:
                self.provider_versions.pop((a.alias, revision), None)
            self.event(name, "uninstalled", {"revision": revision})
            return {"uninstalled": True}

    def resources(self):
        return {
            "prompts": [
                {"id": name + "." + key, "text": value}
                for name, rev in self.active.items()
                for key, value in self.packages[name, rev].manifest.prompts.items()
            ],
            "skills": self.hub.skills.catalog(),
            "tools": self.hub.tools.catalog(include_internal=True),
            "models": self.hub.models.catalog(),
        }

    def flags(self, manifest):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT value FROM memory WHERE namespace='extension-flags' AND key=?",
                (f"{manifest.id}@{manifest.revision}",),
            ).fetchone()
        return json.loads(row[0]) if row else manifest.flags

    def materialize(self, package):
        target = self.root / package.digest
        for name, content in package.files.items():
            path = target / name
            if path.is_symlink() or any(p.is_symlink() for p in path.parents if p != self.root.parent):
                raise PermissionError("extension cache must not contain symlinks")
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.read_text(encoding="utf-8") != content:
                raise PermissionError("extension cache has been modified")
            if not path.exists():
                path.write_text(content, encoding="utf-8")
        return target

    async def call(self, package, method, arguments, *, context=None, _depth=0, initial=None):
        if _depth > 16:
            raise ValueError("extension service continuation limit exceeded")
        m = package.manifest
        state = self.state(m.id, m.revision)
        if initial is not None:
            state = {"revision": 0, "value": initial["state"]}
        if (m.id, m.revision) not in self.packages and not state["value"]:
            state = {"revision": 0, "value": m.initial_state}
        request = {
            "protocol_version": 1,
            "id": uuid.uuid4().hex,
            "method": method,
            "params": arguments,
            "context": {
                "extension": m.id,
                "revision": m.revision,
                "settings": initial["settings"] if initial is not None else self.settings(m.id, m),
                "flags": self.flags(m),
                "state": state,
                "run_id": getattr(context, "run_id", None),
            },
        }
        env = {
            k: v
            for k, v in os.environ.items()
            if k.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "LANG", "TEMP", "TMP", "HOME", "USERPROFILE",
                     "APPDATA", "LOCALAPPDATA", "CARGO_HOME", "RUSTUP_HOME", "LIB", "LIBPATH", "INCLUDE",
                     "VSCMD_ARG_TGT_ARCH", "VCTOOLSINSTALLDIR", "VSINSTALLDIR", "VCINSTALLDIR",
                     "WINDOWSSDKDIR", "WINDOWSSDKVERSION", "UNIVERSALCRTSDKDIR", "UCRTVERSION"}
            or k in m.env_allow
        }
        if m.runtime in ("javascript", "wasm"):
            command = (
                [sys.executable, "--eah-worker", m.runtime]
                if getattr(sys, "frozen", False)
                else [
                    sys.executable,
                    "-m",
                    "easyagent.wasm_worker" if m.runtime == "wasm" else "easyagent.extension_worker",
                ]
            )
            payload = {
                "source": package.files[m.entrypoint],
                "request": request,
                "memory_mb": m.memory_mb,
                "timeout_seconds": m.timeout_seconds,
            }
            directory = None
        else:
            directory = self.materialize(package)
            import shutil

            if m.runtime == "python":
                command = [
                    (shutil.which("python3") or "python3")
                    if getattr(sys, "frozen", False)
                    else sys.executable,
                    m.entrypoint,
                ]
            elif m.runtime == "node":
                command = ["node", m.entrypoint]
            else:
                command = [
                    "cargo",
                    "run",
                    "--offline",
                    "--locked",
                    "--quiet",
                    "--manifest-path",
                    m.entrypoint,
                ]
            payload = request
        from .extension_process import PersistentWorker, terminate

        process, readers = None, []
        try:
            if m.transport == "ndjson":
                worker = self.processes.setdefault(package.digest, PersistentWorker(command, directory, env))
                response = await worker.call(payload, m.timeout_seconds + 1)
            else:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=directory,
                    env=env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=os.name == "posix",
                )
                async with asyncio.timeout(m.timeout_seconds + 1):
                    readers = [
                        asyncio.create_task(bounded_read(process.stdout, 1_000_000)),
                        asyncio.create_task(bounded_read(process.stderr, 32_000)),
                    ]
                    process.stdin.write((encode(payload) + "\n").encode())
                    await process.stdin.drain()
                    process.stdin.close()
                    output, stderr = await asyncio.gather(*readers)
                    await process.wait()
                if process.returncode:
                    # Pure workers have no credentials or host access. Their bounded traceback
                    # gives generated-code repair the actual syntax/runtime error.
                    detail = ': ' + stderr.decode('utf-8', errors='replace')[-2000:] if m.runtime in ('javascript', 'wasm') else ''
                    raise ValueError(f"extension {m.id} handler failed (exit {process.returncode})" + detail)
                response = json.loads(output)
            from .extension_contracts import ExtensionResponse

            ExtensionResponse.model_validate(response)
            if response.get("error"):
                raise ValueError("extension handler error: " + response["error"]["code"])
            if not isinstance(response, dict) or set(response) - {
                "result",
                "state",
                "calls",
                "continue",
                "error",
            }:
                raise ValueError("extension response contains unknown fields")
            if "calls" in response:
                if context is None or not context.job or method.startswith("lifecycle."):
                    raise PermissionError("service calls require a managed tool invocation")
                calls = response["calls"]
                if (
                    not isinstance(calls, list)
                    or len(calls) > 16
                    or not isinstance(response.get("continue"), str)
                ):
                    raise ValueError("bounded service calls require a continuation handler")
                outputs = []
                for index, call in enumerate(calls):
                    name = call["name"]
                    if name not in m.required_services:
                        raise PermissionError("extension service was not declared")
                    owner = self.owners.get(name)
                    if name.startswith("host.") and name not in m.permissions:
                        raise PermissionError("host service permission was not granted")
                    outputs.append(
                        await self.hub.tools.invoke(
                            self.store,
                            context.job,
                            name,
                            call["input"],
                            f"extension:{context.invocation_id}:{_depth}:{index}",
                            revision=m.service_versions.get(name, m.dependencies.get(owner)),
                        )
                    )
                return await self.call(
                    package,
                    response["continue"],
                    {"input": arguments, "results": outputs},
                    context=context,
                    _depth=_depth + 1,
                )
            if "result" not in response:
                raise ValueError("extension response requires result")
            if "state" in response:
                if method.startswith("lifecycle.") or initial is not None:
                    raise ValueError("candidate validation cannot mutate persistent state")
                self.set_state(m.id, state["revision"], response["state"], m)
            return response["result"]
        finally:
            if process is not None:
                await terminate(process)
            for reader in readers:
                reader.cancel()
            if readers:
                await asyncio.gather(*readers, return_exceptions=True)

    async def close(self):
        for worker in self.processes.values():
            await worker.close()
        self.processes.clear()

    def run_snapshot(self, job):
        return self.store.run(job["run_id"])["spec"].get("metadata", {}).get("extensions", {})

    async def dispatch(self, event, payload, *, snapshot=None, job=None):
        selected = snapshot if snapshot is not None else self.run_snapshot(job) if job else self.snapshot()
        handlers = []
        for name, revision in selected.items():
            package = self.packages[name, revision]
            handlers += [(h.priority, name, h, package) for h in package.manifest.hooks if h.event == event]
        value = copy.deepcopy(payload)
        for _, name, hook, package in sorted(handlers, key=lambda item: (item[0], item[1])):
            try:
                result = await self.call(package, hook.handler, {"event": event, "value": value})
                if not isinstance(result, dict) or set(result) - {"patch", "deny", "message"}:
                    raise ValueError("hook result must contain patch, deny or message")
                if result.get("deny"):
                    raise PermissionError("extension rejected action: " + name)
                if patch := result.get("patch"):
                    # Hook targets cannot change identities, grants, tool selections or budgets.
                    allowed = {
                        "tool.before_call": {"arguments"},
                        "tool.after_result": {"result"},
                        "context.transform": {"messages"},
                        "model.before_request": {"prompt", "messages"},
                        "model.after_response": {"text", "data"},
                        "session.input": {"text"},
                    }
                    if not isinstance(patch, dict) or set(patch) - allowed.get(event, set()):
                        raise PermissionError("hook patch exceeds event contract")
                    value.update(patch)
            except PermissionError:
                raise
            except Exception as exc:
                self.event(name, "hook.failed", {"event": event, "error": type(exc).__name__})
                if hook.required:
                    raise
        return value

    def provider_binding(self, alias, snapshot):
        if owner := self.owners.get(alias):
            if owner in snapshot:
                return self.provider_versions.get((alias, snapshot[owner]))
        return None

    def command(self, name, arguments, conversation_id=None):
        owner = self.owners.get(name)
        if not owner or owner not in self.active:
            raise KeyError(name)
        p = self.packages[owner, self.active[owner]]
        if name not in [a.spec.name for a in p.manifest.commands + p.manifest.services]:
            raise PermissionError("not an extension command or service")
        if conversation_id:
            self.hub.conversations.get(conversation_id)
        return {
            "run_id": self.hub.submit(
                {
                    "name": "扩展命令 · " + name,
                    "metadata": {"extension_session": conversation_id} if conversation_id else {},
                    "steps": [{"id": "command", "target": name, "input": arguments}],
                }
            )
        }
