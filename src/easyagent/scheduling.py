import json
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from croniter import croniter

from .contracts import Contract, Workflow
from pydantic import Field


class Trigger(Contract):
    name: str = Field(min_length=1, max_length=100)
    kind: str
    workflow: Workflow
    interval_seconds: float | None = Field(default=None, ge=1, le=31536000)
    start_at: float | None = Field(default=None, ge=0)
    cron: str | None = None
    timezone: str = "UTC"


class Scheduler:
    def __init__(self, hub):
        self.hub = hub

    def create(self, trigger: Trigger):
        if trigger.kind not in ("schedule", "webhook"):
            raise ValueError("trigger kind must be schedule or webhook")
        if trigger.cron:
            if trigger.kind != "schedule" or trigger.interval_seconds:
                raise ValueError("cron requires schedule without interval_seconds")
            trigger.start_at = self.next_cron(trigger.cron, trigger.timezone, trigger.start_at or time.time())
        if trigger.kind == "schedule" and trigger.start_at is None:
            raise ValueError("scheduled triggers need start_at")
        identifier = uuid.uuid4().hex
        with self.hub.store.transaction() as db:
            db.execute(
                "INSERT INTO triggers VALUES(?,?,?,?,?,?,1)",
                (
                    identifier,
                    trigger.name,
                    trigger.kind,
                    trigger.workflow.model_dump_json(),
                    trigger.interval_seconds,
                    trigger.start_at,
                ),
            )
            if trigger.cron:
                db.execute(
                    "INSERT INTO memory VALUES('cron-schedules',?,?,?,?)",
                    (
                        identifier,
                        json.dumps({"cron": trigger.cron, "timezone": trigger.timezone}),
                        "operator",
                        time.time(),
                    ),
                )
        return {"id": identifier, **trigger.model_dump(), "enabled": True}

    def list(self):
        with self.hub.store.connect() as db:
            return [
                dict(r) | {"workflow": json.loads(r["workflow"])}
                for r in db.execute("SELECT * FROM triggers")
            ]

    def enable(self, identifier, enabled):
        with self.hub.store.connect() as db:
            result = db.execute("UPDATE triggers SET enabled=? WHERE id=?", (int(enabled), identifier))
            if not result.rowcount:
                raise KeyError(identifier)

    def fire(self, identifier, payload, event_id):
        if not event_id or len(event_id) > 200:
            raise ValueError("webhooks need an event id for deduplication")
        with self.hub.store.connect() as db:
            row = db.execute(
                "SELECT * FROM triggers WHERE id=? AND enabled=1 AND kind='webhook'", (identifier,)
            ).fetchone()
        if not row:
            raise KeyError(identifier)
        workflow = Workflow.model_validate_json(row["workflow"])
        workflow.inputs.update(payload)
        return self.hub.submit(workflow, f"trigger:{identifier}:{event_id}")

    def tick(self):
        now = time.time()
        with self.hub.store.connect() as db:
            due = db.execute(
                "SELECT * FROM triggers WHERE enabled=1 AND kind='schedule' AND next_at<=?", (now,)
            ).fetchall()
        for row in due:
            key = f"schedule:{row['id']}:{row['next_at']}"
            self.hub.submit(Workflow.model_validate_json(row["workflow"]), key)
            # Coalesce missed intervals to one execution. Never replay a burst after downtime.
            next_at = now + row["interval_seconds"] if row["interval_seconds"] else None
            with self.hub.store.connect() as db:
                schedule = db.execute(
                    "SELECT value FROM memory WHERE namespace='cron-schedules' AND key=?", (row["id"],)
                ).fetchone()
            if schedule:
                definition = json.loads(schedule[0])
                next_at = self.next_cron(definition["cron"], definition["timezone"], now)
            with self.hub.store.connect() as db:
                db.execute(
                    "UPDATE triggers SET next_at=?,enabled=? WHERE id=? AND next_at=?",
                    (next_at, int(next_at is not None), row["id"], row["next_at"]),
                )

    @staticmethod
    def next_cron(expression, zone, after):
        if len(expression.split()) != 5 or not croniter.is_valid(expression):
            raise ValueError("cron must contain five valid fields")
        return (
            croniter(expression, datetime.fromtimestamp(after, ZoneInfo(zone))).get_next(datetime).timestamp()
        )
