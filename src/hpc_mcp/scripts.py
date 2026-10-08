"""Deterministic single-node batch scripts; commands are argv, never evaluated at preview."""

from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
import re
import shlex

from .jobs import relative_path


@dataclass(frozen=True)
class Resources:
    cpus: int = 1
    queue: str | None = None
    memory_mb: int | None = None
    memory_scope: str | None = None
    time_minutes: int | None = None
    account: str | None = None
    qos: str | None = None
    lsf_resource_requirement: str | None = None

    def __post_init__(self):
        if type(self.cpus) is not int or self.cpus < 1:
            raise ValueError("cpus must be a positive integer")
        for name in ("cpus", "memory_mb", "time_minutes"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError(f"{name} must be a positive integer")
        for name in ("queue", "account", "qos"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", value)):
                raise ValueError(f"{name} must be a single scheduler identifier")
        if (self.memory_mb is None) != (self.memory_scope is None):
            raise ValueError("memory_mb and memory_scope must be specified together")


def line_value(value, label):
    if not isinstance(value, str) or not value or any(c in value for c in "\x00\r\n"):
        raise ValueError(f"{label} must be a nonempty string without NUL or newlines")
    return value


def script_generate(scheduler: str, spec: dict) -> dict:
    """Return a script and effective settings without reading or writing input files."""
    if scheduler not in ("lsf", "slurm"):
        raise ValueError("scheduler must be lsf or slurm")
    allowed = {"command", "resources", "environment", "init_scripts", "stdin", "stdout", "stderr"}
    if not isinstance(spec, dict) or set(spec) - allowed:
        raise ValueError("unknown script spec settings")
    command = spec.get("command")
    if not isinstance(command, list) or not command:
        raise ValueError("command must be a nonempty argv list")
    for argument in command:
        line_value(argument, "command argument")
    if not isinstance(spec.get("resources", {}), dict):
        raise ValueError("resources must be an object")
    try:
        resources = Resources(**spec.get("resources", {}))
    except TypeError as exc:
        raise ValueError(str(exc)) from exc
    if resources.memory_scope not in (None, "job", "per_cpu", "lsf_reservation"):
        raise ValueError("memory_scope must be job, per_cpu or lsf_reservation")
    warnings = []
    directives = []
    if scheduler == "slurm":
        if resources.lsf_resource_requirement is not None or resources.memory_scope == "lsf_reservation":
            raise ValueError("LSF resource settings cannot be used with Slurm")
        directives = ["#SBATCH --nodes=1", "#SBATCH --ntasks=1", f"#SBATCH --cpus-per-task={resources.cpus}"]
        for key, value in (("partition", resources.queue), ("account", resources.account), ("qos", resources.qos)):
            if value is not None:
                directives.append(f"#SBATCH --{key}={value}")
        if resources.memory_mb is not None:
            option = "mem-per-cpu" if resources.memory_scope == "per_cpu" else "mem"
            directives.append(f"#SBATCH --{option}={resources.memory_mb}M")
        if resources.time_minutes is not None:
            days, minutes = divmod(resources.time_minutes, 1440)
            hours, minutes = divmod(minutes, 60)
            duration = (f"{days}-" if days else "") + f"{hours:02d}:{minutes:02d}:00"
            directives.append(f"#SBATCH --time={duration}")
    else:
        if resources.account is not None or resources.qos is not None:
            raise ValueError("account and qos are Slurm-only settings")
        if resources.memory_mb is not None and resources.memory_scope != "lsf_reservation":
            raise ValueError("LSF memory requires memory_scope='lsf_reservation'; site policy sets its scope")
        requirement = "span[hosts=1]"
        if resources.memory_mb is not None:
            requirement += f" rusage[mem={resources.memory_mb}MB]"
            warnings.append("LSF rusage memory reservation scope depends on site policy; this is not a hard memory limit.")
        if resources.lsf_resource_requirement is not None:
            requirement += " " + line_value(resources.lsf_resource_requirement, "lsf_resource_requirement")
            warnings.append("Additional LSF resource expressions require target-site validation.")
        directives = [f"#BSUB -n {resources.cpus}", f"#BSUB -R {shlex.quote(requirement)}"]
        if resources.queue is not None:
            directives.append(f"#BSUB -q {resources.queue}")
        if resources.time_minutes is not None:
            hours, minutes = divmod(resources.time_minutes, 60)
            directives.append(f"#BSUB -W {hours}:{minutes:02d}")
    environment = spec.get("environment", {})
    if not isinstance(environment, dict):
        raise ValueError("environment must be an object")
    exports = []
    for name, value in environment.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError("invalid environment variable name")
        exports.append(f"export {name}={shlex.quote(line_value(value, 'environment value'))}")
    init_scripts = spec.get("init_scripts", [])
    if not isinstance(init_scripts, list):
        raise ValueError("init_scripts must be a list")
    sources = []
    for path in init_scripts:
        if not line_value(path, "init_script").startswith("/"):
            raise ValueError("compute init_scripts must use remote absolute paths")
        sources.append("source -- " + shlex.quote(path))
    invocation = "exec -- " + shlex.join(command)
    paths = {}
    for name, operator in (("stdin", "<"), ("stdout", ">"), ("stderr", "2>")):
        if spec.get(name) is not None:
            paths[name] = relative_path(line_value(spec[name], name))
            invocation += f" {operator} {shlex.quote(paths[name])}"
    if paths.get("stdin") in [paths.get("stdout"), paths.get("stderr")] and "stdin" in paths:
        raise ValueError("stdout/stderr cannot overwrite stdin")
    if "stdout" in paths and paths.get("stderr") == paths["stdout"]:
        raise ValueError("stdout and stderr must use distinct paths")
    # No trailing cleanup command can hide the program's exit status.
    parents = sorted({str(PurePosixPath(paths[key]).parent) for key in ("stdout", "stderr")
                      if key in paths and str(PurePosixPath(paths[key]).parent) != "."})
    directories = ["mkdir -p -- " + shlex.join(parents)] if parents else []
    lines = ["#!/bin/bash", *directives, "", "set -euo pipefail", *sources, *exports,
             *directories, invocation, ""]
    return {"ok": True, "scheduler": scheduler, "script": "\n".join(lines),
            "spec": {"command": command, "resources": asdict(resources),
                     "environment": environment, "init_scripts": init_scripts, **paths},
            "warnings": warnings,
            "notes": ["Single node, single task; cpus are shared-memory slots. No MPI/GPU/array generation.",
                      "Run identity, cwd and scheduler logs are assigned by job_submit.",
                      "Parent directories for declared stdout/stderr are created in the remote run."]}
