import hashlib
import json
import sys

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from conftest import live_server
from easyagent.extensions import build_package
from easyagent.package_sources import PackageSources
from easyagent.skill_packages import builtin_skills


async def test_backend_memory_context_approval_and_stream_provider(hub):
    source = """function handle(r){if(r.method.startsWith('lifecycle.'))return {result:{}};
    if(r.method==='memory')return {result:{items:[{key:'color',value:'Remember blue',source:'fixture'}]}};
    if(r.method==='context')return {result:{summary:'Retain the goal and confirmed evidence'}};
    if(r.method==='approval')return {result:{receipt:r.params.payload.invocation_id}};
    if(r.method==='stream'){if(!r.params.cursor)return {result:{deltas:[{type:'text_delta',text:'hello'}],done:false,cursor:'next'}};
    return {result:{deltas:[{type:'text_delta',text:' world'}],done:true,result:{text:'hello world',usage:{mock:true}}}};}
    return {result:{}};}"""
    p = build_package(
        {
            "id": "backendtest",
            "revision": 1,
            "title": "Backend integration",
            "backends": [
                {"kind": "memory", "handler": "memory", "operations": ["search"]},
                {"kind": "context", "handler": "context", "operations": ["compact"]},
                {"kind": "approval", "handler": "approval", "operations": ["present"]},
            ],
            "providers": [
                {
                    "alias": "backendtest.model",
                    "model": "test",
                    "capabilities": ["chat"],
                    "handler": "final",
                    "stream_handler": "stream",
                    "cancel_handler": "close",
                }
            ],
        },
        {"extension.js": source},
    )
    await hub.extensions.install({"package": p})
    for kind in ("memory", "context", "approval"):
        hub.backends.select(kind, {"extension": "backendtest", "revision": 1})
    r = await hub.wait(
        hub.submit(
            {
                "name": "stream",
                "steps": [
                    {
                        "id": "a",
                        "kind": "agent",
                        "target": "backendtest.model",
                        "input": {"prompt": "hello", "memory_namespaces": ["profile"], "streaming": True},
                    }
                ],
            }
        )
    )
    assert r["status"] == "succeeded", r
    events = hub.store.events(r["id"])
    assert (
        "".join(e["payload"].get("text", "") for e in events if e["kind"] == "model.delta") == "hello world"
    )
    assert "Remember blue" in json.dumps(r["steps"][0]["state"]["messages"])
    assert (await hub.backends.call("context", "compact", {"messages": []}))["summary"]
    frozen = hub.prepare(
        {
            "name": "memory search",
            "steps": [{"id": "read", "target": "memory.search", "input": {"namespace": "profile"}}],
        }
    )
    hub.backends.reset("memory")
    searched = await hub.wait(hub.submit(frozen))
    assert searched["steps"][0]["output"]["items"][0]["value"] == "Remember blue"
    hub.backends.select("memory", {"extension": "backendtest", "revision": 1})
    with pytest.raises(Exception, match="select another backend"):
        await hub.extensions.disable("backendtest")


async def test_real_terminal_and_browser_with_approval(hub, tmp_path):
    app = FastAPI()

    @app.get("/")
    async def home():
        return HTMLResponse(
            '<title>Browser test</title><label>Name<input id="name"></label><button onclick="document.getElementById(\'result\').textContent=document.getElementById(\'name\').value">Save</button><p id="result"></p>'
        )

    async with live_server(app) as origin:
        await hub.execution.configure(
            {
                "terminal_enabled": True,
                "workspace": str(tmp_path),
                "browser_enabled": True,
                "browser_origins": [origin],
            }
        )

        async def call(kind, operation, payload):
            r = await hub.wait(
                hub.submit(
                    {
                        "name": kind,
                        "steps": [
                            {
                                "id": "do",
                                "target": "backend." + kind,
                                "input": {"operation": operation, "payload": payload},
                            }
                        ],
                    }
                )
            )
            assert r["status"] == "waiting_approval", r
            hub.tools.approve(hub.store, r["approvals"][0]["id"], True)
            return await hub.wait(r["id"])

        run = await call("terminal", "execute", {"argv": [sys.executable, "-c", "print(6*7)"]})
        assert run["status"] == "succeeded" and run["steps"][0]["output"]["stdout"].strip() == "42"
        # One workflow retains browser session across steps, and each action requires approval.
        steps = [
            {
                "id": "open",
                "target": "backend.browser",
                "input": {"operation": "navigate", "payload": {"url": origin}},
            },
            {
                "id": "fill",
                "target": "backend.browser",
                "depends_on": ["open"],
                "input": {"operation": "fill", "payload": {"selector": "#name", "value": "verified"}},
            },
            {
                "id": "click",
                "target": "backend.browser",
                "depends_on": ["fill"],
                "input": {"operation": "click", "payload": {"selector": "button"}},
            },
        ]
        identifier = hub.submit({"name": "browser sequence", "steps": steps})
        for _ in steps:
            r = await hub.wait(identifier)
            assert r["status"] == "waiting_approval", r
            hub.tools.approve(hub.store, r["approvals"][0]["id"], True)
        r = await hub.wait(identifier)
        assert r["status"] == "succeeded", r
        assert "verified" in r["steps"][-1]["output"]["text"]
        job = {"run_id": identifier, "id": "snapshot"}
        await hub.execution.sessions[identifier][1].evaluate("localStorage.setItem('persisted', 'yes')")
        await hub.execution.browser("snapshot", {}, job)
        await hub.execution.close()
        restored = await hub.execution.browser("snapshot", {}, job)
        assert restored["title"] == "Browser test"
        assert (
            await hub.execution.sessions[identifier][1].evaluate("localStorage.getItem('persisted')") == "yes"
        )
        assert not any(s["name"].startswith("browser.profile.") for s in hub.connections.secrets())
        await hub.execution.browser("close", {}, job)
        with pytest.raises(ValueError):
            hub.connections.secret("browser.profile." + identifier)


