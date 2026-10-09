"""Persistent background sync processes; no monitoring or automatic submission."""

from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
import threading
from datetime import datetime, timezone

from .history import now


def initialize(history):
    with history.connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, data TEXT NOT NULL)")


def operation_get(history, operation_id):
    initialize(history)
    with history.connect() as db:
        row = db.execute("SELECT data FROM operations WHERE id=?", (operation_id,)).fetchone()
    if row is None:
        raise ValueError("unknown operation")
    return json.loads(row[0])


def operation_update(history, operation_id, **changes):
    with history.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT data FROM operations WHERE id=?", (operation_id,)).fetchone()
        if row is None:
            raise ValueError("unknown operation")
        value = json.loads(row[0])
        value.update(changes, updated_at=now())
        db.execute("UPDATE operations SET data=? WHERE id=?", (json.dumps(value), operation_id))
    return value


def start(jobs, run_id, options=None):
    if options is not None and not isinstance(options, dict):
        raise ValueError("options must be a job_sync argument object")
    options = options or {}
    allowed = {"mode", "includes", "excludes", "destination", "layout", "overwrite", "checksum",
               "compress", "timeout", "resume", "max_file_bytes", "max_total_bytes", "reserve_bytes", "stable_only"}
    if set(options) - allowed:
        raise ValueError("unknown sync options")
    initialize(jobs.history)
    with jobs.history.lock(run_id):
        run = jobs.history.get(run_id)
        if run["phase"] != "submitted":
            raise ValueError("sync requires a confirmed submitted run")
        cluster = jobs._cluster(run)
        with jobs.history.connect() as db:
            active = db.execute("SELECT id FROM operations WHERE run_id=? AND json_extract(data,'$.state') IN ('queued','running')", (run_id,)).fetchone()
            if active:
                raise ValueError(f"sync operation already registered: {active[0]}; inspect it first")
            operation_id = "op_" + uuid.uuid4().hex
            value = {"operation_id": operation_id, "run_id": run_id, "created_at": now(),
                     "state": "queued", "options": options, "cluster": asdict(cluster), "progress": {}}
            db.execute("INSERT INTO operations VALUES (?,?,?)", (operation_id, run_id, json.dumps(value)))
        jobs.history.update(run_id, "sync_operation_registered", last_sync_operation=operation_id)
        try:
            process = subprocess.Popen([sys.executable, "-m", "hpc_mcp.worker", str(jobs.history.root), operation_id],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, cwd=str(jobs.history.root), env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1])))
            operation_update(jobs.history, operation_id, pid=process.pid)
            threading.Thread(target=process.wait, daemon=True).start()
        except OSError as error:
            operation_update(jobs.history, operation_id, state="failed", error=str(error))
            raise
    return {"ok": True, "operation": operation_get(jobs.history, operation_id)}


def inspect(jobs, operation_id):
    operation = operation_get(jobs.history, operation_id)
    pid = operation.get("pid")
    if not pid and operation["state"] in ("queued", "running") and (datetime.now(timezone.utc) - datetime.fromisoformat(operation["created_at"])).total_seconds() > 30:
        operation = operation_update(jobs.history, operation_id, state="interrupted", error="worker never registered")
    if pid and operation["state"] in ("queued", "running"):
        try:
            os.kill(pid, 0)
            command = Path(f"/proc/{pid}/cmdline")
            if command.exists() and operation_id.encode() not in command.read_bytes().split(b"\0"):
                raise ProcessLookupError("PID reused by another process")
        except (ProcessLookupError, FileNotFoundError):
            operation = operation_update(jobs.history, operation_id, state="interrupted",
                error="worker exited without a final result; retry sync with resume after inspection")
        except PermissionError:
            pass
    return {"ok": True, "operation": operation}
