"""Deterministic structured batch scripts; commands are argv, never evaluated at preview."""

from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
import re
import shlex

from .jobs import relative_path


@dataclass(frozen=True)
class Resources:
    cpus: int = 1
    nodes: int | None = None
    tasks: int = 1
    tasks_per_node: int | None = None
    slurm_constraint: str | None = None
    slurm_gres: str | None = None
    slurm_gpus_per_task: str | None = None
    lsf_gpu: str | None = None
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
        for name in ("cpus", "tasks", "nodes", "tasks_per_node", "memory_mb", "time_minutes"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError(f"{name} must be a positive integer")
        if self.tasks is None:
            raise ValueError("tasks must be a positive integer")
        if self.tasks_per_node and self.tasks_per_node > self.tasks:
            raise ValueError("tasks_per_node cannot exceed tasks")
        for name in ("queue", "account", "qos"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", value)):
                raise ValueError(f"{name} must be a single scheduler identifier")
        for name in ("slurm_constraint", "slurm_gres", "slurm_gpus_per_task", "lsf_gpu", "lsf_resource_requirement"):
            value = getattr(self, name)
            if value is not None:
                line_value(value, name)
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
    allowed = {"command", "resources", "environment", "init_scripts", "setup_steps", "stdin", "stdout", "stderr", "output_directories", "launcher", "container", "scratch", "remote_dependencies"}
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
    if resources.memory_scope not in (None, "job", "per_node", "per_cpu", "lsf_reservation"):
        raise ValueError("memory_scope must be job, per_node, per_cpu or lsf_reservation")
    warnings = []
    directives = []
    if scheduler == "slurm":
        if resources.lsf_resource_requirement is not None or resources.memory_scope == "lsf_reservation":
            raise ValueError("LSF resource settings cannot be used with Slurm")
        nodes = resources.nodes or 1
        if nodes > resources.tasks:
            raise ValueError("nodes cannot exceed tasks")
        if resources.tasks_per_node and nodes * resources.tasks_per_node != resources.tasks:
            raise ValueError("Slurm nodes * tasks_per_node must equal tasks in the supported uniform layout")
        if nodes > 1 and resources.memory_scope == "job":
            raise ValueError("multi-node Slurm memory must use per_node or per_cpu scope")
        if resources.lsf_gpu is not None:
            raise ValueError("lsf_gpu requires LSF")
        if resources.slurm_gres and resources.slurm_gpus_per_task:
            raise ValueError("choose Slurm GRES or GPUs per task, not both")
        directives = [f"#SBATCH --nodes={nodes}", f"#SBATCH --ntasks={resources.tasks}", f"#SBATCH --cpus-per-task={resources.cpus}"]
        if resources.tasks_per_node:
            directives.append(f"#SBATCH --ntasks-per-node={resources.tasks_per_node}")
        for option, value, pattern in (("constraint", resources.slurm_constraint, r"[A-Za-z0-9_.&|!*()+-]+"),
                                      ("gres", resources.slurm_gres, r"gpu(?::[A-Za-z0-9_.-]+)?:[1-9][0-9]*"),
                                      ("gpus-per-task", resources.slurm_gpus_per_task, r"(?:[A-Za-z0-9_.-]+:)?[1-9][0-9]*")):
            if value is not None:
                if not isinstance(value, str) or not re.fullmatch(pattern, value):
                    raise ValueError(f"invalid Slurm {option}")
                directives.append(f"#SBATCH --{option}={value}")
        if resources.slurm_gpus_per_task and (not isinstance(spec.get("launcher"), dict) or spec["launcher"].get("kind") != "srun"):
            raise ValueError("Slurm GPUs per task require the supported srun launcher")
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
        if any(value is not None for value in (resources.slurm_constraint, resources.slurm_gres, resources.slurm_gpus_per_task)):
            raise ValueError("Slurm GPU/constraint settings require Slurm")
        if resources.nodes not in (None, 1):
            raise ValueError("LSF exact multi-node counts are unsupported; use tasks_per_node for slot packing")
        if resources.nodes == 1 and resources.tasks_per_node not in (None, resources.tasks):
            raise ValueError("single-node LSF requires all tasks on one host")
        single_host = resources.tasks == 1 or resources.nodes == 1 or resources.tasks_per_node == resources.tasks
        requirement = "span[hosts=1]" if single_host else (
            f"span[ptile={resources.tasks_per_node * resources.cpus}]" if resources.tasks_per_node else "")
        if resources.tasks > 1:
            warnings.append("LSF -n requests tasks * cpus slots; ptile is slot packing, not an exact node count. Site MPI rank/core binding must match this model.")
        if resources.lsf_gpu is not None:
            gpu = line_value(resources.lsf_gpu, "lsf_gpu")
            if not re.fullmatch(r"[A-Za-z0-9_./:=,-]+", gpu) or not re.search(r"(?:^|:)num=[1-9][0-9]*(?:/(?:task|host))?(?:$|:)", gpu):
                raise ValueError("lsf_gpu must be a native colon-separated request including num=N[/task|host]")
            if resources.lsf_resource_requirement and re.search(r"gpu|ngpus", resources.lsf_resource_requirement, re.I):
                raise ValueError("do not combine native lsf_gpu with GPU resource expressions")
            warnings.append("LSF GPU syntax and supported fields depend on site/version; the request is preserved without Slurm conversion.")
        if resources.memory_mb is not None:
            requirement += f" rusage[mem={resources.memory_mb}MB]"
            warnings.append("LSF rusage memory reservation scope depends on site policy; this is not a hard memory limit.")
        if resources.lsf_resource_requirement is not None:
            expression = line_value(resources.lsf_resource_requirement, "lsf_resource_requirement")
            if re.search(r"\bspan\s*\[", expression):
                raise ValueError("LSF span is generated from the task layout; do not override it in resource expressions")
            requirement += " " + expression
            warnings.append("Additional LSF resource expressions require target-site validation.")
        directives = [f"#BSUB -n {resources.tasks * resources.cpus}"]
        if requirement.strip():
            directives.append(f"#BSUB -R {shlex.quote(requirement.strip())}")
        if resources.lsf_gpu is not None:
            directives.append("#BSUB -gpu " + shlex.quote(resources.lsf_gpu))
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
    from .environment_setup import render_setup
    ordered_setup, step_environment = render_setup(spec.get('setup_steps', []))
    if ordered_setup and (spec.get('init_scripts') or environment):
        raise ValueError('setup_steps cannot be combined with init_scripts or environment; express the complete order as steps')
    omp = step_environment.get("OMP_NUM_THREADS", environment.get("OMP_NUM_THREADS"))
    if omp is not None and omp.isdecimal() and (int(omp) < 1 or int(omp) > resources.cpus):
        raise ValueError("OMP_NUM_THREADS cannot exceed allocated cpus per task")
    init_scripts = spec.get("init_scripts", [])
    if not isinstance(init_scripts, list):
        raise ValueError("init_scripts must be a list")
    sources = []
    for path in init_scripts:
        if not line_value(path, "init_script").startswith("/"):
            raise ValueError("compute init_scripts must use remote absolute paths")
        sources.append("source -- " + shlex.quote(path))
    from .execution import execution
    if step_environment and spec.get('container'):
        raise ValueError('container environment uses explicit environment/init_scripts; ordered export steps are unsupported')
    setup, invocation, execution_spec, execution_warnings, has_scratch = execution(spec, scheduler, resources, command)
    warnings.extend(execution_warnings)
    invocation = ("" if has_scratch else "exec -- ") + invocation
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
    requested = spec.get("output_directories", [])
    if not isinstance(requested, list):
        raise ValueError("output_directories must be a list of relative directories")
    parents = sorted(set(parents) | {relative_path(p) for p in requested})
    directories = ["mkdir -p -- " + shlex.join(parents)] if parents else []
    lines = ["#!/bin/bash", *directives, "", "set -euo pipefail", *sources, *exports, *ordered_setup,
             *directories, *setup, invocation, ""]
    return {"ok": True, "scheduler": scheduler, "script": "\n".join(lines),
            "spec": {"command": command, "resources": asdict(resources),
                     "environment": environment, "init_scripts": init_scripts, "setup_steps": spec.get('setup_steps', []), "output_directories": requested, **execution_spec, **paths},
            "warnings": warnings,
            "notes": ["cpus means CPUs per task; tasks means process count. Arrays are not generated. OpenMP environment is explicit.",
                      "Run identity, cwd and scheduler logs are assigned by job_submit.",
                      "Parent directories for declared stdout/stderr are created in the remote run."]}
