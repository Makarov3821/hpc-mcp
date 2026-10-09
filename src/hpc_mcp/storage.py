"""Optional independent CoW input snapshots and explicitly scoped storage cleanup."""

from contextlib import contextmanager
import fcntl
import hashlib
from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile
import time

from .jobs import TERMINAL, file_manifest
from .sync_support import positive


class InputCache:
    def __init__(self, history):
        self.root = history.root / "input-cache"
        if self.root.is_symlink():
            raise ValueError("input cache cannot be a symlink")
        self.root.mkdir(exist_ok=True)

    @contextmanager
    def lock(self):
        if (self.root / "cache.lock").is_symlink():
            raise ValueError("cache lock cannot be a symlink")
        with (self.root / "cache.lock").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def copy(self, source, destination):
        source, destination = Path(source), Path(destination)
        with self.lock():
            with source.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            blob = self.root / digest
            if blob.is_symlink():
                raise ValueError("input cache blob cannot be a symlink")
            valid = False
            if blob.is_file():
                with blob.open("rb") as stream:
                    valid = hashlib.file_digest(stream, "sha256").hexdigest() == digest
            if not valid:
                fd, name = tempfile.mkstemp(prefix=".blob-", dir=self.root)
                os.close(fd)
                try:
                    shutil.copyfile(source, name)
                    with open(name, "rb") as stream:
                        if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                            raise ValueError("input changed while caching")
                    os.replace(name, blob)
                    blob.chmod(0o444)
                finally:
                    Path(name).unlink(missing_ok=True)
            os.utime(blob, None)
            try:
                result = subprocess.run(["cp", "--reflink=auto", "--", str(blob), str(destination)],
                                        capture_output=True, timeout=300)
                if result.returncode:
                    shutil.copyfile(blob, destination)
            except (OSError, subprocess.SubprocessError):
                shutil.copyfile(blob, destination)
            destination.chmod(0o600)
        return str(destination)

    def cleanup(self, older_than_seconds=86400, max_cache_bytes=10 * 1024 ** 3, dry_run=True):
        positive(older_than_seconds, "older_than_seconds", True)
        positive(max_cache_bytes, "max_cache_bytes", True)
        if type(dry_run) is not bool:
            raise ValueError("dry_run must be boolean")
        with self.lock():
            files = []
            for file in self.root.iterdir():
                if re.fullmatch(r"[0-9a-f]{64}", file.name) or file.name.startswith(".blob-"):
                    if file.is_symlink() or not file.is_file():
                        raise ValueError("invalid cache entry")
                    stat = file.stat()
                    files.append({"path": str(file), "bytes": stat.st_size, "last_used": stat.st_mtime})
            total = sum(f["bytes"] for f in files)
            remaining = total
            candidates = []
            for file in sorted(files, key=lambda f: f["last_used"]):
                if time.time() - file["last_used"] >= older_than_seconds or remaining > max_cache_bytes:
                    candidates.append(file)
                    remaining -= file["bytes"]
            if not dry_run:
                for file in candidates:
                    Path(file["path"]).unlink()
            return {"ok": True, "dry_run": dry_run, "candidates": candidates,
                    "bytes": total - remaining, "remaining_bytes": remaining,
                    "notes": ["Snapshots are independent files; removing cache blobs does not remove inputs.",
                              "CoW sharing depends on filesystem support; fallback copies may use extra space."]}


def local_cleanup(jobs, run_id, categories=None, older_than_seconds=86400, dry_run=True):
    categories = ["sync_history"] if categories is None else categories
    if not isinstance(categories, list) or not categories or set(categories) - {"snapshot", "sync_history", "old_outputs"}:
        raise ValueError("categories: snapshot, sync_history, old_outputs")
    positive(older_than_seconds, "older_than_seconds", True)
    if type(dry_run) is not bool:
        raise ValueError("dry_run must be boolean")
    with jobs.history.lock(run_id):
        run = jobs.history.get(run_id)
        root = jobs.history.root / run_id
        if run["state"] not in TERMINAL or run.get("sync_state") != "complete":
            raise ValueError("storage cleanup requires a terminal run and complete sync")
        candidates = []
        paths = []
        if "snapshot" in categories:
            paths.extend([("snapshot", root / "input"), ("snapshot", root / "original-input")])
        for category, folder in (("sync_history", "sync-history"), ("old_outputs", "outputs")):
            if category in categories and (root / folder).exists():
                if (root / folder).is_symlink():
                    raise ValueError("storage directory cannot be a symlink")
                paths.extend((category, path) for path in (root / folder).iterdir()
                             if re.fullmatch(r"[0-9a-f]{32}", path.name))
        installed = Path(run["output_dir"]).resolve() if run.get("output_dir") else None
        for category, path in paths:
            if not path.exists():
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                raise ValueError("cleanup path escaped registered run")
            if installed is not None and (installed == path.resolve() or installed.is_relative_to(path.resolve()) or path.resolve().is_relative_to(installed)):
                continue
            files = file_manifest(path, hashes=False)
            newest = max([path.stat().st_mtime, *(p.stat().st_mtime for p in path.rglob("*"))])
            if time.time() - newest >= older_than_seconds:
                candidates.append({"category": category, "path": str(path), "bytes": sum(f["size"] for f in files)})
        if not dry_run:
            for candidate in candidates:
                shutil.rmtree(candidate["path"])
            jobs.history.update(run_id, "storage_cleaned", last_storage_cleanup=candidates,
                                snapshot_removed=run.get("snapshot_removed", False) or
                                any(c["category"] == "snapshot" for c in candidates))
        return {"ok": True, "run_id": run_id, "dry_run": dry_run, "candidates": candidates,
                "bytes": sum(c["bytes"] for c in candidates),
                "notes": ["History, template provenance and file hashes remain; deleted snapshots cannot be replayed.",
                          "Latest installed outputs and project results are protected. No timer is installed."]}
