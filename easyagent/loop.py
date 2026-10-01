"""MissionRunner: single-chain ReAct loop with checkpoints, budget fuses, steer.

Skeleton follows the observe → tool → loop idea of the original
``autonomy.py`` operator, but with the three-level concurrency control
(max_children / max_depth / max_active_children) and the reflection
sub-loop removed: one durable chain per run.

Dependencies (store / registry / decisions / llm) are constructor-injected
and may be mocks; when omitted they are lazily imported from
``easyagent.*``. Zero paid calls happen unless a real ``llm``/``decisions``
implementation is injected.

Steer: :meth:`queue_command` accepts a dict ``{"action", "message?"}`` or an
object with ``.action`` / ``.message``. Actions: pause | resume | cancel |
redirect. While a run is ``awaiting_confirm`` (an untrusted tool raised
``ApprovalRequired``), ``resume`` counts as the human approving the call and
the tool is executed via ``registry.call_approved``.
"""
from __future__ import annotations

import importlib
import json
import os
import queue
import re
import threading
import time
from typing import Any


def _default_store() -> Any:
    """Open the default SQLite store (same DB path convention as server.py)."""
    mod = _lazy("easyagent.store")
    if mod is None or not hasattr(mod, "init_db"):
        return None
    here = os.path.dirname(os.path.abspath(__file__))
    db = os.environ.get(
        "EASYAGENT_DB",
        os.path.join(os.path.dirname(here), "data", "easyagent.db"),
    )
    os.makedirs(os.path.dirname(db), exist_ok=True)
    return mod.init_db(db)


def _lazy(name: str):
    try:
        return importlib.import_module(name)
    except Exception:
        return None


class _Cancelled(Exception):
    """Raised inside the run thread when a cancel steer arrives."""


def _goal_sig(goal: str) -> str:
    """Stable signature for a mission goal (bypass counting key)."""
    v = re.sub(r"[^a-z0-9]+", "-", (goal or "").casefold()).strip("-")[:40]
    return v if re.fullmatch(r"[a-z][a-z0-9-]*", v or "") else "goal"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


class _DefaultDecisions:
    """Neutral fallback when no decisions provider is injected/available."""

    def risk_gate(self, tool_name: str, args: dict) -> bool:
        return True  # fail closed

    def completion_score(self, summary: str) -> float:
        return 3.0  # neutral: accept the finish, don't loop forever

    def gap_triage(self, need: str) -> str:
        return "ask"


def _norm_command(cmd: Any) -> tuple[str, str | None]:
    if isinstance(cmd, dict):
        return str(cmd.get("action", "")), cmd.get("message")
    return str(getattr(cmd, "action", "")), getattr(cmd, "message", None)


