"""Audio input and spoken replies through configured transcription/speech HTTP APIs."""

import base64

import httpx
from pydantic import Field

from .contracts import Contract, ToolSpec
from .http_tools import HTTPTool


class VoiceSettings(Contract):
    base_url: str
    credential: str | None = None
    transcription_model: str = "whisper-1"
    speech_model: str = "tts-1"
    voice: str = "alloy"


class Voice:
    def __init__(self, hub):
        self.hub = hub
        self.register()
        hub.backends.local["media"] = self.media

    def settings(self):
        rows = self.hub.store.memory_search("voice-settings", limit=1)
        return rows[0]["value"] if rows else None

    def save(self, body):
        s = VoiceSettings.model_validate(body)
        HTTPTool(name="voice.validate", description="Voice endpoint", url=s.base_url)
        if s.credential:
            self.hub.connections.secret(s.credential)
        self.hub.store.memory_put("voice-settings", "default", s.model_dump(), "operator")
        return s.model_dump()

    async def media(self, operation, payload, job):
        s = (
            self.hub.store.run(job["run_id"])["spec"].get("metadata", {}).get("voice_settings")
            if job
            else self.settings()
        )
        if not s:
            raise ValueError("connect transcription and speech models first")
        settings = VoiceSettings.model_validate(s)
        headers = {}
        if settings.credential:
            headers["Authorization"] = "Bearer " + self.hub.connections.secret(settings.credential)
        if operation == "transcribe":
            info, data = self.hub.artifacts.get(payload["artifact"])
            if not info["media_type"].startswith("audio/") or len(data) > 1_000_000:
                raise ValueError("audio must be under 1 MB")
            kwargs = {
                "files": {"file": (info["name"], data, info["media_type"])},
                "data": {"model": settings.transcription_model},
            }
            path = "/audio/transcriptions"
        elif operation == "speak":
            text = payload.get("text", "")
            if not isinstance(text, str) or not 1 <= len(text) <= 12000:
                raise ValueError("speech text must have 1–12000 characters")
            kwargs = {"json": {"model": settings.speech_model, "voice": settings.voice, "input": text}}
            path = "/audio/speech"
        else:
            raise ValueError("use the connected model/API nodes for other media operations")
        async with httpx.AsyncClient(timeout=90, follow_redirects=False) as client:
            async with client.stream(
                "POST", settings.base_url.rstrip("/") + path, headers=headers, **kwargs
            ) as r:
                r.raise_for_status()
                chunks = []
                size = 0
                async for chunk in r.aiter_bytes():
                    size += len(chunk)
                    if size > 10_000_000:
                        raise ValueError("voice response exceeds limit")
                    chunks.append(chunk)
                data = b"".join(chunks)
                if operation == "transcribe":
                    import json

                    text = json.loads(data).get("text")
                    if not isinstance(text, str):
                        raise ValueError("transcription response missing text")
                    return {"text": text}
                mime = r.headers.get("content-type", "audio/mpeg").split(";")[0]
                if not mime.startswith("audio/"):
                    raise ValueError("speech response is not audio")
        return {"artifact": self.hub.artifacts.put("reply.mp3", data, mime, job["run_id"] if job else None)}

    def register(self):
        for operation, key in [("transcribe", "artifact"), ("speak", "text")]:

            async def handler(args, ctx, operation=operation):
                return await self.hub.backends.call("media", operation, args, ctx.job)

            self.hub.tools.register(
                ToolSpec(
                    name="voice." + operation,
                    description="Use configured voice service to " + operation,
                    input_schema={
                        "type": "object",
                        "properties": {key: {"type": "string"}},
                        "required": [key],
                        "additionalProperties": False,
                    },
                ),
                handler,
            )


def install_voice(app, hub):
    @app.get("/v1/voice/settings")
    async def get():
        return hub.voice.settings()

    @app.put("/v1/voice/settings")
    async def put(body: VoiceSettings):
        return hub.voice.save(body)

    class Audio(Contract):
        data: str = Field(max_length=1_400_000)
        media_type: str = Field(default="audio/webm", pattern=r"^audio/[a-z0-9.+-]+$")

    @app.post("/v1/voice/input")
    async def transcribe(body: Audio):
        data = base64.b64decode(body.data, validate=True)
        if len(data) > 1_000_000:
            raise ValueError("audio exceeds 1 MB")
        audio = hub.artifacts.put("recording." + body.media_type.split("/")[1], data, body.media_type)
        return {
            "id": hub.submit(
                {
                    "name": "语音转文字",
                    "steps": [
                        {
                            "id": "transcribe",
                            "target": "voice.transcribe",
                            "timeout_seconds": 100,
                            "input": {"artifact": audio["id"]},
                        }
                    ],
                }
            )
        }

    class Speech(Contract):
        text: str = Field(min_length=1, max_length=12000)

    @app.post("/v1/voice/output")
    async def speak(body: Speech):
        return {
            "id": hub.submit(
                {
                    "name": "朗读回复",
                    "steps": [
                        {
                            "id": "speak",
                            "target": "voice.speak",
                            "timeout_seconds": 100,
                            "input": body.model_dump(),
                        }
                    ],
                }
            )
        }
