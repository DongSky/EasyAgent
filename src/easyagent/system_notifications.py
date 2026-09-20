"""Browser-bound OS notification outbox. Acceptance is not proof of display or reading."""

import json
import secrets
import time
import uuid
from typing import Literal

from pydantic import Field

from .contracts import Contract
from .store import Conflict, encode


class NotificationDevice(Contract):
    device_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class NotificationReceipt(NotificationDevice):
    claim_token: str = Field(pattern=r"^[a-f0-9]{32}$")
    outcome: Literal["submitted", "failed"]
    reason: Literal["permission_denied", "unsupported", "notification_error"] | None = None


class SystemNotifications:
    def __init__(self, connections):
        self.connections, self.store = connections, connections.store
        with self.store.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS system_notification_claims("
                "delivery_id TEXT PRIMARY KEY REFERENCES deliveries(id),"
                "device_id TEXT NOT NULL,token TEXT NOT NULL)"
            )

    def claim(self, device_id):
        # SQLite transaction fences all tabs/processes; a lost claim is never silently replayed.
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM deliveries WHERE status='queued' AND next_at<=? "
                "AND json_extract(config,'$.kind')='system' "
                "AND json_extract(config,'$.device_id')=? ORDER BY next_at LIMIT 1",
                (time.time(), device_id),
            ).fetchone()
            if not row:
                return {"notification": None}
            token = uuid.uuid4().hex
            db.execute(
                "INSERT INTO system_notification_claims VALUES(?,?,?)", (row["id"], device_id, token)
            )
            db.execute(
                "UPDATE deliveries SET status='sending',attempts=attempts+1,next_at=? WHERE id=?",
                (time.time(), row["id"]),
            )
        config, payload = json.loads(row["config"]), json.loads(row["payload"])
        return {
            "notification": {
                "id": row["id"],
                "claim_token": token,
                "title": payload.get("title") or config["title"],
                "text": payload["text"],
                "run_id": row["run_id"],
            }
        }

    def acknowledge(self, delivery_id, body):
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT c.*,d.status,d.receipt FROM system_notification_claims c "
                "JOIN deliveries d ON d.id=c.delivery_id WHERE delivery_id=?", (delivery_id,)
            ).fetchone()
            if not row:
                raise KeyError(delivery_id)
            if row["device_id"] != body.device_id or not secrets.compare_digest(row["token"], body.claim_token):
                raise Conflict("通知回执与接收浏览器不匹配")
            receipt = {
                "transport": "web_notification", "outcome": body.outcome,
                "display_confirmed": False, "read_confirmed": False,
            }
            error = None
            if body.outcome == "failed":
                error = {
                    "permission_denied": "通知权限未获允许，请在浏览器设置中开启",
                    "unsupported": "当前浏览器不支持系统通知",
                    "notification_error": "浏览器未能提交系统通知",
                }[body.reason or "notification_error"]
            elif body.reason:
                raise ValueError("成功提交的通知不能带失败原因")
            if row["status"] in ("submitted", "failed"):
                if json.loads(row["receipt"]) != receipt:
                    raise Conflict("通知回执已确认，不能覆盖")
            elif row["status"] in ("sending", "uncertain"):
                db.execute(
                    "UPDATE deliveries SET status=?,receipt=?,error=? WHERE id=?",
                    (body.outcome, encode(receipt), error, delivery_id),
                )
            else:
                raise Conflict("通知尚未领取")
        return {"id": delivery_id, "status": body.outcome, "receipt": receipt}

    def test(self, connector_id, device_id):
        from .connections import Connector

        with self.store.connect() as db:
            row = db.execute("SELECT definition FROM connectors WHERE id=?", (connector_id,)).fetchone()
        if not row:
            raise KeyError(connector_id)
        c = Connector.model_validate_json(row[0])
        if c.kind != "system" or c.device_id != device_id:
            raise Conflict("请先将系统通知保存到当前浏览器")
        return self.connections.enqueue(
            c,
            {"title": "EasyAgent · 通知测试", "text": "系统通知已接入，你的助手可以通过这里发送提醒。"},
            "test_" + uuid.uuid4().hex,
        )


def install_system_notifications(app, hub):
    @app.post("/v1/connections/system/claim")
    async def claim(body: NotificationDevice):
        return hub.connections.system.claim(body.device_id)

    @app.post("/v1/connections/system/{delivery_id}/receipt")
    async def acknowledge(delivery_id: str, body: NotificationReceipt):
        return hub.connections.system.acknowledge(delivery_id, body)

    @app.post("/v1/connections/{connector_id}/test-system", status_code=201)
    async def test(connector_id: str, body: NotificationDevice):
        return hub.connections.system.test(connector_id, body.device_id)
