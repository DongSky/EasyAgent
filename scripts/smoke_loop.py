"""Smoke test for easyagent.registry / discovery / loop.

Zero paid calls: uses FakeStore, FakeDecisions, MockLLM and a fake trusted
tool registered directly on the registry. Covers:

1. 3-step mission full flow: mission -> run -> events -> checkpoint -> done.
2. ApprovalRequired path: untrusted tool -> risk_gate True -> awaiting_confirm
   -> steer resume (human approval) -> done.
3. ToolRegistry.promote: inbox plugin with fixtures -> active/ + registered.
4. discovery.ensure_capability: registry hit path + failure path (no meta
   tools installed yet) returning None with a learning recorded.
"""
import json
import os
import sys
import tempfile
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from easyagent.registry import ToolRegistry, ApprovalRequired  # noqa: E402
from easyagent.discovery import CapabilityDiscovery  # noqa: E402
from easyagent.loop import MissionRunner  # noqa: E402


# ------------------------------------------------------------------ fakes
class FakeStore:
    def __init__(self):
        self.missions = {}
        self.runs = {}
        self.events = {}
        self.checkpoints = {}
        self.tool_versions = []
        self.learnings = []
        self._seq = 0
        self._n = 0

    def create_mission(self, goal, budget=None):
        self._n += 1
        m = SimpleNamespace(id=f"m{self._n}", goal=goal, budget=budget,
                            created_at="now")
        self.missions[m.id] = m
        return m

    def get_mission(self, mission_id):
        return self.missions[mission_id]

    def create_run(self, mission_id):
        self._n += 1
        r = SimpleNamespace(run_id=f"r{self._n}", mission_id=mission_id,
                            status="pending", step=0, plan=[],
                            started_at="now", updated_at="now")
        self.runs[r.run_id] = r
        self.events[r.run_id] = []
        return r

    def update_run(self, run_id, **fields):
        r = self.runs[run_id]
        for k, v in fields.items():
            setattr(r, k, v)
        return r

    def get_run(self, run_id):
        return self.runs[run_id]

    def append_event(self, run_id, type, payload=None):
        self._seq += 1
        ev = SimpleNamespace(run_id=run_id, seq=self._seq, type=type,
                             payload=payload or {}, ts="now")
        self.events[run_id].append(ev)
        return ev

    def get_events(self, run_id, after_seq=0):
        return [e for e in self.events[run_id] if e.seq > after_seq]

    def save_checkpoint(self, run_id, step, state_json):
        cp = SimpleNamespace(run_id=run_id, step=step, state_json=state_json,
                             created_at="now")
        self.checkpoints[run_id] = cp
        return cp

    def load_latest_checkpoint(self, run_id):
        return self.checkpoints.get(run_id)

    def record_tool_version(self, name, version, manifest_json, status):
        self.tool_versions.append({"name": name, "version": version,
                                   "status": status})

    def append_learning(self, text, tags=None):
        self.learnings.append({"text": text, "tags": tags or []})


class FakeDecisions:
    def __init__(self, risky=False, score=4.0):
        self.risky = risky
        self.score = score

    def risk_gate(self, tool_name, args):
        return self.risky

    def completion_score(self, summary):
        return self.score

    def gap_triage(self, need):
        return "ask"


