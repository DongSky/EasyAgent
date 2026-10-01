"""Smoke: gate degradation policy + bypass counter + shell scaffold + lifespan.

Zero paid calls. Covers:

1. EASYAGENT_GATE_POLICY=ask: gap_triage (Jev down) -> "ask" (not "explore");
   plugin_judge down -> promotion held as pending_human, NOT moved to active.
2. EASYAGENT_GATE_POLICY=halt: gap_triage raises GateHalted;
   ensure_capability returns None without building; promote refuses.
3. Default (open): old fail-open behavior preserved.
4. Bypass counter: mission solved with raw shell.exec x N, no scaffold ->
   bypass recorded; 2nd bypass -> ensure_capability forced (mocked).
5. scaffold accepts {"kind": "shell", "commands": [...]} and the generated
   impl.py actually replays the commands.
6. server lifespan exit calls registry.stop_watch().
"""
import asyncio
import json
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))

from easyagent.registry import ToolRegistry  # noqa: E402
from easyagent.discovery import CapabilityDiscovery  # noqa: E402
from easyagent.loop import MissionRunner  # noqa: E402
from easyagent.decisions import ProviderUnavailable, GateHalted  # noqa: E402


# ------------------------------------------------------------------ fakes
class FakeStore:
    def __init__(self):
        self.missions = {}
        self.runs = {}
        self.events = {}
        self.tool_versions = []
        self.learnings = []
        self.bypasses = []
        self._seq = 0
        self._n = 0

    def create_mission(self, goal, budget=None):
        self._n += 1
        m = SimpleNamespace(id=f"m{self._n}", goal=goal, budget=budget)
        self.missions[m.id] = m
        return m

    def get_mission(self, mission_id):
        return self.missions[mission_id]

    def create_run(self, mission_id):
        self._n += 1
        r = SimpleNamespace(run_id=f"r{self._n}", mission_id=mission_id,
                            status="pending")
        self.runs[r.run_id] = r
        self.events[r.run_id] = []
        return r

    def update_run(self, run_id, **fields):
        r = self.runs[run_id]
        for k, v in fields.items():
            setattr(r, k, v)
        return r

    def append_event(self, run_id, type, payload=None):
        self._seq += 1
        ev = SimpleNamespace(run_id=run_id, seq=self._seq, type=type,
                             payload=payload or {}, ts="now")
        self.events[run_id].append(ev)
        return ev

    def get_events(self, run_id, after_seq=0):
        return [e for e in self.events[run_id] if e.seq > after_seq]

    def record_tool_version(self, name, version, manifest_json, status):
        self.tool_versions.append({"name": name, "version": version,
                                   "status": status})

    def append_learning(self, text, tags=None):
        self.learnings.append({"text": text, "tags": tags or []})

    def record_bypass(self, goal_sig, mission_id, primitive_calls,
                      detail=""):
        self.bypasses.append({"goal_sig": goal_sig, "mission_id": mission_id,
                              "primitive_calls": primitive_calls})
        return len(self.bypasses)

    def count_bypasses(self, goal_sig):
        return sum(1 for b in self.bypasses if b["goal_sig"] == goal_sig)


class _BrokenProvider:
    """Every decision call fails: Jev AND the fallback subagent judge down."""

    def decide_raw(self, kind, instructions, criteria, state):
        raise ProviderUnavailable("all providers down")


class FakeMemory:
    def __init__(self):
        self.learnings = []

    def append_learning(self, text, tags=None):
        self.learnings.append({"text": text, "tags": tags or []})

    def search(self, query, limit=5):
        return []


def _write_inbox_plugin(base, name):
    d = os.path.join(base, "inbox", name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "manifest.json"), "w") as f:
        json.dump({"name": name, "version": "1.0.0",
                   "description": "t", "trust": "trusted"}, f)
    with open(os.path.join(d, "tool.json"), "w") as f:
        json.dump({"args": {}}, f)
    with open(os.path.join(d, "impl.py"), "w") as f:
        f.write("def run(args, ctx):\n    return {'ok': True}\n")
    with open(os.path.join(d, "fixtures.json"), "w") as f:
        json.dump([{"name": "smoke", "args": {},
                    "expect": {"ok": True}}], f)


def _set_policy(v):
    if v is None:
        os.environ.pop("EASYAGENT_GATE_POLICY", None)
    else:
        os.environ["EASYAGENT_GATE_POLICY"] = v


