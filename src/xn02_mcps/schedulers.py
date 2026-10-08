"""Scheduler queries and strict parsers; no job submission."""

SLURM_QUERY = "sinfo --noheader --exact --format='%P|%a|%l|%D|%t|%c|%m|%G|%N'"
LSF_QUERY = "bqueues -w"
COMMANDS = {
    "slurm": ("sbatch", "squeue", "sinfo", "scancel", "sacct"),
    "lsf": ("bsub", "bjobs", "bqueues", "bkill", "bhist", "bacct"),
}


def number(value: str) -> int | None:
    if value.lower() in ("-", "n/a", "(null)"):
        return None
    return int(value)


def parse_lsf(text: str) -> list[dict]:
    lines = [line.split() for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("bqueues returned no header; cannot confirm an empty queue list")
    required = {"QUEUE_NAME", "PRIO", "STATUS", "MAX", "JL/U", "JL/P", "JL/H",
                "NJOBS", "PEND", "RUN", "SUSP"}
    columns = lines[0]
    if len(columns) != len(set(columns)):
        raise ValueError("duplicate bqueues columns")
    missing = required - set(columns)
    if missing:
        raise ValueError(f"bqueues header missing required columns: {', '.join(sorted(missing))}")
    queues = []
    for row in lines[1:]:
        if len(row) != len(columns):
            raise ValueError("malformed bqueues row")
        values = dict(zip(columns, row, strict=True))
        queue = {
            "name": values["QUEUE_NAME"], "priority": number(values["PRIO"]),
            "state": values["STATUS"], "max_slots": number(values["MAX"]),
            "per_user_slot_limit": number(values["JL/U"]),
            "per_processor_slot_limit": None if values["JL/P"] == "-"
            else float(values["JL/P"]),
            "per_host_slot_limit": number(values["JL/H"]),
            "job_slots": number(values["NJOBS"]), "pending_slots": number(values["PEND"]),
            "running_slots": number(values["RUN"]), "suspended_slots": number(values["SUSP"]),
        }
        extras = {key: value for key, value in values.items() if key not in required}
        if extras:
            # Site extensions retain their original values without guessing their semantics.
            queue["extra_fields"] = extras
        queues.append(queue)
    return queues


def parse_slurm(text: str) -> list[dict]:
    queues = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        row = [value.strip() for value in line.split("|")]
        if len(row) != 9:
            raise ValueError("malformed sinfo row")
        raw_name, available, limit, nodes, state, cpus, memory, gres, hosts = row
        name = raw_name.removesuffix("*")
        if not name:
            raise ValueError("empty partition name")
        group = {
            "node_count": number(nodes), "state": state,
            "cpus_per_node": number(cpus), "memory_per_node_mb": number(memory),
            "gres": gres, "hosts": hosts,
        }
        queue = queues.setdefault(name, {
            "name": name, "default": raw_name.endswith("*"), "availability": available,
            "time_limit": limit, "node_count": 0, "node_groups": [],
        })
        queue["node_count"] += group["node_count"] or 0
        queue["node_groups"].append(group)
    return list(queues.values())
