"""Real connected model writes, tests and publishes a pure tool, then reuse it twice.

Explicit opt-in: uv run python -m examples.demos.live_extension_code --live --model live-gpt
Only synthetic inputs; the sole approval is publication of this demo's tested pure package.
"""

import argparse
import asyncio
import json
from pathlib import Path
import uuid
import httpx


async def main(args):
    if not args.live:
        raise SystemExit("Add --live to use the configured model and publish the synthetic demo tool.")
    root = Path(args.output)
    root.mkdir(parents=True, exist_ok=True)
    namespace = "verify" + uuid.uuid4().hex[:8]
    package_id = namespace + "_total"
    prompt = f"""Write and publish a reusable pure JavaScript extension tool {package_id}.sum.
Use code.create then code.test then code.publish, in that order. If testing fails, repair with a new revision.
The package id is {package_id}, revision 1, runtime javascript, entrypoint extension.js.
The entrypoint must be a global function handle(request) returning {{result: value}}; for lifecycle.* return {{result:{{}}}}.
The sum handler accepts {{items:[numbers]}} and returns {{total:number}}, rounded to 2 decimal places.
Declare strict object input/output JSON Schemas and a read-only tool. No permissions, dependencies, hooks, or external I/O.
Include integration scenarios [12.5,12.5,5] -> {{total:30}}, [] -> {{total:0}}, [0.1,0.2] -> {{total:0.3}}.
Actually call all three tools; your final answer must contain the installed tool name. Do not just describe code."""
    workflow = {
        "name": "真实模型 · 开发可复用计算扩展",
        "limits": {"model_calls": 10, "tool_calls": 12, "output_tokens": 24000},
        "steps": [
            {
                "id": "develop",
                "kind": "agent",
                "target": args.model,
                "timeout_seconds": 240,
                "max_attempts": 1,
                "input": {
                    "prompt": prompt,
                    "tools": ["code.create", "code.test", "code.publish"],
                    "code_development": {"namespace": namespace},
                    "max_turns": 10,
                    "max_output_tokens": 4096,
                },
            }
        ],
    }
    async with httpx.AsyncClient(base_url=args.url, timeout=30) as client:

        async def request(path, body=None):
            response = await (client.get(path) if body is None else client.post(path, json=body))
            response.raise_for_status()
            return response.json()

        identifier = (await request("/v1/runs", workflow))["id"]
        (root / "development-run-id.txt").write_text(identifier)

        async def wait(run_id, allow_publish=False):
            async with asyncio.timeout(300):
                while True:
                    run = await request("/v1/runs/" + run_id)
                    if run["status"] == "waiting_approval":
                        assert allow_publish, run
                        candidates = await request("/v1/code/candidates")
                        for approval in run["approvals"]:
                            assert approval["tool"] == "code.publish"
                            row = next(c for c in candidates if c["id"] == approval["arguments"]["id"])
                            m = row["package"]["manifest"]
                            assert (
                                m["id"] == package_id
                                and m["runtime"] == "javascript"
                                and not m["permissions"]
                            )
                            assert row["status"] == "tested" and all(r["passed"] for r in row["report"])
                            (root / "generated-package.json").write_text(
                                json.dumps(row, ensure_ascii=False, indent=2)
                            )
                            await request("/v1/approvals/" + approval["id"], {"approved": True})
                    if run["status"] in ("succeeded", "failed", "cancelled", "needs_attention"):
                        return run
                    await asyncio.sleep(0.5)

        run = await wait(identifier, True)
        (root / "development-run.json").write_text(json.dumps(run, ensure_ascii=False, indent=2))
        assert run["status"] == "succeeded", run
        proofs = []
        for title, items, expected in [("搬家预算", [12.5, 12.5, 5], 30), ("旅行预算", [0.1, 0.2], 0.3)]:
            flow = {
                "name": title,
                "steps": [{"id": "sum", "target": package_id + ".sum", "input": {"items": items}}],
            }
            completed = await wait((await request("/v1/runs", flow))["id"])
            assert completed["status"] == "succeeded" and completed["steps"][0]["output"] == {
                "total": expected
            }, completed
            proofs.append(completed)
        (root / "reuse-runs.json").write_text(json.dumps(proofs, ensure_ascii=False, indent=2))
        print(
            json.dumps(
                {
                    "real_model": args.model,
                    "development_run": identifier,
                    "published_tool": package_id + ".sum",
                    "reuse_runs": [r["id"] for r in proofs],
                    "passed": True,
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--model", default="live-gpt")
    parser.add_argument("--url", default="http://127.0.0.1:8766")
    parser.add_argument("--output", default=".eah/live-acceptance/extensions-final/code")
    asyncio.run(main(parser.parse_args()))
