"""Single-job scheduler submission, status and cancellation."""

import re
import shlex


def submit_command(scheduler: str, run_id: str, script: str, remote_dir: str) -> str:
    # Keep a leading '-' in a filename from becoming a scheduler option.
    file = shlex.quote("./" + script)
    cwd = shlex.quote(remote_dir)
    stdout = shlex.quote(remote_dir + "/stdout.log")
    stderr = shlex.quote(remote_dir + "/stderr.log")
    if scheduler == "slurm":
        return (f"sbatch --parsable --job-name={run_id} --chdir={cwd} --output={stdout} "
                f"--error={stderr} {file}")
    return f"bsub -J {run_id} -cwd {cwd} -o {stdout} -e {stderr} < {file}"


def parse_job_id(scheduler: str, output: str) -> str:
    if scheduler == "slurm":
        matches = re.findall(r"^([0-9]+)(?:;[A-Za-z0-9_.-]+)?$", output, re.MULTILINE)
    else:
        matches = re.findall(r"Job <([0-9]+)> is submitted", output)
    if len(matches) != 1:
        raise ValueError("submission response must contain exactly one job ID")
    return matches[0]


def normalized(scheduler: str, raw: str) -> str:
    state = raw.split()[0].rstrip("+")
    maps = {
        "slurm": {
            "PENDING": "pending", "CONFIGURING": "pending", "RUNNING": "running",
            "COMPLETING": "running", "SUSPENDED": "suspended", "COMPLETED": "succeeded",
            "CANCELLED": "cancelled", "FAILED": "failed", "TIMEOUT": "failed",
            "OUT_OF_MEMORY": "failed", "NODE_FAIL": "failed", "PREEMPTED": "failed",
            "BOOT_FAIL": "failed", "DEADLINE": "failed",
        },
        "lsf": {"PEND": "pending", "RUN": "running", "DONE": "succeeded", "EXIT": "failed",
                "PSUSP": "suspended", "USUSP": "suspended", "SSUSP": "suspended"},
    }
    return maps[scheduler].get(state, "unknown")


def query_commands(scheduler: str, job_id: str, created_at: str,
                   scheduler_cluster: str | None = None) -> list[str]:
    if not re.fullmatch(r"[0-9]+", job_id):
        raise ValueError("invalid stored job ID")
    if scheduler == "slurm":
        date = shlex.quote(created_at[:10])
        scope = cluster_scope(scheduler_cluster)
        return [f"squeue {scope} --noheader --jobs={job_id} --format='%i|%T|%r'",
                f"sacct {scope} -X --noheader --parsable2 --jobs={job_id} --starttime={date} "
                "--format=JobIDRaw,State%40,ExitCode"]
    return [f"bjobs -a -noheader -o \"jobid stat exit_code delimiter='|'\" {job_id}"]


def cluster_scope(name: str | None) -> str:
    if name is None:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ValueError("invalid scheduler cluster name")
    return "--clusters=" + shlex.quote(name)


def parse_status(scheduler: str, text: str, job_id: str, accounting=False) -> dict | None:
    matches = []
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = [value.strip() for value in line.split("|")]
        if len(fields) != 3:
            raise ValueError("malformed scheduler status response")
        if fields[0] != job_id:
            # Arrays, job steps and other jobs are not substitutes for this job.
            continue
        if not fields[1]:
            raise ValueError("missing job state")
        detail = {"raw_state": fields[1], "state": normalized(scheduler, fields[1])}
        if scheduler == "lsf" or accounting:
            detail["exit_code"] = fields[2]
        else:
            detail["reason"] = fields[2]
        matches.append(detail)
    if len(matches) > 1:
        raise ValueError("multiple records for the requested job")
    return matches[0] if matches else None
