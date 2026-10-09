"""Bounded update discovery and reviewable commands; never update the running server."""

from __future__ import annotations

import importlib.metadata
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.request
import sqlite3
from contextlib import closing

REPOSITORY = "https://github.com/Makarov3821/hpc-mcp"
API = "https://api.github.com/repos/Makarov3821/hpc-mcp"
DEFAULT_MAX_AGE = 86400


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                            text=True, timeout=10, check=True)
    return result.stdout.strip()


def installation() -> dict:
    root = Path(__file__).resolve().parents[2]
    version = "unknown"
    editable = False
    packaged_commit = None
    try:
        distribution = importlib.metadata.distribution("hpc-mcp")
        version = distribution.version
        direct = json.loads(distribution.read_text("direct_url.json") or "{}")
        editable = direct.get("dir_info", {}).get("editable") is True
        vcs = direct.get("vcs_info", {})
        if direct.get("url") in (REPOSITORY, REPOSITORY + ".git") and vcs.get("vcs") == "git":
            commit = vcs.get("commit_id")
            if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit):
                packaged_commit = commit
    except importlib.metadata.PackageNotFoundError:
        editable = True  # PYTHONPATH source checkout
    except (ValueError, TypeError):
        pass
    result = {"version": version, "python": sys.executable, "kind": "package",
              "repository": REPOSITORY, "checkout": None}
    if packaged_commit:
        result["commit"] = packaged_commit
    try:
        source_layout = root / "src" / "hpc_mcp" / "updates.py" == Path(__file__).resolve()
        if (editable or source_layout) and (root / "pyproject.toml").is_file():
            result.update(kind="source", checkout=str(root),
                          commit=_git(root, "rev-parse", "HEAD"),
                          branch=_git(root, "branch", "--show-current"),
                          dirty=bool(_git(root, "status", "--porcelain", "--untracked-files=normal")),
                          origin=_git(root, "remote", "get-url", "origin"))
    except (OSError, subprocess.SubprocessError):
        result.update(kind="unknown", checkout=None)
    return result


def _request(path: str, timeout: int) -> dict:
    request = urllib.request.Request(API + path, headers={
        "Accept": "application/vnd.github+json", "User-Agent": "hpc-mcp-update-check"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("update response exceeds 1 MiB")
    return json.loads(raw)


class UpdateService:
    def __init__(self, state_dir: str | Path):
        self.root = Path(state_dir).expanduser().absolute()
        self.cache = self.root / "update-check.json"

    def cached(self) -> dict | None:
        try:
            value = json.loads(self.cache.read_text())
            if not isinstance(value, dict) or value.get("repository") != REPOSITORY:
                return None
            return value
        except (OSError, ValueError):
            return None

    def check(self, force: bool = False, max_age_seconds: int = DEFAULT_MAX_AGE,
              timeout: int = 10) -> dict:
        if not isinstance(force, bool):
            raise ValueError("force must be boolean")
        for name, value, maximum in (("max_age_seconds", max_age_seconds, 604800),
                                     ("timeout", timeout, 60)):
            if type(value) is not int or not (0 if name == "max_age_seconds" else 1) <= value <= maximum:
                raise ValueError(f"invalid {name}")
        local = installation()
        cached = self.cached()
        now = time.time()
        if not force and cached and cached.get("local_commit") == local.get("commit") \
                and cached.get("local_version") == local["version"] \
                and type(cached.get("checked_at")) in (int, float) \
                and 0 <= now - cached["checked_at"] < max_age_seconds:
            return {**cached, "cached": True, "installation": local}
        result = {"ok": False, "repository": REPOSITORY, "checked_at": now,
                  "local_commit": local.get("commit"), "local_version": local["version"],
                  "update_available": None, "cached": False, "installation": local}
        try:
            latest = _request("/commits/main", timeout)["sha"]
            if not isinstance(latest, str) or not re.fullmatch(r"[0-9a-f]{40}", latest):
                raise ValueError("invalid upstream commit")
            status = "unknown"
            if local.get("commit") == latest:
                status = "identical"
            elif local.get("commit"):
                status = _request(f"/compare/{local['commit']}...{latest}", timeout)["status"]
                if status not in ("ahead", "behind", "diverged", "identical"):
                    raise ValueError("invalid upstream comparison")
            result.update(ok=True, latest_commit=latest, comparison=status,
                          update_available=True if status == "ahead" else
                          False if status in ("behind", "identical") else None,
                          release_url=REPOSITORY + "/commit/" + latest)
        except (OSError, ValueError, KeyError, TypeError) as error:
            result["error"] = str(error)
            return result  # Offline/rate limit is unknown, never 'up to date'.
        self.root.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".update-", dir=self.root)
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(result, stream)
            os.replace(temporary, self.cache)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return result

    def plan(self, force: bool = False, max_age_seconds: int = DEFAULT_MAX_AGE,
             timeout: int = 10) -> dict:
        check = self.check(force, max_age_seconds, timeout)
        local = check["installation"]
        blockers = []
        origins = {REPOSITORY, REPOSITORY + ".git",
                   "git@github.com:Makarov3821/hpc-mcp.git"}
        if not check["ok"]:
            blockers.append("Cannot verify upstream; retry update_check when online.")
        if local["kind"] != "source":
            blockers.append("Only a verified editable Git checkout supports this update plan; reinstall using README.")
        else:
            if local.get("origin") not in origins:
                blockers.append("Origin does not match the official repository.")
            if local.get("branch") != "main":
                blockers.append("Switch to main after preserving local branch work.")
            if local.get("dirty"):
                blockers.append("Commit or stash local changes, including untracked files, before updating.")
            if check.get("comparison") in ("behind", "diverged", "unknown"):
                blockers.append("Local history needs review; automatic fast-forward plan unavailable.")
        database = self.root / "history.sqlite3"
        if database.exists():
            try:
                with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
                    if db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='monitor_runtime'").fetchone():
                        runtime = db.execute("SELECT data FROM monitor_runtime WHERE id=1").fetchone()
                        if runtime and json.loads(runtime[0]).get("state") in ("starting", "running", "stopping"):
                            blockers.append("Stop the coordinator with monitor_stop and wait for stopped before updating; it survives MCP shutdown.")
                    if db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='operations'").fetchone():
                        active = db.execute("SELECT id FROM operations WHERE json_extract(data,'$.state') IN ('queued','running') LIMIT 1").fetchone()
                        if active:
                            blockers.append(f"Finish or inspect sync operation {active[0]} before updating; workers survive MCP shutdown.")
            except (sqlite3.Error, ValueError, TypeError, AttributeError) as error:
                blockers.append(f"Cannot verify sync operation state: {error}")
        commands = []
        if not blockers and check.get("update_available"):
            root = Path(local["checkout"])
            # Fetch a verified commit, rather than racing an advancing main branch.
            for argv in (["git", "fetch", "origin", check["latest_commit"]],
                         ["git", "merge", "--ff-only", check["latest_commit"]],
                         [local["python"], "-m", "pip", "install", "--upgrade", "-c",
                          str(root / "requirements-mcp.lock"), "-e", str(root) + "[mcp]"]):
                commands.append({"argv": argv, "cwd": str(root), "display": shlex.join(argv)})
        return {"ok": not blockers, "check": check, "blockers": blockers,
                "commands": commands, "execute_in_order": True,
                "stop_on_error": True, "restart_required": bool(commands),
                "notes": ["Stop the MCP client connection before running these commands; recheck local changes first.",
                          "Keep the same absolute config/state paths when restarting; history and snapshots are retained.",
                          "Commands are a plan only. This tool never fetches, installs or restarts the server."]}
