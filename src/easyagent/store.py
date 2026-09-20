"""Small SQLite persistence boundary. All leases are fenced by a unique owner."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .contracts import Workflow


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class Conflict(ValueError):
    pass


class LeaseLost(RuntimeError):
    pass


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3, 4):
                raise RuntimeError(f"unsupported database version {version}")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs(
                  id TEXT PRIMARY KEY, name TEXT NOT NULL, spec TEXT NOT NULL,
                  status TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                  idempotency_key TEXT UNIQUE, digest TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS steps(
                  run_id TEXT NOT NULL REFERENCES runs(id), id TEXT NOT NULL,
                  spec TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                  ready_at REAL NOT NULL DEFAULT 0, owner TEXT, lease_until REAL,
                  state TEXT NOT NULL DEFAULT '{}', output TEXT, error TEXT,
                  PRIMARY KEY(run_id,id));
                CREATE INDEX IF NOT EXISTS steps_ready ON steps(status,ready_at,lease_until);
                CREATE TABLE IF NOT EXISTS events(
                  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id),
                  kind TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS events_run ON events(run_id,id);
                CREATE TABLE IF NOT EXISTS invocations(
                  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, step_id TEXT NOT NULL,
                  tool TEXT NOT NULL, arguments TEXT NOT NULL, status TEXT NOT NULL,
                  approved INTEGER NOT NULL DEFAULT 0, output TEXT, error TEXT);
                CREATE TABLE IF NOT EXISTS memory(
                  namespace TEXT NOT NULL,key TEXT NOT NULL,value TEXT NOT NULL,
                  source TEXT NOT NULL,updated REAL NOT NULL,PRIMARY KEY(namespace,key));
                CREATE TABLE IF NOT EXISTS policies(
                  id TEXT PRIMARY KEY,name TEXT NOT NULL,body TEXT NOT NULL,
                  status TEXT NOT NULL,report TEXT,created REAL NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_policy ON policies(name) WHERE status='active';
                CREATE TABLE IF NOT EXISTS policy_events(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,policy_id TEXT NOT NULL,
                  action TEXT NOT NULL,created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS run_usage(
                  run_id TEXT PRIMARY KEY REFERENCES runs(id),model_calls INTEGER NOT NULL DEFAULT 0,
                  tool_calls INTEGER NOT NULL DEFAULT 0,output_reserved INTEGER NOT NULL DEFAULT 0,
                  tokens INTEGER NOT NULL DEFAULT 0,cost REAL NOT NULL DEFAULT 0,unknown_usage INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS model_calls(
                  id TEXT PRIMARY KEY,run_id TEXT NOT NULL,step_id TEXT NOT NULL,alias TEXT NOT NULL,
                  status TEXT NOT NULL,usage TEXT,created REAL NOT NULL,finished REAL);
                CREATE TABLE IF NOT EXISTS documents(
                  id TEXT PRIMARY KEY,namespace TEXT NOT NULL,title TEXT NOT NULL,source TEXT NOT NULL,
                  digest TEXT NOT NULL,embedding_model TEXT,created REAL NOT NULL,
                  UNIQUE(namespace,source));
                CREATE TABLE IF NOT EXISTS chunks(
                  document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                  position INTEGER NOT NULL,content TEXT NOT NULL,embedding TEXT,
                  PRIMARY KEY(document_id,position));
                CREATE TABLE IF NOT EXISTS artifacts(
                  id TEXT PRIMARY KEY,run_id TEXT REFERENCES runs(id),name TEXT NOT NULL,
                  media_type TEXT NOT NULL,content BLOB NOT NULL,digest TEXT NOT NULL,created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS triggers(
                  id TEXT PRIMARY KEY,name TEXT NOT NULL,kind TEXT NOT NULL,workflow TEXT NOT NULL,
                  interval_seconds REAL,next_at REAL,enabled INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS child_runs(
                  parent_id TEXT NOT NULL REFERENCES runs(id),step_id TEXT NOT NULL,
                  slot INTEGER NOT NULL,child_id TEXT NOT NULL REFERENCES runs(id),
                  PRIMARY KEY(parent_id,step_id,slot));
                CREATE TABLE IF NOT EXISTS memory_expiry(
                  namespace TEXT NOT NULL,key TEXT NOT NULL,expires REAL NOT NULL,PRIMARY KEY(namespace,key));
                CREATE TABLE IF NOT EXISTS input_requests(
                  id TEXT PRIMARY KEY,run_id TEXT NOT NULL,step_id TEXT NOT NULL,prompt TEXT NOT NULL,
                  schema TEXT NOT NULL,status TEXT NOT NULL,output TEXT);
                CREATE TABLE IF NOT EXISTS definition_versions(
                  kind TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL,
                  body TEXT NOT NULL, scope TEXT, operation_id TEXT UNIQUE, created REAL NOT NULL,
                  PRIMARY KEY(kind,id,revision));
            """)
            columns = {r[1] for r in db.execute("PRAGMA table_info(run_usage)")}
            if "child_runs" not in columns:
                db.execute("ALTER TABLE run_usage ADD COLUMN child_runs INTEGER NOT NULL DEFAULT 0")
            if "tool_revision" not in {r[1] for r in db.execute("PRAGMA table_info(invocations)")}:
                db.execute("ALTER TABLE invocations ADD COLUMN tool_revision INTEGER")
            db.execute("PRAGMA user_version=4")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def event(self, db, run_id, kind, payload):
        db.execute("INSERT INTO events(run_id,kind,payload,created) VALUES(?,?,?,?)",
                   (run_id, kind, encode(payload), time.time()))

    def submit(self, workflow: Workflow, key=None, parent=None, parent_job=None):
        spec = encode(workflow.model_dump())
        digest = hashlib.sha256(spec.encode()).hexdigest()
        with self.transaction() as db:
            if parent_job:
                self.assert_owner(db, parent_job)
            old = db.execute("SELECT id,digest FROM runs WHERE idempotency_key=?", (key,)).fetchone()
            if old:
                if old["digest"] != digest:
                    raise Conflict("idempotency key was used with a different workflow")
                return old["id"]
            run_id = uuid.uuid4().hex
            now = time.time()
            db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)",
                       (run_id, workflow.name, spec, "queued", now, now, key, digest))
            db.execute("INSERT INTO run_usage(run_id) VALUES(?)", (run_id,))
            if parent:
                parent_id, step_id, slot = parent
                status = db.execute("SELECT status FROM runs WHERE id=?", (parent_id,)).fetchone()
                if not status or status[0] in ("cancelled", "failed", "succeeded"):
                    raise Conflict("parent run is terminal")
                self.reserve(db, parent_id, "child_runs")
                db.execute("INSERT INTO child_runs VALUES(?,?,?,?)", (parent_id, step_id, slot, run_id))
            for s in workflow.steps:
                db.execute("INSERT INTO steps(run_id,id,spec,status,ready_at) VALUES(?,?,?,?,?)",
                           (run_id, s.id, encode(s.model_dump()), "queued", s.not_before))
            self.event(db, run_id, "run.created", {"name": workflow.name})
        return run_id

    def run(self, run_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise KeyError(run_id)
            result = dict(row)
            result["spec"] = json.loads(result["spec"])
            result["steps"] = [self.decode_step(s) for s in db.execute(
                "SELECT * FROM steps WHERE run_id=? ORDER BY rowid", (run_id,))]
            result["approvals"] = [dict(a) | {"arguments": json.loads(a["arguments"])} for a in db.execute(
                """WITH RECURSIVE tree(id) AS (SELECT ? UNION ALL SELECT c.child_id FROM child_runs c JOIN tree ON c.parent_id=tree.id)
                SELECT i.id,i.tool,i.arguments,i.status,i.run_id FROM invocations i
                JOIN steps s ON s.run_id=i.run_id AND s.id=i.step_id
                WHERE i.run_id IN (SELECT id FROM tree) AND
                ((i.status='approval' AND s.status='waiting_approval') OR (i.status='uncertain' AND s.status='needs_attention'))""",
                (run_id,))]
            result["children"] = [dict(r) for r in db.execute("SELECT c.step_id,c.slot,r.id,r.name,r.status FROM child_runs c JOIN runs r ON r.id=c.child_id WHERE c.parent_id=? ORDER BY c.slot", (run_id,))]
            result["input_requests"] = [dict(r) | {"schema": json.loads(r["schema"])} for r in db.execute("""WITH RECURSIVE tree(id) AS
                (SELECT ? UNION ALL SELECT c.child_id FROM child_runs c JOIN tree ON c.parent_id=tree.id)
                SELECT q.* FROM input_requests q JOIN steps s ON q.run_id=s.run_id AND q.step_id=s.id
                WHERE q.run_id IN (SELECT id FROM tree) AND q.status='waiting' AND s.status='waiting_input'""", (run_id,))]
            usage = db.execute("SELECT * FROM run_usage WHERE run_id=?", (run_id,)).fetchone()
            result["usage"] = dict(usage) if usage else {}
            return result

    def runs(self, limit=100, query="", statuses=()):
        clauses, params = [], []
        if query:
            clauses.append("instr(lower(name), lower(?)) > 0")
            params.append(query)
        if statuses:
            clauses.append("status IN (" + ",".join("?" for _ in statuses) + ")")
            params.extend(statuses)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT id,name,status,created,updated FROM runs" + where +
                " ORDER BY created DESC LIMIT ?", (*params, limit))]

    @staticmethod
    def decode_step(row):
        d = dict(row)
        for k in ("spec", "state", "output"):
            if d[k] is not None:
                d[k] = json.loads(d[k])
        return d

    def events(self, run_id, after=0, limit=200):
        with self.connect() as db:
            return [dict(r) | {"payload": json.loads(r["payload"])} for r in db.execute(
                "SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id LIMIT ?", (run_id, after, limit))]

    def reconcile(self, db, run_id):
        current = db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()[0]
        if current == "cancelled":
            return
        rows = db.execute("SELECT id,status,spec FROM steps WHERE run_id=?", (run_id,)).fetchall()
        states = {r["id"]: r["status"] for r in rows}
        changed = True
        while changed:
            changed = False
            for r in rows:
                if states[r["id"]] not in ("queued", "retrying", "waiting_children", "waiting_remote"):
                    continue
                deps = json.loads(r["spec"])["depends_on"]
                if any(states[d] in ("failed", "skipped", "cancelled") for d in deps):
                    states[r["id"]] = "skipped"
                    db.execute("UPDATE steps SET status='skipped',error='dependency failed' WHERE run_id=? AND id=?",
                               (run_id, r["id"]))
                    changed = True
        values = set(states.values())
        children = {r[0] for r in db.execute("SELECT r.status FROM child_runs c JOIN runs r ON c.child_id=r.id WHERE c.parent_id=?", (run_id,))}
        if values <= {"succeeded", "skipped"}:
            status = "succeeded"
        elif "running" in values:
            status = "running"
        elif "needs_attention" in values:
            status = "needs_attention"
        elif "waiting_approval" in values:
            status = "waiting_approval"
        elif "waiting_input" in values:
            status = "waiting_input"
        elif "waiting_children" in values and "needs_attention" in children:
            status = "needs_attention"
        elif "waiting_children" in values and "waiting_approval" in children:
            status = "waiting_approval"
        elif "waiting_children" in values and "waiting_input" in children:
            status = "waiting_input"
        elif values & {"queued", "retrying", "waiting_children", "waiting_remote"}:
            status = "queued"
        else:
            status = "failed"
        if status != current:
            db.execute("UPDATE runs SET status=?,updated=? WHERE id=?", (status, time.time(), run_id))
            self.event(db, run_id, "run.status", {"status": status})
        parent = db.execute("SELECT parent_id FROM child_runs WHERE child_id=?", (run_id,)).fetchone()
        if parent:
            self.reconcile(db, parent[0])

    @staticmethod
    def scope_run_ids(db, roots):
        if roots is None:
            return None
        return {r[0] for r in db.execute("""WITH RECURSIVE tree(id) AS
            (SELECT value FROM json_each(?) UNION ALL
             SELECT c.child_id FROM child_runs c JOIN tree ON c.parent_id=tree.id)
            SELECT id FROM tree""", (encode(sorted(roots)),))}

    def claim(self, lease_seconds, run_roots=None):
        now = time.time()
        with self.transaction() as db:
            allowed = self.scope_run_ids(db, run_roots)
            if allowed == set():
                return None
            for run in db.execute("SELECT id,spec,created FROM runs WHERE status NOT IN ('succeeded','failed','cancelled')").fetchall():
                if allowed is not None and run["id"] not in allowed:
                    continue
                limit = json.loads(run["spec"]).get("limits", {}).get("wall_time_seconds", 604800)
                if now - run["created"] > limit:
                    affected = [r[0] for r in db.execute("WITH RECURSIVE tree(id) AS (SELECT ? UNION ALL SELECT c.child_id FROM child_runs c JOIN tree ON c.parent_id=tree.id) SELECT id FROM tree", (run["id"],))]
                    for identifier in affected:
                        db.execute("UPDATE runs SET status='failed',updated=? WHERE id=? AND status NOT IN ('succeeded','failed','cancelled')", (now, identifier))
                        db.execute("UPDATE steps SET status='failed',owner=NULL,lease_until=NULL,error='run time budget exhausted' WHERE run_id=? AND status NOT IN ('succeeded','skipped','failed','cancelled')", (identifier,))
                    self.event(db, run["id"], "run.time_budget_exhausted", {})
            candidates = db.execute("""SELECT s.* FROM steps s JOIN runs r ON r.id=s.run_id
                WHERE r.status NOT IN ('cancelled','succeeded','failed') AND
                ((s.status IN ('queued','retrying','waiting_children','waiting_remote') AND s.ready_at<=?) OR
                 (s.status='running' AND s.lease_until<?)) ORDER BY r.created,s.rowid""", (now, now)).fetchall()
            for row in candidates:
                if allowed is not None and row["run_id"] not in allowed:
                    continue
                spec = json.loads(row["spec"])
                states = {r[0]: r[1] for r in db.execute(
                    "SELECT id,status FROM steps WHERE run_id=?", (row["run_id"],))}
                if any(states[d] != "succeeded" for d in spec["depends_on"]):
                    self.reconcile(db, row["run_id"])
                    continue
                owner = uuid.uuid4().hex
                db.execute("""UPDATE steps SET status='running',owner=?,lease_until=?,attempts=attempts+1
                              WHERE run_id=? AND id=?""", (owner, now + lease_seconds, row["run_id"], row["id"]))
                if row["status"] not in ("waiting_children", "waiting_remote"):
                    self.event(db, row["run_id"], "step.started", {"step": row["id"], "recovered": row["status"] == "running"})
                self.reconcile(db, row["run_id"])
                return self.decode_step(db.execute("SELECT * FROM steps WHERE run_id=? AND id=?",
                                                   (row["run_id"], row["id"])).fetchone())
        return None

    def assert_owner(self, db, job):
        row = db.execute("SELECT status,owner,lease_until FROM steps WHERE run_id=? AND id=?",
                         (job["run_id"], job["id"])).fetchone()
        if not row or row["status"] != "running" or row["owner"] != job["owner"] or row["lease_until"] < time.time():
            raise LeaseLost("step lease was lost or run cancelled")

    def heartbeat(self, job, seconds):
        with self.transaction() as db:
            self.assert_owner(db, job)
            db.execute("UPDATE steps SET lease_until=? WHERE run_id=? AND id=?",
                       (time.time() + seconds, job["run_id"], job["id"]))

    def checkpoint(self, job, state):
        with self.transaction() as db:
            self.assert_owner(db, job)
            db.execute("UPDATE steps SET state=? WHERE run_id=? AND id=?",
                       (encode(state), job["run_id"], job["id"]))
        job["state"] = state

    def finish(self, job, status, output=None, error=None, delay=0):
        with self.transaction() as db:
            self.assert_owner(db, job)
            db.execute("""UPDATE steps SET status=?,output=?,error=?,ready_at=?,owner=NULL,lease_until=NULL
                        WHERE run_id=? AND id=?""",
                       (status, encode(output) if output is not None else None, error, time.time() + delay,
                        job["run_id"], job["id"]))
            if status in ("waiting_approval", "needs_attention", "waiting_children", "waiting_input", "waiting_remote"):
                db.execute("UPDATE steps SET attempts=MAX(0,attempts-1) WHERE run_id=? AND id=?", (job["run_id"], job["id"]))
            if status != "waiting_children":
                self.event(db, job["run_id"], "step." + status, {"step": job["id"], "error": error})
            self.reconcile(db, job["run_id"])

    def cancel(self, run_id):
        with self.connect() as db:
            children = [r[0] for r in db.execute("SELECT child_id FROM child_runs WHERE parent_id=?", (run_id,))]
        for child in children:
            self.cancel(child)
        with self.transaction() as db:
            row = db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise KeyError(run_id)
            if row[0] in ("succeeded", "failed", "cancelled"):
                return
            db.execute("UPDATE runs SET status='cancelled',updated=? WHERE id=?", (time.time(), run_id))
            db.execute("""UPDATE steps SET status='cancelled',owner=NULL,lease_until=NULL
                        WHERE run_id=? AND status NOT IN ('succeeded','failed','skipped')""", (run_id,))
            self.event(db, run_id, "run.cancelled", {})

    def memory_put(self, namespace, key, value, source="user", expires=None):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO memory VALUES(?,?,?,?,?)",
                       (namespace, key, encode(value), source, time.time()))
            db.execute("DELETE FROM memory_expiry WHERE namespace=? AND key=?", (namespace, key))
            if expires is not None:
                db.execute("INSERT INTO memory_expiry VALUES(?,?,?)", (namespace, key, expires))

    def memory_search(self, namespace, query="", limit=20):
        with self.connect() as db:
            # Literal substring matching; wildcard characters have no special semantics.
            return [dict(r) | {"value": json.loads(r["value"])} for r in db.execute(
                """SELECT m.* FROM memory m LEFT JOIN memory_expiry e ON e.namespace=m.namespace AND e.key=m.key
                WHERE m.namespace=? AND (instr(m.key,?)>0 OR instr(m.value,?)>0)
                AND (e.expires IS NULL OR e.expires>?) ORDER BY m.updated DESC LIMIT ?""",
                (namespace, query, query, time.time(), limit))]

    def reserve(self, db, run_id, kind, amount=1):
        parent = db.execute("SELECT parent_id FROM child_runs WHERE child_id=?", (run_id,)).fetchone()
        if parent:
            self.reserve(db, parent[0], kind, amount)
        spec = json.loads(db.execute("SELECT spec FROM runs WHERE id=?", (run_id,)).fetchone()[0])
        limits = spec.get("limits", {})
        db.execute("INSERT OR IGNORE INTO run_usage(run_id) VALUES(?)", (run_id,))
        usage = db.execute("SELECT * FROM run_usage WHERE run_id=?", (run_id,)).fetchone()
        caps = {"model_calls": limits.get("model_calls", 64), "tool_calls": limits.get("tool_calls", 256),
                "output_reserved": limits.get("output_tokens", 131072), "child_runs": limits.get("child_runs", 256)}
        if kind not in caps or usage[kind] + amount > caps[kind]:
            raise ValueError("run budget exceeded: " + kind)
        db.execute(f"UPDATE run_usage SET {kind}={kind}+? WHERE run_id=?", (amount, run_id))

    def reserve_cost(self, db, run_id, amount, price_known):
        parent = db.execute("SELECT parent_id FROM child_runs WHERE child_id=?", (run_id,)).fetchone()
        if parent:
            self.reserve_cost(db, parent[0], amount, price_known)
        spec = json.loads(db.execute("SELECT spec FROM runs WHERE id=?", (run_id,)).fetchone()[0])
        cap = spec.get("limits", {}).get("cost_usd")
        current = db.execute("SELECT cost FROM run_usage WHERE run_id=?", (run_id,)).fetchone()[0]
        if cap is not None and (not price_known or current + amount > cap):
            raise ValueError("cost budget requires known prices and sufficient remaining reservation")
        db.execute("UPDATE run_usage SET cost=cost+? WHERE run_id=?", (amount, run_id))

    def settle(self, db, run_id, output_delta, tokens, cost_delta, unknown):
        parent = db.execute("SELECT parent_id FROM child_runs WHERE child_id=?", (run_id,)).fetchone()
        if parent:
            self.settle(db, parent[0], output_delta, tokens, cost_delta, unknown)
        db.execute("UPDATE run_usage SET output_reserved=output_reserved+?,tokens=tokens+?,cost=MAX(0,cost+?),unknown_usage=unknown_usage+? WHERE run_id=?",
                   (output_delta, tokens, cost_delta, unknown, run_id))

    def backup(self, destination):
        with self.connect() as source, sqlite3.connect(destination) as target:
            source.backup(target)