async def test_catalog_digest_and_real_http_import(hub):
    p = builtin_skills()[0]["package"]
    raw = json.dumps(p).encode()
    app = FastAPI()
    from fastapi.responses import Response

    @app.get("/package")
    async def package():
        return Response(raw, media_type="application/json")

    async with live_server(app) as origin:

        @app.get("/catalog")
        async def catalog():
            return {
                "items": [
                    {
                        "id": "moving",
                        "kind": "skill",
                        "title": "Moving",
                        "url": origin + "/package",
                        "sha256": hashlib.sha256(raw).hexdigest(),
                    }
                ]
            }

        source = PackageSources(hub)
        source.save({"id": "local", "title": "fixture", "url": origin + "/catalog"})
        await source.catalog("local")
        preview = await source.preview("local", "moving")
        hub.skill_packages.install({"package": preview["package"]})
        assert hub.skills.load("moving-checklist")
        with pytest.raises(KeyError):
            await source.catalog("missing")
        with pytest.raises(KeyError):
            await source.preview("local", "missing")


async def test_voice_http_upload_binary_output_and_channel_receipts(hub):
    from fastapi import Request, Response

    app = FastAPI()
    sent = []

    @app.post("/audio/transcriptions")
    async def transcribe(request: Request):
        raw = await request.body()
        assert b"RIFF" in raw
        return {"text": "three boxes"}

    @app.post("/audio/speech")
    async def speech(request: Request):
        assert (await request.json())["input"] == "hello"
        return Response(b"ID3test-audio", media_type="audio/mpeg")

    @app.post("/slack")
    async def slack(request: Request):
        sent.append(await request.json())
        return {"ok": True, "ts": "fixture"}

    @app.post("/feishu")
    async def feishu(request: Request):
        sent.append(await request.json())
        return {"code": 0}

    async with live_server(app) as origin:
        hub.voice.save({"base_url": origin})
        artifact = hub.artifacts.put("voice.wav", b"RIFFtest", "audio/wav")
        frozen = hub.prepare(
            {
                "name": "voice",
                "steps": [
                    {"id": "stt", "target": "voice.transcribe", "input": {"artifact": artifact["id"]}},
                    {"id": "tts", "target": "voice.speak", "input": {"text": "hello"}},
                ],
            }
        )
        hub.voice.save({"base_url": "http://127.0.0.1:1"})
        r = await hub.wait(hub.submit(frozen))
        assert r["status"] == "succeeded", r
        assert next(s for s in r["steps"] if s["id"] == "stt")["output"]["text"] == "three boxes"
        out = next(s for s in r["steps"] if s["id"] == "tts")["output"]["artifact"]
        assert hub.artifacts.get(out["id"])[1] == b"ID3test-audio"
        for kind in ("slack", "feishu"):
            hub.connections.save(
                {"id": kind, "title": kind, "kind": kind, "url": origin + "/" + kind, "recipient": "test"}
            )
            run = await hub.wait(
                hub.submit(
                    {
                        "name": "channel",
                        "steps": [{"id": "send", "target": "connection." + kind, "input": {"text": "hello"}}],
                    }
                )
            )
            hub.tools.approve(hub.store, run["approvals"][0]["id"], True)
            run = await hub.wait(run["id"])
            assert run["status"] == "succeeded"
        import asyncio

        for _ in range(100):
            if len(sent) == 2:
                break
            await asyncio.sleep(0.05)
        assert sent[0]["text"] == "hello" and sent[1]["content"]["text"] == "hello"


