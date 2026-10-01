"""Smoke tests for the multi-client hardening round.

Covers:
 1. Bearer auth on /v1 + legacy routes (401/201 matrix, /healthz open)
 2. Completion webhook notify (local HTTP server receives the POST)
 3. Terminal: shell.exec works
 4. Builtin embedded commands: file.write / file.edit
 5. Steer commands: queue_command pause/resume/cancel
 6. New skill: scaffold (shell macro) -> plugin.promote -> call it
 7. memory.consolidate: duplicate learnings merged, backup kept, search intact
 8. plugin.merge: two shell macros -> merged inbox plugin -> promote -> run
 9. Prompt tool trimming: EASYAGENT_MAX_PROMPT_TOOLS keeps builtins+builders,
    drops the rest by usage, emits tools_trimmed
10. _finish triggers notify (best-effort, monkeypatched)

Offline: no Jev, no external network. Uses temp dirs for plugins/db/memory.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

TMP = tempfile.mkdtemp(prefix="easyagent-mc-")
os.environ["EASYAGENT_PLUGINS"] = os.path.join(TMP, "plugins")
os.environ["EASYAGENT_DB"] = os.path.join(TMP, "easyagent.db")
os.environ["EASYAGENT_WORKSPACE"] = os.path.join(TMP, "work")
os.environ["EASYAGENT_API_KEY"] = "test-key-123"
os.environ["EASYAGENT_GATE_POLICY"] = "open"  # judge fail-open offline

from fastapi.testclient import TestClient  # noqa: E402

import easyagent.server as server  # noqa: E402
from easyagent import memory as mem_mod  # noqa: E402
from easyagent import notify as notify_mod  # noqa: E402
from easyagent import registry as reg_mod  # noqa: E402
from easyagent import store as store_mod  # noqa: E402
from easyagent.tools import meta as meta_mod  # noqa: E402
from easyagent.tools import plugin_tools  # noqa: E402
from easyagent.tools import shell as shell_mod  # noqa: E402
from easyagent.tools import files as files_mod  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))


# ---------------------------------------------------------------- 1. auth
c = TestClient(server.app, raise_server_exceptions=False)
check("healthz open", c.get("/healthz").status_code == 200)
check("v1 no key -> 401", c.post("/v1/missions", json={"goal": "x"}).status_code == 401)
check("v1 bad key -> 401",
      c.post("/v1/missions", json={"goal": "x"},
             headers={"Authorization": "Bearer wrong"}).status_code == 401)
H = {"Authorization": "Bearer test-key-123"}
r = c.post("/v1/missions", json={"goal": "auth probe"}, headers=H)
check("v1 good key -> 201", r.status_code == 201, r.text[:120])
run_id = r.json()["run_id"]
check("legacy good key -> 201",
      c.post("/missions", json={"goal": "x"}, headers=H).status_code == 201)
check("v1 get run w/ key", c.get(f"/v1/runs/{run_id}", headers=H).status_code == 200)
check("v1 get run w/o key -> 401", c.get(f"/v1/runs/{run_id}").status_code == 401)
check("v1 tools w/ key", c.get("/v1/tools", headers=H).status_code == 200)
check("v1 steer w/o key -> 401",
      c.post(f"/v1/runs/{run_id}/steer", json={"action": "pause"}).status_code == 401)
check("query key rejected everywhere",
      c.post("/v1/missions?key=test-key-123", json={"goal": "x"}).status_code == 401)
rt = c.post("/v1/sse-tokens", headers=H)
tok = rt.json().get("token", "") if rt.status_code == 200 else ""
check("sse token minted", bool(tok), rt.text[:120])
check("sse one-time token accepted",
      c.get(f"/v1/runs/{run_id}/events?token={tok}&after_seq=0").status_code == 200)
check("sse token single-use",
      c.get(f"/v1/runs/{run_id}/events?token={tok}&after_seq=0").status_code == 401)
check("sse bearer accepted",
      c.get(f"/v1/runs/{run_id}/events", headers=H).status_code == 200)
check("sse no creds -> 401",
      c.get(f"/v1/runs/{run_id}/events").status_code == 401)
check("resume needs key",
      c.post(f"/v1/runs/{run_id}/resume").status_code == 401)

# ---------------------------------------------------------- 2. webhook
received = []


class _Hook(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        received.append(json.loads(body))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


httpd = HTTPServer(("127.0.0.1", 0), _Hook)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
os.environ["EASYAGENT_NOTIFY_WEBHOOK"] = (
    f"http://127.0.0.1:{httpd.server_address[1]}/hook")
res = notify_mod.notify_run_finished("r_test", "done", goal="g", summary="s")
check("notify ok", res.get("ok") is True and len(received) == 1, str(res))
check("notify payload", received and received[0].get("run_id") == "r_test"
      and received[0].get("status") == "done", str(received[:1]))
os.environ.pop("EASYAGENT_NOTIFY_WEBHOOK", None)
res2 = notify_mod.notify_run_finished("r_x", "done")
check("notify no webhook -> graceful", res2.get("ok") is False
      and "reason" in res2, str(res2))
httpd.shutdown()

# ---------------------------------------------------------- 3. terminal
out = shell_mod.run({"command": "echo hello-terminal"}) if hasattr(shell_mod, "run") else None
if out is None:
    # shell module exposes exec-style entry; find it
    fns = [n for n in dir(shell_mod) if not n.startswith("_")]
    check("shell module has entry", False, str(fns))
else:
    check("shell.exec echo", out.get("ok") and "hello-terminal" in str(out), str(out)[:200])

# ---------------------------------------------------------- 4. file cmds
p = os.path.join(TMP, "work", "note.txt")
r1 = files_mod.write({"path": p, "content": "line1\n"})
r2 = files_mod.edit({"path": p, "old_text": "line1", "new_text": "line1 edited"})
content = open(p).read() if os.path.exists(p) else ""
check("file.write", r1.get("ok") is True, str(r1)[:150])
check("file.edit", r2.get("ok") is True and "edited" in content, str(r2)[:150])

# ---------------------------------------------------------- 5. steer queue
from easyagent import loop as loop_mod  # noqa: E402


class _StubLLM:
    def chat(self, messages, tools=None):
        return {"content": "done", "tool_calls": []}


class _StubDecisions:
    def risk_gate(self, tool_name, args):
        return True

    def completion_score(self, summary):
        return 4.0

    def gap_triage(self, need):
        return "skip"


store = store_mod.Store(os.path.join(TMP, "loop.db"))
reg = reg_mod.ToolRegistry(os.path.join(TMP, "plugins"), store=store)
reg.discover()
from easyagent.tools import register_builtins  # noqa: E402
register_builtins(reg)
plugin_tools.set_registry(reg)
runner = loop_mod.MissionRunner(store=store, registry=reg,
                                decisions=_StubDecisions(), llm=_StubLLM())
mission = store.create_mission("steer probe")
run = store.create_run(mission.id)
runner._runs[run.run_id] = {"queue": __import__("queue").Queue(), "cost": 0.0,
                            "pending_redirect": None, "mission_id": mission.id,
                            "run_id": run.run_id, "tool_counts": {},
                            "completion_score": 0.0}
from easyagent.contracts import SteerCommand  # noqa: E402
for action in ("pause", "resume", "cancel"):
    ok = runner.queue_command(run.run_id, SteerCommand(action=action))
    check(f"steer queue {action}", ok is True)

# --------------------------------------- 6. scaffold -> promote -> call
# NOTE: shell macros run commands via shlex.split (no shell=True), so no
# redirection/pipes in recipes; assert on captured stdout instead.
sc = meta_mod.scaffold({
    "name": "mc_probe",
    "description": "multi-client smoke probe",
    "recipe": {"kind": "shell",
               "commands": ["echo probe-ok"],
               "cwd": TMP},
})
check("scaffold shell macro", sc.get("ok") is True, str(sc)[:200])
pr = reg.promote("mc_probe")
check("promote mc_probe", pr.get("ok") is True, str(pr)[:300])
tooldef = reg.get("mc_probe")
res = tooldef.run({}, {}) if tooldef else {}
check("promoted tool runs",
      res.get("ok") is True and "probe-ok" in str(res.get("steps")),
      str(res)[:250])

# ---------------------------------------------------------- 7. consolidate
mem = mem_mod.Memory(path=os.path.join(TMP, "learnings.jsonl"))
mem.append_learning("部署用 docker compose up -d 启动服务", tags=["deploy"])
mem.append_learning("部署时用 docker compose up -d 来启动服务", tags=["deploy"])
mem.append_learning("部署服务使用 docker compose up -d 命令", tags=["deploy"])
mem.append_learning("Python 虚拟环境用 python -m venv 创建", tags=["python"])
mem.append_learning("创建 Python 虚拟环境：python -m venv", tags=["python"])
mem.append_learning("记得给服务器打安全补丁", tags=["ops"])
before = len(mem._read_all())
con = mem.consolidate(max_entries=3, similarity=0.4)
after = len(mem._read_all())
check("consolidate shrinks", con.get("ok") and after < before,
      f"before={before} after={after} {con}")
check("consolidate backup", con.get("backup") and os.path.exists(con["backup"]),
      str(con.get("backup")))
hits = mem.search("docker compose 部署")
check("search survives consolidate",
      any("docker" in h.get("text", "") for h in hits), str(hits)[:200])
con2 = mem.consolidate(max_entries=100)
check("consolidate no-op when small", con2.get("merged") == 0, str(con2))

# ---------------------------------------------------------- 8. plugin.merge
for nm, cmds in (("mc_a", ["echo a1", "echo common"]),
                 ("mc_b", ["echo common", "echo b1"])):
    meta_mod.scaffold({"name": nm, "description": f"macro {nm}",
                       "recipe": {"kind": "shell", "commands": cmds, "cwd": TMP}})
    pr = reg.promote(nm)
    check(f"promote {nm}", pr.get("ok") is True, str(pr)[:200])
mg = reg.merge_plugins("mc_a", "mc_b", "mc_merged")
check("merge ok", mg.get("ok") is True, str(mg)[:250])
check("merge dedupes", mg.get("ok") and mg.get("commands") ==
      ["echo a1", "echo common", "echo b1"], str(mg.get("commands")))
check("merge lands in inbox (not auto-promoted)",
      mg.get("ok") and os.path.isdir(os.path.join(TMP, "plugins", "inbox", "mc_merged"))
      and reg.get("mc_merged") is None)
pr = reg.promote("mc_merged")
check("promote merged", pr.get("ok") is True, str(pr)[:250])
res = reg.get("mc_merged").run({}, {})
check("merged tool runs", res.get("ok") is True and len(res.get("steps", [])) == 3,
      str(res)[:250])
bad = reg.merge_plugins("mc_a", "nope_missing", "mc_bad")
check("merge missing -> clean error", bad.get("ok") is False, str(bad)[:150])

# tool-level dispatch for the two new tools
plugin_tools.set_registry(reg)
check("tool plugin.merge dispatches",
      plugin_tools.merge({"name_a": "mc_a", "name_b": "mc_b",
                          "new_name": "mc_m2"}).get("ok") is True)
from easyagent.tools import memory_tools  # noqa: E402
check("tool memory.consolidate dispatches",
      memory_tools.consolidate({"max_entries": 100}).get("ok") is True)

# ---------------------------------------------------------- 9. tool trim
reg2 = reg_mod.ToolRegistry(os.path.join(TMP, "plugins2"), store=store)
register_builtins(reg2)
for i in range(60):
    reg2.register_tool(f"junk.tool_{i:02d}", lambda a, c: {"ok": True},
                       trust="untrusted", description=f"junk {i}",
                       version="0.0.1", builtin=False)
runner2 = loop_mod.MissionRunner(store=store, registry=reg2,
                                 decisions=_StubDecisions(), llm=_StubLLM())
os.environ["EASYAGENT_MAX_PROMPT_TOOLS"] = "20"
schema = runner2._tools_schema("r_trim")
names = [t["function"]["name"] for t in schema]
check("trim to limit", len(schema) == 20, f"got {len(schema)}")
builtins_kept = all(any(n == t["function"]["name"] for t in schema)
                    for n in ("shell_exec", "file_write", "file_edit"))
check("trim keeps builtins", builtins_kept, str(names[:25]))
check("trim keeps builders",
      "plugin_promote" in names and "memory_consolidate" in names, str(names))
# limit is 20, 14 builtins are rank 0 -> exactly 6 zero-usage junk fill the rest
junk_kept = [n for n in names if n.startswith("junk_tool")]
check("trim drops junk beyond limit",
      junk_kept == [f"junk_tool_{i:02d}" for i in range(6)], str(junk_kept))
# usage ranking: give one junk tool heavy usage, it should outrank other junk
store.record_tool_call("junk.tool_50", True, 1.0)
schema2 = runner2._tools_schema(None)
names2 = [t["function"]["name"] for t in schema2]
junk2 = [n for n in names2 if n.startswith("junk_tool")]
check("trim ranks junk by usage", junk2[0] == "junk_tool_50", str(junk2[:8]))
evs = store.get_events("r_trim", after_seq=0)
check("tools_trimmed event emitted",
      any(e.type == "tools_trimmed" for e in evs),
      str([(e.type) for e in evs][:10]))
os.environ.pop("EASYAGENT_MAX_PROMPT_TOOLS", None)
full = runner2._tools_schema(None)
check("no trim by default", len(full) > 20, f"got {len(full)}")

# ---------------------------------------------------------- 10. _finish notify
calls = []
orig_async = notify_mod.notify_run_finished_async
notify_mod.notify_run_finished_async = lambda *a, **k: calls.append((a, k)) or None
try:
    m2 = store.create_mission("notify probe")
    r2o = store.create_run(m2.id)
    runner._runs[r2o.run_id] = {"queue": __import__("queue").Queue(), "cost": 0.0,
                                "pending_redirect": None, "mission_id": m2.id,
                                "run_id": r2o.run_id, "tool_counts": {},
                                "completion_score": 0.0}
    runner._finish(r2o.run_id, runner._runs[r2o.run_id], "done", "all good")
    check("_finish calls notify",
          len(calls) == 1 and calls[0][0][:2] == (r2o.run_id, "done"), str(calls)[:200])
finally:
    notify_mod.notify_run_finished_async = orig_async

print()
print(f"{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
print("ALL MULTI-CLIENT SMOKE TESTS PASSED")
