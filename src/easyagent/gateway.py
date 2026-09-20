"""Signed incoming channel messages are mapped to persistent conversations."""

import hashlib
import hmac
import time
from pydantic import Field
from .contracts import AgentConfig, Contract
from .store import Conflict, encode


class GatewayProfile(Contract):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,40}$")
    credential: str
    model: str
    agent: AgentConfig = Field(default_factory=lambda: AgentConfig(prompt=""))
    reply_connector: str | None = None


class InboundMessage(Contract):
    event_id: str = Field(min_length=1, max_length=160)
    thread: str = Field(min_length=1, max_length=160)
    text: str = Field(min_length=1, max_length=64000)


class Gateway:
    def __init__(self, hub):
        self.hub = hub
        with hub.store.connect() as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS gateway_threads(channel TEXT NOT NULL,thread TEXT NOT NULL,conversation TEXT NOT NULL,PRIMARY KEY(channel,thread));
            CREATE TABLE IF NOT EXISTS gateway_events(channel TEXT NOT NULL,event_id TEXT NOT NULL,digest TEXT NOT NULL,conversation TEXT NOT NULL,PRIMARY KEY(channel,event_id));
            CREATE TABLE IF NOT EXISTS gateway_replies(run_id TEXT PRIMARY KEY,delivery_run TEXT NOT NULL);""")

    def list(self):
        return [r["value"] for r in self.hub.store.memory_search("gateway-profiles", limit=100)]

    def save(self, body):
        p = GatewayProfile.model_validate(body)
        self.hub.connections.secret(p.credential)
        self.hub.prepare(
            {
                "name": "gateway",
                "steps": [{"id": "agent", "kind": "agent", "target": p.model, "input": p.agent.model_dump()}],
            }
        )
        if p.reply_connector:
            self.hub.tools.spec("connection." + p.reply_connector)
        self.hub.store.memory_put("gateway-profiles", p.id, p.model_dump(), "operator")
        return p

    async def accept(self, identifier, raw, timestamp, signature):
        raw_profile = next((p for p in self.list() if p["id"] == identifier), None)
        if raw_profile is None:
            raise KeyError(identifier)
        profile = GatewayProfile.model_validate(raw_profile)
        if abs(time.time() - int(timestamp)) > 300:
            raise PermissionError("incoming message timestamp expired")
        expected = hmac.new(
            self.hub.connections.secret(profile.credential).encode(),
            timestamp.encode() + b"." + raw,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise PermissionError("invalid channel signature")
        message = InboundMessage.model_validate_json(raw)
        checksum = hashlib.sha256(raw).hexdigest()
        # Deterministic conversation ID avoids a cross-process create/link race.
        thread = hashlib.sha256((identifier + ":" + message.thread).encode()).hexdigest()
        conversation = "channel-" + thread
        prepared = self.hub.prepare(
            {
                "name": "incoming",
                "steps": [
                    {
                        "id": "agent",
                        "kind": "agent",
                        "target": profile.model,
                        "input": profile.agent.model_dump(),
                    }
                ],
            }
        )
        with self.hub.store.transaction() as db:
            old = db.execute(
                "SELECT * FROM gateway_events WHERE channel=? AND event_id=?", (identifier, message.event_id)
            ).fetchone()
            if old and old["digest"] != checksum:
                raise Conflict("event ID reused with different message")
            db.execute(
                "INSERT OR IGNORE INTO conversations VALUES(?,?,?,?,NULL,NULL,NULL,0,?)",
                (
                    conversation,
                    identifier + " · " + message.thread[:80],
                    profile.model,
                    encode(prepared.steps[0].input),
                    time.time(),
                ),
            )
            db.execute(
                "INSERT OR IGNORE INTO gateway_threads VALUES(?,?,?)", (identifier, thread, conversation)
            )
            db.execute(
                "INSERT OR IGNORE INTO gateway_events VALUES(?,?,?,?)",
                (identifier, message.event_id, checksum, conversation),
            )
        turn = await self.hub.conversations.send(
            conversation, {"text": message.text, "idempotency_key": "channel:" + message.event_id}
        )
        return {
            "conversation": conversation,
            "turn": turn["id"],
            "run_id": turn.get("run_id"),
            "duplicate": bool(old),
        }

    def replies(self):
        for profile in self.list():
            if not profile.get("reply_connector"):
                continue
            with self.hub.store.connect() as db:
                rows = db.execute(
                    "SELECT t.run_id,m.content FROM gateway_threads g JOIN conversation_turns t ON t.conversation=g.conversation JOIN conversation_messages m ON m.turn_id=t.id AND m.role='assistant' LEFT JOIN gateway_replies r ON r.run_id=t.run_id WHERE g.channel=? AND t.status='succeeded' AND r.run_id IS NULL",
                    (profile["id"],),
                ).fetchall()
            for row in rows:
                # The connector is a normal write tool: replies wait for confirmation before delivery.
                run = self.hub.submit(
                    {
                        "name": "渠道回复 · " + profile["id"],
                        "steps": [
                            {
                                "id": "reply",
                                "target": "connection." + profile["reply_connector"],
                                "input": {"text": row["content"][:4000]},
                            }
                        ],
                    },
                    "channel-reply:" + row["run_id"],
                )
                with self.hub.store.connect() as db:
                    db.execute("INSERT OR IGNORE INTO gateway_replies VALUES(?,?)", (row["run_id"], run))


def install_gateway(app, hub):
    from fastapi import Request
    @app.get("/v1/gateway/connections")
    async def listing():
        return hub.gateway.list()

    @app.post("/v1/gateway/connections", status_code=201)
    async def save(body: GatewayProfile):
        return hub.gateway.save(body)

    @app.post("/v1/gateway/incoming/{identifier}", status_code=202)
    async def incoming(identifier: str, request: Request):
        return await hub.gateway.accept(
            identifier,
            await request.body(),
            request.headers.get("x-eah-timestamp", "0"),
            request.headers.get("x-eah-signature", ""),
        )
