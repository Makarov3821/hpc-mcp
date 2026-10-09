"""Snapshot, submit, track and retrieve ordinary batch jobs."""

import hashlib
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import uuid
import fnmatch
import time
import os
from dataclasses import replace

from .config import Cluster
from .history import History, now
from .job_scheduler import cluster_scope, parse_job_id, parse_status, query_commands, submit_command
from .ssh import SSHTransport
from .transfer import Transfer

TERMINAL = {"succeeded", "failed", "cancelled"}


def relative_path(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("relative paths must be strings")
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or ".." in path.parts or
            any(c in value for c in "\x00\r\n\\") or value == "."):
        raise ValueError("file paths must be relative and cannot contain '..' or control characters")
    return path.as_posix()


def file_manifest(root: Path, hashes: bool = True) -> list[dict]:
    manifest = []
    for file in sorted(root.rglob("*")):
        if file.is_symlink():
            raise ValueError(f"symlinks are not supported: {file.relative_to(root)}")
        if file.is_dir():
            continue
        if not file.is_file():
            raise ValueError(f"unsupported file type: {file.relative_to(root)}")
        relative = relative_path(file.relative_to(root).as_posix())
        item = {"path": relative, "size": file.stat().st_size}
        if hashes:
            with file.open("rb") as stream:
                item["sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
        manifest.append(item)
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
                    max_input_bytes: int | None = None, project_root: str | None = None,
                    input_files: list[str] | None = None, generated_script: str | None = None,
                    template_context: dict | None = None,
                    input_overrides: dict[str, str] | None = None,
                    input_sources: dict[str, str] | None = None,
                    original_inputs: dict[str, str] | None = None,
                    application_context: dict | None = None,
                    generation_context: dict | None = None) -> dict:
        if cluster not in self.clusters:
            raise ValueError(f"unknown cluster: {cluster}")
        config = self.clusters[cluster]
        source = Path(input_dir).expanduser().resolve()
        if not source.is_dir():
            raise ValueError("input_dir must be an existing directory")
        if source.is_relative_to(self.history.root):
            raise ValueError("input_dir cannot be inside the local state directory")
        script = relative_path(script)
        if generated_script is not None:
            if not isinstance(generated_script, str) or not generated_script.startswith("#!/bin/bash\n"):
                raise ValueError("generated_script must be a Bash script")
            if (source / script).exists() or (source / script).is_symlink():
                raise ValueError("generated script path conflicts with an existing input")
        project = Path(project_root).expanduser().resolve() if project_root else None
        if project is not None:
            if not project.is_dir() or not source.is_relative_to(project):
                raise ValueError("input_dir must be within an existing project_root")
            if project.is_relative_to(self.history.root):
                raise ValueError("project_root cannot be inside the state directory")
            if outputs is None or not outputs or output_mode == "all":
                raise ValueError("project tasks require explicit filtered outputs")
        input_sources = input_sources or {}
        for name, origin in input_sources.items():
            relative_path(name)
            original = Path(origin)
            boundary = project or source
            if not original.is_file() or original.is_symlink() or not original.resolve().is_relative_to(boundary):
                raise ValueError("mapped input source must be a regular file within project_root")
            if any(parent.is_symlink() for parent in original.parents if parent.is_relative_to(boundary)):
                raise ValueError("mapped input source cannot follow symlinks")
            if name.startswith((".hpc-mcp-", ".xn02-")):
                raise ValueError("reserved mapped input filename")
        selected = None
        if input_files is not None:
            if not isinstance(input_files, list) or not input_files:
                raise ValueError("input_files must be a nonempty list of relative file paths")
            selected = {relative_path(p) for p in input_files} | {script}
            for path in selected:
                if path in input_sources or (path == script and generated_script is not None):
                    continue
                if not (source / path).is_file():
                    raise ValueError(f"selected input must be an existing file: {path}")
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
                        candidate.resolve() == self.history.root or name == ".hpc-mcp-sync" or
                        (selected is not None and relative not in selected and
                         not any(p.startswith(relative + "/") for p in selected))):
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
            for name, origin in input_sources.items():
                original_relative = Path(origin).resolve().relative_to(project or source).as_posix()
                for candidate in (name, original_relative):
                    if any(fnmatch.fnmatchcase(candidate, pattern) or
                           any(fnmatch.fnmatchcase(part, pattern) for part in PurePosixPath(candidate).parts)
                           for pattern in policy.input_exclude):
                        raise ValueError("mapped inputs conflict with input exclusions")
                if Path(origin).resolve().is_relative_to(self.history.root):
                    raise ValueError("mapped inputs cannot come from the state directory")
            planned_bytes = 0
            for directory, directories, names in os.walk(source, topdown=True, followlinks=False):
                skipped = set(ignore(directory, directories + names))
                directories[:] = [name for name in directories if name not in skipped]
                for name in names:
                    if name in skipped:
                        continue
                    path = Path(directory) / name
                    relative = path.relative_to(source).as_posix()
                    if relative in input_sources:
                        continue
                    planned_bytes += len(input_overrides[relative].encode()) if input_overrides and relative in input_overrides else path.stat().st_size
            planned_bytes += sum(Path(path).stat().st_size for path in input_sources.values())
            if generated_script is not None:
                planned_bytes += len(generated_script.encode())
            if planned_bytes > policy.max_input_bytes:
                raise ValueError(f"input snapshot exceeds max_input_bytes={policy.max_input_bytes}")
            copier = shutil.copy2
            if config.input_cache:
                from .storage import InputCache
                copier = InputCache(self.history).copy
            shutil.copytree(source, staged, dirs_exist_ok=True, ignore=ignore, copy_function=copier)
            for name, origin in input_sources.items():
                destination = staged / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                copier(origin, destination)
            for name, content in (input_overrides or {}).items():
                name = relative_path(name)
                if not (staged / name).is_file() or not isinstance(content, str):
                    raise ValueError("input overrides must replace included text files")
                (staged / name).chmod(0o600)
                (staged / name).write_text(content)
            if generated_script is not None:
                destination = staged / script
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(generated_script)
            if not (staged / script).is_file():
                raise ValueError("script must be a regular included file within input_dir")
            text = (staged / script).read_text()
            if re.search(r"^\s*#SBATCH\s+.*(?:--array\b|-a\b)", text, re.MULTILINE) or \
                    re.search(r"^\s*#BSUB\s+-J\s+.*\[", text, re.MULTILINE):
                raise ValueError("job arrays are not supported in this phase")
            manifest = file_manifest(staged)
            if selected is not None and selected != {f["path"] for f in manifest}:
                raise ValueError("selected inputs conflict with input exclusions")
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
                "project_root": str(project) if project else None,
                "input_files": sorted(selected) if selected is not None else None,
                "input_sources": input_sources,
                "output_mode": policy.output_mode, "output_exclude": policy.output_exclude,
                "input_exclude": policy.input_exclude, "max_input_bytes": policy.max_input_bytes,
                "manifest": manifest, "excluded": sorted(set(excluded)), "sync_state": "not_synced",
                "command": submit_command(config.scheduler, run_id, script, remote),
                "notes": ["作业名覆盖为 run_id，工作目录指定为执行目录，调度输出写入固定日志。",
                          "脚本保持原样；绝对路径与程序内部输出位置需要用户自行核对。",
                          "不执行输入脚本；prepare 只创建本地快照，submit 才上传和提交。"],
            }
            if generated_script is not None:
                run["generated_script"] = generated_script
            if generation_context is not None:
                run["generation"] = generation_context
            if template_context is not None:
                run["template"] = template_context
            if original_inputs:
                originals = run_root / "original-input"
                originals.mkdir()
                for name, content in original_inputs.items():
                    name = relative_path(name)
                    if not isinstance(content, str):
                        raise ValueError("original input snapshots must be text")
                    destination = originals / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(content)
                run["original_manifest"] = file_manifest(originals)
            if application_context is not None:
                run["application"] = dict(application_context)
                if original_inputs:
                    run["application"]["original_snapshot"] = str(run_root / "original-input" / application_context["input"])
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

    def job_submit(self, run_id: str, _workflow_token: str | None = None) -> dict:
        with self.history.lock(run_id):
            from .workflow import submission_guard
            submission_guard(self.history, run_id, _workflow_token)
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
        candidate = None
        for index, command in enumerate(query_commands(run["scheduler"], run["job_id"],
                                                       run["created_at"], run.get("scheduler_cluster"))):
            source = ("bjobs", "bjobs", "bjobs_long", "bacct", "bhist")[index] \
                if run["scheduler"] == "lsf" else ("squeue", "sacct")[index]
            result = self.transport.run(cluster, command)
            diagnostics.append({"command": command, **result.diagnostic()})
            if not result.ok:
                continue
            try:
                status = parse_status(run["scheduler"], result.stdout, run["job_id"], index > 0,
                                      source, run["run_id"])
            except ValueError as exc:
                diagnostics[-1]["parse_error"] = str(exc)
                continue
            if status and status["state"] != "unknown":
                status["status_source"] = source
                if candidate and status["raw_state"] != candidate["raw_state"]:
                    diagnostics[-1]["ignored"] = "conflicts with earlier scheduler observation"
                    continue
                if candidate and candidate.get("exit_code") not in (None, "", "-") and \
                        status.get("exit_code") not in (None, "", "-", candidate["exit_code"]):
                    diagnostics[-1]["ignored"] = "exit code conflicts with earlier scheduler observation"
                    continue
                if candidate:
                    for key in ("exit_code", "exit_reason", "termination_reason", "signal"):
                        if status.get(key) is None:
                            status[key] = candidate.get(key)
                candidate = status
                # EXIT alone is inconclusive about who terminated the job. Enrich it,
                # retaining the observation even if all reason/history queries fail.
                if run["scheduler"] != "lsf" or status["raw_state"] != "EXIT" or \
                        status.get("termination_reason"):
                    break
        if candidate:
            updated = self.history.update(run_id, "status_observed", **candidate,
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
                 timeout: int | None = None, resume: bool | None = None,
                 max_file_bytes: int | None = None, max_total_bytes: int | None = None,
                 reserve_bytes: int | None = None, stable_only: bool = False,
                 _operation_id: str | None = None) -> dict:
        from .sync_support import preview, positive
        if type(stable_only) is not bool or (resume is not None and type(resume) is not bool):
            raise ValueError("stable_only/resume must be boolean")
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
            resume = cluster.sync_resume if resume is None else resume
            max_file_bytes = positive(cluster.max_output_file_bytes if max_file_bytes is None else max_file_bytes, "max_file_bytes")
            max_total_bytes = positive(cluster.max_output_bytes if max_total_bytes is None else max_total_bytes, "max_total_bytes")
            reserve_bytes = positive(cluster.sync_reserve_bytes if reserve_bytes is None else reserve_bytes, "reserve_bytes", True)
            attempt_id = uuid.uuid4().hex
            root = self.history.root / run_id
            project = Path(run["project_root"]) if run.get("project_root") else None
            inplace = project is not None and (destination is None or
                Path(destination).expanduser().resolve() == Path(run["input_dir"]))
            if inplace:
                target = Path(run["input_dir"])
                if policy.output_mode != "filtered" or policy.sync_overwrite == "replace":
                    raise ValueError("return to input_dir requires filtered mode and error/merge overwrite")
            elif destination is not None:
                target = Path(destination).expanduser().resolve()
                source = Path(run["input_dir"])
                if (target == Path(target.anchor) or target.is_relative_to(source)
                        or source.is_relative_to(target)
                        or (target.is_relative_to(self.history.root) and target != root / "outputs")):
                    raise ValueError("destination overlaps input/state data or is a filesystem root")
            else:
                target = root / "outputs" if policy.sync_layout == "direct" \
                    else root / "outputs" / attempt_id
            if destination is None and not inplace and ((root / "outputs").is_symlink() or not target.resolve().is_relative_to(root)):
                raise ValueError("default output directory escaped run storage")
            if target.exists() and not inplace:
                if not target.is_dir() or policy.sync_overwrite == "error":
                    raise ValueError("destination exists; choose overwrite='replace' or 'merge'")
                file_manifest(target)  # Reject destination symlinks before writing into it.
            effective = {"mode": policy.output_mode, "includes": policy.output_include,
                         "excludes": policy.output_exclude, "destination": str(target),
                         "layout": "direct" if inplace else policy.sync_layout,
                         "overwrite": policy.sync_overwrite,
                         "checksum": policy.transfer_checksum, "compress": policy.transfer_compress,
                         "timeout": policy.transfer_timeout}
            # Query before marking output final; failed queries cannot confirm completion.
            status = self.job_status(run_id)
            run = self.history.get(run_id)
            cache = project / ".hpc-mcp-sync" / run_id if project else root / "sync-attempts"
            if project:
                self._check_project_cache(project, cache)
            if stable_only and (not status["ok"] or run["state"] not in TERMINAL):
                raise ValueError("stable_only requires a fresh confirmed terminal state")
            selection = preview(self, run, cluster, policy, project or root, max_file_bytes, max_total_bytes, reserve_bytes)
            if not selection["ok"]:
                return {"ok": False, "run": run, "preview": selection, "error": "sync preflight failed"}
            # Check space on both staging and final destination filesystems.
            destination_parent = target
            while not destination_parent.exists():
                destination_parent = destination_parent.parent
            if destination and shutil.disk_usage(destination_parent).free < selection["required_free_bytes"]:
                raise ValueError("insufficient free space at destination")
            effective.update(resume=resume, max_file_bytes=max_file_bytes, max_total_bytes=max_total_bytes,
                             reserve_bytes=reserve_bytes, stable_only=stable_only)
            attempt = cache / attempt_id
            previous = run.get("sync_options", {})
            previous_path = Path(previous.get("staging_dir", "/"))
            previous_target = Path(previous.get("destination", "/"))
            same_without_destination = {k: v for k, v in previous.items() if k not in ("staging_dir", "destination")} == {k: v for k, v in effective.items() if k != "destination"}
            if resume and destination is None and not inplace and policy.sync_layout == "snapshot" \
                    and run.get("sync_state") in ("failed", "syncing") and same_without_destination \
                    and run.get("sync_selection", {}).get("fingerprint") == selection["fingerprint"] \
                    and previous_target.parent == root / "outputs" and re.fullmatch(r"[0-9a-f]{32}", previous_target.name) \
                    and not previous_target.exists() and not previous_target.is_symlink() \
                    and previous_target.resolve().is_relative_to(root):
                target = previous_target
                effective["destination"] = str(target)
            same_options = {k: v for k, v in previous.items() if k != "staging_dir"} == effective
            reused = False
            if resume and run.get("sync_state") in ("failed", "syncing") and same_options \
                    and run.get("sync_selection", {}).get("fingerprint") == selection["fingerprint"] \
                    and previous_path.parent == cache and re.fullmatch(r"[0-9a-f]{32}", previous_path.name) \
                    and previous_path.is_dir() and not previous_path.is_symlink():
                for entry in previous_path.rglob("*"):
                    if entry.is_symlink() or (not entry.is_file() and not entry.is_dir()):
                        raise ValueError("reusable staging contains unsafe entries")
                attempt = previous_path
                reused = True
            attempt.mkdir(parents=True, exist_ok=reused)
            effective["staging_dir"] = str(attempt)
            self.history.update(run_id, "sync_started", sync_state="syncing", sync_options=effective,
                                sync_selection=selection, sync_resumed=reused)
            last_progress = [0.0]
            def progress(detail):
                if time.monotonic() - last_progress[0] >= 1:
                    last_progress[0] = time.monotonic()
                    self.history.update(run_id, "sync_progress", sync_progress=detail)
                    if _operation_id:
                        from .operations import operation_update
                        operation_update(self.history, _operation_id, progress=detail)
            result = self.transfer.run(cluster, attempt, run["remote_dir"], download=True,
                patterns=policy.output_include, mode=policy.output_mode, excludes=policy.output_exclude,
                checksum=policy.transfer_checksum, compress=policy.transfer_compress,
                timeout=policy.transfer_timeout, resume=resume,
                max_file_bytes=max_file_bytes, max_total_bytes=max_total_bytes, progress=progress)
            if not result.ok:
                updated = self.history.update(run_id, "sync_failed", sync_state="failed",
                                              sync_error=result.diagnostic(), output_dir=str(attempt))
                return {"ok": False, "run": updated, "error": result.diagnostic()}
            # rsync partial files are private state, never install them as user outputs.
            for partial in list(attempt.rglob(".hpc-mcp-transfer-partial")):
                if partial.is_symlink():
                    raise ValueError("partial cache cannot contain symlinks")
                if partial.is_dir():
                    shutil.rmtree(partial)
            files = file_manifest(attempt)
            status = self.job_status(run_id)
            run = self.history.get(run_id)
            if stable_only and (not status["ok"] or run["state"] not in TERMINAL):
                updated = self.history.update(run_id, "sync_terminal_unverified", sync_state="failed",
                    output_dir=str(attempt), sync_error="terminal state no longer confirmed; retry after inspection")
                return {"ok": False, "run": updated, "error": updated["sync_error"]}
            after = preview(self, run, cluster, policy, project or root, max_file_bytes, max_total_bytes, 0, check_space=False)
            expected = [(f["path"], f["size"]) for f in selection["files"]]
            actual = [(f["path"], f["size"]) for f in files]
            if not after["ok"] or after["fingerprint"] != selection["fingerprint"] or actual != expected:
                updated = self.history.update(run_id, "sync_source_changed", sync_state="failed",
                    output_dir=str(attempt), sync_error="source changed or incomplete transfer; preview and retry")
                return {"ok": False, "run": updated, "error": updated["sync_error"]}
            backup = None
            try:
                if inplace:
                    protected = {f["path"] for f in run["manifest"]}
                    for file in files:
                        relative = file["path"]
                        dest = target / relative
                        if relative in protected or dest.is_symlink() or not dest.resolve().is_relative_to(target):
                            raise ValueError(f"output would overwrite an input or follow a symlink: {relative}")
                        if dest.exists() and (not dest.is_file() or policy.sync_overwrite == "error"):
                            raise ValueError(f"output exists: {relative}; choose overwrite='merge'")
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
            except (OSError, ValueError) as exc:
                updated = self.history.update(run_id, "sync_install_failed", sync_state="failed",
                    sync_error=str(exc), output_dir=str(attempt), sync_backup=str(backup) if backup else None)
                return {"ok": False, "run": updated, "error": str(exc)}
            if project:
                self._prune_cache_parents(cache)
            transfer_stats = {}
            for label, key in (("Literal data", "transferred_bytes"), ("Matched data", "reused_bytes")):
                match = re.search(re.escape(label) + r": ([0-9,]+) bytes", result.stdout)
                if match:
                    transfer_stats[key] = int(match[1].replace(",", ""))
            final = status["ok"] and run["state"] in TERMINAL
            updated = self.history.update(run_id, "synced", sync_state="complete" if final else "partial",
                output_dir=str(target), output_manifest=files, sync_error=None,
                sync_progress={"installed_bytes": sum(f["size"] for f in files), "files": len(files)},
                sync_transfer_stats=transfer_stats,
                sync_backup=str(backup) if backup else None)
            return {"ok": True, "run": updated, "final": final, "resumed": reused,
                    "warning": None if files else "no files matched the configured output patterns"}

    def job_sync_preview(self, run_id: str, mode: str | None = None,
                         includes: list[str] | None = None, excludes: list[str] | None = None,
                         max_file_bytes: int | None = None, max_total_bytes: int | None = None,
                         reserve_bytes: int | None = None, timeout: int | None = None) -> dict:
        from .sync_support import preview, positive
        with self.history.lock(run_id):
            run = self.history.get(run_id)
            if run["phase"] != "submitted":
                raise ValueError("preview requires a confirmed submitted run")
            cluster = self._cluster(run)
            policy = replace(cluster, output_mode=("filtered" if includes is not None else run.get("output_mode", "filtered")) if mode is None else mode,
                             output_include=run["outputs"] if includes is None else includes,
                             output_exclude=run.get("output_exclude", []) if excludes is None else excludes,
                             transfer_timeout=cluster.transfer_timeout if timeout is None else timeout)
            result = preview(self, run, cluster, policy,
                Path(run["project_root"]) if run.get("project_root") else self.history.root / run_id,
                positive(cluster.max_output_file_bytes if max_file_bytes is None else max_file_bytes, "max_file_bytes"),
                positive(cluster.max_output_bytes if max_total_bytes is None else max_total_bytes, "max_total_bytes"),
                positive(cluster.sync_reserve_bytes if reserve_bytes is None else reserve_bytes, "reserve_bytes", True))
            return {**result, "run_id": run_id, "scheduler_state": run["state"],
                    "notes": ["Read-only remote selection; free space is a preflight estimate, not a filesystem quota.",
                              "Running outputs may change. Sync rechecks selection before installing."]}

    @staticmethod
    def _check_project_cache(project: Path, cache: Path):
        if not project.is_dir() or project.resolve() != project:
            raise ValueError("project_root moved or became a symlink; restore its original path")
        for path in (project / ".hpc-mcp-sync", cache):
            if path.is_symlink() or not path.resolve().is_relative_to(project.resolve()):
                raise ValueError("project sync cache cannot contain symlinks or escape project_root")

    @staticmethod
    def _prune_cache_parents(cache: Path):
        for path in (cache, cache.parent):
            try:
                path.rmdir()
            except OSError:
                break

    def job_cache_cleanup(self, run_id: str, older_than_seconds: int = 86400,
                          dry_run: bool = True) -> dict:
        """Remove inactive download attempts only; never inputs, results or remote data."""
        if type(older_than_seconds) is not int or older_than_seconds < 0 or type(dry_run) is not bool:
            raise ValueError("older_than_seconds must be nonnegative; dry_run must be boolean")
        with self.history.lock(run_id):
            run = self.history.get(run_id)
            project = Path(run["project_root"]) if run.get("project_root") else None
            cache = project / ".hpc-mcp-sync" / run_id if project else \
                self.history.root / run_id / "sync-attempts"
            if project:
                self._check_project_cache(project, cache)
            if cache.is_symlink():
                raise ValueError("sync cache cannot be a symlink")
            candidates = []
            for path in sorted(cache.iterdir()) if cache.exists() else []:
                if path.is_symlink() or not path.is_dir() or not re.fullmatch(r"[0-9a-f]{32}", path.name):
                    continue
                files = file_manifest(path, hashes=False)
                newest = max([path.stat().st_mtime, *(p.stat().st_mtime for p in path.rglob("*"))])
                if time.time() - newest >= older_than_seconds:
                    candidates.append({"path": str(path), "bytes": sum(f["size"] for f in files)})
            if not dry_run:
                for candidate in candidates:
                    shutil.rmtree(candidate["path"])
                changes = {"last_cache_cleanup": candidates}
                if run.get("output_dir") in {c["path"] for c in candidates}:
                    changes["output_dir"] = None
                self.history.update(run_id, "cache_cleaned", **changes)
                if project:
                    self._prune_cache_parents(cache)
            return {"ok": True, "run_id": run_id, "dry_run": dry_run, "candidates": candidates,
                    "bytes": sum(c["bytes"] for c in candidates)}

    def job_sync_start(self, run_id: str, options: dict | None = None) -> dict:
        from .operations import start
        return start(self, run_id, options)

    def job_sync_operation(self, operation_id: str) -> dict:
        from .operations import inspect
        return inspect(self, operation_id)

    def job_storage_cleanup(self, run_id: str, categories: list[str] | None = None,
                            older_than_seconds: int = 86400, dry_run: bool = True) -> dict:
        from .storage import local_cleanup
        return local_cleanup(self, run_id, categories, older_than_seconds, dry_run)

    def input_cache_cleanup(self, older_than_seconds: int = 86400,
                            max_cache_bytes: int = 10 * 1024 ** 3, dry_run: bool = True) -> dict:
        from .storage import InputCache
        result = InputCache(self.history).cleanup(older_than_seconds, max_cache_bytes, dry_run)
        if not dry_run:
            import json
            with self.history.connect() as db:
                db.execute("INSERT INTO events(run_id,time,kind,detail) VALUES (?,?,?,?)",
                           ("maintenance", now(), "input_cache_cleaned", json.dumps(result)))
        return result

    def job_remote_cleanup(self, run_id: str, dry_run: bool = True) -> dict:
        if type(dry_run) is not bool:
            raise ValueError("dry_run must be boolean")
        with self.history.lock(run_id):
            run = self.history.get(run_id)
            if run.get("remote_removed"):
                return {"ok": True, "already_removed": True, "run_id": run_id}
            cluster = self._cluster(run)
            if run["phase"] != "submitted" or run.get("sync_state") != "complete":
                raise ValueError("remote cleanup requires submitted run and complete selected-output sync")
            status = self.job_status(run_id)
            if not status["ok"] or status["run"]["state"] not in TERMINAL:
                raise ValueError("remote cleanup requires a fresh terminal scheduler state")
            run = self.history.get(run_id)
            if not re.fullmatch(r"r_[0-9a-f]{32}", run_id) or run["remote_dir"] != cluster.work_root.rstrip("/") + "/" + run_id:
                raise ValueError("remote cleanup path does not match registered run")
            output = Path(run["output_dir"])
            for item in run.get("output_manifest", []):
                path = output / item["path"]
                if path.is_symlink() or not path.resolve().is_relative_to(output.resolve()) or not path.is_file():
                    raise ValueError("synced result missing or unsafe; resync before remote cleanup")
                with path.open("rb") as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() != item["sha256"]:
                        raise ValueError("synced result changed; resync before remote cleanup")
            if not run.get("output_manifest"):
                raise ValueError("remote cleanup requires nonempty verified downloaded outputs")
            directory = shlex.quote(run["remote_dir"])
            base = shlex.quote(cluster.work_root)
            prefix = self._receipt_prefix(run)
            receipt = run.get("submission_response", {}).get("stdout", "")
            digest = hashlib.sha256(receipt.encode()).hexdigest()
            validation = (f"test -d {directory}\ntest ! -L {directory}\n"
                f"hpc_base=$(realpath -e -- {base})\nhpc_dir=$(realpath -e -- {directory})\n"
                f'test "$hpc_dir" = "$hpc_base/{run_id}"\n'
                f"cd -- {directory}\ntest -f {prefix}-exit\ntest ! -L {prefix}-response\n"
                f'test "$(cat {prefix}-exit)" = 0\n'
                f'test "$(sha256sum {prefix}-response | cut -c 1-64)" = {digest}\n')
            command = validation + (f"du -sb -- {directory}" if dry_run else
                                    f"cd -- /\nrm -rf --one-file-system -- {directory}")
            result = self.transport.run(cluster, command)
            if not dry_run:
                self.history.update(run_id, "remote_cleaned" if result.ok else "remote_cleanup_failed",
                                    remote_removed=result.ok, remote_cleanup_diagnostic=result.diagnostic())
            return {"ok": result.ok, "run_id": run_id, "dry_run": dry_run,
                    "remote_dir": run["remote_dir"], "diagnostic": result.diagnostic(),
                    "notes": ["Deletes only this registered run directory; original cluster inputs are untouched.",
                              "Only selected downloaded outputs are retained locally; unselected remote files will be lost."]}
