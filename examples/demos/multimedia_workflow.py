"""Real image → video acceptance using Studio APIs and durable workflow nodes.

Credentials stay in the Hub process. Existing run IDs are resumed, never resubmitted.
This example submits billable generation only with --approve-generation.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import httpx

IMAGE_MODEL = "gpt-image-2.5-flare"
VIDEO_MODEL = "doubao-seedance-2-5-260628"


def value_at(value, path):
    for part in path.split('.'):
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


class Acceptance:
    def __init__(self, client, output, approve):
        self.client, self.output, self.approve = client, output, approve
        output.mkdir(parents=True, exist_ok=True)

    def save(self, name, value):
        (self.output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2))

    async def post(self, path, body):
        response = await self.client.post(path, json=body)
        response.raise_for_status()
        return response.json()

    async def run(self, name, workflow):
        path = self.output / (name + "-id.json")
        if path.exists():
            identifier = json.loads(path.read_text())["id"]
        else:
            self.save(name + "-workflow.json", workflow)
            await self.post("/v1/studio/workflows", workflow)
            created = await self.post("/v1/runs", workflow)
            self.save(path.name, created)
            identifier = created["id"]
        allowed = {s.get("target") for s in workflow["steps"]}
        previous = None
        async with asyncio.timeout(1900):
            while True:
                response = await self.client.get("/v1/runs/" + identifier)
                response.raise_for_status()
                run = response.json()
                self.save(name + "-run.json", run)
                if run["status"] != previous:
                    print(name, identifier, run["status"], flush=True)
                    previous = run["status"]
                if run["status"] == "succeeded":
                    return run
                if run["status"] == "waiting_approval" and self.approve:
                    for approval in run["approvals"]:
                        if approval["status"] == "approval" and approval["tool"] in allowed:
                            await self.post("/v1/approvals/" + approval["id"], {"approved": True})
                elif run["status"] in ("failed", "cancelled", "needs_attention", "waiting_input", "waiting_approval"):
                    events = await self.client.get("/v1/runs/" + identifier + "/events")
                    self.save(name + "-events.json", events.json())
                    raise RuntimeError(f"{name}: {run['status']}; inspect saved events. No automatic resubmission.")
                await asyncio.sleep(3)

    async def node(self, operation, model=None, defaults=None, polling=None):
        return await self.post("/v1/studio/model-catalog/nodes", {
            "operation_id": operation, "model": model, "defaults": defaults or {}, "polling": polling})


async def main(args):
    async with httpx.AsyncClient(base_url=args.hub, timeout=60,
                                 headers={"Authorization": "Bearer " + args.hub_token} if args.hub_token else {}) as client:
        demo = Acceptance(client, Path(args.output), args.approve_generation)
        await demo.post("/v1/studio/model-catalog/discover", {
            "base_url": args.base_url, "api_key_env": "OPENAI_API_KEY"})
        reference = Path(args.reference)
        mime = "image/png" if reference.suffix.lower() == ".png" else "image/jpeg"
        response = await client.post("/v1/artifacts/upload", params={"name": reference.name},
                                     headers={"Content-Type": mime}, content=reference.read_bytes())
        response.raise_for_status()
        image = await demo.node("v1.post.v1_images_edits", IMAGE_MODEL,
                                {"body": {"model": IMAGE_MODEL, "n": 1, "size": "1024x1024", "quality": "medium"}})
        draw = image["step"] | {"id": "draw", "max_attempts": 1}
        draw["input"]["body"].update(image=response.json()["id"], prompt=Path(args.image_prompt).read_text())
        image_flow = {"name": "参考图生成 Q 版表情", "steps": [draw]}
        result = await demo.run("image", image_flow)
        artifact = result["steps"][0]["output"]["artifacts"][0]
        response = await client.get("/v1/artifacts/" + artifact["id"] + "/content")
        response.raise_for_status()
        (demo.output / "expression.png").write_bytes(response.content)

        reference, upload = artifact['id'], None
        if getattr(args, 'video_reference_mode', 'inline') == 'url-upload':
            upload_node = await demo.node(args.video_upload_operation)
            upload = upload_node['step'] | {'id': 'upload_image', 'max_attempts': 1,
                'input': {'body': {args.video_upload_file_field: artifact['id']}}}
            uploaded = await demo.run('reference_upload', {'name': '上传视频参考图', 'steps': [upload]})
            reference = value_at(uploaded['steps'][0]['output'], args.video_upload_url_path)
            if not isinstance(reference, str) or not reference.startswith('https://'):
                raise ValueError('Image upload did not return an HTTPS reference URL at the configured path')

        video = await demo.node("seedance.post.api_v3_contents_generations_tasks", VIDEO_MODEL)
        animate = video["step"] | {"id": "animate", "max_attempts": 1}
        animate["input"] = {"body": {"model": VIDEO_MODEL, "content": [
            {"type": "text", "text": Path(args.video_prompt).read_text()},
            {"type": "image_url", "image_url": {"url": {"$ref": "$input.image"}}, "role": "first_frame"}],
            # Seedance 2.5 first-frame generation requires adaptive; it follows the input image ratio.
            "duration": 4, "ratio": "adaptive", "generate_audio": False}}
        video_flow = {"name": "表情图生成 4 秒动画", "inputs": {"image": reference}, "steps": [animate]}
        result = await demo.run("video", video_flow)
        receipt = result["steps"][0]["output"]["result"]
        if receipt.get("id"):
            task_id, ref = receipt["id"], "animate.result.id"
        elif receipt.get("data", {}).get("task_id"):
            task_id, ref = receipt["data"]["task_id"], "animate.result.data.task_id"
        else:
            raise ValueError("Submission returned no documented task ID; inspect receipt before polling")
        waiter = await demo.node("volc.get.api_v3_contents_generations_tasks_id", polling={
            "status_path": "status", "pending": ["pending", "queued", "running"], "succeeded": ["succeeded"],
            "failed": ["failed", "cancelled", "expired"], "interval_seconds": 10,
            "max_polls": 180, "deadline_seconds": 1800})
        wait = waiter["step"] | {"id": "wait", "input": {"id": task_id}}
        result = await demo.run("poll", {"name": "等待动画完成", "steps": [wait]})
        video_url = result["steps"][0]["output"]["content"]["video_url"]
        # Pin the actual receipt URL in a read-only node, with no generation credentials attached.
        definition = {"name": "demo.media.download_video", "description": "Download the generated video receipt",
            "url": video_url, "response_mode": "artifact", "artifact_name": "animation.mp4",
            "artifact_media_types": ["video/mp4"], "max_response_bytes": 10_000_000, "timeout_seconds": 120}
        existing = await client.get("/v1/studio/apis/" + definition["name"])
        if existing.status_code == 200:
            saved = await client.put("/v1/studio/apis/" + definition["name"],
                params={"expected_revision": existing.json()["revision"]}, json=definition)
            saved.raise_for_status()
            saved = saved.json()
        else:
            existing.raise_for_status() if existing.status_code != 404 else None
            saved = await demo.post("/v1/studio/apis", definition)
        result = await demo.run("download", {"name": "保存生成的动画", "steps": [{
            "id": "download", "target": definition["name"], "tool_revision": saved["revision"], "timeout_seconds": 130}]})
        artifact = result["steps"][0]["output"]
        response = await client.get("/v1/artifacts/" + artifact["id"] + "/content")
        response.raise_for_status()
        (demo.output / "animation.mp4").write_bytes(response.content)
        # Export the data links between generation stages, using the actual observed receipt shape.
        animate["depends_on"] = ["draw"]
        animate["input"]["body"]["content"][1]["image_url"]["url"] = {"$ref": "draw.artifacts.0.id"}
        if upload:
            upload['depends_on'] = ['draw']
            upload['input']['body'][args.video_upload_file_field] = {'$ref': 'draw.artifacts.0.id'}
            animate['depends_on'] = ['upload_image']
            animate['input']['body']['content'][1]['image_url']['url'] = {
                '$ref': 'upload_image.' + args.video_upload_url_path}
        wait.update(depends_on=["animate"], input={"id": {"$ref": ref}})
        workflow = {"name": "参考图 → 表情 → 动画 → 等待结果", "steps": [draw, *([upload] if upload else []), animate, wait]}
        demo.save("complete-workflow.json", workflow)
        await demo.post("/v1/studio/workflows", workflow)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hub", default="http://127.0.0.1:8766")
    parser.add_argument("--hub-token", default="")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--image-prompt", required=True)
    parser.add_argument("--video-prompt", required=True)
    parser.add_argument("--output", default=".eah/multimedia-demo")
    parser.add_argument('--video-reference-mode', choices=['inline', 'url-upload'], default='inline',
                        help='Use inline image bytes, or upload an image when the service requires an HTTPS URL')
    parser.add_argument('--video-upload-operation', default='pixverse.post.openapi_v2_image_upload')
    parser.add_argument('--video-upload-file-field', default='image')
    parser.add_argument('--video-upload-url-path', default='result.Resp.img_url')
    parser.add_argument("--approve-generation", action="store_true")
    asyncio.run(main(parser.parse_args()))
