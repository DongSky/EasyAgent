"""Capability discovery: close a capability gap in bounded steps.

``ensure_capability(need, ctx) -> str | None``:

1. ``registry.get(need)`` hit → return the tool name.
2. ``memory.search(need)`` finds a learning containing an actionable recipe
   (a ``recipe:`` line or a ``recipe`` key) → ``scaffold`` a plugin from it and
   run it through :meth:`ToolRegistry.promote`.
3. Bounded exploration (≤10 steps, ≤300s, no paid calls): use
   ``easyagent.tools.meta`` ``fetch_docs`` → ``probe`` → ``scaffold`` →
   ``promote``. On failure return ``None`` and ``append_learning`` the reason.

All heavy dependencies are injected or lazily imported so the module works
with mocks (zero paid calls in tests).
"""
from __future__ import annotations

import importlib
import json
import re
import time
from typing import Any, Callable


def _slug(text: str, fallback: str = "tool") -> str:
    value = re.sub(r"[^a-z0-9]+", "-", (text or "").casefold()).strip("-")[:48]
    return value if re.fullmatch(r"[a-z][a-z0-9-]*", value or "") else fallback


def _lazy_module(name: str):
    try:
        return importlib.import_module(name)
    except Exception:
        return None


def _extract_recipe(hit: Any) -> Any | None:
    """Pull an actionable recipe out of a memory search hit, if present."""
    text = ""
    tags: list = []
    if isinstance(hit, dict):
        if hit.get("recipe") is not None:
            return hit["recipe"]
        text = str(hit.get("text") or hit.get("content") or "")
        tags = hit.get("tags") or []
    else:
        text = str(hit)
    if "recipe" in [str(t).casefold() for t in tags]:
        return text.strip() or None
    m = re.search(r"(?im)^recipe:\s*(.+)$", text)
    if not m:
        return None
    candidate = m.group(1).strip()
    # allow the recipe to continue on following indented lines
    rest = text[m.end():]
    for line in rest.splitlines():
        if line.strip() == "" or not line[:1].isspace():
            break
        candidate += "\n" + line.strip()
    try:
        return json.loads(candidate)
    except Exception:
        return candidate or None


