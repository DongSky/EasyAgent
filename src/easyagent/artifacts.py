import hashlib
import time
import uuid


class Artifacts:
    def __init__(self, store):
        self.store = store

    def put(self, name, content, media_type="text/plain", run_id=None):
        if isinstance(content, str):
            content = content.encode()
        if len(content) > 50_000_000 or not name or len(name) > 200:
            raise ValueError("artifact needs a name and must be below 50 MB")
        digest = hashlib.sha256(content).hexdigest()
        with self.store.transaction() as db:
            old = db.execute("SELECT id FROM artifacts WHERE run_id IS ? AND name=? AND digest=?", (run_id, name, digest)).fetchone()
            if old:
                return self.metadata(old[0], db)
            identifier = uuid.uuid4().hex
            db.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?)", (identifier, run_id, name, media_type, content, digest, time.time()))
            if run_id:
                self.store.event(db, run_id, "artifact.created", {"id": identifier, "name": name, "digest": digest})
            return self.metadata(identifier, db)

    def metadata(self, identifier, db):
        row = db.execute("SELECT id,run_id,name,media_type,digest,created,length(content) AS size FROM artifacts WHERE id=?", (identifier,)).fetchone()
        if not row:
            raise KeyError(identifier)
        return dict(row)

    def get(self, identifier):
        with self.store.connect() as db:
            info = self.metadata(identifier, db)
            return info, db.execute("SELECT content FROM artifacts WHERE id=?", (identifier,)).fetchone()[0]

    def list(self, run_id=None):
        with self.store.connect() as db:
            return [dict(r) for r in db.execute("SELECT id,run_id,name,media_type,digest,created,length(content) AS size FROM artifacts WHERE (? IS NULL OR run_id=?) ORDER BY created DESC LIMIT 1000", (run_id, run_id))]
