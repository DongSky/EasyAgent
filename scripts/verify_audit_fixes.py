"""Verify this session's audit-driven fixes (run from repo root):

- leakage review catches unconditional-answer cheats (textual + behavioral)
- legit input-driven plugins still promote
- determinism re-runs are env-tunable and enforced
- resume_from_checkpoint starts a new run seeded from the checkpoint
- prune reads the persistent tool_usage table (survives restart)
"""
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.getcwd())
os.environ.setdefault("EASYAGENT_GATE_POLICY", "open")

from easyagent import registry as regmod
from easyagent import decisions as decisions_mod
from easyagent.store import Store

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f" -- {extra}" if extra and not cond else ""))


def make_plugin(inbox, name, impl_src, fixtures):
    d = os.path.join(inbox, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "manifest.json"), "w") as f:
        json.dump({"name": name, "version": "0.1.0",
                   "description": "test plugin",
                   "tools": [{"name": name, "description": "t",
                              "schema": {"type": "object", "properties": {}}}]}, f)
    with open(os.path.join(d, "impl.py"), "w") as f:
        f.write(impl_src)
    with open(os.path.join(d, "fixtures.json"), "w") as f:
        json.dump(fixtures, f)
    return d


LONG_OUT = "the quick brown fox jumps over the lazy dog 12345"
LONG_IN = "input query about aardvarks and zebras 67890"

work = tempfile.mkdtemp(prefix="auditfix_")
store = Store(os.path.join(work, "s.db"))
reg = regmod.ToolRegistry(os.path.join(work, "plugins"), store=store)
reg.decisions = decisions_mod.MockProvider()  # isolate leak/determinism gates

# --- 1a. naive unconditional cheat (output literal in source, no input lit)
make_plugin(reg.inbox_dir, "cheat_naive",
            f"def run(args, ctx):\n    return {{'answer': '{LONG_OUT}'}}\n",
            [{"name": "f1", "args": {"q": LONG_IN},
              "expect": {"result_contains": LONG_OUT}}])
r = reg.promote("cheat_naive")
check("naive unconditional cheat rejected",
      not r.get("ok") and ("leakage" in json.dumps(r).lower()),
      json.dumps(r)[:200])

# --- 1b. sneaky cheat: output built at runtime (no literal), ignores input
sneaky = (
    "import base64\n"
    f"_E = base64.b64decode('{__import__('base64').b64encode(LONG_OUT.encode()).decode()}').decode()\n"
    "def run(args, ctx):\n"
    "    return {'answer': _E}\n"
)
make_plugin(reg.inbox_dir, "cheat_sneaky", sneaky,
            [{"name": "f1", "args": {"q": LONG_IN},
              "expect": {"result_contains": LONG_OUT}}])
r = reg.promote("cheat_sneaky")
check("sneaky runtime-built cheat rejected (behavioral probe)",
      not r.get("ok") and ("hardcoded" in json.dumps(r).lower() or "leakage" in json.dumps(r).lower()),
      json.dumps(r)[:300])

# --- 1c. legit input-driven tool still promotes
legit = (
    "def run(args, ctx):\n"
    "    q = (args or {}).get('q', '')\n"
    "    return {'answer': 'echo:' + q}\n"
)
make_plugin(reg.inbox_dir, "legit_echo", legit,
            [{"name": "f1", "args": {"q": LONG_IN},
              "expect": {"result_contains": "echo:" + LONG_IN}}])
r = reg.promote("legit_echo")
check("legit input-driven plugin promotes", r.get("ok"), json.dumps(r)[:300])

# --- 1d. input-insensitive but honest tool (no distinctive output) still promotes
make_plugin(reg.inbox_dir, "legit_fixed",
            "def run(args, ctx):\n    return {'ok': True}\n",
            [{"name": "f1", "args": {}, "expect": {"equals": {"ok": True}}}])
r = reg.promote("legit_fixed")
check("honest input-insensitive plugin promotes", r.get("ok"), json.dumps(r)[:300])

# --- 2. determinism: flaky-on-2nd-run impl still rejected; env tunable
flake = (
    "import os\n"
    "def run(args, ctx):\n"
    "    p = '/tmp/easyagent_flaky_probe'\n"
    "    n = int(open(p).read()) if os.path.exists(p) else 0\n"
    "    open(p, 'w').write(str(n + 1))\n"
    "    return {'n': n}\n"
)
try:
    os.remove("/tmp/easyagent_flaky_probe")
