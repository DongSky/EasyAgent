"""Opt-in bounded background reviews and automatic conversation compaction."""

import asyncio
import json
import time
from pydantic import Field
from .contracts import Contract


class MaintenanceSettings(Contract):
    auto_compact: bool = False
    compact_after_chars: int = Field(default=48000, ge=4000, le=500000)
    review_model: str | None = None
    review_limit_per_day: int = Field(default=0, ge=0, le=100)


class Maintenance:
    def __init__(self, hub):
        self.hub = hub
        with hub.store.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS maintenance_jobs(id TEXT PRIMARY KEY,kind TEXT NOT NULL,status TEXT NOT NULL,until REAL NOT NULL,error TEXT)"
            )

    def settings(self):
        rows = self.hub.store.memory_search("maintenance-settings", limit=1)
        return MaintenanceSettings.model_validate(rows[0]["value"] if rows else {})

    def configure(self, body):
        body = MaintenanceSettings.model_validate(body)
        if body.review_limit_per_day and (
            not body.review_model or body.review_model not in self.hub.models.bindings
        ):
            raise ValueError("select a connected review model")
        self.hub.store.memory_put("maintenance-settings", "settings", body.model_dump(), "operator")
        return body

    def claim(self, identifier, kind):
        with self.hub.store.transaction() as db:
            row = db.execute("SELECT * FROM maintenance_jobs WHERE id=?", (identifier,)).fetchone()
            if row and (row["status"] in ("done", "failed") or row["until"] > time.time()):
                return False
            db.execute(
                "INSERT OR REPLACE INTO maintenance_jobs VALUES(?,?,'running',?,NULL)",
                (identifier, kind, time.time() + 240),
            )
        return True

    def finish(self, identifier, error=None):
        with self.hub.store.connect() as db:
            db.execute(
                "UPDATE maintenance_jobs SET status=?,error=?,until=? WHERE id=?",
                ("failed" if error else "done", error, time.time(), identifier),
            )

    async def tick(self):
        await self.hub.execution.cleanup()
        settings = self.settings()
        for row in self.hub.store.memory_search("compaction-requests", limit=50):
            key = "requested-compact:" + row["key"]
            if not self.claim(key, "compact"):
                continue
            try:
                await self.hub.conversations.compact(row["value"]["conversation"], row["value"]["options"])
            except Exception as exc:
                self.finish(key, type(exc).__name__)
            else:
                self.finish(key)
            return
        if settings.auto_compact:
            for c in self.hub.conversations.list():
                if c["active_run"]:
                    continue
                full = self.hub.conversations.get(c["id"])
                messages = [m for m in full["messages"] if m["id"] > full["summary_until"]]
                if (
                    len(messages) <= 6
                    or sum(len(m["content"]) for m in messages) < settings.compact_after_chars
                ):
                    continue
                key = "compact:" + c["id"] + ":" + str(messages[-1]["id"])
                if not self.claim(key, "compact"):
                    continue
                try:
                    await self.hub.conversations.compact(c["id"], {})
                except Exception as exc:
                    self.finish(key, type(exc).__name__)
                else:
                    self.finish(key)
                return
        if settings.review_limit_per_day:
            day = int(time.time() // 86400)
            with self.hub.store.connect() as db:
                used = db.execute(
                    "SELECT count(*) FROM maintenance_jobs WHERE kind='review' AND id LIKE ?",
                    (f"review:{day}:%",),
                ).fetchone()[0]
                rows = db.execute(
                    "SELECT id,spec FROM runs WHERE status IN ('succeeded','failed') ORDER BY created DESC LIMIT 100"
                ).fetchall()
            if used >= settings.review_limit_per_day:
                return
            for row in rows:
                flow = json.loads(row["spec"])
                # Only explicitly tagged workflows are eligible; reviews and evals never review themselves.
                name = flow.get("metadata", {}).get("learn_as")
                if not name:
                    continue
                with self.hub.store.connect() as db:
                    if db.execute(
                        "SELECT 1 FROM learned_skills WHERE source_run=? AND name=?", (row["id"], name)
                    ).fetchone():
                        continue
                key = f"review:{day}:" + row["id"]
                if not self.claim(key, "review"):
                    continue
                try:
                    await self.hub.learning.propose(
                        {"run_id": row["id"], "model": settings.review_model, "name": name}
                    )
                except Exception as exc:
                    self.finish(key, type(exc).__name__)
                else:
                    self.finish(key)
                return

    async def loop(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(2)


def install_maintenance(app, hub):
    @app.get("/v1/maintenance/settings")
    async def settings():
        return hub.maintenance.settings()

    @app.put("/v1/maintenance/settings")
    async def save(body: MaintenanceSettings):
        return hub.maintenance.configure(body)

    @app.get("/v1/maintenance/jobs")
    async def jobs():
        with hub.store.connect() as db:
            return [
                dict(r) for r in db.execute("SELECT * FROM maintenance_jobs ORDER BY until DESC LIMIT 100")
            ]