# ------------------------------------------------------------------ tests
def test_policy_ask():
    _set_policy("ask")
    try:
        store = FakeStore()
        registry = ToolRegistry(tempfile.mkdtemp(), store=store)
        disc = CapabilityDiscovery(registry, memory=FakeMemory(), store=store,
                                   decisions=_BrokenProvider())
        assert disc._gap_triage("do X") == "ask", "policy=ask must hand to human"

        plugins = tempfile.mkdtemp()
        os.makedirs(os.path.join(plugins, "inbox"))
        reg2 = ToolRegistry(plugins, store=store)
        reg2.decisions = _BrokenProvider()
        _write_inbox_plugin(plugins, "held")
        rep = reg2.promote("held")
        assert not rep["ok"], rep
        assert "human review" in rep["reason"], rep
        assert os.path.isdir(os.path.join(plugins, "inbox", "held")), \
            "held plugin must stay in inbox"
        assert not os.path.isdir(os.path.join(plugins, "active", "held")), \
            "held plugin must NOT be activated"
        statuses = [r["status"] for r in store.tool_versions]
        assert "pending_human" in statuses, statuses
    finally:
        _set_policy(None)
    print("PASS test_policy_ask: triage->ask, promote held for human")


def test_policy_halt():
    _set_policy("halt")
    try:
        store = FakeStore()
        registry = ToolRegistry(tempfile.mkdtemp(), store=store)
        mem = FakeMemory()
        disc = CapabilityDiscovery(registry, memory=mem, store=store,
                                   decisions=_BrokenProvider())
        try:
            disc._gap_triage("do X")
            raise AssertionError("policy=halt must raise GateHalted")
        except GateHalted:
            pass
        assert disc.ensure_capability("do X", {}) is None
        assert any("halted" in l["text"] for l in mem.learnings), \
            mem.learnings

        plugins = tempfile.mkdtemp()
        os.makedirs(os.path.join(plugins, "inbox"))
        reg2 = ToolRegistry(plugins, store=store)
        reg2.decisions = _BrokenProvider()
        _write_inbox_plugin(plugins, "stopped")
        rep = reg2.promote("stopped")
        assert not rep["ok"] and rep["reason"].startswith("halted"), rep
        assert os.path.isdir(os.path.join(plugins, "inbox", "stopped"))
        statuses = [r["status"] for r in store.tool_versions]
        assert "halted" in statuses, statuses
    finally:
        _set_policy(None)
    print("PASS test_policy_halt: triage raises, ensure->None, promote refused")


def test_policy_open_default():
    _set_policy(None)
    store = FakeStore()
    registry = ToolRegistry(tempfile.mkdtemp(), store=store)
    disc = CapabilityDiscovery(registry, memory=FakeMemory(), store=store,
                               decisions=_BrokenProvider())
    assert disc._gap_triage("do X") == "explore"

    plugins = tempfile.mkdtemp()
    os.makedirs(os.path.join(plugins, "inbox"))
    reg2 = ToolRegistry(plugins, store=store)
    reg2.decisions = _BrokenProvider()
    _write_inbox_plugin(plugins, "openp")
    rep = reg2.promote("openp")
    assert rep["ok"], rep
    assert rep["plugin_judge"] == "unavailable (fail-open)", rep
    assert os.path.isdir(os.path.join(plugins, "active", "openp"))
    print("PASS test_policy_open_default: explicit fail-open preserved")


def _bypass_runner(store):
    runner = MissionRunner.__new__(MissionRunner)
    runner.store = store
    runner.registry = None
    return runner


def _emit_shell_events(store, run_id, cmds):
    for c in cmds:
        store.append_event(run_id, "tool_call",
                           {"name": "shell.exec", "args": {"command": c}})