class MissionRunner:
    """Runs missions as background ReAct threads."""

    def __init__(
        self,
        store: Any = None,
        registry: Any = None,
        decisions: Any = None,
        llm: Any = None,
        checkpoint_every: int = 5,
    ):
        self.store = store or _default_store()
        self.registry = registry
        if self.registry is None:
            reg_mod = _lazy("easyagent.registry")
            if reg_mod is not None:
                here = os.path.dirname(os.path.abspath(__file__))
                plugins = os.path.join(os.path.dirname(here), "plugins")
                self.registry = reg_mod.ToolRegistry(plugins, store=self.store)
                try:
                    self.registry.discover()
                except Exception:
                    pass
                try:
                    tools_mod = _lazy("easyagent.tools")
                    if tools_mod is not None and hasattr(tools_mod, "register_builtins"):
                        tools_mod.register_builtins(self.registry)
                except Exception:
                    pass
        self.decisions = decisions or self._default_decisions()
        # share one decisions provider with the registry's risk gate
        try:
            if self.registry is not None and getattr(self.registry, "decisions", None) is None:
                self.registry.decisions = self.decisions
        except Exception:
            pass
        # bind the runner's registry to the plugin.promote tool so the agent
        # can hot-load its own scaffolded plugins
        try:
            plugin_tools_mod = _lazy("easyagent.tools.plugin_tools")
            if plugin_tools_mod is not None and hasattr(plugin_tools_mod, "set_registry"):
                plugin_tools_mod.set_registry(self.registry)
        except Exception:
            pass
        self.llm = llm or self._default_llm()
        self.checkpoint_every = max(1, checkpoint_every)
        self._threads: dict[str, threading.Thread] = {}
        self._runs: dict[str, dict] = {}  # run_id -> {"queue", "cost", "pending_redirect", "mission_id"}
        self._tool_name_map: dict[str, str] = {}  # sanitized LLM fn name -> registry name
        self._trim_notified: set[str] = set()  # run_ids already told about tool trimming

    # ------------------------------------------------------------- lazy defaults
    def _default_decisions(self) -> Any:
        mod = _lazy("easyagent.decisions")
        if mod is not None:
            try:
                return mod.make_provider()
            except Exception:
                pass
        return _DefaultDecisions()

    def _default_llm(self) -> Any:
        mod = _lazy("easyagent.llm")
        if mod is not None:
            try:
                import os
                if os.environ.get("EASYAGENT_MOCK_LLM") == "1" and hasattr(mod, "MockClient"):
                    return mod.MockClient()
                if hasattr(mod, "ModelClient"):
                    return mod.ModelClient()
            except Exception:
                pass
        return None

    # ------------------------------------------------------------------ control
    def start(self, mission_id: str) -> str:
        """Create a run and start its ReAct thread. Returns run_id."""
        run = self.store.create_run(mission_id)
        return self.start_existing(run.run_id, mission_id)

    def start_existing(self, run_id: str, mission_id: str,
                       resume: dict | None = None) -> str:
        """Attach to an already-created run (e.g. by server.py) and start it.

        ``resume`` optionally seeds the run from a checkpoint state:
        {"history": [...], "step": int, "cost": float}.
        """
        self._runs[run_id] = {
            "queue": queue.Queue(),
            "cost": float((resume or {}).get("cost") or 0.0),
            "pending_redirect": None,
            "mission_id": mission_id,
            "run_id": run_id,
            "tool_counts": {},
            "completion_score": 0.0,
            "resume": resume,
        }
        self._emit(run_id, "status", {"status": "pending", "mission_id": mission_id,
                                      "resumed": bool(resume)})
        thread = threading.Thread(
            target=self._loop, args=(run_id,), name=f"mission-{run_id[:12]}",
            daemon=True,
        )
        self._threads[run_id] = thread
        thread.start()
        return run_id

    def queue_command(self, run_id: str, command: Any) -> bool:
        """Enqueue a steer command. Emits a ``steer`` event for visibility."""
        st = self._runs.get(run_id)
        if st is None:
            return False
        action, message = _norm_command(command)
        self._emit(run_id, "steer", {"action": action, "message": message})
        st["queue"].put({"action": action, "message": message})
        return True

    def wait(self, run_id: str, timeout: float | None = None) -> bool:
        """Join the run thread. Returns True if it finished within timeout."""
        t = self._threads.get(run_id)
        if t is None:
            return True
        t.join(timeout=timeout)
        return not t.is_alive()

    def get_run(self, run_id: str) -> Any:
        return self.store.get_run(run_id)

    def get_events(self, run_id: str, after_seq: int = 0) -> list:
        return self.store.get_events(run_id, after_seq=after_seq)

    # ------------------------------------------------------------------ internals
    def _emit(self, run_id: str, type: str, payload: dict | None = None) -> None:
        try:
            self.store.append_event(run_id, type, payload or {})
        except Exception:
            pass

    # Tools the model must always see even when the prompt list is trimmed:
    # without these it cannot extend itself or stay regularized.
    _BUILDER_TOOLS = ("scaffold", "plugin.promote", "plugin.merge",
                      "plugin.prune", "memory.search", "memory.append",
                      "memory.consolidate")

    def _tools_schema(self, run_id: str | None = None) -> list[dict]:
        """OpenAI-compatible function tool definitions for the LLM.

        Providers only allow [a-zA-Z0-9_-] in function names, so dotted
        internal names (``shell.exec``) are sanitized (``shell_exec``);
        the reverse mapping is kept in ``self._tool_name_map`` for dispatch.

        Anti-bloat: when the registry holds more than
        ``EASYAGENT_MAX_PROMPT_TOOLS`` (default 48) tools, only the top
        slice is sent — the self-improvement core first, then other
        builtins, then the rest ranked by recorded usage. A ``tools_trimmed``
        event tells clients (and the log) what was dropped; the agent can
        still discover dropped plugins via memory.search recipes.
        """
        try:
            tools = self.registry.list_tools()
        except Exception:
            return []
        tools = self._prioritize_tools(tools, run_id)
        try:
            out = []
            name_map = {}
            for t in tools:
                params = t.get("schema") or {"type": "object", "properties": {}}
                fn_name = re.sub(r"[^a-zA-Z0-9_-]", "_", t["name"])
                name_map[fn_name] = t["name"]
                out.append({
                    "type": "function",
                    "function": {
                        "name": fn_name,
                        "description": t.get("description", ""),
                        "parameters": params,
                    },
                })
            self._tool_name_map = name_map
            return out
        except Exception:
            return []

    def _prioritize_tools(self, tools: list[dict],
                          run_id: str | None) -> list[dict]:
        max_tools = _env_int("EASYAGENT_MAX_PROMPT_TOOLS", 48)
        if len(tools) <= max_tools:
            return tools
        usage: dict[str, int] = {}
        try:
            for u in self.store.get_tool_usage():
                usage[u["name"]] = int(u.get("calls") or 0)
        except Exception:
            pass

        def rank(t: dict) -> tuple:
            name = t["name"]
            # Self-improvement core first: without these the agent can neither
            # extend itself nor stay regularized, so they survive even tiny
            # limits. (These are builtins too, hence checked before builtin.)
            if name in self._BUILDER_TOOLS:
                return (0, self._BUILDER_TOOLS.index(name), name)
            if t.get("builtin"):
                return (1, 0, name)
            return (2, -usage.get(name, 0), name)

        ordered = sorted(tools, key=rank)
        kept = ordered[:max_tools]
        if run_id and run_id not in self._trim_notified:
            self._trim_notified.add(run_id)
            self._emit(run_id, "tools_trimmed", {
                "total": len(tools),
                "sent": len(kept),
                "dropped": [t["name"] for t in ordered[max_tools:]],
            })
        return kept

    def _completion_score(self, summary: str) -> float:
        try:
            fn = getattr(self.decisions, "completion_score", None)
            if fn is None:
                return 3.0
            return float(fn(summary))
        except Exception:
            return 3.0

    # ------------------------------------------------------------- steer handling
    def _pump_steer(self, run_id: str, st: dict) -> None:
        """Handle all queued commands without blocking (pause blocks inside)."""
        q = st["queue"]
        while True:
            try:
                cmd = q.get_nowait()
            except queue.Empty:
                return
            action, message = cmd["action"], cmd.get("message")
            if action == "pause":
                self.store.update_run(run_id, status="paused")
                self._emit(run_id, "status", {"status": "paused"})
                self._wait_while_paused(run_id, st)
            elif action == "cancel":
                raise _Cancelled()
            elif action == "redirect":
                if message:
                    st["pending_redirect"] = message
            elif action == "resume":
                self.store.update_run(run_id, status="running")

    def _wait_while_paused(self, run_id: str, st: dict) -> None:
        q = st["queue"]
        while True:
            cmd = q.get()  # block until a steer command arrives
            action, message = cmd["action"], cmd.get("message")
            if action == "cancel":
                raise _Cancelled()
            if action == "redirect" and message:
                st["pending_redirect"] = message
                continue  # stay paused
            if action == "resume":
                self.store.update_run(run_id, status="running")
                self._emit(run_id, "status", {"status": "running"})
                return
            # pause / unknown: stay paused

    def _await_approval(self, run_id: str, st: dict, tool_name: str, args: dict):
        """Block in awaiting_confirm until resume (approve) / cancel / redirect."""
        q = st["queue"]
        while True:
            cmd = q.get()
            action, message = cmd["action"], cmd.get("message")
            if action == "cancel":
                raise _Cancelled()
            if action == "redirect" and message:
                st["pending_redirect"] = message
                continue  # still need an explicit resume to approve
            if action == "resume":
                self.store.update_run(run_id, status="running")
                self._emit(run_id, "status",
                           {"status": "running", "approved_tool": tool_name})
                return self.registry.call_approved(
                    tool_name, args, {"run_id": run_id,
                                     "mission_id": st["mission_id"]})
            # pause / unknown: keep waiting

    def _call_tool(self, run_id: str, st: dict, name: str, args: dict) -> dict:
        from easyagent.registry import ApprovalRequired
        try:
            counts = st.setdefault("tool_counts", {})
            counts[name] = counts.get(name, 0) + 1
            return self.registry.call(
                name, args, {"run_id": run_id, "mission_id": st["mission_id"]})
        except ApprovalRequired:
            self.store.update_run(run_id, status="awaiting_confirm")
            self._emit(run_id, "status",
                       {"status": "awaiting_confirm", "tool": name, "args": args})
            return self._await_approval(run_id, st, name, args)
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def resume_from_checkpoint(self, run_id: str) -> dict:
        """Start a NEW run seeded from run_id's latest checkpoint
        (history, step, cost restored). The old run is untouched."""
        try:
            cp = self.store.load_latest_checkpoint(run_id)
        except Exception as exc:
            return {"ok": False, "reason": f"checkpoint load failed: {exc}"}
        if cp is None:
            return {"ok": False, "reason": f"no checkpoint found for run '{run_id}'"}
        try:
            state = json.loads(cp.state_json)
        except Exception as exc:
            return {"ok": False, "reason": f"checkpoint state corrupt: {exc}"}
        mission = self.store.get_mission(state.get("mission_id"))
        if mission is None:
            return {"ok": False, "reason": "checkpoint's mission no longer exists"}
        new_run = self.store.create_run(mission.id)
        self.start_existing(new_run.run_id, mission.id, resume={
            "history": state.get("history") or [],
            "step": state.get("step") or 0,
            "cost": state.get("cost") or 0.0,
        })
        self._emit(new_run.run_id, "status",
                   {"status": "resumed_from", "old_run_id": run_id,
                    "checkpoint_step": cp.step})
        return {"ok": True, "run_id": new_run.run_id,
                "mission_id": mission.id, "resumed_from": run_id,
                "checkpoint_step": cp.step}

    # ------------------------------------------------------------------ the loop
    def _loop(self, run_id: str) -> None:
        st = self._runs[run_id]
        resume = st.pop("resume", None) or {}
        mission = self.store.get_mission(st["mission_id"])
        budget = mission.budget
        started = time.time()
        history: list[dict] = list(resume.get("history") or [])
        step = int(resume.get("step") or 0)
        if resume:
            self._emit(run_id, "status",
                       {"status": "resumed", "from_step": step,
                        "history_restored": len(history)})
        self.store.update_run(run_id, status="running")
        self._emit(run_id, "status", {"status": "running", "goal": mission.goal})
        try:
            while True:
                self._pump_steer(run_id, st)

                # ---- budget fuses: steps / cost / wall clock
                fuse = None
                if step >= budget.max_steps:
                    fuse = f"step budget exhausted ({budget.max_steps})"
                elif (budget.max_cost_usd is not None
                      and st["cost"] >= budget.max_cost_usd):
                    fuse = f"cost budget exhausted (${st['cost']:.4f})"
                elif time.time() - started > budget.max_wall_clock_s:
                    fuse = f"wall-clock budget exhausted ({budget.max_wall_clock_s}s)"
                if fuse:
                    return self._finish(run_id, st, "done",
                                        f"Mission stopped: {fuse}.")

                # ---- one ReAct iteration: observe -> decide -> act
                messages = self._build_messages(mission, history, st)
                try:
                    resp = self.llm.chat(messages, tools=self._tools_schema(run_id)) or {}
                except Exception as exc:
                    self._emit(run_id, "error", {"error": f"llm failed: {exc}"})
                    return self._finish(run_id, st, "failed", f"LLM error: {exc}")
                st["cost"] += float((resp.get("usage") or {}).get("cost_usd") or 0)
                content = resp.get("content") or ""
                tool_calls = resp.get("tool_calls") or []
                self._emit(run_id, "thought", {"step": step, "content": content})

                if not tool_calls:
                    # ---- finish path, gated by completion_score
                    score = self._completion_score(content)
                    st["completion_score"] = score
                    self._emit(run_id, "status",
                               {"completion_score": score, "step": step})
                    wall_left = (time.time() - started) < budget.max_wall_clock_s
                    if score < 3 and (step + 1) < budget.max_steps and wall_left:
                        self._emit(run_id, "status",
                                   {"status": "iterating", "score": score})
                        history.append({"role": "assistant", "content": content})
                        history.append({
                            "role": "user",
                            "content": (f"Completion score {score}/5 is below 3. "
                                        "Keep working on the goal; use tools."),
                        })
                    else:
                        return self._finish(run_id, st, "done", content)
                else:
                    # assistant message carrying all tool calls (OpenAI format:
                    # each call needs an id, tool results reference tool_call_id)
                    history.append({
                        "role": "assistant", "content": content,
                        "tool_calls": [
                            {"id": tc.get("id") or f"call-{step}-{i}",
                             "type": "function",
                             "function": {
                                 "name": tc.get("name", ""),
                                 "arguments": json.dumps(
                                     tc.get("args") or tc.get("arguments") or {},
                                     ensure_ascii=False),
                             }}
                            for i, tc in enumerate(tool_calls)
                        ],
                    })
                    for i, tc in enumerate(tool_calls):
                        tname = tc.get("name", "")
                        # map sanitized LLM function name back to registry name
                        tname = self._tool_name_map.get(tname, tname)
                        targs = tc.get("args") or tc.get("arguments") or {}
                        tid = tc.get("id") or f"call-{step}-{i}"
                        self._emit(run_id, "tool_call",
                                   {"step": step, "name": tname, "args": targs})
                        result = self._call_tool(run_id, st, tname, targs)
                        self._emit(run_id, "tool_result",
                                   {"step": step, "name": tname, "result": result})
                        history.append({"role": "tool", "tool_call_id": tid,
                                        "name": tname,
                                        "content": json.dumps(result, ensure_ascii=False,
                                                              default=str)[:8000]})

                step += 1
                self.store.update_run(run_id, step=step)
                if step % self.checkpoint_every == 0:
                    self._checkpoint(run_id, step, mission, history, st)
        except _Cancelled:
            self._finish(run_id, st, "cancelled", "Cancelled by steer command.")
        except Exception as exc:  # never let the thread die silently
            self._emit(run_id, "error", {"error": f"{type(exc).__name__}: {exc}"})
            self._finish(run_id, st, "failed", str(exc))

    def _build_messages(self, mission, history: list[dict], st: dict) -> list[dict]:
        tool_names = ", ".join(
            t["function"]["name"] for t in self._tools_schema(st.get("run_id"))
        ) or "(none)"
        system = (
            "You are an autonomous agent that EXTENDS ITS OWN CAPABILITIES. "
            "Goal: " + mission.goal + "\n"
            f"Available tools: {tool_names}\n"
            "Each turn, reply with a brief thought and either tool calls "
            "(as structured tool_calls) or a final summary with no tool calls "
            "when the goal is achieved.\n"
            "Capability workflow (use it when no existing tool fits the goal):\n"
            "1. memory.search FIRST — a past mission may have left a recipe.\n"
            "2. Research: fetch_docs / probe for a public HTTP API, or check "
            "what is installed locally via shell.exec.\n"
            "3. Build: scaffold writes plugins/inbox/<name>/{manifest.json,"
            "tool.json,impl.py,fixtures.json} and returns its absolute path. "
            "For non-HTTP tools, write impl.py yourself with file.write using "
            "that absolute path (it must define run(args, ctx) -> dict) plus "
            "manifest.json/tool.json/fixtures.json, then continue at step 4.\n"
            "4. plugin.promote <name> runs fixtures offline and HOT-LOADS the "
            "tool — it becomes callable in your next turn. Fix and retry on "
            "failure; never leave a broken plugin silently.\n"
            "5. Test the new tool on the real goal, then memory.append the "
            "working recipe so future missions remember it.\n"
            "Self-improvement is REGULARIZED (RRSI): the loop that improves "
            "the harness is itself constrained, and you must never weaken "
            "these gates to make a promotion pass. plugin.promote enforces: "
            "(1) leakage review — impls that hardcode fixture answers are "
            "rejected; (2) noise baseline — every promotion needs >=1 "
            "fixture and deterministic re-runs; (3) cost rules — mission "
            "circuit breakers (max steps / cost / wall-clock) plus a "
            "per-tool usage ledger; (4) pruning - plugin.prune demotes "
            "tools that fail too often or go stale; (5) merging - "
            "plugin.merge combines near-duplicate shell-macro plugins "
            "into one inbox plugin (still gated by promote); (6) memory "
            "hygiene - memory.consolidate merges near-duplicate "
            "learnings so the log stays compact and searchable.\n"
            "API keys: NEVER invent or hardcode keys. If a task fundamentally "
            "needs an external API key you do not have, finish with the final "
            "summary exactly: NEED_KEY: <service> - <what the key is for>. "
            "If a key is provided via environment variable, read it from the "
            "environment at call time; never print it or write it to disk."
        )
        # Operational memory: inject the most recent learnings so past
        # missions' lessons shape this run (docs: last 20 entries).
        try:
            get_learnings = getattr(self.store, "get_learnings", None)
            learnings = (get_learnings(limit=20)
                         if callable(get_learnings) else []) or []
        except Exception:
            learnings = []
        learned_lines = [str(l.get("text", "")).strip() for l in learnings
                         if isinstance(l, dict)
                         and str(l.get("text", "")).strip()]
        if learned_lines:
            system += ("\nOperational memory (learnings from past missions):\n"
                       + "\n".join(f"- {t}" for t in learned_lines) + "\n")
        messages = [{"role": "system", "content": system}]
        redirect = st.get("pending_redirect")
        if redirect:
            st["pending_redirect"] = None
            messages.append({"role": "user",
                             "content": f"[steer redirect] {redirect}"})
        messages.extend(history)
        return messages

    def _checkpoint(self, run_id: str, step: int, mission, history: list[dict],
                    st: dict) -> None:
        state = {
            "mission_id": st["mission_id"],
            "goal": mission.goal,
            "step": step,
            "history": history[-20:],
            "cost": st["cost"],
        }
        try:
            self.store.save_checkpoint(run_id, step,
                                       json.dumps(state, ensure_ascii=False))
        except Exception:
            pass
        self._emit(run_id, "checkpoint", {"step": step})

    def _finish(self, run_id: str, st: dict, status: str, summary: str) -> None:
        try:
            self.store.update_run(run_id, status=status)
        except Exception:
            pass
        if summary:
            self._emit(run_id, "artifact",
                       {"kind": "summary", "content": str(summary)[:4000]})
        self._check_bypass(run_id, st, status, summary)
        self._emit(run_id, "status", {"status": status})
        self._emit(run_id, "done", {"status": status,
                                    "summary": str(summary)[:2000]})
        # RRSI pruning checkpoint: every finished run re-evaluates the plugin
        # population. A dry-run report is always emitted when candidates
        # exist; real demotion only with EASYAGENT_AUTO_PRUNE=1 (best-effort).
        try:
            if self.registry is not None and hasattr(self.registry, "prune"):
                auto = os.environ.get("EASYAGENT_AUTO_PRUNE", "").strip() == "1"
                report = self.registry.prune(dry_run=not auto)
                cands = report.get("pruned") or []
                if cands:
                    self._emit(run_id, "prune_candidates",
                               {"dry_run": report.get("dry_run", True),
                                "candidates": cands,
                                "hint": "run plugin.prune with dry_run=false "
                                        "to demote, or set EASYAGENT_AUTO_PRUNE=1"})
        except Exception:
            pass
        # Best-effort push notification (mobile clients whose SSE died in
        # the background). Never raises; see easyagent/notify.py.
        try:
            from . import notify as _notify
            goal = ""
            try:
                mission = self.store.get_mission(st.get("mission_id") or "")
                goal = getattr(mission, "goal", "") or ""
            except Exception:
                pass
            _notify.notify_run_finished_async(run_id, status, goal=goal,
                                                summary=str(summary or ""))
        except Exception:
            pass

    # --------------------------------------------- bypass counter (RRSI glue)
    _PRIMITIVE_TOOLS = ("shell.exec", "file.write", "file.edit")
    _BUILDER_TOOLS = ("scaffold", "plugin.promote")

    def _check_bypass(self, run_id: str, st: dict, status: str,
                      summary: str) -> None:
        """Detect 'bypass': mission solved with raw primitives, no plugin built.

        When the same goal family is bypassed EASYAGENT_BYPASS_THRESHOLD times
        (default 2), the capability workflow is forced: run ensure_capability
        with the recorded shell transcript as the scaffold recipe. Set
        EASYAGENT_BYPASS_AUTO=0 to disable the check entirely.
        """
        try:
            if os.environ.get("EASYAGENT_BYPASS_AUTO", "1") in (
                    "0", "false", "no", ""):
                return
            if status != "done":
                return
            if float(st.get("completion_score") or 0) < 3:
                return
            if "Mission stopped" in str(summary or ""):
                return
            counts = st.get("tool_counts") or {}
            if any(counts.get(t) for t in self._BUILDER_TOOLS):
                return  # agent already used the capability workflow
            primitive = sum(counts.get(t, 0) for t in self._PRIMITIVE_TOOLS)
            if primitive < _env_int("EASYAGENT_BYPASS_PRIMITIVE_THRESHOLD", 3):
                return
            mission = self.store.get_mission(st["mission_id"])
            goal = mission.goal if mission is not None else ""
            sig = _goal_sig(goal)
            cmds = self._shell_transcript(run_id)
            detail = json.dumps({"goal": goal[:200], "commands": cmds[:10]},
                                ensure_ascii=False)
            self.store.record_bypass(sig, st["mission_id"], primitive,
                                     detail)
            n = self.store.count_bypasses(sig)
            self._emit(run_id, "bypass",
                       {"goal_sig": sig, "count": n,
                        "primitive_calls": primitive})
            self.store.append_learning(
                f"bypass #{n} for goal family '{sig}': mission solved with "
                f"{primitive} raw shell/file calls instead of building a "
                f"plugin. Goal: {goal[:120]}",
                ["bypass"])
            if n >= _env_int("EASYAGENT_BYPASS_THRESHOLD", 2):
                self._emit(run_id, "scaffold_suggested",
                           {"goal_sig": sig, "bypasses": n})
                self._force_scaffold(run_id, st, goal, sig, cmds, n)
        except Exception:
            pass  # bypass accounting must never break mission teardown

    def _shell_transcript(self, run_id: str) -> list[str]:
        """Recorded shell.exec commands for a run, in order."""
        cmds: list[str] = []
        try:
            for ev in self.store.get_events(run_id):
                if ev.type != "tool_call":
                    continue
                payload = ev.payload if isinstance(ev.payload, dict) else {}
                if payload.get("name") != "shell.exec":
                    continue
                cmd = (payload.get("args") or {}).get("command")
                if cmd:
                    cmds.append(str(cmd))
        except Exception:
            pass
        return cmds

    def _force_scaffold(self, run_id: str, st: dict, goal: str, sig: str,
                        cmds: list[str], n: int) -> None:
        """Bypass threshold hit: force the capability workflow.

        Runs CapabilityDiscovery.ensure_capability with a planner built from
        the recorded shell transcript, so the 'macro' becomes a real plugin
        candidate. It still passes every promote gate (fixtures, determinism,
        leak review, judge + EASYAGENT_GATE_POLICY).
        """
        cmds = [c for c in cmds if c][:10]
        if not cmds or self.registry is None:
            return
        try:
            from easyagent.discovery import CapabilityDiscovery
        except Exception:
            return

        def _planner(need: str, docs_text: str, ctx: dict) -> dict:
            return {"kind": "shell", "commands": list(cmds)}

        name = f"bypass-{sig}-{n}"[:48]
        recipe_json = json.dumps({"kind": "shell", "commands": cmds},
                                 ensure_ascii=False)
        try:
            self.store.append_learning(
                f"bypass threshold hit for '{sig}': forcing scaffold from "
                f"recorded shell transcript.\nrecipe: {recipe_json}",
                ["bypass", "scaffold_suggested", "recipe"])
        except Exception:
            pass
        disc = CapabilityDiscovery(
            registry=self.registry, store=self.store, planner=_planner,
            max_steps=5, max_wall_s=120)
        try:
            plugin = disc.ensure_capability(
                goal,
                {"tool_name": name,
                 "description": f"auto-scaffolded from bypassed mission: "
                                f"{goal[:80]}",
                 "planner": _planner})
        except Exception:
            plugin = None
        self._emit(run_id, "bypass_scaffold",
                   {"goal_sig": sig, "plugin": plugin})


# ------------------------------------------------- module-level glue (server.py)
_default_runner: "MissionRunner | None" = None


def get_default_runner() -> "MissionRunner":
    """Process-wide default runner (used by server.py entrypoints)."""
    global _default_runner
    if _default_runner is None:
        _default_runner = MissionRunner()
    return _default_runner


def start(run_id: str, mission_id: str) -> str:
    """Entrypoint for server.py: start an already-created run."""
    return get_default_runner().start_existing(run_id, mission_id)


def start_background(run_id: str, mission_id: str) -> str:
    """Alias of start (the runner is always background-threaded)."""
    return start(run_id, mission_id)


def queue_command(run_id: str, command: Any) -> bool:
    """Entrypoint for server.py: steer a running run."""
    return get_default_runner().queue_command(run_id, command)


def resume_from_checkpoint(run_id: str) -> dict:
    """Entrypoint for server.py: start a NEW run seeded from run_id's latest
    checkpoint (history, step, cost restored). The old run is untouched."""
    return get_default_runner().resume_from_checkpoint(run_id)
