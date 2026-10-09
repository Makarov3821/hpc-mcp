"""Conservative Gaussian input inspection, snapshot preparation and log evidence."""

from __future__ import annotations

import difflib
import hashlib
from pathlib import Path
import re

from .jobs import relative_path
from .templates import TemplateService
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

    def prepare(self, cluster: str, input_file: str, project_root: str, spec: dict,
                outputs: list[str], changes: dict | None = None,
                dependencies: list[str] | None = None, allow_unresolved: bool = False,
                max_input_bytes: int | None = None) -> dict:
        if type(allow_unresolved) is not bool:
            raise ValueError("allow_unresolved must be boolean")
        report = gaussian_inspect(input_file)
        path = Path(report["input_file"])
        with path.open("rb") as stream:
            raw = stream.read(MAX_CARD_BYTES + 1)
        if len(raw) > MAX_CARD_BYTES:
            raise ValueError("input changed during inspection")
        original = raw.decode("utf-8")
        if hashlib.sha256(original.encode()).hexdigest() != report["sha256"]:
            raise ValueError("input changed during inspection")
        if cluster not in self.jobs.clusters:
            raise ValueError("unknown cluster")
        from .scripts import script_generate
        script_generate(self.jobs.clusters[cluster].scheduler, spec)
        if dependencies is not None and not isinstance(dependencies, list):
            raise ValueError("dependencies must be a list of relative files")
        changes = {} if changes is None else changes
        if not isinstance(changes, dict) or set(changes) - {"cpus", "memory", "paths"}:
            raise ValueError("changes accepts cpus, memory and paths only")
        if "cpus" in changes and (type(changes["cpus"]) is not int or changes["cpus"] < 1):
            raise ValueError("changes.cpus must be positive")
        if "memory" in changes:
            memory_bytes(changes["memory"])
        mapping = changes.get("paths", {})
        if not isinstance(mapping, dict):
            raise ValueError("changes.paths maps literal checkpoint values to relative paths")
        for value in mapping.values():
            checkpoint_path(value)
        lines = original.splitlines(keepends=True)
        used = set()
        for field in report["analysis"]["fields"]:
            key, value = field["key"], field["value"]
            replacement = None
            if key in ("nproc", "nprocshared") and "cpus" in changes:
                replacement = "%NProcShared=" + str(changes["cpus"])
            elif key == "mem" and "memory" in changes:
                replacement = "%Mem=" + changes["memory"].strip()
            elif key in ("chk", "oldchk"):
                target_key = f"{key}:{value}" if f"{key}:{value}" in mapping else value
                if target_key in mapping:
                    replacement = "%" + key + "=" + mapping[target_key]
                    used.add(target_key)
            if replacement is not None:
                lines[field["line"] - 1] = replacement + "\n"
        if set(mapping) != used:
            raise ValueError("changes.paths contains a value absent from the card")
        # Explicit resource edits apply to every Link1 section, even if originally omitted.
        for section in reversed(report["analysis"]["sections"]):
            keys = {f["key"] for f in section["fields"]}
            additions = []
            if "cpus" in changes and not keys & {"nproc", "nprocshared"}:
                additions.append("%NProcShared=" + str(changes["cpus"]) + "\n")
            if "memory" in changes and "mem" not in keys:
                additions.append("%Mem=" + changes["memory"].strip() + "\n")
            lines[section["start_line"] - 1:section["start_line"] - 1] = additions
        effective = "".join(lines)
        analysis = inspect_text(effective, path.name)
        if analysis["unresolved"] and not allow_unresolved:
            raise ValueError("unresolved Link0 fields require explicit review or changes; use gaussian_inspect")
        resources = spec.get("resources", {}) if isinstance(spec, dict) else {}
        cpus = resources.get("cpus", 1)
        if analysis["cpus"] and (type(cpus) is not int or cpus < analysis["cpus"]):
            raise ValueError("scheduler CPUs are fewer than Gaussian requests")
        scope = resources.get("memory_scope")
        if analysis["memory_bytes"] and resources.get("memory_mb"):
            allocated = resources["memory_mb"] * 1024 ** 2 * (cpus if scope == "per_cpu" else 1)
            if scope != "lsf_reservation" and allocated < analysis["memory_bytes"]:
                raise ValueError("scheduler memory is smaller than Gaussian %mem")
        input_files = {path.name, *(relative_path(p) for p in dependencies or [])}
        sources = {}
        for field in report["analysis"]["fields"]:
            source_key = f"oldchk:{field['value']}" if f"oldchk:{field['value']}" in mapping else field["value"]
            if field["key"] == "oldchk" and source_key in mapping and checkpoint_path(mapping[source_key]) in analysis["required_checkpoints"]:
                original_path = field["value"]
                if any(c in original_path for c in "$~\"'`"):
                    raise ValueError("old checkpoint source must be a literal local path")
                original_path = original_path if Path(original_path).suffix else original_path + ".chk"
                local = (path.parent / original_path).absolute()
                sources[checkpoint_path(mapping[source_key])] = str(local)
        for checkpoint in analysis["required_checkpoints"]:
            local = Path(sources[checkpoint]) if checkpoint in sources else path.parent / checkpoint
            if not local.is_file() or local.is_symlink():
                raise ValueError(f"required old checkpoint missing: {checkpoint}")
            input_files.add(checkpoint)
        # Guess=Read/Geom=Check can also read the same %chk; require caller to supply it explicitly.
        route = " ".join(r["text"] for s in analysis["sections"] for r in s["route"])
        if re.search(r"(?i)(guess\s*=\s*(?:\([^)]*\bread\b|read)|geom\s*=\s*(?:allcheck|check))", route):
            if not any(f["key"] == "oldchk" and "path" in f for f in analysis["fields"]):
                raise ValueError("checkpoint-based route requires explicit %oldchk and a distinct output %chk")
        if input_files & set(analysis["candidate_outputs"]):
            raise ValueError("checkpoint outputs overlap uploaded inputs; use distinct %oldchk/%chk paths")
        spec = dict(spec)
        if spec.get("stdin", path.name) != path.name:
            raise ValueError("Gaussian stdin must be the selected card")
        spec["stdin"] = path.name
        spec.setdefault("stdout", path.with_suffix(".log").name)
        parents = {str(Path(f["path"]).parent) for f in analysis["fields"]
                   if f["key"] == "chk" and "path" in f and str(Path(f["path"]).parent) != "."}
        spec["output_directories"] = sorted(set(spec.get("output_directories", [])) | parents)
        provenance = {"kind": "gaussian", "input": path.name, "original_sha256": report["sha256"],
                      "effective_sha256": hashlib.sha256(effective.encode()).hexdigest(),
                      "analysis": analysis, "changes": changes,
                      "log": spec["stdout"], "expected_sections": len(analysis["sections"]),
                      "diff": "".join(difflib.unified_diff(original.splitlines(True), effective.splitlines(True),
                                                        fromfile="original", tofile="snapshot"))}
        rendered = TemplateService(self.jobs).job_prepare_generated
        result = rendered(cluster, str(path.parent), spec, outputs, project_root,
                          sorted(input_files), max_input_bytes=max_input_bytes,
                          input_overrides={path.name: effective}, input_sources=sources,
                          original_inputs={path.name: original}, application_context=provenance)
        run = result["run"]
        return {**result, "gaussian": run["application"]}

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
