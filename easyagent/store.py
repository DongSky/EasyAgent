"""SQLite store for EasyAgent rewrite (SPEC §2).

Tables: missions, runs, events, checkpoints, tool_versions, learnings.
All times are stored as ISO-8601 UTC strings (contracts use ``str`` timestamps).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

from .contracts import Budget, Checkpoint, Mission, RunEvent, RunState

_SCHEMA = """
CREATE TABLE IF NOT EXISTS missions (
    id TEXT PRIMARY KEY,
    goal TEXT NOT NULL,
    budget_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL,
    status TEXT NOT NULL,
    step INTEGER NOT NULL DEFAULT 0,
    plan_json TEXT NOT NULL DEFAULT '[]',
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    ts TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS checkpoints (
    run_id TEXT NOT NULL,
    step INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, step)
);
CREATE TABLE IF NOT EXISTS tool_versions (
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    manifest_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (name, version)
);
CREATE TABLE IF NOT EXISTS learnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    tags_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tool_usage (
    name TEXT PRIMARY KEY,
    calls INTEGER NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    total_ms REAL NOT NULL DEFAULT 0,
    last_call TEXT
);
CREATE TABLE IF NOT EXISTS bypass_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_sig TEXT NOT NULL,
    mission_id TEXT NOT NULL,
    primitive_calls INTEGER NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_run_seq ON events (run_id, seq);
CREATE INDEX IF NOT EXISTS idx_runs_mission ON runs (mission_id);
CREATE INDEX IF NOT EXISTS idx_bypass_sig ON bypass_events (goal_sig);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