class CapabilityDiscovery:
    """Close capability gaps via registry → memory recipes → bounded exploration."""

    def __init__(
        self,
        registry: Any,
        memory: Any = None,
        store: Any = None,
        max_steps: int = 10,
        max_wall_s: int = 300,
        planner: Callable[[str, str, dict], Any] | None = None,
        decisions: Any = None,
    ):
        """
        :param registry: ToolRegistry (or compatible mock).
        :param memory: object with ``search(query, limit)`` / ``append_learning``;
            lazily imported from ``easyagent.memory`` when None.
        :param store: optional Store fallback for ``append_learning``.
        :param planner: optional callable ``(need, docs_text, ctx) -> recipe``
            used to turn fetched docs into a scaffold recipe (e.g. an LLM step).
        :param decisions: optional DecisionProvider for the gap_triage gate
            (Jev gate 2); ``make_provider()`` default when None.
        """
        self.registry = registry
        self._memory_obj = memory
        self.store = store
        self.max_steps = max_steps
        self.max_wall_s = max_wall_s
        self.planner = planner
        self.decisions = decisions

    # ------------------------------------------------------------- dependencies
    def _memory(self) -> Any | None:
        if self._memory_obj is not None:
            return self._memory_obj
        mod = _lazy_module("easyagent.memory")
        if mod is None:
            return None
        self._memory_obj = mod  # module-level search()/append_learning()
        return self._memory_obj

    def _meta(self) -> Any | None:
        """easyagent.tools.meta with fetch_docs/probe/scaffold (or None)."""
        return _lazy_module("easyagent.tools.meta")

    def _gap_triage(self, need: str) -> str:
        """Jev gate 2: explore | skip | ask.

        Degradation chain: Jev -> fallback subagent judge -> EASYAGENT_GATE_POLICY
        (open -> "explore", ask -> "ask", halt -> raise GateHalted).
        """
        decisions = None
        try:
            decisions = _lazy_module("easyagent.decisions")
            if decisions is not None:
                provider = self.decisions
                if provider is None:
                    provider = decisions.make_provider()
                verdict = decisions.gap_triage(need, provider=provider)
                if verdict in ("explore", "skip", "ask"):
                    return verdict
        except Exception:
            pass
        policy = "open"
        if decisions is not None:
            try:
                policy = decisions.gate_policy()
            except Exception:
                pass
        if policy == "halt":
            exc_cls = getattr(decisions, "GateHalted", RuntimeError)
            raise exc_cls(f"gap_triage unavailable for '{need}' (policy=halt)")
        return "ask" if policy == "ask" else "explore"

    def _learn(self, text: str, tags: list[str] | None = None) -> None:
        mem = self._memory()
        if mem is not None:
            try:
                mem.append_learning(text, tags or [])
                return
            except Exception:
                pass
        if self.store is not None:
            try:
                self.store.append_learning(text, tags or [])
            except Exception:
                pass

    # ------------------------------------------------------------------ main
    def ensure_capability(self, need: str, ctx: dict | None = None) -> str | None:
        """Return a tool name that satisfies ``need``, or None."""
        ctx = ctx or {}

        # 1. already registered?
        if self.registry.get(need) is not None:
            return need

        # 1.5 Jev gate 2: triage the gap before building anything.
        # policy=halt raises GateHalted -> stop, do not build.
        try:
            triage = self._gap_triage(need)
        except Exception:
            self._learn(
                f"gap triage for '{need}': halted (no decision provider); "
                "no build attempted",
                ["gap", "halted"])
            return None
        if triage in ("skip", "ask"):
            self._learn(
                f"gap triage for '{need}': {triage}; no build attempted",
                ["gap", triage])
            return None

        # 2. memory recipe -> scaffold -> promote
        recipe = self._recipe_from_memory(need)
        if recipe is not None:
            name = self._build_and_promote(
                ctx.get("tool_name") or _slug(need),
                ctx.get("description") or f"auto-built tool for: {need}",
                recipe,
                ctx,
            )
            if name:
                return name

        # 3. bounded exploration
        return self._explore(need, ctx)

    def _recipe_from_memory(self, need: str) -> Any | None:
        mem = self._memory()
        if mem is None:
            return None
        try:
            hits = mem.search(need, limit=5) or []
        except Exception:
            return None
        for hit in hits:
            recipe = _extract_recipe(hit)
            if recipe:
                return recipe
        return None

    def _build_and_promote(self, name: str, description: str, recipe: Any,
                           ctx: dict | None = None) -> str | None:
        meta = self._meta()
        if meta is None:
            return None
        try:
            res = meta.scaffold(
                {"name": name, "description": description, "recipe": recipe},
                ctx or {})
        except Exception:
            return None
        if not isinstance(res, dict) or not res.get("ok"):
            return None
        try:
            report = self.registry.promote(name)
        except Exception:
            return None
        return name if isinstance(report, dict) and report.get("ok") else None

    # ------------------------------------------------------------- exploration
    def _explore(self, need: str, ctx: dict) -> str | None:
        """Bounded (steps/time, no paid calls) docs → probe → scaffold → promote."""
        deadline = time.time() + self.max_wall_s
        steps = 0
        reason = "no recipe source (memory, planner, or ctx['recipe'])"
        name = ctx.get("tool_name") or _slug(need)
        description = ctx.get("description") or f"auto-built tool for: {need}"
        recipe: Any = ctx.get("recipe")
        docs_text = ""
        meta = self._meta()
        if meta is None:
            reason = "easyagent.tools.meta unavailable"
            self._learn(f"explored {need}: failed because {reason}",
                        ["exploration", "failed"])
            return None

        while steps < self.max_steps and time.time() < deadline:
            steps += 1
            # fetch docs once, if a URL was supplied
            docs_url = ctx.get("docs_url")
            if docs_url and not docs_text:
                try:
                    doc = meta.fetch_docs({"url": docs_url}, ctx)
                    docs_text = str((doc or {}).get("text", ""))[:12000]
                except Exception as exc:
                    reason = f"fetch_docs failed: {exc}"
                    break
            # obtain a recipe
            if recipe is None:
                planner = ctx.get("planner") or self.planner
                if planner is None:
                    reason = ("no recipe: supply ctx['recipe'], a planner, or a "
                              "memory learning with a recipe:")
                    break
                try:
                    recipe = planner(need, docs_text, ctx)
                except Exception as exc:
                    reason = f"planner failed: {exc}"
                    break
                if recipe is None:
                    reason = "planner returned no recipe"
                    break
            # optional probe step when the recipe names an HTTP endpoint
            if isinstance(recipe, dict) and recipe.get("url"):
                try:
                    probed = meta.probe(
                        {"method": recipe.get("method", "GET"),
                         "url": recipe["url"],
                         "headers": recipe.get("headers"),
                         "body": recipe.get("body")}, ctx)
                except Exception as exc:
                    reason = f"probe raised: {exc}"
                    break
                if not (probed or {}).get("ok"):
                    reason = f"probe failed: {(probed or {}).get('status')}"
                    break
            # scaffold + promote
            built = self._build_and_promote(name, description, recipe, ctx)
            if built:
                return built
            reason = f"scaffold/promote failed for '{name}'"
            break

        if steps >= self.max_steps:
            reason = f"step budget ({self.max_steps}) exhausted; last: {reason}"
        elif time.time() >= deadline:
            reason = f"wall-clock budget ({self.max_wall_s}s) exhausted; last: {reason}"
        self._learn(f"explored {need}: failed because {reason}",
                    ["exploration", "failed"])
        return None
