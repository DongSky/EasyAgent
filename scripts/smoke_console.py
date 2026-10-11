"""Offline console/API regressions. Use --serve for manual UI QA on port 8766.

All data lives in a fresh temporary directory. No external model calls.
The QA model writes/reads a test file, exercises a missing file and requests
approval for a harmless shell command. Goals containing '持续' keep running.
"""
import os
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
TMP = tempfile.mkdtemp(prefix="easyagent-console-")
os.environ.update(EASYAGENT_DB=f"{TMP}/state.db", EASYAGENT_PLUGINS=f"{TMP}/plugins",
                  EASYAGENT_WORKSPACE=f"{TMP}/work", EASYAGENT_MOCK_LLM="1",
                  JEV_ENABLED="0", EASYAGENT_BYPASS_AUTO="0")
os.environ.pop("EASYAGENT_NOTIFY_WEBHOOK", None)
os.environ.pop("EASYAGENT_API_KEY", None)

from fastapi.testclient import TestClient
from easyagent import server, loop, registry
from easyagent.decisions import MockProvider
from easyagent.contracts import Budget
from easyagent.tools import files, meta


class QAProvider(MockProvider):
    def risk_gate(self, name, args):
        return name == "shell.exec"

    def completion_score(self, summary):
        return 4.0


class QAModel:
    def chat(self, messages, tools=None):
        time.sleep(0.1)
        results = [m for m in messages if m["role"] == "tool"]
        goal = messages[0]["content"]
        n = len(results)
        if "持续" in goal and n >= 4:
            name, args = "file.read", {"path": "qa.txt"}
        elif n < 4:
            name, args = [
                ("file.write", {"path": "qa.txt", "content": "console QA passed"}),
                ("file.read", {"path": "qa.txt"}),
                ("file.read", {"path": "missing.txt"}),
                ("shell.exec", {"command": ["echo", "console-approval-ok"]}),
            ][n]
        else:
            return {"content": "离线验证完成：文件写入、读取、错误展示和人工放行正常。", "tool_calls": []}
        return {"content": f"QA step {n}: {name}", "tool_calls": [
            {"id": f"qa-{n}", "name": name, "arguments": args}], "usage": {}}


reg = registry.get_default_registry()
reg.decisions = QAProvider()
runner = loop.MissionRunner(store=server._store, registry=reg, decisions=reg.decisions,
                            llm=QAModel(), checkpoint_every=1)
loop._default_runner = runner
client = TestClient(server.app)


def wait_status(rid, status):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if server._store.get_run(rid).status == status:
            return
        time.sleep(0.01)
    raise AssertionError(f"expected {status}: {server._store.get_run(rid)}")


