"""Local encrypted credentials and managed connector delivery; no key values in catalogues."""

import asyncio
import hashlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from typing import Literal
from urllib.parse import quote

from cryptography.fernet import Fernet
import httpx
from pydantic import Field, model_validator

from .contracts import Contract, ToolSpec
from .http_tools import HTTPTool
from .store import Conflict, encode


class SecretWrite(Contract):
    value: str = Field(min_length=1, max_length=32000)


class Connector(Contract):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,40}$")
    title: str = Field(min_length=1, max_length=160)
    kind: Literal["system", "webhook", "telegram", "caldav", "slack", "discord", "feishu"]
    url: str = ""
    device_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    credential: str | None = None
    authorization_prefix: str = "Bearer "
    recipient: str | None = None
    idempotent: bool = False

    @model_validator(mode="after")
    def valid(self):
        if self.kind == "system":
            if not self.device_id:
                raise ValueError("系统通知需要绑定接收浏览器")
            if self.url or self.credential or self.recipient or self.idempotent:
                raise ValueError("系统通知不使用服务地址、访问凭证、收件人或自动重试")
            return self
        if self.device_id:
            raise ValueError("只有系统通知可绑定接收浏览器")
        HTTPTool(name="connector.validate", description="Validate connector URL", url=self.url)
        if self.kind == "telegram" and (
            self.url != "https://api.telegram.org" or not self.credential or not self.recipient
        ):
            raise ValueError("Telegram requires its official API, a credential alias and recipient")
        if any(c in self.authorization_prefix for c in "\r\n"):
            raise ValueError("invalid authorization prefix")
        return self


