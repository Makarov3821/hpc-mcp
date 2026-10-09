"""Exact rsync selection previews and reusable sync metadata."""

import hashlib
import json
from pathlib import Path
import shutil
import tempfile

from .jobs import relative_path


def preview(jobs, run, cluster, policy, target, max_file_bytes, max_total_bytes, reserve_bytes, check_space=True):
    with tempfile.TemporaryDirectory(prefix="hpc-mcp-preview-") as directory:
        result = jobs.transfer.run(cluster, Path(directory), run["remote_dir"], download=True,
            patterns=policy.output_include, mode=policy.output_mode, excludes=policy.output_exclude,
            checksum=policy.transfer_checksum, compress=policy.transfer_compress,
            timeout=policy.transfer_timeout, dry_run=True)
    if not result.ok:
        return {"ok": False, "error": result.diagnostic()}
    files = []
    for line in result.stdout.splitlines():
        if not line.startswith("HPC_FILE|"):
            continue
        parts = line.split("|", 4)
        if len(parts) != 5 or len(parts[1]) < 2:
            raise ValueError("unsupported rsync preview record")
        if parts[1][1] != "f":
            continue
        path = relative_path(parts[4])
        if not parts[2].isdigit() or not parts[3]:
            raise ValueError("invalid rsync preview size/time")
        files.append({"path": path, "size": int(parts[2]), "mtime": parts[3]})
    if len({f["path"] for f in files}) != len(files):
        raise ValueError("duplicate preview paths")
    files.sort(key=lambda f: f["path"])
    total = sum(f["size"] for f in files)
    existing = Path(target)
    while not existing.exists():
        existing = existing.parent
    free = shutil.disk_usage(existing).free
    blockers = []
    if any(f["size"] > max_file_bytes for f in files):
        blockers.append("max_file_bytes exceeded")
    if total > max_total_bytes:
        blockers.append("max_total_bytes exceeded")
    # An existing resumed file and its replacement can coexist during rsync.
    if check_space and free < 2 * total + reserve_bytes:
        blockers.append("insufficient local free space (2x selected bytes plus reserve required)")
    return {"ok": not blockers, "files": files, "count": len(files), "bytes": total,
            "free_bytes": free, "required_free_bytes": 2 * total + reserve_bytes,
            "blockers": blockers, "max_file_bytes": max_file_bytes, "max_total_bytes": max_total_bytes,
            "reserve_bytes": reserve_bytes,
            "fingerprint": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()}


def positive(value, name, allow_zero=False):
    if type(value) is not int or value < (0 if allow_zero else 1):
        raise ValueError(f"{name} must be a {'nonnegative' if allow_zero else 'positive'} integer")
    return value