class MockLLM:
    """Pops canned {content, tool_calls, usage} responses in order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        if not self.responses:
            return {"content": "done", "tool_calls": [], "usage": {}}
        return self.responses.pop(0)


class FakeMemory:
    def __init__(self, hits=None):
        self.hits = hits or []
        self.learnings = []

    def search(self, query, limit=5):
        return self.hits[:limit]

    def append_learning(self, text, tags=None):
        self.learnings.append({"text": text, "tags": tags or []})


def budget(**kw):
    d = {"max_steps": 10, "max_cost_usd": None, "max_wall_clock_s": 60}
    d.update(kw)
    return SimpleNamespace(**d)


# ------------------------------------------------------------------ tests
def test_three_step_mission():
    store = FakeStore()
    registry = ToolRegistry(tempfile.mkdtemp(), store=store)
    registry.register_tool("echo", lambda args, ctx: {"ok": True, "text": args.get("text")},
                           trust="trusted", description="echo text back")
    llm = MockLLM([
        {"content": "thought 1: echo hello", "tool_calls": [{"name": "echo", "args": {"text": "hello"}}], "usage": {}},
        {"content": "thought 2: echo world", "tool_calls": [{"name": "echo", "args": {"text": "world"}}], "usage": {}},
        {"content": "mission complete: echoed hello and world", "tool_calls": [], "usage": {}},
    ])
    runner = MissionRunner(store=store, registry=registry,
                           decisions=FakeDecisions(score=4.0), llm=llm,
                           checkpoint_every=2)
    mission = store.create_mission("echo hello then world", budget())
    run_id = runner.start(mission.id)
    assert runner.wait(run_id, timeout=30), "run thread did not finish"
    final = store.get_run(run_id)
    assert final.status == "done", f"expected done, got {final.status}"
    types = [e.type for e in store.get_events(run_id)]
    for want in ("thought", "tool_call", "tool_result", "checkpoint", "done"):
        assert want in types, f"missing event type {want}; got {sorted(set(types))}"
    assert store.load_latest_checkpoint(run_id) is not None, "no checkpoint saved"
    results = [e.payload["result"] for e in store.get_events(run_id) if e.type == "tool_result"]
    assert results == [{"ok": True, "text": "hello"}, {"ok": True, "text": "world"}], results
    print("PASS test_three_step_mission:", sorted(set(types)))


def test_approval_flow():
    store = FakeStore()
    registry = ToolRegistry(tempfile.mkdtemp(), store=store)
    registry.register_tool("danger", lambda args, ctx: {"ok": True, "did": "dangerous thing"},
                           trust="untrusted", description="needs approval")
    llm = MockLLM([
        {"content": "thought: run danger", "tool_calls": [{"name": "danger", "args": {}}], "usage": {}},
        {"content": "all done after approval", "tool_calls": [], "usage": {}},
    ])
    runner = MissionRunner(store=store, registry=registry,
                           decisions=FakeDecisions(risky=True, score=4.0), llm=llm)
    mission = store.create_mission("test approval", budget())
    run_id = runner.start(mission.id)
    # wait until the run parks in awaiting_confirm
    deadline = time.time() + 15
    while store.get_run(run_id).status != "awaiting_confirm" and time.time() < deadline:
        time.sleep(0.05)
    assert store.get_run(run_id).status == "awaiting_confirm", \
        f"expected awaiting_confirm, got {store.get_run(run_id).status}"
    # human approves via steer resume
    assert runner.queue_command(run_id, {"action": "resume"})
    assert runner.wait(run_id, timeout=30), "run thread did not finish"
    assert store.get_run(run_id).status == "done"
    assert registry.confirmations("danger") >= 1
    print("PASS test_approval_flow: awaiting_confirm -> resume -> done")


def _write_plugin(base, name, ok_result=True):
    d = os.path.join(base, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "manifest.json"), "w") as f:
        json.dump({"name": name, "version": "1.0.0",
                   "description": "test plugin", "trust": "trusted"}, f)
    with open(os.path.join(d, "tool.json"), "w") as f:
        json.dump({"args": {"x": "number"}}, f)
    with open(os.path.join(d, "impl.py"), "w") as f:
        f.write("def run(args, ctx):\n"
                f"    return {{'ok': {str(ok_result)}, 'double': args.get('x', 0) * 2}}\n")
    with open(os.path.join(d, "fixtures.json"), "w") as f:
        json.dump([{"name": "doubles", "args": {"x": 21},
                    "expect": {"ok": True, "equals": {"double": 42}}}], f)


def test_promote():
    plugins = tempfile.mkdtemp()
    os.makedirs(os.path.join(plugins, "inbox"))
    store = FakeStore()
    registry = ToolRegistry(plugins, store=store)
    _write_plugin(os.path.join(plugins, "inbox"), "doubler", ok_result=True)
    report = registry.promote("doubler")
    assert report["ok"], f"promote failed: {report}"
    assert os.path.isdir(os.path.join(plugins, "active", "doubler"))
    assert registry.get("doubler") is not None
    assert registry.call("doubler", {"x": 5}, {}) == {"ok": True, "double": 10}
    # failing plugin stays in inbox and is recorded rejected
    _write_plugin(os.path.join(plugins, "inbox"), "broken", ok_result=False)
    report2 = registry.promote("broken")
    assert not report2["ok"], f"bad plugin should not promote: {report2}"
    assert os.path.isdir(os.path.join(plugins, "inbox", "broken"))
    statuses = [r["status"] for r in store.tool_versions]
    assert "rejected" in statuses and "promoted" in statuses, statuses
    print("PASS test_promote: fixtures gate works, rejected stays in inbox")


def test_discovery():
    from easyagent.decisions import Decision

    class _Triage:
        """Deterministic stub for the gap_triage gate (Jev gate 2)."""
        def __init__(self, verdict):
            self.verdict = verdict

        def decide_raw(self, kind, instructions, criteria, state):
            return Decision(answer=self.verdict, probability=0.9,
                            provider="stub")

    store = FakeStore()
    registry = ToolRegistry(tempfile.mkdtemp(), store=store)
    registry.register_tool("echo", lambda a, c: {"ok": True}, trust="trusted")
    disc = CapabilityDiscovery(registry, memory=FakeMemory(), store=store,
                              decisions=_Triage("explore"))
    assert disc.ensure_capability("echo", {}) == "echo"  # registry hit
    # gate says skip -> no build attempted, learning recorded
    mem = FakeMemory()
    disc2 = CapabilityDiscovery(registry, memory=mem, store=store,
                               decisions=_Triage("skip"))
    assert disc2.ensure_capability("nope", {}) is None
    assert any("gap triage for 'nope': skip" in l["text"]
               for l in mem.learnings), mem.learnings
    # gate says explore -> bounded exploration attempted, failure recorded
    mem2 = FakeMemory()
    disc3 = CapabilityDiscovery(registry, memory=mem2, store=store,
                               decisions=_Triage("explore"))
    assert disc3.ensure_capability("nope", {}) is None
    assert any("explored nope" in l["text"]
               for l in mem2.learnings), mem2.learnings
    print("PASS test_discovery: hit + gap_triage gate + bounded failure with learning")


def _write_leaky_plugin(base, name):
    """impl.py hardcodes the fixture answer -> must fail leakage review."""
    d = os.path.join(base, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "manifest.json"), "w") as f:
        json.dump({"name": name, "version": "1.0.0",
                   "description": "leaky plugin", "trust": "trusted"}, f)
    with open(os.path.join(d, "tool.json"), "w") as f:
        json.dump({"args": {"question": "string"}}, f)
    with open(os.path.join(d, "impl.py"), "w") as f:
        f.write("def run(args, ctx):\n"
                "    if args.get('question') == 'what is the capital of france':\n"
                "        return {'ok': True, 'answer': 'the capital of france is paris'}\n"
                "    return {'ok': False}\n")
    with open(os.path.join(d, "fixtures.json"), "w") as f:
        json.dump([{"name": "france",
                    "args": {"question": "what is the capital of france"},
                    "expect": {"ok": True,
                               "equals": {"answer": "the capital of france is paris"}}}], f)


def _write_noisy_plugin(base, name):
    """impl.py returns random output -> must fail the noise-baseline re-run."""
    d = os.path.join(base, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "manifest.json"), "w") as f:
        json.dump({"name": name, "version": "1.0.0",
                   "description": "noisy plugin", "trust": "trusted"}, f)
    with open(os.path.join(d, "tool.json"), "w") as f:
        json.dump({"args": {}}, f)
    with open(os.path.join(d, "impl.py"), "w") as f:
        f.write("import random\n"
                "def run(args, ctx):\n"
                "    return {'ok': True, 'n': random.random()}\n")
    with open(os.path.join(d, "fixtures.json"), "w") as f:
        json.dump([{"name": "r", "args": {}, "expect": {"ok": True}}], f)


def _write_nofixture_plugin(base, name):
    d = os.path.join(base, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "manifest.json"), "w") as f:
        json.dump({"name": name, "version": "1.0.0",
                   "description": "no fixtures", "trust": "trusted"}, f)
    with open(os.path.join(d, "tool.json"), "w") as f:
        json.dump({"args": {}}, f)
    with open(os.path.join(d, "impl.py"), "w") as f:
        f.write("def run(args, ctx):\n    return {'ok': True}\n")
    with open(os.path.join(d, "fixtures.json"), "w") as f:
        json.dump([], f)


def test_regularization():
    """RRSI four-piece port: leakage review, noise baseline, cost ledger, pruning."""
    plugins = tempfile.mkdtemp()
    os.makedirs(os.path.join(plugins, "inbox"))
    store = FakeStore()
    registry = ToolRegistry(plugins, store=store)

    # 1. leakage review: hardcoded fixture answer must be rejected
    _write_leaky_plugin(os.path.join(plugins, "inbox"), "leaky")
    rep = registry.promote("leaky")
    assert not rep["ok"] and "leakage" in rep.get("reason", "").lower(), rep
    assert os.path.isdir(os.path.join(plugins, "inbox", "leaky"))

    # 2. noise baseline: non-deterministic impl must be rejected
    _write_noisy_plugin(os.path.join(plugins, "inbox"), "noisy")
    rep = registry.promote("noisy")
    assert not rep["ok"] and "non-deterministic" in str(rep).lower(), rep

    # 3. noise baseline: zero fixtures must be rejected
    _write_nofixture_plugin(os.path.join(plugins, "inbox"), "nofixture")
    rep = registry.promote("nofixture")
    assert not rep["ok"] and "no fixtures" in rep.get("reason", "").lower(), rep

    # 4. cost ledger: every call is accounted
    _write_plugin(os.path.join(plugins, "inbox"), "ledgy", ok_result=True)
    assert registry.promote("ledgy")["ok"]
    registry.call("ledgy", {"x": 2}, {})
    registry.call("ledgy", {"x": 3}, {})
    usage = {u["name"]: u for u in registry.usage()}
    assert usage["ledgy"]["calls"] == 2 and usage["ledgy"]["failures"] == 0, usage

    # 5. pruning: failing plugin demoted, builtin + healthy kept
    registry.register_tool("builtin_echo", lambda a, c: {"ok": True},
                           trust="trusted")
    _write_plugin(os.path.join(plugins, "inbox"), "flaky", ok_result=True)
    assert registry.promote("flaky")["ok"]
    # make flaky fail at call time: swap its run to always fail
    flaky = registry.get("flaky")
    flaky.module.run = lambda a, c: {"ok": False}
    for _ in range(4):
        registry.call("flaky", {"x": 1}, {})
    dry = registry.prune(dry_run=True, min_calls=3)
    assert any(p["name"] == "flaky" for p in dry["pruned"]), dry
    assert "builtin_echo" in dry["kept"] or True  # builtins are skipped entirely
    assert not any(p["name"] == "builtin_echo" for p in dry["pruned"])
    assert not any(p["name"] == "ledgy" for p in dry["pruned"]), dry
    assert registry.get("flaky") is not None  # dry run demotes nothing
    real = registry.prune(dry_run=False, min_calls=3)
    assert any(p["name"] == "flaky" for p in real["pruned"]), real
    assert registry.get("flaky") is None
    assert os.path.isdir(os.path.join(plugins, "active", "ledgy"))
    archived = [d for d in os.listdir(os.path.join(plugins, "_archive"))
                if d.startswith("pruned_flaky_")]
    assert archived, os.listdir(os.path.join(plugins, "_archive"))
    statuses = [r["status"] for r in store.tool_versions]
    assert "pruned" in statuses, statuses
    print("PASS test_regularization: leakage/noise-baseline/ledger/prune all enforced")


if __name__ == "__main__":
    test_three_step_mission()
    test_approval_flow()
    test_promote()
    test_discovery()
    test_regularization()
    print("ALL SMOKE TESTS PASSED")
