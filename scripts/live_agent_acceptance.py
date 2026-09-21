"""Opt-in, real-model product acceptance, starting only from natural-language messages.

    uv run python scripts/live_agent_acceptance.py --database .eah/hub.db --model gpt

Reads one existing connection into a fresh isolated database. No tools, nodes, workflow
graphs or model responses are supplied by the harness. Browser interactions create and
reuse the workflow; independent assertions verify actual artifacts, topology and receipts.
Credentials remain in memory, and are never written into the evidence files.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import socket
import sqlite3
import time

from cryptography.fernet import Fernet
from playwright.async_api import async_playwright, expect
import uvicorn

from easyagent.api import create_app
from easyagent.models import HTTPProvider
from easyagent.runtime import Hub
from easyagent.studio import install_studio
from easyagent_app import mount_app


WORKFLOW_REQUEST = """请创建并实际跑通一个以后可重复使用的销售汇总流程。
我每次会提供两份 CSV，流程输入分别叫 left_csv 和 right_csv。
两份数据分别按客户汇总金额，互不依赖，可以并行处理，再把两边结果合并。
请你自己新增一个可复用的纯计算节点来完成汇总和合并，测试后加入节点库；不要把示例答案写死。
这类确定性计算使用代码节点就可以，不需要再委派语言模型来算数。
最后生成可下载的 totals.json，文件内容只需客户名到总金额的 JSON 对象，并保存流程。
left_csv:
customer,amount
Alice,17.25
Bob,8
Alice,2.75
right_csv:
customer,amount
Bob,4.5
Carol,9
Alice,-1
不需要联网。完成后告诉我真实结果和保存的流程。"""

REUSE_REQUEST = """运行刚才保存的流程，这次换成下面的数据；不要重建节点或修改流程。
left_csv:
customer,amount
Dana,3.5
Eli,11
Dana,6.5
right_csv:
customer,amount
Eli,-1.25
Finn,7
Dana,2
仍然输出可下载的 totals.json。"""

DELEGATION_REQUEST = """创建并运行一个可重复使用的文字复核流程。
它只有一个负责复核的智能体节点和一个保存报告的节点。在复核节点内部，明确安排两个子助手并行工作：
一个检查算术，另一个检查表述是否矛盾；两个助手只分析提供的内容，不联网，也不修改文件。
复核节点收齐两个结果后整理 Markdown 报告，交给保存节点生成 review.md。请先保存流程再运行验证。
本次待复核内容：『订单单价 12 元、数量 3 件，总额 35 元。活动仅周六开放，但欢迎大家本周日到场。』
流程的 message 输入保留这段待复核内容，以后可以替换成其他文字。报告必须指出正确总额和时间冲突。"""


@asynccontextmanager
async def serve(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.setblocking(False)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    task.result()
                await asyncio.sleep(.02)
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        sock.close()


def connect(hub, source, alias):
    with sqlite3.connect(f"file:{Path(source).resolve()}?mode=ro", uri=True) as db:
        row = db.execute("SELECT value FROM memory WHERE namespace='model-connections' AND key=?", (alias,)).fetchone()
        if not row:
            raise ValueError("Model connection not found: " + alias)
        config = json.loads(row[0])
        credential = ""
        if config.get("credential"):
            encrypted = db.execute("SELECT ciphertext FROM vault WHERE name=?", (config["credential"],)).fetchone()[0]
            credential = Fernet(Path(source).with_suffix(".secrets.key").read_bytes()).decrypt(encrypted.encode()).decode()
    if not {"chat", "decision"}.issubset(config["capabilities"]):
        raise ValueError("Acceptance needs a connected chat/decision model")
    hub.models.register(alias, HTTPProvider(config["base_url"], credential, config["dialect"]), config["model"], config["capabilities"])
    return {k: config[k] for k in ("alias", "model", "dialect", "capabilities")}


def family(hub, root):
    result = [hub.store.run(root)]
    for child in result[0]["children"]:
        result.extend(family(hub, child["id"]))
    return result


def evidence(hub, conversation, turn):
    root = turn["task"]["run_id"]
    runs = family(hub, root)
    calls = []
    with hub.store.connect() as db:
        for run in runs:
            calls.extend(dict(r) for r in db.execute(
                "SELECT run_id,step_id,tool,status,arguments,output,error FROM invocations WHERE run_id=? ORDER BY rowid", (run["id"],)))
    # Provider continuation blobs are opaque; do not copy them into user-visible transcripts.
    for run in runs:
        for step in run["steps"]:
            for message in step.get("state", {}).get("messages", []):
                message.pop("provider_state", None)
    artifacts = [a for run in runs for a in hub.artifacts.list(run["id"])]
    return {"conversation": conversation, "turn": turn, "runs": runs, "calls": calls, "artifacts": artifacts}


def assert_totals(hub, record, expected):
    assert record["turn"]["status"] == "succeeded", record["turn"]
    artifacts = [a for a in record["artifacts"] if a["name"] == "totals.json"]
    assert artifacts, "Missing totals.json artifact"
    actual = [json.loads(hub.artifacts.get(a["id"])[1]) for a in artifacts]
    assert expected in actual, (expected, actual)


def assert_delegation(hub, record):
    agent_nodes = [(r, s) for r in record["runs"] for s in r["steps"]
                   if s["spec"]["kind"] == "agent" and r["id"] != record["runs"][0]["id"]]
    orchestrators = [(r, s) for r, s in agent_nodes if len([c for c in r["children"] if c["step_id"] == s["id"]]) >= 2]
    assert orchestrators, "No workflow agent node delegated to two children"
    reports = [hub.artifacts.get(a["id"])[1].decode() for a in record["artifacts"] if a["name"] == "review.md"]
    assert any("36" in r and "周六" in r and "周日" in r for r in reports), reports
    # Verify actual model execution overlapped; spawning two sequential children is not parallel work.
    with hub.store.connect() as db:
        overlapping = False
        for run, step in orchestrators:
            ids = [c["id"] for c in run["children"] if c["step_id"] == step["id"]]
            intervals = [db.execute("SELECT min(created),max(finished) FROM model_calls WHERE run_id=?", (child,)).fetchone() for child in ids]
            overlapping |= any(all(v is not None for v in (*a, *b)) and a[0] < b[1] and b[0] < a[1]
                               for i, a in enumerate(intervals) for b in intervals[i + 1:])
    assert overlapping, "Child model executions never overlapped"


async def send(page, hub, text, timeout, destination, output, label, model=None):
    if model:
        await page.locator("#conversations [data-model]").select_option(model)
    await page.locator("#conversations [data-destination]").select_option(destination)
    await page.locator("#workspaceMessage").fill(text)
    async with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/messages")) as sent:
        await page.locator("#conversations [data-send]").click()
    response = await sent.value
    assert response.status == 202, await response.text()
    turn_id = (await response.json())["id"]
    with hub.store.connect() as db:
        conversation = db.execute("SELECT conversation FROM conversation_turns WHERE id=?", (turn_id,)).fetchone()[0]
    last_count, deadline = -1, time.monotonic() + timeout
    while True:
        current = hub.conversations.get(conversation)
        turn = next(t for t in current["turns"] if t["id"] == turn_id)
        root = turn.get("task", {}).get("run_id")
        if root:
            run = hub.store.run(root)
            count = run["usage"]["model_calls"]
            if count != last_count:
                print(f"[{label}] model_calls={count}, status={run['status']}", flush=True)
                last_count = count
        if turn["status"] in ("succeeded", "failed", "cancelled", "waiting_connections"):
            break
        if time.monotonic() >= deadline:
            await hub.chat.interrupt(conversation)
            turn = next(t for t in hub.conversations.get(conversation)["turns"] if t["id"] == turn_id)
            break
        await asyncio.sleep(1)
    record = evidence(hub, conversation, turn)
    (output / f"{label}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2))
    await page.screenshot(path=str(output / f"{label}.png"), full_page=True)
    assert turn["status"] == "succeeded", f"{label}: {turn['status']}; see {output / (label + '.json')}"
    await expect(page.locator(f'[data-turn="{turn_id}"] .chat-result-file').first).to_be_visible(timeout=15000)
    await page.screenshot(path=str(output / f"{label}.png"), full_page=True)
    return record


async def main(args):
    if args.verify:
        output = Path(args.verify).resolve()
        hub = Hub(output / "hub.db")
        try:
            record = json.loads((output / "delegation.json").read_text())
            assert_delegation(hub, record)
            verification = {"passed": True, "checks": ["workflow_node_delegates", "correct_report", "concurrent_model_calls"],
                            "source": "Existing browser execution receipts; no additional model calls"}
            (output / "verification.json").write_text(json.dumps(verification, ensure_ascii=False, indent=2))
            print(json.dumps(verification, ensure_ascii=False, indent=2))
        finally:
            await hub.stop()
        return
    output = Path(args.output).resolve() / time.strftime("%Y%m%d-%H%M%S")
    output.mkdir(parents=True)
    workspace = output / "workspace"
    workspace.mkdir()
    hub = Hub(output / "hub.db", poll_seconds=.05)
    summary = {"model": connect(hub, args.database, args.model), "passed": False, "checks": {}}
    await hub.execution.configure({"terminal_enabled": True, "workspace": str(workspace)})
    # Review is optional learning, not part of producing the requested deliverable.
    hub.autonomy.configure({"reflection": False})
    app = create_app(hub, manage_workers=False)
    install_studio(app, hub)
    mount_app(app)
    await hub.start()
    print("evidence:", output, flush=True)
    try:
        async with serve(app) as url, async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                page = await browser.new_page(viewport={"width": 1440, "height": 1050})
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                await page.goto(url + "/#conversations")
                if args.only_delegation:
                    third = await send(page, hub, DELEGATION_REQUEST, args.timeout, "create", output, "delegation", args.model)
                    assert_delegation(hub, third)
                    summary["checks"]["subagents_inside_workflow_node"] = True
                    assert not errors, errors
                    summary["passed"] = True
                    return
                first = await send(page, hub, WORKFLOW_REQUEST, args.timeout, "create", output, "create", args.model)
                assert_totals(hub, first, {"Alice": 19, "Bob": 12.5, "Carol": 9})
                published = [c for c in hub.code.list() if c["status"] == "published"]
                assert published, "Agent did not create/test/publish a new node"
                selected = first["turn"]["task"]["selected"]
                saved = hub.development.get("workflow", selected["id"], selected["revision"])
                steps = saved["workflow"]["steps"]
                assert any(len(s["depends_on"]) >= 2 for s in steps), "Workflow has no parallel-branch join"
                assert sum(not s["depends_on"] for s in steps) >= 2, "Independent input branches were serialized"
                executed = [json.loads(c["output"]) for c in first["calls"] if c["tool"] == "workflows.run" and c["status"] == "succeeded"]
                assert any(r["status"] == "succeeded" and r["revision"] == selected["revision"] for r in executed)
                summary["checks"]["create_node_workflow_parallel_join_artifact"] = True
                # A second browser message reuses the exact graph with held-out data.
                await page.reload()
                second = await send(page, hub, REUSE_REQUEST, args.timeout, selected["key"], output, "reuse")
                assert_totals(hub, second, {"Dana": 12, "Eli": 9.75, "Finn": 7})
                assert len([c for c in hub.code.list() if c["status"] == "published"]) == len(published)
                assert hub.development.get("workflow", selected["id"])["revision"] == selected["revision"]
                summary["checks"]["reuse_with_unseen_data"] = True
                if not args.skip_delegation:
                    await page.locator("#conversations [data-new]").click()
                    third = await send(page, hub, DELEGATION_REQUEST, args.timeout, "create", output, "delegation", args.model)
                    assert_delegation(hub, third)
                    summary["checks"]["subagents_inside_workflow_node"] = True
                assert not errors, errors
                summary["passed"] = True
            finally:
                await browser.close()
    except Exception as exc:
        summary["failure"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
        await hub.stop()
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=".eah/hub.db")
    parser.add_argument("--model", default="gpt")
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--output", default=".eah/live-agent-acceptance")
    parser.add_argument("--skip-delegation", action="store_true")
    parser.add_argument("--only-delegation", action="store_true")
    parser.add_argument("--verify", help="Recheck existing delegation evidence without calling a model")
    asyncio.run(main(parser.parse_args()))
