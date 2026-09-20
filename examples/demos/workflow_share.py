"""Opt-in live sharing acceptance: exported workflow -> fresh Hub -> local bindings -> execution.

Requires the source Studio's general classification component and live-gpt connection.
Pass --live; this calls a real language model and decision API and may incur charges.
"""

import argparse
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path


from easyagent.api import create_app
from easyagent.client import HubClient
from easyagent.models import HTTPProvider
from easyagent.runtime import Hub
from easyagent.studio import install_studio
from easyagent.workflow_packages import import_workflow


async def main(args):
    if not args.live:
        raise SystemExit("Pass --live to run the real acceptance.")
    for key in ("OPENAI_BASE_URL", "OPENAI_API_KEY", "TYPESAFE_API_KEY"):
        if not os.environ.get(key):
            raise SystemExit("Missing environment variable: " + key)
    out = Path(
        args.output
        or ".eah/live-acceptance/workflow-share-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    out.mkdir(parents=True, exist_ok=False)
    async with HubClient(args.base_url, timeout=120) as client:
        component = await client.instantiate(
            "library.decision.classify_receipt",
            step_id="classify",
            inputs={
                "state": "已购风扇无法启动，需要维修，不咨询购买新产品。",
                "instructions": "区分售后服务和售前购买。",
                "criteria": {"support": "已购产品的退换维修", "sales": "新产品购买咨询"},
                "filename": "classification.json",
            },
        )
        workflow = {
            "name": "完整工作流分享 · 分类与回执（真实验收）",
            "steps": [
                component["step"],
                {
                    "id": "respond",
                    "kind": "agent",
                    "target": "live-gpt",
                    "depends_on": ["classify"],
                    "max_attempts": 1,
                    "timeout_seconds": 120,
                    "input": {
                        "prompt": {"$ref": "classify.results.0.decision.choice"},
                        "tools": ["core.echo"],
                        "max_turns": 4,
                        "max_output_tokens": 800,
                        "instructions": "将输入作为分类结果。如果为 support，调用 core.echo，text 参数必须是 售后需要处理；否则传入 需要重新确认。调用后最终答复必须原样返回工具返回的 text，不添加其他内容。",
                    },
                },
                {
                    "id": "receipt",
                    "kind": "artifact",
                    "depends_on": ["respond"],
                    "input": {"name": "shared-workflow.txt", "content": {"$ref": "respond.text"}},
                },
            ],
        }
        saved = await client.request("POST", "/v1/studio/workflows", workflow)
        package = await client.request("GET", f"/v1/studio/workflows/{saved['id']}/package")
        (out / "workflow.eah-workflow.json").write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding='utf-8')
    os.environ["EAH_SHARED_DECISION_KEY"] = os.environ["TYPESAFE_API_KEY"]
    target = Hub(out / "receiving.db", poll_seconds=0.03)
    target.models.register(
        "local-gpt",
        HTTPProvider(
            os.environ["OPENAI_BASE_URL"], os.environ["OPENAI_API_KEY"], dialect="responses", timeout=120
        ),
        args.model,
        ["chat", "decision"],
    )
    imported = import_workflow(
        target,
        {
            "package": package,
            "model_bindings": {"live-gpt": "local-gpt"},
            "credential_bindings": {"TYPESAFE_API_KEY": "EAH_SHARED_DECISION_KEY"},
        },
    )
    await target.start()
    try:
        run = await target.wait(target.submit(imported["workflow"]), timeout=240)
        (out / "run.json").write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding='utf-8')
        assert run["status"] == "succeeded", run["status"]
        assert run["steps"][0]["output"]["results"][0]["decision"]["choice"] == "support"
        meta, content = target.artifacts.get(run["steps"][-1]["output"]["id"])
        assert content.decode() == "售后需要处理", content.decode()
        (out / meta["name"]).write_bytes(content)
        with target.store.connect() as db:
            calls = [
                dict(r)
                for r in db.execute("SELECT tool,status FROM invocations WHERE run_id=?", (run["id"],))
            ]
        assert any(c["tool"] == "core.echo" and c["status"] == "succeeded" for c in calls)
        report = {
            "source_workflow_id": saved["id"],
            "imported_workflow_id": imported["id"],
            "run_id": run["id"],
            "status": run["status"],
            "tool_calls": calls,
            "package_digest": package["digest"],
            "output": content.decode(),
            "source_model": "live-gpt",
            "receiving_model": "local-gpt",
            "execution_started_on_import": imported["execution_started"],
        }
        (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({"evidence": str(out.resolve()), **report}, ensure_ascii=False, indent=2))
    finally:
        await target.stop()
    if args.serve:
        import uvicorn

        app = create_app(target)
        install_studio(app, target)
        await uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.serve)).serve()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--base-url", default="http://127.0.0.1:8766")
    parser.add_argument("--model", default="gpt-6-astra")
    parser.add_argument("--output")
    parser.add_argument("--serve", type=int)
    asyncio.run(main(parser.parse_args()))