async def test_legacy_signed_extension_survives_restart_and_workflow_share(hub, tmp_path):
    import base64
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from easyagent.components import digest
    from easyagent.runtime import Hub
    from easyagent.workflow_packages import export_workflow, import_workflow

    raw = build_package(
        {
            "id": "legacy",
            "revision": 1,
            "title": "Legacy",
            "tools": [{"handler": "echo", "spec": {"name": "legacy.echo"}}],
            "providers": [
                {"alias": "legacy.model", "model": "fixture", "handler": "model", "capabilities": ["chat"]}
            ],
        },
        {"extension.js": "function handle(r){return {result:r.params};}"},
    ).model_dump()
    # Reproduce the exact pre-backend/pre-stream v1 artifact and sign those bytes.
    for key in ("backends", "service_versions"):
        raw["manifest"].pop(key, None)
    for provider in raw["manifest"]["providers"]:
        provider.pop("stream_handler", None)
        provider.pop("cancel_handler", None)
    key = Ed25519PrivateKey.generate()
    raw["publisher"] = "legacy-publisher"
    raw["digest"] = digest({k: v for k, v in raw.items() if k not in ("digest", "signature")})
    raw["signature"] = base64.b64encode(key.sign(raw["digest"].encode())).decode()
    trust = base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    hub.store.memory_put("extension-publishers", "legacy-publisher", trust, "operator")
    await hub.extensions.install({"package": raw})
    await hub.stop()
    restarted = Hub(hub.store.path, poll_seconds=0.01)
    fresh = Hub(tmp_path / "recipient.db", poll_seconds=0.01)
    await restarted.start()
    await fresh.start()
    try:
        package = export_workflow(
            restarted,
            {
                "name": "Legacy workflow",
                "steps": [{"id": "echo", "target": "legacy.echo", "input": {"value": 42}}],
            },
        )
        bundled = package.requirements["extension_packages"][0]
        assert bundled["digest"] == raw["digest"] and bundled["signature"] == raw["signature"]
        fresh.store.memory_put("extension-publishers", "legacy-publisher", trust, "operator")
        await fresh.extensions.install({"package": bundled})
        saved = import_workflow(fresh, {"package": package.model_dump()})
        result = await fresh.wait(fresh.submit(saved["workflow"]))
        assert result["status"] == "succeeded", result
        assert result["steps"][0]["output"] == {"value": 42}
    finally:
        await restarted.stop()
        await fresh.stop()


async def test_compaction_backend_failure_falls_back_to_deletion(hub):
    """A broken summariser must never be worse than not installing one (fail-open)."""
    from easyagent.contracts import ModelResult, ToolCall, ToolSpec

    source = """function handle(r){if(r.method.startsWith('lifecycle.'))return {result:{}};
    if(r.method==='context')throw new Error('summariser unavailable');
    return {result:{}};}"""
    package = build_package(
        {
            "id": "ctxfail",
            "revision": 1,
            "title": "Failing context engine",
            "backends": [{"kind": "context", "handler": "context", "operations": ["compact"]}],
        },
        {"extension.js": source},
    )
    await hub.extensions.install({"package": package})
    hub.backends.select("context", {"extension": "ctxfail", "revision": 1})

    async def long_result(args, ctx):
        return {"number": args["number"], "text": "x" * 1100}

    hub.tools.register(ToolSpec(name="fixture.long"), long_result)

    class Model:
        async def generate(self, request, model):
            observed = [json.loads(m["content"])["number"] for m in request.messages if m["role"] == "tool"]
            number = max(observed, default=0) + 1
            if number > 5:
                return ModelResult(text="finished without a summary")
            return ModelResult(tool_calls=[ToolCall(id=str(number), name="fixture.long", arguments={"number": number})])

    hub.models.register("planner", Model(), "fixture", {"chat"})
    run = await hub.wait(
        hub.submit(
            {
                "name": "failing summariser",
                "steps": [
                    {
                        "id": "a",
                        "kind": "agent",
                        "target": "planner",
                        "input": {"prompt": "Keep original request", "tools": ["fixture.long"], "context_chars": 4000},
                    }
                ],
            }
        )
    )
    assert run["status"] == "succeeded", run
    kinds = {e["kind"] for e in hub.store.events(run["id"])}
    assert "context.compaction_failed" in kinds
    assert "context.compacted" in kinds
    # The instruction survives even though the summariser never produced anything.
    assert "Keep original request" in json.dumps(run["steps"][0]["state"]["messages"])