class ConsoleTests(unittest.TestCase):
    def create(self, goal="持续测试", **budget):
        res = client.post("/v1/missions", json={"goal": goal, "budget": budget})
        self.assertEqual(res.status_code, 201, res.text)
        rid = res.json()["run_id"]
        self.addCleanup(lambda: runner.queue_command(rid, {"action": "cancel"}))
        return rid

    def test_validation(self):
        for data in ({"goal": " "}, {"goal": "x", "budget": {"max_steps": 0}},
                     {"goal": "x", "budget": {"max_cost_usd": -1}},
                     {"goal": "x", "budget": {"max_wall_clock_s": 0}}):
            self.assertEqual(client.post("/v1/missions", json=data).status_code, 422)

    def test_controls_and_checkpoint(self):
        rid = self.create()
        self.assertEqual(client.post(f"/v1/runs/{rid}/steer", json={"action": "pause"}).status_code, 200)
        wait_status(rid, "paused")
        step = server._store.get_run(rid).step
        time.sleep(0.15)
        self.assertEqual(server._store.get_run(rid).step, step)
        client.post(f"/v1/runs/{rid}/steer", json={"action": "redirect", "message": "新的方向"})
        self.assertEqual(server._store.get_run(rid).status, "paused")
        client.post(f"/v1/runs/{rid}/steer", json={"action": "resume"})
        wait_status(rid, "awaiting_confirm")
        self.assertTrue(any("新的方向" in m.get("content", "") for m in
                            json.loads(server._store.load_latest_checkpoint(rid).state_json)["history"]))
        client.post(f"/v1/runs/{rid}/steer", json={"action": "resume"})
        wait_status(rid, "running")
        client.post(f"/v1/runs/{rid}/steer", json={"action": "cancel"})
        wait_status(rid, "cancelled")
        self.assertEqual(client.post(f"/v1/runs/{rid}/steer", json={"action": "pause"}).status_code, 409)
        events = server._store.get_events(rid)
        self.assertEqual(sum(e.type == "steer" for e in events), 5, "one event per command")
        resumed = client.post(f"/v1/runs/{rid}/resume")
        self.assertEqual(resumed.status_code, 200)
        new_id = resumed.json()["run_id"]
        self.assertNotEqual(new_id, rid)
        runner.queue_command(new_id, {"action": "cancel"})
        runner.wait(new_id, 5)

    def test_completion_and_sse_replay(self):
        rid = self.create("完成测试")
        wait_status(rid, "awaiting_confirm")
        client.post(f"/v1/runs/{rid}/steer", json={"action": "resume"})
        wait_status(rid, "done")
        events = server._store.get_events(rid)
        self.assertIn("done", [e.type for e in events])
        self.assertIn("artifact", [e.type for e in events])
        self.assertTrue(any(e.type == "tool_result" and e.payload["result"].get("ok") is False for e in events))
        response = client.get(f"/v1/runs/{rid}/events")
        self.assertIn('"type":"artifact"', response.text)
        self.assertIn('"type":"done"', response.text)
        self.assertIn("event: end", response.text)
        replay = client.get(f"/v1/runs/{rid}/events?after_seq={events[-1].seq}")
        self.assertNotIn('"type":', replay.text)

    def test_budgets(self):
        for budget in ({"max_steps": 1}, {"max_cost_usd": 0}):
            rid = self.create(**budget)
            wait_status(rid, "done")
            self.assertTrue(any("budget exhausted" in str(e.payload) for e in server._store.get_events(rid)))
        rid = self.create(max_wall_clock_s=1)
        wait_status(rid, "awaiting_confirm")
        runner.queue_command(rid, {"action": "resume"})
        wait_status(rid, "done")
        self.assertTrue(any("wall-clock budget exhausted" in str(e.payload) for e in server._store.get_events(rid)))

    def test_missing_and_inactive(self):
        for url in ("/v1/runs/missing", "/v1/runs/missing/events"):
            self.assertEqual(client.get(url).status_code, 404)
        m = server._store.create_mission("orphan", Budget())
        r = server._store.create_run(m.id)
        self.assertEqual(client.post(f"/v1/runs/{r.run_id}/steer", json={"action": "pause"}).status_code, 409)
        self.assertEqual(client.post(f"/v1/runs/{r.run_id}/resume").status_code, 400)

    def test_registry_and_promotion(self):
        previous = loop._default_runner
        try:
            loop._default_runner = None
            self.assertIs(loop.get_default_runner().registry, registry.get_default_registry())
        finally:
            loop._default_runner = previous
        self.assertEqual(reg.plugins_dir, os.environ["EASYAGENT_PLUGINS"])
        self.assertEqual(meta.plugins_root(), Path(reg.plugins_dir))
        self.assertGreater(len(client.get("/v1/tools").json()["tools"]), 10)
        result = client.post("/v1/tools/promote", json={"name": "missing"})
        self.assertFalse(result.json()["ok"])

    def test_file_paths(self):
        self.assertFalse(files.read({"path": "../outside.txt"})["ok"])
        self.assertFalse(files.write({"path": "../outside.txt", "content": "blocked"})["ok"])
        configured = os.environ.pop("EASYAGENT_PLUGINS")
        try:
            self.assertEqual(meta.plugins_root(), Path(registry._default_plugins_dir()))
        finally:
            os.environ["EASYAGENT_PLUGINS"] = configured

    def test_terminal_state_published_last(self):
        mission = server._store.create_mission("event ordering", Budget())
        run = server._store.create_run(mission.id)
        original = server._store.update_run
        def update(run_id, **fields):
            if fields.get("status") == "done":
                types = [e.type for e in server._store.get_events(run_id)]
                self.assertIn("artifact", types)
                self.assertIn("done", types)
            return original(run_id, **fields)
        server._store.update_run = update
        try:
            runner._finish(run.run_id, {"mission_id": mission.id}, "done", "summary")
        finally:
            server._store.update_run = original


if __name__ == "__main__":
    if "--serve" in sys.argv:
        import uvicorn
        print(f"Offline QA data: {TMP}")
        uvicorn.run(server.app, host="127.0.0.1", port=8766)
    else:
        unittest.main()