class Store:
    """Thread-safe SQLite wrapper implementing the SPEC §2 API."""

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock, self._db:
            self._db.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---- missions ----
    def create_mission(self, goal: str, budget: Budget | None = None) -> Mission:
        budget = budget or Budget()
        mission_id = _new_id()
        created_at = _now()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO missions (id, goal, budget_json, created_at) VALUES (?,?,?,?)",
                (mission_id, goal, budget.model_dump_json(), created_at),
            )
        return Mission(id=mission_id, goal=goal, budget=budget, created_at=created_at)

    def get_mission(self, mission_id: str) -> Mission | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM missions WHERE id=?", (mission_id,)
            ).fetchone()
        if row is None:
            return None
        return Mission(
            id=row["id"],
            goal=row["goal"],
            budget=Budget.model_validate_json(row["budget_json"]),
            created_at=row["created_at"],
        )

    # ---- runs ----
    def create_run(self, mission_id: str) -> RunState:
        run_id = _new_id()
        started_at = _now()
        state = RunState(
            run_id=run_id,
            mission_id=mission_id,
            status="pending",
            step=0,
            plan=[],
            started_at=started_at,
            updated_at=started_at,
        )
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO runs (run_id, mission_id, status, step, plan_json, started_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    run_id,
                    mission_id,
                    state.status,
                    state.step,
                    json.dumps(state.plan),
                    started_at,
                    started_at,
                ),
            )
        return state

    def update_run(self, run_id: str, **fields) -> RunState | None:
        allowed = {"status", "step", "plan"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if "plan" in updates:
            updates["plan_json"] = json.dumps(updates.pop("plan"))
        with self._lock, self._db:
            if updates:
                clause = ", ".join(f"{k} = ?" for k in updates)
                self._db.execute(
                    f"UPDATE runs SET {clause}, updated_at = ? WHERE run_id = ?",
                    (*updates.values(), _now(), run_id),
                )
            else:
                self._db.execute(
                    "UPDATE runs SET updated_at = ? WHERE run_id = ?", (_now(), run_id)
                )
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> RunState | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        return RunState(
            run_id=row["run_id"],
            mission_id=row["mission_id"],
            status=row["status"],
            step=row["step"],
            plan=json.loads(row["plan_json"]),
            started_at=row["started_at"],
            updated_at=row["updated_at"],
        )

    # ---- events ----
    def append_event(self, run_id: str, type: str, payload: dict | None = None) -> RunEvent:
        ts = _now()
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT COALESCE(MAX(seq), 0) AS m FROM events WHERE run_id=?",
                (run_id,),
            ).fetchone()
            seq = row["m"] + 1
            self._db.execute(
                "INSERT INTO events (run_id, seq, type, payload_json, ts) VALUES (?,?,?,?,?)",
                (run_id, seq, type, json.dumps(payload or {}), ts),
            )
        return RunEvent(run_id=run_id, seq=seq, type=type, payload=payload or {}, ts=ts)

    def get_events(self, run_id: str, after_seq: int = 0) -> list[RunEvent]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM events WHERE run_id=? AND seq > ? ORDER BY seq",
                (run_id, after_seq),
            ).fetchall()
        return [
            RunEvent(
                run_id=r["run_id"],
                seq=r["seq"],
                type=r["type"],
                payload=json.loads(r["payload_json"]),
                ts=r["ts"],
            )
            for r in rows
        ]

    # ---- checkpoints ----
    def save_checkpoint(self, run_id: str, step: int, state_json: str) -> Checkpoint:
        created_at = _now()
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO checkpoints (run_id, step, state_json, created_at)"
                " VALUES (?,?,?,?)",
                (run_id, step, state_json, created_at),
            )
        return Checkpoint(
            run_id=run_id, step=step, state_json=state_json, created_at=created_at
        )

    def load_latest_checkpoint(self, run_id: str) -> Checkpoint | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM checkpoints WHERE run_id=? ORDER BY step DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return Checkpoint(
            run_id=row["run_id"],
            step=row["step"],
            state_json=row["state_json"],
            created_at=row["created_at"],
        )

    # ---- tool versions ----
    def record_tool_version(
        self, name: str, version: str, manifest_json: str, status: str
    ) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO tool_versions (name, version, manifest_json, status, created_at)"
                " VALUES (?,?,?,?,?)",
                (name, version, manifest_json, status, _now()),
            )

    def list_tool_versions(self, name: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT name, version, manifest_json, status, created_at FROM tool_versions"
                " WHERE name=? ORDER BY created_at DESC",
                (name,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- tool usage ledger (RRSI cost rules: every tool call is accounted) ----
    def record_tool_call(self, name: str, ok: bool, ms: float) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO tool_usage (name, calls, failures, total_ms, last_call)"
                " VALUES (?,?,?,?,?)"
                " ON CONFLICT(name) DO UPDATE SET"
                " calls=calls+1, failures=failures+excluded.failures,"
                " total_ms=total_ms+excluded.total_ms, last_call=excluded.last_call",
                (name, 1, 0 if ok else 1, ms, _now()),
            )

    def get_tool_usage(self, name: str | None = None) -> list[dict]:
        with self._lock:
            if name:
                rows = self._db.execute(
                    "SELECT name, calls, failures, total_ms, last_call FROM tool_usage"
                    " WHERE name=?",
                    (name,),
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT name, calls, failures, total_ms, last_call FROM tool_usage"
                    " ORDER BY calls DESC",
                ).fetchall()
        return [dict(r) for r in rows]

    # ---- bypass counter: missions solved with raw primitives instead of plugins
    def record_bypass(self, goal_sig: str, mission_id: str,
                      primitive_calls: int, detail: str = "") -> int:
        """Record one bypass event; returns the row id."""
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO bypass_events (goal_sig, mission_id, primitive_calls,"
                " detail, created_at) VALUES (?,?,?,?,?)",
                (goal_sig, mission_id, primitive_calls, detail, _now()),
            )
            return cur.lastrowid

    def count_bypasses(self, goal_sig: str) -> int:
        """How many bypasses have been recorded for this goal signature."""
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) AS c FROM bypass_events WHERE goal_sig=?",
                (goal_sig,),
            ).fetchone()
        return int(row["c"]) if row else 0

    # ---- learnings ----
    def append_learning(self, text: str, tags: list[str] | None = None) -> int:
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO learnings (text, tags_json, created_at) VALUES (?,?,?)",
                (text, json.dumps(tags or []), _now()),
            )
        return cur.lastrowid

    def get_learnings(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, text, tags_json, created_at FROM learnings ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "id": r["id"],
                "text": r["text"],
                "tags": json.loads(r["tags_json"]),
                "ts": r["created_at"],
            }
            for r in rows
        ]


def init_db(path: str) -> Store:
    """Create (or open) the SQLite database and return a Store."""
    return Store(path)