def test_bypass_counter_and_force():
    os.environ["EASYAGENT_BYPASS_AUTO"] = "1"
    os.environ["EASYAGENT_BYPASS_PRIMITIVE_THRESHOLD"] = "3"
    os.environ["EASYAGENT_BYPASS_THRESHOLD"] = "2"
    try:
        store = FakeStore()
        runner = _bypass_runner(store)
        mission = store.create_mission("render a wave video")
        run = store.create_run(mission.id)
        st = {"mission_id": mission.id, "completion_score": 4.0,
              "tool_counts": {"shell.exec": 3}}
        _emit_shell_events(store, run.run_id,
                            ["python3 make_wave.py", "ls wave.mp4"])

        forced = {}

        def fake_force(run_id, st, goal, sig, cmds, n):
            forced["called"] = True
            forced["cmds"] = cmds
            forced["sig"] = sig

        runner._force_scaffold = fake_force
        # 1st bypass: recorded, not forced
        runner._finish(run.run_id, st, "done", "video rendered ok")
        assert store.count_bypasses("render-a-wave-video") == 1
        assert not forced, "must not force on 1st bypass"
        assert any(b["tags"] and "bypass" in b["tags"]
                   for b in store.learnings)
        kinds = [e.type for e in store.get_events(run.run_id)]
        assert "bypass" in kinds, kinds

        # 2nd bypass: forced scaffold
        run2 = store.create_run(mission.id)
        st2 = {"mission_id": mission.id, "completion_score": 4.0,
               "tool_counts": {"shell.exec": 4}}
        _emit_shell_events(store, run2.run_id, ["python3 make_wave.py"])
        runner._finish(run2.run_id, st2, "done", "video rendered ok")
        assert store.count_bypasses("render-a-wave-video") == 2
        assert forced.get("called"), "must force scaffold on 2nd bypass"
        assert forced["cmds"] == ["python3 make_wave.py"], forced
        kinds2 = [e.type for e in store.get_events(run2.run_id)]
        assert "scaffold_suggested" in kinds2, kinds2

        # not a bypass: agent used the capability workflow
        run3 = store.create_run(mission.id)
        st3 = {"mission_id": mission.id, "completion_score": 4.0,
               "tool_counts": {"shell.exec": 5, "scaffold": 1}}
        runner._finish(run3.run_id, st3, "done", "ok")
        assert store.count_bypasses("render-a-wave-video") == 2

        # not a bypass: low score / fuse stop
        run4 = store.create_run(mission.id)
        st4 = {"mission_id": mission.id, "completion_score": 2.0,
               "tool_counts": {"shell.exec": 9}}
        runner._finish(run4.run_id, st4, "done", "ok")
        run5 = store.create_run(mission.id)
        st5 = {"mission_id": mission.id, "completion_score": 4.0,
               "tool_counts": {"shell.exec": 9}}
        runner._finish(run5.run_id, st5, "done",
                       "Mission stopped: max steps reached")
        assert store.count_bypasses("render-a-wave-video") == 2
    finally:
        for k in ("EASYAGENT_BYPASS_AUTO",
                  "EASYAGENT_BYPASS_PRIMITIVE_THRESHOLD",
                  "EASYAGENT_BYPASS_THRESHOLD"):
            os.environ.pop(k, None)
    print("PASS test_bypass_counter_and_force: count, threshold, exclusions")


def test_shell_scaffold():
    from easyagent.tools import meta
    old_root = os.environ.get("EASYAGENT_PLUGINS")
    tmp = tempfile.mkdtemp()
    os.environ["EASYAGENT_PLUGINS"] = tmp
    try:
        out = meta.scaffold(
            {"name": "wave-macro", "description": "replay wave render",
             "recipe": {"kind": "shell",
                        "commands": ["echo one", "echo two"]}}, {})
        assert out["ok"], out
        impl_path = os.path.join(out["path"], "impl.py")
        assert os.path.exists(impl_path)
        ns = {}
        with open(impl_path) as f:
            exec(compile(f.read(), impl_path, "exec"), ns)
        res = ns["run"]({}, {})
        assert res["ok"] and len(res["steps"]) == 2, res
        assert res["steps"][0]["stdout"].strip() == "one"
        res2 = ns["run"]({"args": ["X"]}, {})
        assert res2["steps"][1]["stdout"].strip() == "two X", res2
        bad = meta.scaffold(
            {"name": "bad", "description": "t",
             "recipe": {"kind": "shell", "commands": []}}, {})
        assert not bad["ok"], bad
    finally:
        if old_root is None:
            os.environ.pop("EASYAGENT_PLUGINS", None)
        else:
            os.environ["EASYAGENT_PLUGINS"] = old_root
    print("PASS test_shell_scaffold: macro plugin generated and replays")


def test_lifespan_stop_watch():
    import easyagent.server as server_mod

    calls = []

    class FakeReg:
        def watch(self):
            calls.append("watch")

        def stop_watch(self):
            calls.append("stop_watch")

    server_mod._load_sibling = lambda name: SimpleNamespace(
        get_default_registry=lambda: FakeReg())

    async def go():
        async with server_mod.lifespan(SimpleNamespace()):
            calls.append("inside")

    asyncio.run(go())
    assert calls == ["watch", "inside", "stop_watch"], calls
    print("PASS test_lifespan_stop_watch: watcher stopped on shutdown")


def main():
    test_policy_ask()
    test_policy_halt()
    test_policy_open_default()
    test_bypass_counter_and_force()
    test_shell_scaffold()
    test_lifespan_stop_watch()
    print("ALL GATE/BYPASS SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
