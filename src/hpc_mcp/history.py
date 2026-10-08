"""Durable execution records and append-only events."""

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import sqlite3


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class History:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.database = self.root / "history.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, time TEXT NOT NULL,
                    kind TEXT NOT NULL, detail TEXT NOT NULL
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def add(self, run: dict):
        with self.connect() as db:
            db.execute("INSERT INTO runs VALUES (?, ?, ?)",
                       (run["run_id"], run["created_at"], json.dumps(run)))
            db.execute("INSERT INTO events(run_id,time,kind,detail) VALUES (?,?,?,?)",
                       (run["run_id"], now(), "prepared", "{}"))

    def get(self, run_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT data FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown run: {run_id}")
        return json.loads(row[0])

    def update(self, run_id: str, kind: str, **changes) -> dict:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise ValueError(f"unknown run: {run_id}")
            run = json.loads(row[0])
            run.update(changes, updated_at=now())
            db.execute("UPDATE runs SET data=? WHERE run_id=?", (json.dumps(run), run_id))
            db.execute("INSERT INTO events(run_id,time,kind,detail) VALUES (?,?,?,?)",
                       (run_id, now(), kind, json.dumps(changes)))
        return run

    def list(self, cluster: str | None = None, limit: int = 50, offset: int = 0) -> list[dict]:
        if not 1 <= limit <= 500 or offset < 0:
            raise ValueError("limit must be 1..500 and offset must be nonnegative")
        with self.connect() as db:
            # Cluster names live in JSON; keep filtering inside SQLite before pagination.
            rows = db.execute(
                "SELECT data FROM runs WHERE (? IS NULL OR json_extract(data,'$.cluster')=?) "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?", (cluster, cluster, limit, offset)
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def events(self, run_id: str) -> list[dict]:
        self.get(run_id)
        with self.connect() as db:
            rows = db.execute("SELECT time,kind,detail FROM events WHERE run_id=? ORDER BY id",
                              (run_id,)).fetchall()
        return [{"time": t, "kind": k, "detail": json.loads(d)} for t, k, d in rows]

    @contextmanager
    def lock(self, run_id: str):
        self.get(run_id)  # Validate before using an ID in a path.
        path = self.root / run_id / "operation.lock"
        with path.open("a") as file:
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("another submit/recover/sync operation is running") from exc
            try:
                yield
            finally:
                fcntl.flock(file, fcntl.LOCK_UN)
