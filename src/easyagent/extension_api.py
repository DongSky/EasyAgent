from fastapi import Query
from fastapi.responses import HTMLResponse, Response

from .extension_contracts import (
    EVENTS,
    ExtensionActivation,
    ExtensionCall,
    ExtensionInstall,
    ExtensionManifest,
    ExtensionSettings,
    ExtensionStateWrite,
)


def install_extensions(app, hub):
    host = hub.extensions

    @app.get("/v1/extensions/examples/guide")
    async def example():
        import json
        from pathlib import Path
        import examples
        from .extensions import build_package

        root = Path(examples.__file__).parent / "extensions" / "guide"
        return build_package(
            json.loads((root / "manifest.json").read_text()),
            {"extension.js": (root / "extension.js").read_text()},
        )

    @app.get("/v1/extensions/schema")
    async def schema():
        from .extension_contracts import ExtensionRequest, ExtensionResponse, ExtensionPackage

        return {
            "request": ExtensionRequest.model_json_schema(),
            "response": ExtensionResponse.model_json_schema(),
            "package": ExtensionPackage.model_json_schema(),
            "manifest": ExtensionManifest.model_json_schema(),
            "events": list(EVENTS),
            "protocol_version": 1,
        }

    @app.get("/v1/extensions")
    async def catalog():
        return host.catalog()

    @app.post("/v1/extensions/preview")
    async def preview(body: ExtensionInstall):
        return host.preview(body)

    @app.post("/v1/extensions/install", status_code=201)
    async def install(body: ExtensionInstall):
        return await host.install(body)

    @app.post("/v1/extensions/{name}/activate")
    async def activate(name: str, body: ExtensionActivation):
        return await host.activate(name, body.revision)

    @app.post("/v1/extensions/{name}/disable")
    async def disable(name: str):
        return await host.disable(name)

    @app.get("/v1/extensions/{name}/package")
    async def package(name: str, revision: int = Query(ge=1)):
        return Response(
            host.packages[name, revision].model_dump_json(),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="extension.eah-extension.json"'},
        )

    @app.post("/v1/extensions/commands/{name}", status_code=202)
    async def command(name: str, body: ExtensionCall):
        return host.command(name, body.input, body.conversation_id)

    @app.get("/v1/extensions/resources")
    async def resources():
        value = host.resources()
        await host.dispatch("resources.discover", {"resources": value})
        return value

    from .extension_contracts import PublisherTrust

    @app.get("/v1/extensions/publishers")
    async def publishers():
        return host.publishers()

    @app.put("/v1/extensions/publishers/{publisher}")
    async def trust(publisher: str, body: PublisherTrust):
        import base64
        import re
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", publisher):
            raise ValueError("invalid publisher ID")
        Ed25519PublicKey.from_public_bytes(base64.b64decode(body.public_key, validate=True))
        hub.store.memory_put("extension-publishers", publisher, body.public_key, "operator")
        return {"trusted": True, "publisher": publisher}

    @app.get("/v1/extensions/{name}/flags")
    async def flags(name: str):
        m = host.packages[name, host.active[name]].manifest
        return {"value": host.flags(m)}

    @app.put("/v1/extensions/{name}/flags")
    async def set_flags(name: str, body: ExtensionSettings):
        from jsonschema import validate

        m = host.packages[name, host.active[name]].manifest
        validate(body.value, m.flags_schema)
        hub.store.memory_put("extension-flags", f"{name}@{m.revision}", body.value, "operator")
        return {"saved": True}

    @app.get("/v1/extensions/{name}/settings")
    async def get_settings(name: str):
        return {"value": host.settings(name, host.packages[name, host.active[name]].manifest)}

    @app.delete("/v1/extensions/{name}/{revision}")
    async def uninstall(name: str, revision: int):
        return await host.uninstall(name, revision)

    @app.post("/v1/extensions/events/{event}")
    async def emit(event: str, body: ExtensionCall):
        if not event.startswith("custom."):
            raise ValueError("client events must use custom. namespace")
        await host.dispatch(event, body.input)
        return {"delivered": True}

    @app.get("/v1/extensions/{name}/state")
    async def state(name: str):
        if name not in host.active:
            raise KeyError(name)
        return host.state(name)

    @app.put("/v1/extensions/{name}/state")
    async def write_state(name: str, body: ExtensionStateWrite):
        return host.set_state(name, body.revision, body.value)

    @app.get("/v1/extensions/{name}/events")
    async def events(name: str, after: int = Query(default=0, ge=0)):
        return host.events(after, name)

    @app.put("/v1/extensions/{name}/settings")
    async def settings(name: str, body: ExtensionSettings):
        return host.set_settings(name, body.value)

    @app.get("/v1/extensions/{name}/views/{view_id}")
    async def view(name: str, view_id: str):
        p = host.packages[name, host.active[name]]
        v = next((v for v in p.manifest.views if v.id == view_id), None)
        if not v or not v.entrypoint:
            raise KeyError(view_id)
        # Custom UI never gets the Hub token, same-origin privileges or network.
        return HTMLResponse(
            p.files[v.entrypoint],
            headers={
                "Content-Security-Policy": "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; form-action 'none'; base-uri 'none'",
                "X-Content-Type-Options": "nosniff",
            },
        )
