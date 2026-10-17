"""Durable execution records and append-only events."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3

TELEMETRY_KINDS = ('status_observed', 'status_unavailable', 'sync_progress',
                   'sync_installation_progress')
TELEMETRY_LIMIT = 1000
MAX_EVENT_DETAIL_BYTES = 64 * 1024
MAX_EVENT_PAGE_BYTES = 1024 ** 2


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
                CREATE INDEX IF NOT EXISTS events_run_id ON events(run_id, id);
                CREATE INDEX IF NOT EXISTS events_time_id ON events(time, id);
                CREATE INDEX IF NOT EXISTS events_run_kind_id ON events(run_id, kind, id);
                CREATE TABLE IF NOT EXISTS history_archives (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS external_approvals (
                    nonce TEXT PRIMARY KEY, action TEXT NOT NULL, subject TEXT NOT NULL,
                    data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS approvals_scope ON external_approvals(action,subject);
                CREATE TABLE IF NOT EXISTS approval_revocations (
                    nonce TEXT NOT NULL, subject TEXT NOT NULL, PRIMARY KEY(nonce,subject));
            """)
        from .approvals import ApprovalPolicy
        self.approvals = ApprovalPolicy(self.root)

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
            detail = changes
            if kind in TELEMETRY_KINDS:
                detail = {k: changes[k] for k in ('state', 'raw_state', 'status_query_ok',
                    'status_source', 'last_status_query', 'sync_progress') if k in changes}
                diagnostics = changes.get('status_diagnostics', [])
                if diagnostics:
                    detail['diagnostic'] = {k: diagnostics[-1][k] for k in
                        ('returncode', 'error', 'parse_error') if k in diagnostics[-1]}
                if kind == 'sync_installation_progress':
                    journal = changes['sync_installation']
                    detail = {'phase': journal['phase'], 'started': journal['started'],
                              'installed_files': sum(f.get('installed', False)
                                  for f in journal['files'].values())}
                db.execute("DELETE FROM events WHERE run_id=? AND kind IN (?,?,?,?) AND id <= "
                    "(SELECT id FROM events WHERE run_id=? AND kind IN (?,?,?,?) "
                    "ORDER BY id DESC LIMIT 1 OFFSET ?)",
                    (run_id, *TELEMETRY_KINDS, run_id, *TELEMETRY_KINDS, TELEMETRY_LIMIT - 1))
            db.execute("INSERT INTO events(run_id,time,kind,detail) VALUES (?,?,?,?)",
                       (run_id, now(), kind, json.dumps(detail)))
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

    def event_page(self, run_id: str, after_id: int = 0, limit: int = 100) -> dict:
        if type(after_id) is not int or after_id < 0 or type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError('after_id must be nonnegative and limit must be 1..500')
        self.get(run_id)
        items, size, more = [], 0, False
        with self.connect() as db:
            rows = db.execute("SELECT id,time,kind,detail FROM events WHERE run_id=? AND id>? "
                              "ORDER BY id LIMIT ?", (run_id, after_id, limit + 1))
            for i, t, k, detail in rows:
                raw = detail.encode()
                item = {'id': i, 'time': t, 'kind': k}
                if len(raw) > MAX_EVENT_DETAIL_BYTES:
                    item.update(detail={'bytes': len(raw),
                        'sha256': hashlib.sha256(raw).hexdigest()}, detail_omitted=True)
                else:
                    item['detail'] = json.loads(detail)
                item_bytes = len(json.dumps(item).encode())
                if len(items) >= limit or size + item_bytes > MAX_EVENT_PAGE_BYTES:
                    more = True
                    break
                items.append(item); size += item_bytes
        return {'ok': True, 'run_id': run_id, 'events': items,
                'next_after_id': items[-1]['id'] if more else None}

    def event_detail(self, run_id, event_id, offset=0, limit=16384):
        if (type(event_id) is not int or event_id < 1 or type(offset) is not int or offset < 0
                or type(limit) is not int or not 1 <= limit <= 16384):
            raise ValueError('event_id positive, offset nonnegative, limit 1..16384 characters')
        self.get(run_id)
        with self.connect() as db:
            row = db.execute('SELECT length(detail),substr(detail,?,?) FROM events '
                             'WHERE run_id=? AND id=?', (offset + 1, limit, run_id, event_id)).fetchone()
        if row is None:
            raise ValueError('event is missing or archived; inspect history_archives')
        total, text = row
        return {'ok': True, 'run_id': run_id, 'event_id': event_id, 'text': text,
                'total_characters': total,
                'next_offset': offset + len(text) if offset + len(text) < total else None}

    def events(self, run_id: str, after_id: int = 0, limit: int = 100) -> list[dict]:
        return self.event_page(run_id, after_id, limit)['events']

    def archive_events(self, older_than_seconds=86400, limit=1000, dry_run=True):
        from .history_archives import archive
        return archive(self, older_than_seconds, limit, dry_run)

    def archives(self, limit=50, offset=0):
        if type(limit) is not int or not 1 <= limit <= 500 or type(offset) is not int or offset < 0:
            raise ValueError('limit must be 1..500 and offset nonnegative')
        with self.connect() as db:
            rows = db.execute('SELECT data FROM history_archives ORDER BY created_at,id '
                              'LIMIT ? OFFSET ?', (limit, offset)).fetchall()
        return {'ok': True, 'archives': [json.loads(row[0]) for row in rows]}

    @contextmanager
    def lock(self, run_id: str):
        self.get(run_id)  # Validate before using an ID in a path.
        root = self.root / run_id
        if root.is_symlink() or not root.resolve().is_relative_to(self.root):
            raise ValueError("registered run directory cannot be a symlink or escape state")
        path = root / "operation.lock"
        if path.is_symlink():
            raise ValueError("operation lock cannot be a symlink")
        with path.open("a") as file:
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("another submit/recover/sync operation is running") from exc
            try:
                yield
            finally:
                fcntl.flock(file, fcntl.LOCK_UN)
