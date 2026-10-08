"""Single-job scheduler submission, status and cancellation."""

import re
import shlex
from datetime import datetime, timedelta


LSF_CANCEL_REASONS = {"TERM_OWNER", "TERM_FORCE_OWNER", "TERM_ADMIN", "TERM_FORCE_ADMIN",
                      "TERM_BUCKET_KILL"}


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
    # Include rotated logs; start a day early to accommodate the cluster's timezone.
    date = (datetime.fromisoformat(created_at) - timedelta(days=1)).strftime("%Y/%m/%d/00:00")
    interval = shlex.quote(date + ",")
    return [f"LC_ALL=C bjobs -a -noheader -o \"jobid stat exit_code exit_reason delimiter='|'\" {job_id}",
            f"LC_ALL=C bjobs -a -noheader -o \"jobid stat exit_code delimiter='|'\" {job_id}",
            f"LC_ALL=C bjobs -l {job_id}",
            f"LC_ALL=C bacct -l -S {interval} {job_id}",
            f"LC_ALL=C bhist -a -l -n 0 -S {interval} {job_id}"]


def cluster_scope(name: str | None) -> str:
    if name is None:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ValueError("invalid scheduler cluster name")
    return "--clusters=" + shlex.quote(name)


def lsf_detail(raw: str, exit_code: str | None = None, reason: str | None = None,
               signal: str | None = None) -> dict:
    reason = reason if reason and reason != "-" else None
    keywords = set(re.findall(r"\bTERM_[A-Z_]+\b", reason or ""))
    if len(keywords) > 1:
        raise ValueError("ambiguous LSF termination reasons")
    keyword = next(iter(keywords), None)
    state = normalized("lsf", raw)
    if raw == "EXIT" and keyword in LSF_CANCEL_REASONS:
        state = "cancelled"
    return {"raw_state": raw, "state": state, "exit_code": exit_code,
            "exit_reason": reason, "termination_reason": keyword, "signal": signal}


def parse_lsf_long(text: str, job_id: str, expected_name: str | None = None) -> dict | None:
    """Parse one identified job block, using timestamped scheduler lifecycle events."""
    headers = list(re.finditer(r"^\s*Job <([^>]+)>,", text, re.MULTILINE))
    blocks = [text[h.start():headers[i + 1].start() if i + 1 < len(headers) else len(text)]
              for i, h in enumerate(headers) if h.group(1) == job_id]
    if len(blocks) > 1:
        raise ValueError("multiple historical records for the requested job")
    if not blocks:
        if text.strip() and not headers:
            raise ValueError("unsupported LSF long response: no job identity")
        return None
    block = blocks[0]
    header = block.splitlines()[0]
    # Header attributes can wrap, but event text must never be treated as command/header data.
    timestamps = list(re.finditer(
        r"^\s*(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+[A-Z][a-z]{2}\s+\d{1,2}\s+"
        r"\d{2}:\d{2}:\d{2}(?:\s+\d{4})?:\s*", block, re.MULTILINE))
    if timestamps:
        header = " ".join(block[:timestamps[0].start()].split())
    header = header.split("Command <", 1)[0]
    name = re.search(r"Job Name <([^>]+)>", header)
    if expected_name and name and name.group(1) != expected_name:
        raise ValueError("historical job name does not match this run")
    current = re.search(r"\bStatus <([A-Z]+)>", header)
    detail = None
    for i, event in enumerate(timestamps):
        end = timestamps[i + 1].start() if i + 1 < len(timestamps) else len(block)
        message = " ".join(block[event.end():end].split())
        if re.match(r"Done successfully\b|Completed <done>", message, re.IGNORECASE):
            detail = lsf_detail("DONE", "0")
        elif re.match(r"Exited\b|Completed <exit>", message, re.IGNORECASE):
            code = re.search(r"Exited with exit code\s+(-?\d+)", message, re.IGNORECASE)
            signal = re.search(r"Exited by signal\s+(\d+)", message, re.IGNORECASE)
            # Reasons belong to the exit statement or the clause after Completed <exit>.
            statement = message.split(";", 1)
            evidence = statement[0] if message.lower().startswith("exited") else \
                (statement[1].split(";", 1)[0] if len(statement) > 1 else "")
            reason = re.search(r"\bTERM_[A-Z_]+\b.*?(?:\.|$)", evidence)
            previous = detail if detail and detail["raw_state"] == "EXIT" else {}
            detail = lsf_detail("EXIT", code.group(1) if code else previous.get("exit_code"),
                reason.group(0) if reason else previous.get("exit_reason"),
                signal.group(1) if signal else previous.get("signal"))
        elif re.match(r"Submitted\b|Requeued\b|Pending\b", message, re.IGNORECASE):
            detail = lsf_detail("PEND")
        elif re.match(r"Started\b|Starting\b|Dispatched\b|Running\b|Restarted\b", message, re.IGNORECASE):
            detail = lsf_detail("RUN")
    if current:
        raw = current.group(1)
        if detail is None or detail["raw_state"] != raw:
            detail = lsf_detail(raw, "0" if raw == "DONE" else None)
    return detail


def parse_status(scheduler: str, text: str, job_id: str, accounting=False,
                 source: str | None = None, expected_name: str | None = None) -> dict | None:
    if scheduler == "lsf" and source in ("bjobs_long", "bacct", "bhist"):
        return parse_lsf_long(text, job_id, expected_name)
    matches = []
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = [value.strip() for value in line.split("|", 3)]
        if len(fields) not in ((3, 4) if scheduler == "lsf" else (3,)):
            raise ValueError("malformed scheduler status response")
        if fields[0] != job_id:
            # Arrays, job steps and other jobs are not substitutes for this job.
            continue
        if not fields[1]:
            raise ValueError("missing job state")
        detail = {"raw_state": fields[1], "state": normalized(scheduler, fields[1])}
        if scheduler == "lsf":
            detail = lsf_detail(fields[1], fields[2], fields[3] if len(fields) == 4 else None)
        elif accounting:
            detail["exit_code"] = fields[2]
        else:
            detail["reason"] = fields[2]
        matches.append(detail)
    if len(matches) > 1:
        raise ValueError("multiple records for the requested job")
    return matches[0] if matches else None