except OSError:
    pass
make_plugin(reg.inbox_dir, "flaky", flake,
            [{"name": "f1", "args": {}, "expect": {"equals": {"n": 0}}}])
# first run -> n=0 matches; re-runs -> n=1,2 differ
r = reg.promote("flaky")
check("non-deterministic plugin rejected", not r.get("ok"), json.dumps(r)[:200])

# --- 3. resume_from_checkpoint
from easyagent import loop as loopmod


class StubLLM:
    def chat(self, messages, tools=None):
        return {"content": "Task completed successfully. All done.",
                "tool_calls": [], "usage": {"cost_usd": 0.0}}


store2 = Store(os.path.join(work, "s2.db"))
reg2 = regmod.ToolRegistry(os.path.join(work, "plugins2"), store=store2)
runner = loopmod.MissionRunner(store=store2, registry=reg2,
                               decisions=None, llm=StubLLM(),
                               checkpoint_every=1000)
runner._completion_score = lambda content: 5
mission = store2.create_mission("test goal")
old_run = store2.create_run(mission.id)
hist = [{"role": "user", "content": "do the thing"},
        {"role": "assistant", "content": "working on it"}]
store2.save_checkpoint(old_run.run_id, 7, json.dumps({
    "mission_id": mission.id, "goal": "test goal", "step": 7,
    "history": hist, "cost": 0.5}))
res = runner.resume_from_checkpoint(old_run.run_id)
check("resume returns new run", res.get("ok") and res["run_id"] != old_run.run_id,
      json.dumps(res)[:200])
new_id = res.get("run_id")
ok_done = runner.wait(new_id, timeout=30)
check("resumed run finishes", ok_done)
evs = store2.get_events(new_id)
kinds = [e.type for e in evs]
check("resumed event emitted", "status" in kinds and any(
    (e.payload or {}).get("status") == "resumed" for e in evs
    if e.type == "status"))
check("resume with no checkpoint fails cleanly",
      not runner.resume_from_checkpoint("nope")["ok"])

# --- 4. prune reads persistent table after 'restart'
store3 = Store(os.path.join(work, "s3.db"))
pdir = os.path.join(work, "plugins3")
reg3 = regmod.ToolRegistry(pdir, store=store3)
d = os.path.join(reg3.active_dir, "staleplug")
os.makedirs(d, exist_ok=True)
with open(os.path.join(d, "manifest.json"), "w") as f:
    json.dump({"name": "staleplug", "version": "0.1.0",
               "tools": [{"name": "staleplug.tool", "description": "t",
                          "schema": {"type": "object", "properties": {}}}]}, f)
with open(os.path.join(d, "impl.py"), "w") as f:
    f.write("def run(args, ctx):\n    return {'ok': False, 'error': 'boom'}\n")
import types as _t
for _reg in (reg3,):
    _reg._tools["staleplug.tool"] = regmod.ToolDef(
        name="staleplug.tool", version="0.1.0", description="t",
        trust="trusted", module=_t.SimpleNamespace(run=lambda a, c: {"ok": False}),
        manifest_path=os.path.join(d, "manifest.json"), builtin=False)
# record 10 failing calls -> persist to DB
for _ in range(10):
    store3.record_tool_call("staleplug.tool", False, 1.0)
# 'restart': brand-new registry with empty memory
reg4 = regmod.ToolRegistry(pdir, store=store3)
reg4._tools["staleplug.tool"] = regmod.ToolDef(
        name="staleplug.tool", version="0.1.0", description="t",
        trust="trusted", module=_t.SimpleNamespace(run=lambda a, c: {"ok": False}),
        manifest_path=os.path.join(d, "manifest.json"), builtin=False)
rep = reg4.prune(dry_run=True, min_calls=5, max_failure_rate=0.5)
names = [p["name"] for p in rep.get("pruned", [])]
check("prune sees persistent usage after restart", "staleplug.tool" in names,
      json.dumps(rep)[:300])

shutil.rmtree(work, ignore_errors=True)
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
