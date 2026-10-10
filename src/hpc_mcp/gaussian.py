"""Conservative read-only Gaussian input inspection and synced log evidence."""

from __future__ import annotations

import hashlib
from pathlib import Path
import re

from .jobs import relative_path
from .history import now

MAX_CARD_BYTES = 4 * 1024 * 1024
LINK0 = re.compile(r"^\s*%([A-Za-z][A-Za-z0-9]*)\s*(?:=\s*(.*))?$")


def memory_bytes(value: str) -> int:
    if not isinstance(value, str):
        raise ValueError("Gaussian memory must be a string")
    match = re.fullmatch(r"([0-9]+)(KB|MB|GB|TB|KW|MW|GW|TW)?", value.strip(), re.I)
    if not match or int(match[1]) < 1:
        raise ValueError("Gaussian memory must be a positive integer with a supported unit")
    unit = (match[2] or "W").upper()
    power = {"W": 0, "KB": 1, "MB": 2, "GB": 3, "TB": 4,
             "KW": 1, "MW": 2, "GW": 3, "TW": 4}[unit]
    return int(match[1]) * 1024 ** power * (8 if unit.endswith("W") else 1)


def checkpoint_path(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("checkpoint path must be a string")
    if any(c.isspace() for c in value) or any(c in value for c in "$~'\"`*?[]"):
        raise ValueError("checkpoint path is dynamic or ambiguous")
    value = relative_path(value)
    # Gaussian uses .chk for extensionless checkpoint names.
    return value + ".chk" if not Path(value).suffix else value


def inspect_text(text: str, filename: str) -> dict:
    sections = []
    unresolved = []
    fields = []
    section = {"index": 1, "start_line": 1, "fields": [], "route": []}
    sections.append(section)
    header = True
    route = False
    for number, line in enumerate(text.splitlines(), 1):
        if line.strip().lower() == "--link1--":
            section = {"index": len(sections) + 1, "start_line": number + 1, "fields": [], "route": []}
            sections.append(section)
            header, route = True, False
            continue
        if header and line.lstrip().startswith("#"):
            header, route = False, True
        if route:
            if not line.strip():
                route = False
            else:
                section["route"].append({"line": number, "text": line})
            continue
        if header and line.lstrip().startswith("%"):
            match = LINK0.fullmatch(line)
            if not match:
                unresolved.append({"line": number, "raw": line, "reason": "unrecognized Link0 syntax"})
                continue
            key, value = match[1].lower(), (match[2] or "").strip()
            item = {"key": key, "raw": line, "value": value, "line": number,
                    "section": section["index"], "certainty": "certain"}
            try:
                if key in ("chk", "oldchk"):
                    item["path"] = checkpoint_path(value)
                elif key in ("nprocshared", "nproc"):
                    if not re.fullmatch(r"[1-9][0-9]*", value):
                        raise ValueError("CPU count must be a positive integer")
                    item["cpus"] = int(value)
                elif key == "mem":
                    item["bytes"] = memory_bytes(value)
                else:
                    raise ValueError("Link0 directive requires explicit review")
            except ValueError as error:
                item["certainty"] = "unresolved"
                unresolved.append({"line": number, "raw": line, "reason": str(error)})
            fields.append(item)
            section["fields"].append(item)
    for segment in sections:
        if not segment["route"]:
            unresolved.append({"line": segment["start_line"], "raw": "", "reason": "section has no route line"})
    checkpoints = sorted({f["path"] for f in fields if f["key"] == "chk" and "path" in f})
    produced, required = set(), set()
    for segment in sections:
        required.update(f["path"] for f in segment["fields"]
                        if f["key"] == "oldchk" and "path" in f and f["path"] not in produced)
        produced.update(f["path"] for f in segment["fields"] if f["key"] == "chk" and "path" in f)
    old = sorted(required)
    return {"file": filename, "sections": sections, "fields": fields, "unresolved": unresolved,
            "candidate_outputs": [str(Path(filename).with_suffix(".log")), *checkpoints],
            "required_checkpoints": old, "cpus": max((f["cpus"] for f in fields if "cpus" in f), default=None),
            "memory_bytes": max((f["bytes"] for f in fields if "bytes" in f), default=None),
            "notes": ["Candidates do not enumerate every program output or route-level dependency.",
                      "Link1 sections form one run; resource requests must cover all sections.",
                      "Extensionless checkpoint paths are reported with .chk; review target installation behavior."]}


def gaussian_inspect(input_file: str) -> dict:
    path = Path(input_file).expanduser()
    if path.suffix.lower() not in (".gjf", ".com") or path.is_symlink() or not path.is_file():
        raise ValueError("input_file must be a regular .gjf/.com file")
    with path.open("rb") as stream:
        raw = stream.read(MAX_CARD_BYTES + 1)
    if len(raw) > MAX_CARD_BYTES:
        raise ValueError("Gaussian card exceeds 4 MiB")
    result = inspect_text(raw.decode("utf-8"), path.name)
    for field in result["fields"]:
        if "path" in field:
            local = path.parent / field["path"]
            field["local_exists"] = local.is_file() and not local.is_symlink()
    return {"ok": True, "input_file": str(path.resolve()),
            "sha256": hashlib.sha256(raw).hexdigest(), "analysis": result}


class GaussianService:
    def __init__(self, jobs):
        self.jobs = jobs

    def result(self, run_id: str, log_path: str | None = None, max_bytes: int = 1048576) -> dict:
        if type(max_bytes) is not int or not 1024 <= max_bytes <= 16 * 1024 * 1024:
            raise ValueError("max_bytes must be 1024..16777216")
        with self.jobs.history.lock(run_id):
            run = self.jobs.history.get(run_id)
            application = run.get("application", {})
            expected = application.get("expected_sections", 1)
            log = relative_path(log_path or application.get("log", "stdout.log"))
            root = Path(run["output_dir"]) if run.get("sync_state") == "complete" and run.get("output_dir") else None
            if root is None:
                raise ValueError("application evidence requires a completed local sync; sync selected logs first")
            path = root / log
            if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
                raise ValueError("log must be a regular file inside the synced output directory")
            # Use only logs in the latest downloaded manifest; never infer from an old local file.
            record = next((f for f in run.get("output_manifest", []) if f["path"] == log), None)
            if not record:
                raise ValueError("log was not included in the latest sync")
            markers = []
            normal, errors, line_number = 0, 0, 0
            last_nonempty_kind = None
            digest = hashlib.sha256()
            # Scan all lines with bounded memory; return only a bounded evidence window.
            with path.open("rb") as stream:
                while True:
                    raw = stream.readline(4096)
                    if not raw:
                        break
                    prefix = raw[:1000].decode(errors="replace")
                    digest.update(raw)
                    while raw and not raw.endswith(b"\n"):
                        raw = stream.readline(4096)
                        digest.update(raw)
                    line_number += 1
                    kind = None
                    if re.match(r"^\s*Normal termination of Gaussian\s+(?:\d+|\w+)\b", prefix):
                        kind, normal = "normal", normal + 1
                    elif re.match(r"^\s*Error termination\b", prefix):
                        kind, errors = "error", errors + 1
                    if prefix.strip():
                        last_nonempty_kind = kind
                    if kind:
                        markers.append({"kind": kind, "line": line_number, "text": prefix[:500].rstrip()})
                        while sum(len(m["text"].encode()) for m in markers) > max_bytes:
                            markers.pop(0)
            digest = digest.hexdigest()
            if digest != record["sha256"]:
                raise ValueError("log changed since sync; synchronize again")
            state = "failed" if errors else \
                "succeeded" if normal == expected and markers and last_nonempty_kind == "normal" \
                else "unknown"
            size = path.stat().st_size
            result = {"kind": "gaussian", "state": state, "log": log, "sha256": digest,
                      "normal_count": normal, "expected_sections": expected,
                      "scanned_bytes": size, "evidence_truncated": normal + errors > len(markers), "evidence": markers,
                      "scheduler_state": run["state"], "checked_at": now()}
            self.jobs.history.update(run_id, "application_checked", application_result=result)
            return {"ok": True, "run_id": run_id, "application_result": result}
