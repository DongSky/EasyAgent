"""Read-only diagnostics and password-encrypted, SQLite-consistent backup/restore."""

import base64
import io
import json
import os
import platform
import shutil
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path
from contextlib import closing
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from .contracts import Contract
from pydantic import Field


class BackupRequest(Contract):
    password: str = Field(min_length=12, max_length=1000)


def cipher(password, salt):
    if len(password) < 12:
        raise ValueError("backup password must contain at least 12 characters")
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=600_000).derive(
        password.encode()
    )
    return Fernet(base64.urlsafe_b64encode(key))


def backup(database, destination, password):
    database, destination = Path(database), Path(destination)
    if destination.exists():
        raise FileExistsError("backup destination exists")
    if not database.is_file():
        raise FileNotFoundError(database)
    with tempfile.TemporaryDirectory() as tmp:
        snap = Path(tmp) / "hub.db"
        with closing(sqlite3.connect(database)) as source, closing(sqlite3.connect(snap)) as target:
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("database integrity check failed")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(snap, "hub.db")
            key = database.with_suffix(".secrets.key")
            if key.exists():
                archive.write(key, "vault.key")
            archive.writestr(
                "manifest.json", json.dumps({"format": "easyagent.backup.v1", "created": time.time()})
            )
        salt = os.urandom(16)
        encrypted = b"EAHB1" + salt + cipher(password, salt).encrypt(buffer.getvalue())
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(encrypted)
    return {"path": str(destination), "encrypted": True, "size": len(encrypted)}


def restore(source, database, password):
    database = Path(database)
    if database.exists() or database.with_suffix(".secrets.key").exists():
        raise FileExistsError("restore requires a new database path")
    data = Path(source).read_bytes()
    if data[:5] != b"EAHB1":
        raise ValueError("unknown backup format")
    decoded = cipher(password, data[5:21]).decrypt(data[21:])
    with zipfile.ZipFile(io.BytesIO(decoded)) as archive:
        if set(archive.namelist()) - {"hub.db", "vault.key", "manifest.json"}:
            raise ValueError("unexpected archive entries")
        if json.loads(archive.read("manifest.json"))["format"] != "easyagent.backup.v1":
            raise ValueError("unknown backup schema")
        database.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=database.parent) as tmp:
            path = Path(tmp) / "check.db"
            path.write_bytes(archive.read("hub.db"))
            with closing(sqlite3.connect(path)) as db:
                if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("backup database corrupt")
            if "vault.key" in archive.namelist():
                fd = os.open(
                    database.with_suffix(".secrets.key"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                with os.fdopen(fd, "wb") as file:
                    file.write(archive.read("vault.key"))
            with open(path, "rb") as source_file:
                fd = os.open(database, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as target_file:
                    shutil.copyfileobj(source_file, target_file)
    return {"database": str(database), "restored": True}


def diagnostics(hub):
    with hub.store.connect() as db:
        integrity = db.execute("PRAGMA quick_check").fetchone()[0]
        counts = {r[0]: r[1] for r in db.execute("SELECT status,count(*) FROM runs GROUP BY status")}
        queued = db.execute("SELECT min(created) FROM runs WHERE status IN ('queued','running')").fetchone()[
            0
        ]
        delivery = {r[0]: r[1] for r in db.execute("SELECT status,count(*) FROM deliveries GROUP BY status")}
    return {
        "healthy": integrity == "ok",
        "database": {"integrity": integrity, "size_bytes": Path(hub.store.path).stat().st_size},
        "platform": platform.system(),
        "runtimes": {name: bool(shutil.which(name)) for name in ("python3", "node", "cargo")},
        "runs": counts,
        "oldest_active_seconds": max(0, time.time() - queued) if queued else 0,
        "deliveries": delivery,
        "extensions": len(hub.extensions.active),
        "mcp": hub.mcp.list(),
        "workers": len([w for w in hub.workers if not w.done()]),
    }


def install_operations(app, hub):
    from fastapi.responses import PlainTextResponse, Response
    downloads = {}

    @app.post("/v1/operations/backup")
    async def download_backup(body: BackupRequest, link: bool = False):
        import asyncio
        import secrets

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "backup.eah"
            await asyncio.to_thread(backup, hub.store.path, destination, body.password)
            data = destination.read_bytes()
            if link:
                now = time.monotonic()
                for key, (expiry, _) in list(downloads.items()):
                    if expiry < now:
                        downloads.pop(key, None)
                while len(downloads) >= 3:
                    downloads.pop(next(iter(downloads)))
                identifier = secrets.token_urlsafe(32)
                downloads[identifier] = (now + 300, data)
                return {"url": "/v1/operations/backups/" + identifier, "expires_in_seconds": 300}
            return Response(
                data,
                media_type="application/octet-stream",
                headers={"Content-Disposition": "attachment; filename=easyagent-backup.eah"},
            )

    @app.get("/v1/operations/backups/{identifier}")
    async def fetch_backup(identifier: str):
        entry = downloads.get(identifier)
        if entry is None or entry[0] < time.monotonic():
            downloads.pop(identifier, None)
            raise KeyError("backup download expired")
        return Response(
            entry[1],
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": "attachment; filename=easyagent-backup.eah",
                "Cache-Control": "no-store",
            },
        )

    @app.get("/v1/operations/doctor")
    async def doctor():
        return diagnostics(hub)

    @app.get("/v1/operations/metrics", response_class=PlainTextResponse)
    async def metrics():
        d = diagnostics(hub)
        lines = ["# TYPE eah_runs gauge"] + [f'eah_runs{{status="{k}"}} {v}' for k, v in d["runs"].items()]
        lines += [
            f"eah_database_bytes {d['database']['size_bytes']}",
            f"eah_workers {d['workers']}",
            f"eah_oldest_active_seconds {d['oldest_active_seconds']}",
        ]
        lines += [f'eah_deliveries{{status="{k}"}} {v}' for k, v in d["deliveries"].items()]
        return "\n".join(lines) + "\n"

    @app.get("/v1/operations/traces/{run_id}")
    async def trace(run_id: str):
        hub.store.run(run_id)
        with hub.store.connect() as db:
            return {
                "trace_id": run_id,
                "events": [
                    dict(r) | {"payload": json.loads(r["payload"])}
                    for r in db.execute("SELECT * FROM events WHERE run_id=? ORDER BY id", (run_id,))
                ],
                "model_calls": [
                    dict(r) for r in db.execute("SELECT * FROM model_calls WHERE run_id=?", (run_id,))
                ],
            }
