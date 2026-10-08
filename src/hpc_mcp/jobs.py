"""Snapshot, submit, track and retrieve ordinary batch jobs."""

import hashlib
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import uuid
import fnmatch
from dataclasses import replace

from .config import Cluster
from .history import History, now
from .job_scheduler import cluster_scope, parse_job_id, parse_status, query_commands, submit_command
from .ssh import SSHTransport
from .transfer import Transfer

TERMINAL = {"succeeded", "failed", "cancelled"}


def relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or ".." in path.parts or
            any(c in value for c in "\x00\r\n\\") or value == "."):
        raise ValueError("file paths must be relative and cannot contain '..' or control characters")
    return path.as_posix()


def file_manifest(root: Path) -> list[dict]:
    manifest = []
    for file in sorted(root.rglob("*")):
        if file.is_symlink():
            raise ValueError(f"symlinks are not supported: {file.relative_to(root)}")
        if file.is_dir():
            continue
        if not file.is_file():
            raise ValueError(f"unsupported file type: {file.relative_to(root)}")
        relative = relative_path(file.relative_to(root).as_posix())
        with file.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        manifest.append({"path": relative, "size": file.stat().st_size, "sha256": digest})
    return manifest


class JobService:
    def __init__(self, clusters: dict[str, Cluster], state_dir=".hpc-mcp", transport=None,
                 transfer=None):
        self.clusters = clusters
        self.history = History(state_dir)
        self.transport = transport if transport is not None else SSHTransport()
        self.transfer = transfer if transfer is not None else Transfer()

    def _cluster(self, run: dict) -> Cluster:
        name = run["cluster"]
        if name not in self.clusters:
            raise ValueError(f"cluster no longer configured: {name}")
        cluster = self.clusters[name]
        snapshot = run["cluster_config"]
        if (cluster.ssh_host != snapshot["ssh_host"] or cluster.scheduler != snapshot["scheduler"]
                or cluster.work_root != snapshot["work_root"]
                or list(cluster.init_scripts) != snapshot["init_scripts"]):
            raise ValueError("cluster destination/environment changed since preparation; restore it")
        return cluster

    def _receipt_prefix(self, run: dict) -> str:
        # Records made before the rename have no prefix field; keep their remote protocol.
        prefix = run.get("internal_prefix", ".xn02")
        if prefix not in (".hpc-mcp", ".xn02"):
            raise ValueError("unsupported internal receipt prefix")
        return prefix

    def job_prepare(self, cluster: str, input_dir: str, script: str,
                    outputs: list[str] | None = None, output_mode: str | None = None,
                    output_exclude: list[str] | None = None, input_exclude: list[str] | None = None,
                    max_input_bytes: int | None = None) -> dict:
        if cluster not in self.clusters:
            raise ValueError(f"unknown cluster: {cluster}")
        config = self.clusters[cluster]
        source = Path(input_dir).expanduser().resolve()
        if not source.is_dir():
            raise ValueError("input_dir must be an existing directory")
        if source.is_relative_to(self.history.root):
            raise ValueError("input_dir cannot be inside the local state directory")
        script = relative_path(script)
        policy = replace(config, output_include=config.output_include if outputs is None else outputs,
            output_mode=("filtered" if outputs is not None else config.output_mode)
            if output_mode is None else output_mode,
            output_exclude=config.output_exclude if output_exclude is None else output_exclude,
            input_exclude=config.input_exclude if input_exclude is None else input_exclude,
            max_input_bytes=config.max_input_bytes if max_input_bytes is None else max_input_bytes)
        patterns = policy.output_include
        run_id = "r_" + uuid.uuid4().hex
        run_root = self.history.root / run_id
        staged = run_root / "input"
        staged.mkdir(parents=True)
        excluded = []

        def ignore(directory, names):
            skipped = []
            for name in names:
                candidate = Path(directory) / name
                relative = candidate.relative_to(source).as_posix()
                if (any(fnmatch.fnmatchcase(relative, p) or fnmatch.fnmatchcase(name, p)
                        for p in policy.input_exclude) or
                        candidate.resolve() == self.history.root):
                    skipped.append(name)
                    excluded.append(candidate.relative_to(source).as_posix())
                elif name.startswith((".hpc-mcp-", ".xn02-")):
                    raise ValueError(f"reserved internal filename: {name}")
                elif candidate.is_symlink():
                    raise ValueError(f"symlinks are not supported: {candidate.relative_to(source)}")
                elif not candidate.is_dir() and not candidate.is_file():
                    raise ValueError(f"unsupported input file: {candidate.relative_to(source)}")
                relative_path(candidate.relative_to(source).as_posix())
            return skipped

        try:
            shutil.copytree(source, staged, dirs_exist_ok=True, ignore=ignore)
            if not (staged / script).is_file():
                raise ValueError("script must be a regular included file within input_dir")
            text = (staged / script).read_text()
            if re.search(r"^\s*#SBATCH\s+.*(?:--array\b|-a\b)", text, re.MULTILINE) or \
                    re.search(r"^\s*#BSUB\s+-J\s+.*\[", text, re.MULTILINE):
                raise ValueError("job arrays are not supported in this phase")
            manifest = file_manifest(staged)
            # Bound preparation size; large-data support will use explicit remote inputs/caching.
            if sum(file["size"] for file in manifest) > policy.max_input_bytes:
                raise ValueError(f"input snapshot exceeds max_input_bytes={policy.max_input_bytes}")
            remote = config.work_root.rstrip("/") + "/" + run_id
            run = {
                "run_id": run_id, "cluster": cluster, "scheduler": config.scheduler,
                "cluster_config": {"ssh_host": config.ssh_host, "scheduler": config.scheduler,
                                   "work_root": config.work_root,
                                   "init_scripts": list(config.init_scripts)},
                "created_at": now(), "phase": "prepared", "state": "unknown",
                "internal_prefix": ".hpc-mcp",
                "job_id": None, "remote_dir": remote, "input_dir": str(source),
                "snapshot_dir": str(staged), "script": script, "outputs": patterns,
                "output_mode": policy.output_mode, "output_exclude": policy.output_exclude,
                "input_exclude": policy.input_exclude, "max_input_bytes": policy.max_input_bytes,
                "manifest": manifest, "excluded": excluded, "sync_state": "not_synced",
                "command": submit_command(config.scheduler, run_id, script, remote),
                "notes": ["作业名覆盖为 run_id，工作目录指定为执行目录，调度输出写入固定日志。",
                          "脚本保持原样；绝对路径与程序内部输出位置需要用户自行核对。",
                          "不执行输入脚本；prepare 只创建本地快照，submit 才上传和提交。"],
            }
            self.history.add(run)
            return {"ok": True, "run": run}
        except Exception:
            shutil.rmtree(run_root)
            raise

    def job_list(self, cluster: str | None = None, limit=50, offset=0) -> dict:
        runs = self.history.list(cluster, limit, offset)
        keys = ("run_id", "cluster", "scheduler", "created_at", "phase", "state", "job_id",
                "remote_dir", "sync_state")
        return {"ok": True, "runs": [{key: r[key] for key in keys} for r in runs]}

    def job_get(self, run_id: str) -> dict:
        return {"ok": True, "run": self.history.get(run_id),
                "events": self.history.events(run_id)}

    def job_submit(self, run_id: str) -> dict:
        with self.history.lock(run_id):
            run = self.history.get(run_id)
            cluster = self._cluster(run)
            prefix = self._receipt_prefix(run)
            if run["phase"] in ("submitted", "rejected"):
                return {"ok": run["phase"] == "submitted", "run": run,
                        "already_processed": True}
            if run["phase"] in ("submitting", "submission_unknown"):
                return self._recover(run, cluster)
            # Holding the OS lock proves any previous uploading process has exited.
            staged = Path(run["snapshot_dir"])
            if file_manifest(staged) != run["manifest"]:
                raise ValueError("prepared snapshot changed; prepare a new run")
            qremote = shlex.quote(run["remote_dir"])
            self.history.update(run_id, "upload_started", phase="uploading")
            initialize = self.transport.run(cluster,
                f"test -d {shlex.quote(cluster.work_root)}\n"
                f"test ! -L {qremote}\nmkdir -p -- {qremote}\n"
                f"test ! -e {qremote}/{prefix}-submit-lock")
            if not initialize.ok:
                return self._failure(run_id, "upload_failed", initialize)
            uploaded = self.transfer.run(cluster, staged, run["remote_dir"])
            if not uploaded.ok:
                return self._failure(run_id, "upload_failed", uploaded)
            checksum = "".join(f"{f['sha256']}  {f['path']}\n" for f in run["manifest"])
            # Checksum text is quoted as data, not evaluated by the remote shell.
            verification = self.transport.run(cluster,
                f"cd -- {qremote}\nprintf '%s' {shlex.quote(checksum)} > {prefix}-input.sha256\n"
                f"sha256sum --check {prefix}-input.sha256 >/dev/null")
            if not verification.ok:
                return self._failure(run_id, "upload_failed", verification)
            self.history.update(run_id, "submission_intent", phase="submitting")
            # Atomic remote lock prohibits retries even if the client loses the response.
            result = self.transport.run(cluster,
                f"cd -- {qremote}\nmkdir {prefix}-submit-lock\nset +e\n"
                f"{run['command']} > {prefix}-response.tmp 2> {prefix}-submit.stderr\n"
                f"hpc_mcp_submit_rc=$?\nmv {prefix}-response.tmp {prefix}-response\n"
                f"printf '%s\\n' \"$hpc_mcp_submit_rc\" > {prefix}-exit.tmp\n"
                f"mv {prefix}-exit.tmp {prefix}-exit\ncat {prefix}-response\n"
                f"cat {prefix}-submit.stderr >&2\nexit \"$hpc_mcp_submit_rc\"")
            if result.error is not None:
                return self._failure(run_id, "submission_unknown", result)
            return self._accept_response(run, result.returncode, result.stdout, result.stderr)

    def _failure(self, run_id, phase, result):
        run = self.history.update(run_id, phase, phase=phase, last_error=result.diagnostic())
        return {"ok": False, "run": run, "error": result.diagnostic()}

    def _accept_response(self, run: dict, returncode: int, stdout: str, stderr: str) -> dict:
        if returncode != 0:
            updated = self.history.update(run["run_id"], "submission_rejected", phase="rejected",
                submission_response={"returncode": returncode, "stdout": stdout, "stderr": stderr})
            return {"ok": False, "run": updated, "error": "scheduler rejected submission"}
        try:
            job_id = parse_job_id(run["scheduler"], stdout)
        except ValueError as exc:
            updated = self.history.update(run["run_id"], "submission_unknown",
                phase="submission_unknown", submission_response={"stdout": stdout, "stderr": stderr})
            return {"ok": False, "run": updated, "error": str(exc)}
        scheduler_cluster = None
        if run["scheduler"] == "slurm":
            match = re.search(r"^" + job_id + r"(?:;([A-Za-z0-9_.-]+))?$", stdout, re.MULTILINE)
            scheduler_cluster = match.group(1)
        updated = self.history.update(run["run_id"], "submitted", phase="submitted", job_id=job_id,
            scheduler_cluster=scheduler_cluster,
            state="pending", submission_response={"returncode": returncode, "stdout": stdout,
                                                  "stderr": stderr}, last_error=None)
        return {"ok": True, "run": updated}

    def job_recover(self, run_id: str) -> dict:
        with self.history.lock(run_id):
            run = self.history.get(run_id)
            return self._recover(run, self._cluster(run))

    def _recover(self, run: dict, cluster: Cluster) -> dict:
        if run["phase"] not in ("submitting", "submission_unknown"):
            return {"ok": True, "run": run, "message": "no ambiguous submission to recover"}
        prefix = self._receipt_prefix(run)
        result = self.transport.run(cluster,
            f"cd -- {shlex.quote(run['remote_dir'])}\n"
            f"test -f {prefix}-exit\ncat {prefix}-exit\ncat {prefix}-response\n"
            f"cat {prefix}-submit.stderr >&2")
        if not result.ok:
            return self._failure(run["run_id"], "submission_unknown", result)
        code, separator, output = result.stdout.partition("\n")
        if not separator or not re.fullmatch(r"[0-9]+", code):
            return {"ok": False, "run": run, "error": "invalid remote receipt; do not resubmit"}
        return self._accept_response(run, int(code), output, result.stderr)

    def job_status(self, run_id: str) -> dict:
        run = self.history.get(run_id)
        if not run["job_id"]:
            return {"ok": False, "run": run, "error": "no confirmed job ID; use job_recover"}
        cluster = self._cluster(run)
        diagnostics = []
        for index, command in enumerate(query_commands(run["scheduler"], run["job_id"],
                                                       run["created_at"], run.get("scheduler_cluster"))):
            result = self.transport.run(cluster, command)
            diagnostics.append({"command": command, **result.diagnostic()})
            if not result.ok:
                continue
            try:
                status = parse_status(run["scheduler"], result.stdout, run["job_id"], index > 0)
            except ValueError as exc:
                diagnostics[-1]["parse_error"] = str(exc)
                continue
            if status:
                updated = self.history.update(run_id, "status_observed", **status,
                    last_status_query=now(), status_query_ok=True, status_diagnostics=diagnostics)
                return {"ok": True, "run": updated}
        # Preserve the last observation; missing data is not evidence of success or failure.
        updated = self.history.update(run_id, "status_unavailable", last_status_query=now(),
                                      status_query_ok=False, status_diagnostics=diagnostics)
        return {"ok": False, "run": updated, "error": "job state unavailable; last state retained"}

    def job_cancel(self, run_id: str) -> dict:
        with self.history.lock(run_id):
            run = self.history.get(run_id)
            if not run["job_id"]:
                raise ValueError("cannot cancel without a confirmed job ID")
            cluster = self._cluster(run)
            verb = "scancel" if run["scheduler"] == "slurm" else "bkill"
            scope = cluster_scope(run.get("scheduler_cluster")) if run["scheduler"] == "slurm" else ""
            result = self.transport.run(cluster, f"{verb} {scope} {shlex.quote(run['job_id'])}")
            updated = self.history.update(run_id, "cancel_requested", cancel_requested=result.ok,
                                          cancel_response=result.diagnostic())
            return {"ok": result.ok, "run": updated}

    def job_logs(self, run_id: str, stream="stdout", lines=100) -> dict:
        if stream not in ("stdout", "stderr") or type(lines) is not int or not 1 <= lines <= 1000:
            raise ValueError("stream must be stdout/stderr; lines must be 1..1000")
        run = self.history.get(run_id)
        cluster = self._cluster(run)
        file = shlex.quote(run["remote_dir"] + "/" + stream + ".log")
        result = self.transport.run(cluster,
            f"test ! -L {file}\ntest -f {file}\ntail -n {lines} -- {file}")
        return {"ok": result.ok, "run_id": run_id, "stream": stream,
                "text": result.stdout if result.ok else "", "error": None if result.ok
                else result.diagnostic()}

    def job_sync(self, run_id: str, mode: str | None = None, includes: list[str] | None = None,
                 excludes: list[str] | None = None, destination: str | None = None,
                 layout: str | None = None, overwrite: str | None = None,
                 checksum: bool | None = None, compress: bool | None = None,
                 timeout: int | None = None) -> dict:
        with self.history.lock(run_id):
            run = self.history.get(run_id)
            if run["phase"] != "submitted":
                raise ValueError("sync requires a confirmed submitted job")
            cluster = self._cluster(run)
            policy = replace(cluster,
                output_mode=("filtered" if includes is not None else run.get("output_mode", "filtered"))
                if mode is None else mode,
                output_include=run["outputs"] if includes is None else includes,
                output_exclude=run.get("output_exclude", []) if excludes is None else excludes,
                sync_layout=cluster.sync_layout if layout is None else layout,
                sync_overwrite=cluster.sync_overwrite if overwrite is None else overwrite,
                transfer_checksum=cluster.transfer_checksum if checksum is None else checksum,
                transfer_compress=cluster.transfer_compress if compress is None else compress,
                transfer_timeout=cluster.transfer_timeout if timeout is None else timeout)
            attempt_id = uuid.uuid4().hex
            root = self.history.root / run_id
            if destination is not None:
                target = Path(destination).expanduser().resolve()
                source = Path(run["input_dir"])
                if (target == Path(target.anchor) or target.is_relative_to(source)
                        or source.is_relative_to(target)
                        or (target.is_relative_to(self.history.root) and target != root / "outputs")):
                    raise ValueError("destination overlaps input/state data or is a filesystem root")
            else:
                target = root / "outputs" if policy.sync_layout == "direct" \
                    else root / "outputs" / attempt_id
            if target.exists():
                if not target.is_dir() or policy.sync_overwrite == "error":
                    raise ValueError("destination exists; choose overwrite='replace' or 'merge'")
                file_manifest(target)  # Reject destination symlinks before writing into it.
            effective = {"mode": policy.output_mode, "includes": policy.output_include,
                         "excludes": policy.output_exclude, "destination": str(target),
                         "layout": policy.sync_layout, "overwrite": policy.sync_overwrite,
                         "checksum": policy.transfer_checksum, "compress": policy.transfer_compress,
                         "timeout": policy.transfer_timeout}
            # Query before marking output final; failed queries cannot confirm completion.
            status = self.job_status(run_id)
            run = self.history.get(run_id)
            attempt = root / "sync-attempts" / attempt_id
            attempt.mkdir(parents=True)
            self.history.update(run_id, "sync_started", sync_state="syncing", sync_options=effective)
            result = self.transfer.run(cluster, attempt, run["remote_dir"], download=True,
                patterns=policy.output_include, mode=policy.output_mode, excludes=policy.output_exclude,
                checksum=policy.transfer_checksum, compress=policy.transfer_compress,
                timeout=policy.transfer_timeout)
            if not result.ok:
                updated = self.history.update(run_id, "sync_failed", sync_state="failed",
                                              sync_error=result.diagnostic(), output_dir=str(attempt))
                return {"ok": False, "run": updated, "error": result.diagnostic()}
            files = file_manifest(attempt)
            backup = None
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() and policy.sync_overwrite == "replace":
                    backup = root / "sync-history" / attempt_id
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(target), str(backup))
                if target.exists():
                    shutil.copytree(attempt, target, dirs_exist_ok=True)
                    shutil.rmtree(attempt)
                else:
                    shutil.move(str(attempt), str(target))
            except OSError as exc:
                updated = self.history.update(run_id, "sync_install_failed", sync_state="failed",
                    sync_error=str(exc), output_dir=str(attempt), sync_backup=str(backup) if backup else None)
                return {"ok": False, "run": updated, "error": str(exc)}
            final = status["ok"] and run["state"] in TERMINAL
            updated = self.history.update(run_id, "synced", sync_state="complete" if final else "partial",
                output_dir=str(target), output_manifest=files, sync_error=None,
                sync_backup=str(backup) if backup else None)
            return {"ok": True, "run": updated, "final": final,
                    "warning": None if files else "no files matched the configured output patterns"}