class Connections:
    def __init__(self, hub):
        self.hub, self.store = hub, hub.store
        self.key_path = Path(self.store.path).with_suffix(".secrets.key")
        self.lock = asyncio.Lock()
        with self.store.connect() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS vault(name TEXT PRIMARY KEY,ciphertext TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS connectors(id TEXT PRIMARY KEY,definition TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS connector_versions(id TEXT NOT NULL,revision INTEGER NOT NULL,definition TEXT NOT NULL,PRIMARY KEY(id,revision));
              CREATE TABLE IF NOT EXISTS deliveries(
                id TEXT PRIMARY KEY,connector TEXT NOT NULL,config TEXT NOT NULL,run_id TEXT NOT NULL,payload TEXT NOT NULL,
                status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,next_at REAL NOT NULL,receipt TEXT,error TEXT);
            """)
        from .system_notifications import SystemNotifications

        self.system = SystemNotifications(self)
        with self.store.transaction() as db:
            db.execute("INSERT OR IGNORE INTO connector_versions SELECT id,1,definition FROM connectors")
            versions = db.execute("SELECT * FROM connector_versions ORDER BY revision").fetchall()
        for row in versions:
            self.register(Connector.model_validate_json(row["definition"]), row["revision"])
        from .models import HTTPProvider

        for row in self.store.memory_search("model-connections", limit=200):
            config = row["value"]
            if row["key"] not in hub.models.bindings:
                hub.models.register(
                    config["alias"],
                    HTTPProvider(
                        config["base_url"],
                        self.secret(config["credential"]) if config.get("credential") else "",
                        config["dialect"],
                    ),
                    config["model"],
                    config["capabilities"],
                )

    def model_config(self, alias):
        with self.store.connect() as db:
            row = db.execute("SELECT value FROM memory WHERE namespace='model-connections' AND key=?", (alias,)).fetchone()
        if not row:
            raise KeyError(alias)
        return json.loads(row[0])

    def default_model(self):
        with self.store.connect() as db:
            row = db.execute("SELECT value FROM memory WHERE namespace='model-settings' AND key='default'").fetchone()
        return json.loads(row[0]) if row else None

    def set_default_model(self, alias):
        from .models import MockProvider
        binding = self.hub.models.bindings.get(alias)
        if alias is not None and (not binding or 'decision' not in binding.capabilities
                                  or isinstance(binding.provider, MockProvider)):
            raise ValueError("请选择支持结构化决策的已连接模型")
        self.store.memory_put('model-settings', 'default', alias, 'operator')

    def model_catalog(self):
        result = []
        for item in self.hub.models.catalog():
            try:
                config = self.model_config(item['alias'])
            except KeyError:
                config = None
            result.append({**item, 'managed': config is not None,
                           **({k: config[k] for k in ('base_url', 'dialect')} if config else {}),
                           'has_key': bool(config and config.get('credential'))})
        return {'connections': result, 'default_model': self.default_model()}

    def assert_model_idle(self, alias):
        prefix = 'media.' + hashlib.sha256(alias.encode()).hexdigest()[:12]
        names = {alias, prefix + '.generations', prefix + '.edits'}
        names.update(row['value']['api'] for row in self.store.memory_search('model-media-bindings', limit=10000)
                     if row['value']['connection'] == alias)

        def uses(value):
            if isinstance(value, str):
                return value in names
            if isinstance(value, dict):
                return any(uses(v) for v in value.values())
            return isinstance(value, list) and any(uses(v) for v in value)

        with self.store.connect() as db:
            for row in db.execute("SELECT spec FROM runs WHERE status NOT IN ('succeeded','failed','cancelled')"):
                if uses(json.loads(row[0])):
                    raise Conflict("这个模型仍被未完成任务使用，请先完成或停止相关任务")
            from .assistant_builder import select_model
            try:
                automatic = select_model(self.hub, 'auto') == alias
            except ValueError:
                automatic = False
            if db.execute("SELECT 1 FROM conversations c JOIN conversation_turns t ON t.conversation=c.id "
                          "WHERE (c.model=? OR (c.model='auto' AND ?)) "
                          "AND t.status IN ('queued','starting','running') LIMIT 1", (alias, automatic)).fetchone():
                raise Conflict("对话仍有待处理消息，请先完成或停止本轮再修改模型连接")

    def retire_model_media(self, alias):
        # Derived adapters have their own encrypted credential. Revoke it as well,
        # so an old workflow cannot keep using a deleted or replaced connection.
        prefix = 'media.' + hashlib.sha256(alias.encode()).hexdigest()[:12]
        with self.store.connect() as db:
            db.execute('DELETE FROM vault WHERE name=?', (prefix,))
        for mode in ('generations', 'edits'):
            name = prefix + '.' + mode
            try:
                self.hub.development.set_archived('api', name, reason='模型连接已修改或删除')
                for version in self.hub.development.list_versions('api', name):
                    self.store.memory_put('disabled-model-adapters', f"{name}@{version['revision']}", True, 'operator')
            except KeyError:
                continue
        for row in self.store.memory_search('model-media-bindings', limit=10000):
            binding = row['value']
            if binding['connection'] != alias:
                continue
            self.store.memory_put('disabled-model-adapters', row['key'], True, 'operator')
            self.hub.development.set_archived('api', binding['api'], reason='模型连接已修改或删除')
            with self.store.connect() as db:
                db.execute('DELETE FROM vault WHERE name=?', (binding['credential'],))

    def save_model(self, body, provider, *, replace=False):
        from .models import ModelBinding
        old = self.model_config(body.alias) if replace else None
        if replace:
            self.assert_model_idle(body.alias)
        elif body.alias in self.hub.models.bindings:
            raise Conflict("这个连接名称已存在")
        config = body.model_dump(exclude={"api_key"})
        if provider.api_key:
            alias = "model." + body.alias
            config["credential"] = alias
            encrypted = self.cipher().encrypt(provider.api_key.encode()).decode()
        with self.store.transaction() as db:
            if provider.api_key:
                db.execute("INSERT OR REPLACE INTO vault VALUES(?,?)", (alias, encrypted))
            elif old and old.get('credential'):
                db.execute('DELETE FROM vault WHERE name=?', (old['credential'],))
            db.execute("INSERT OR REPLACE INTO memory VALUES('model-connections',?,?,?,?)",
                       (body.alias, encode(config), 'operator', time.time()))
        self.hub.models.bindings[body.alias] = ModelBinding(provider, body.model, set(body.capabilities))
        if replace:
            self.retire_model_media(body.alias)
        if self.default_model() == body.alias and 'decision' not in body.capabilities:
            self.set_default_model(None)

    def delete_model(self, alias):
        config = self.model_config(alias)
        self.assert_model_idle(alias)
        self.retire_model_media(alias)
        with self.store.transaction() as db:
            db.execute("DELETE FROM memory WHERE namespace='model-connections' AND key=?", (alias,))
            if config.get('credential'):
                db.execute('DELETE FROM vault WHERE name=?', (config['credential'],))
        self.hub.models.bindings.pop(alias, None)
        if self.default_model() == alias:
            self.set_default_model(None)

    def cipher(self):
        if not self.key_path.exists():
            try:
                fd = os.open(self.key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(Fernet.generate_key())
            except FileExistsError:
                pass
        if self.key_path.is_symlink():
            raise PermissionError("vault key cannot be a symlink")
        return Fernet(self.key_path.read_bytes())

    def put_secret(self, name, value):
        import re

        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,100}", name):
            raise ValueError("invalid credential alias")
        encrypted = self.cipher().encrypt(value.encode()).decode()
        with self.store.connect() as db:
            db.execute("INSERT OR REPLACE INTO vault VALUES(?,?)", (name, encrypted))
        return {"name": name, "configured": True}

    def secret(self, name):
        with self.store.connect() as db:
            row = db.execute("SELECT ciphertext FROM vault WHERE name=?", (name,)).fetchone()
        if not row:
            raise ValueError("connect credential first: " + name)
        return self.cipher().decrypt(row[0].encode()).decode()

    def secrets(self):
        with self.store.connect() as db:
            return [
                {"name": r[0], "configured": True}
                for r in db.execute("SELECT name FROM vault")
                if not r[0].startswith("browser.profile.")
            ]

    def list(self):
        with self.store.connect() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT definition FROM connectors")]

    def save(self, body):
        c = Connector.model_validate(body)
        if c.credential:
            self.secret(c.credential)
        with self.store.transaction() as db:
            old = db.execute(
                "SELECT revision,definition FROM connector_versions WHERE id=? ORDER BY revision DESC LIMIT 1",
                (c.id,),
            ).fetchone()
            revision = (
                old["revision"]
                if old and old["definition"] == c.model_dump_json()
                else (old["revision"] + 1 if old else 1)
            )
            db.execute(
                "INSERT OR IGNORE INTO connector_versions VALUES(?,?,?)",
                (c.id, revision, c.model_dump_json()),
            )
            db.execute("INSERT OR REPLACE INTO connectors VALUES(?,?)", (c.id, c.model_dump_json()))
        self.register(c, revision)
        return c

    def register(self, c, revision):
        async def queue(args, ctx):
            return self.enqueue(c, args, ctx.invocation_id, ctx.run_id)

        fields = (
            {
                "title": {"type": "string", "maxLength": 1000},
                "start": {"type": "string", "format": "date-time"},
                "end": {"type": "string", "format": "date-time"},
                "description": {"type": "string", "maxLength": 8000},
            }
            if c.kind == "caldav"
            else {"text": {"type": "string", "minLength": 1, "maxLength": 4000}}
        )
        if c.kind == "system":
            fields["title"] = {"type": "string", "minLength": 1, "maxLength": 160}
        spec = ToolSpec(
            name="connection." + c.id,
            description=c.title + "；提交到持久投递队列，送达状态单独查询" + (
                "；系统通知发送到绑定浏览器的设备，需允许通知并保持工作室页面打开"
                if c.kind == "system" else ""
            ),
            effect="write",
            idempotent=True,
            input_schema={
                "type": "object",
                "properties": fields,
                "required": ["title", "start", "end"] if c.kind == "caldav" else ["text"],
                "additionalProperties": False,
            },
        )
        self.hub.tools.versions[spec.name, revision] = (spec, queue)
        self.hub.tools.latest[spec.name] = revision
        if spec.name in self.hub.tools.entries:
            self.hub.tools.entries[spec.name] = (spec, queue)
        else:
            self.hub.tools.register(spec, queue)

    def enqueue(self, connector, args, delivery_id, run_id=""):
        with self.store.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO deliveries VALUES(?,?,?,?,?,'queued',0,?,NULL,NULL)",
                (delivery_id, connector.id, connector.model_dump_json(), run_id, encode(args), time.time()),
            )
        return {"delivery_id": delivery_id, "status": "queued", "delivered": False}

    def deliveries(self):
        with self.store.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT id,connector,run_id,status,attempts,receipt,error,"
                    "json_extract(config,'$.kind') AS kind "
                    "FROM deliveries ORDER BY next_at DESC LIMIT 200"
                )
            ]

    async def tick(self):
        if self.lock.locked():
            return
        async with self.lock:
            with self.store.transaction() as db:
                allowed = self.store.scope_run_ids(db, self.hub.run_roots)
                for row in db.execute(
                    "SELECT * FROM deliveries WHERE status='sending' AND next_at<?", (time.time() - 35,)
                ).fetchall():
                    if allowed is not None and row["run_id"] not in allowed:
                        continue
                    c = Connector.model_validate_json(row["config"])
                    db.execute(
                        "UPDATE deliveries SET status=?,error=? WHERE id=?",
                        (
                            "queued" if c.idempotent or c.kind == "caldav" else "uncertain",
                            "浏览器未返回回执，请核验系统通知；不会自动重发" if c.kind == "system" else row["error"],
                            row["id"],
                        ),
                    )
                row = next((r for r in db.execute(
                    "SELECT * FROM deliveries WHERE status='queued' AND next_at<=? "
                    "AND json_extract(config,'$.kind') != 'system' ORDER BY next_at",
                    (time.time(),),
                ) if allowed is None or r["run_id"] in allowed), None)
                if not row:
                    return
                db.execute(
                    "UPDATE deliveries SET status='sending',attempts=attempts+1,next_at=? WHERE id=?",
                    (time.time(), row["id"]),
                )
            c = Connector.model_validate_json(row["config"])
            args = json.loads(row["payload"])
            method, url, headers, body = "POST", c.url, {"Idempotency-Key": row["id"]}, {"json": args}
            status, error, receipt = "delivered", None, None
            try:
                if c.credential:
                    credential = self.secret(c.credential)
                    if c.kind == "telegram":
                        url = c.url + "/bot" + credential + "/sendMessage"
                        body = {"json": {"chat_id": c.recipient, "text": args["text"]}}
                    else:
                        headers["Authorization"] = c.authorization_prefix + credential
                if c.kind == "slack":
                    body = {
                        "json": {"channel": c.recipient, "text": args["text"], "client_msg_id": row["id"]}
                    }
                elif c.kind == "discord":
                    body = {"json": {"content": args["text"], "allowed_mentions": {"parse": []}}}
                elif c.kind == "feishu":
                    body = {"json": {"msg_type": "text", "content": {"text": args["text"]}}}
                if c.kind == "caldav":
                    start = datetime.fromisoformat(args["start"].replace("Z", "+00:00"))
                    end = datetime.fromisoformat(args["end"].replace("Z", "+00:00"))
                    if start.tzinfo is None or end.tzinfo is None or end <= start:
                        raise ValueError("calendar times require zones and end after start")

                    def escaped(value):
                        return (
                            value.replace("\\", "\\\\")
                            .replace("\r", "")
                            .replace("\n", "\\n")
                            .replace(";", "\\;")
                            .replace(",", "\\,")
                        )

                    lines = [
                        "BEGIN:VCALENDAR",
                        "VERSION:2.0",
                        "PRODID:-//EasyAgent//EN",
                        "BEGIN:VEVENT",
                        "UID:" + row["id"] + "@easyagent",
                        "DTSTAMP:" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
                        "DTSTART:" + start.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
                        "DTEND:" + end.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
                        "SUMMARY:" + escaped(args["title"]),
                        "DESCRIPTION:" + escaped(args.get("description", "")),
                        "END:VEVENT",
                        "END:VCALENDAR",
                        "",
                    ]
                    method, url = "PUT", c.url.rstrip("/") + "/" + quote(row["id"]) + ".ics"
                    headers["Content-Type"] = "text/calendar; charset=utf-8"
                    body = {"content": "\r\n".join(lines).encode()}
                async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
                    response = await client.request(method, url, headers=headers, **body)
                    if response.status_code >= 400:
                        status = (
                            "queued"
                            if response.status_code in (429, 503)
                            and (c.idempotent or c.kind == "caldav")
                            and row["attempts"] < 4
                            else "failed"
                        )
                        error = "HTTP " + str(response.status_code)
                    elif 300 <= response.status_code < 400:
                        status, error = "failed", "redirect not accepted"
                    elif c.kind == "telegram" and not response.json().get("ok"):
                        status, error = "failed", "Telegram rejected delivery"
                    elif c.kind == "slack" and not response.json().get("ok"):
                        status, error = "failed", "Slack rejected delivery"
                    elif (
                        c.kind == "feishu"
                        and response.json().get("code", response.json().get("StatusCode", 0)) != 0
                    ):
                        status, error = "failed", "Feishu rejected delivery"
                    else:
                        receipt = encode(
                            {"status_code": response.status_code, "etag": response.headers.get("etag")}
                        )
            except (httpx.TimeoutException, httpx.NetworkError):
                status = (
                    "queued" if (c.idempotent or c.kind == "caldav") and row["attempts"] < 4 else "uncertain"
                )
                error = "network interrupted; check receipt before retrying non-idempotent delivery"
            except Exception as exc:
                status, error = "failed", type(exc).__name__
            with self.store.connect() as db:
                db.execute(
                    "UPDATE deliveries SET status=?,receipt=?,error=?,next_at=? WHERE id=?",
                    (status, receipt, error, time.time() + min(60, 2 ** (row["attempts"] + 1)), row["id"]),
                )


def install_connections(app, hub):
    from .system_notifications import install_system_notifications

    install_system_notifications(app, hub)

    @app.get("/v1/connections/credentials")
    async def secrets():
        return hub.connections.secrets()

    @app.put("/v1/connections/credentials/{name}")
    async def secret(name: str, body: SecretWrite):
        return hub.connections.put_secret(name, body.value)

    @app.get("/v1/connections")
    async def connections():
        return hub.connections.list()

    @app.post("/v1/connections", status_code=201)
    async def save(body: Connector):
        return hub.connections.save(body)

    @app.get("/v1/connections/deliveries")
    async def deliveries():
        return hub.connections.deliveries()
