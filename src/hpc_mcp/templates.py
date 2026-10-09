"""Versioned structured templates and immutable single-run plans in the history database."""

from copy import deepcopy
import hashlib
import json
import re
from pathlib import Path

from .config import validate_patterns
from .history import now
from .jobs import JobService, relative_path
from .scripts import script_generate


TOKEN = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")


def parameter_value(value, kind):
    expected = {"string": str, "integer": int, "boolean": bool}
    if kind not in expected or type(value) is not expected[kind]:
        raise ValueError(f"parameter must have type {kind}")
    return value


def bind(definition: dict, parameters: dict | None, preview=False) -> tuple[dict, dict]:
    declarations = definition.get("parameters", {})
    if not isinstance(declarations, dict) or (parameters is not None and not isinstance(parameters, dict)):
        raise ValueError("parameters must be an object")
    supplied = {} if parameters is None else parameters
    if set(supplied) - set(declarations):
        raise ValueError("unknown template parameters")
    values = {}
    for name, declaration in declarations.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError("invalid parameter name")
        if not isinstance(declaration, dict) or set(declaration) - {"type", "default", "description"}:
            raise ValueError("parameter declarations accept type, default and description")
        kind = declaration.get("type", "string")
        if kind not in ("string", "integer", "boolean"):
            raise ValueError("parameter type must be string, integer or boolean")
        if "default" in declaration:
            parameter_value(declaration["default"], kind)
        if name in supplied:
            value = supplied[name]
        elif "default" in declaration:
            value = declaration["default"]
        elif preview:
            value = {"string": "value", "integer": 1, "boolean": True}[kind]
        else:
            raise ValueError(f"missing required parameter: {name}")
        values[name] = parameter_value(value, kind)

    def resolve(value):
        if isinstance(value, dict):
            return {key: resolve(item) for key, item in value.items()}
        if isinstance(value, list):
            return [resolve(item) for item in value]
        if not isinstance(value, str):
            return value
        for token in TOKEN.findall(value):
            if token not in values:
                raise ValueError(f"undeclared template parameter: {token}")
        match = TOKEN.fullmatch(value)
        if match:
            return values[match.group(1)]
        result = TOKEN.sub(lambda m: str(values[m.group(1)]), value)
        if "{{" in result or "}}" in result:
            raise ValueError("malformed template placeholder")
        return result

    return resolve({key: value for key, value in definition.items() if key != "parameters"}), values


def validate_definition(definition: dict):
    allowed = {"scheduler", "spec", "parameters", "script_name", "input_files", "outputs",
               "output_exclude", "input_exclude", "max_input_bytes"}
    if not isinstance(definition, dict) or set(definition) - allowed:
        raise ValueError("unknown template definition settings")
    if definition.get("scheduler") not in ("lsf", "slurm"):
        raise ValueError("template requires a fixed lsf/slurm scheduler")
    effective, _ = bind(definition, {}, preview=True)
    script_generate(effective["scheduler"], effective.get("spec"))
    script = relative_path(effective.get("script_name", "hpc-mcp-job.sh"))
    if "/" in script or script.startswith((".hpc-mcp-", ".xn02-")):
        raise ValueError("generated script_name must be a nonreserved filename")
    if not effective.get("outputs"):
        raise ValueError("template requires explicit nonempty outputs")
    for key in ("outputs", "output_exclude", "input_exclude"):
        if key in effective:
            validate_patterns(effective[key])
    if "input_files" in effective:
        if not isinstance(effective["input_files"], list) or not effective["input_files"]:
            raise ValueError("input_files must be a nonempty relative file list")
        for path in effective["input_files"]:
            relative_path(path)
    if "max_input_bytes" in effective and (type(effective["max_input_bytes"]) is not int or
                                           effective["max_input_bytes"] < 1):
        raise ValueError("max_input_bytes must be a positive integer")


class TemplateService:
    def __init__(self, jobs: JobService):
        self.jobs = jobs
        with jobs.history.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS templates (
                name TEXT NOT NULL, version INTEGER NOT NULL, created_at TEXT NOT NULL,
                sha256 TEXT NOT NULL, definition TEXT NOT NULL, PRIMARY KEY(name, version))""")

    @staticmethod
    def _name(name):
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", name):
            raise ValueError("template name must be 1..100 letters, digits, '.', '_' or '-'")

    def template_import(self, name: str, definition: dict) -> dict:
        self._name(name)
        validate_definition(definition)
        encoded = json.dumps(definition, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode()) > 262144:
            raise ValueError("template definition exceeds 256 KiB")
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        with self.jobs.history.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("SELECT COALESCE(MAX(version), 0)+1 FROM templates WHERE name=?", (name,)).fetchone()[0]
            db.execute("INSERT INTO templates VALUES (?, ?, ?, ?, ?)", (name, version, now(), digest, encoded))
        return self.template_get(name, version)

    def template_get(self, name: str, version: int | None = None) -> dict:
        self._name(name)
        if version is not None and (type(version) is not int or version < 1):
            raise ValueError("version must be a positive integer")
        with self.jobs.history.connect() as db:
            row = db.execute("SELECT name, version, created_at, sha256, definition FROM templates "
                "WHERE name=? AND (? IS NULL OR version=?) ORDER BY version DESC LIMIT 1",
                (name, version, version)).fetchone()
        if row is None:
            raise ValueError("template/version not found")
        if hashlib.sha256(row[4].encode()).hexdigest() != row[3]:
            raise ValueError("stored template checksum mismatch")
        return {"ok": True, "template": {"name": row[0], "version": row[1], "created_at": row[2],
                                          "sha256": row[3], "definition": json.loads(row[4])}}

    def template_list(self, limit: int = 50, offset: int = 0) -> dict:
        if type(limit) is not int or not 1 <= limit <= 500 or type(offset) is not int or offset < 0:
            raise ValueError("limit must be 1..500 and offset nonnegative")
        with self.jobs.history.connect() as db:
            rows = db.execute("SELECT t.name, t.version, t.created_at FROM templates t WHERE "
                "t.version=(SELECT MAX(version) FROM templates WHERE name=t.name) "
                "ORDER BY t.name LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        return {"ok": True, "templates": [{"name": r[0], "version": r[1], "created_at": r[2]} for r in rows]}

    def job_prepare_generated(self, cluster: str, input_dir: str, spec: dict, outputs: list[str],
                              project_root: str | None = None, input_files: list[str] | None = None,
                              script_name: str = "hpc-mcp-job.sh", output_exclude: list[str] | None = None,
                              input_exclude: list[str] | None = None, max_input_bytes: int | None = None,
                              context: dict | None = None,
                              input_overrides: dict[str, str] | None = None,
                              input_sources: dict[str, str] | None = None,
                              original_inputs: dict[str, str] | None = None,
                              application_context: dict | None = None) -> dict:
        if cluster not in self.jobs.clusters:
            raise ValueError(f"unknown cluster: {cluster}")
        script_name = relative_path(script_name)
        if "/" in script_name or script_name.startswith((".hpc-mcp-", ".xn02-")):
            raise ValueError("script_name must be a nonreserved filename")
        if not outputs:
            raise ValueError("generated jobs require explicit outputs")
        rendered = script_generate(self.jobs.clusters[cluster].scheduler, spec)
        if input_files is not None and (not isinstance(input_files, list) or
                                       any(not isinstance(path, str) for path in input_files)):
            raise ValueError("input_files must be a list of relative strings")
        redirects = [spec.get("stdout"), spec.get("stderr")]
        selected = set(input_files or []) | {script_name}
        if spec.get("stdin"):
            # Declared stdin is always a required uploaded input.
            if input_files is not None:
                input_files = sorted(set(input_files) | {spec["stdin"]})
            selected.add(spec["stdin"])
        if any(path in selected for path in redirects if path):
            raise ValueError("program output cannot overwrite a selected input")
        if input_files is None and any((Path(input_dir).expanduser() / path).exists()
                                      for path in redirects if path):
            raise ValueError("existing output would be snapshotted as input; use an explicit input_files list")
        prepared = self.jobs.job_prepare(cluster, input_dir, script_name, outputs, "filtered",
            output_exclude, input_exclude, max_input_bytes, project_root, input_files,
            rendered["script"], context, input_overrides, input_sources, original_inputs, application_context)
        prepared["rendered"] = rendered
        return prepared

    def template_plan(self, name: str, cluster: str, input_dir: str,
                      parameters: dict | None = None, version: int | None = None,
                      project_root: str | None = None) -> dict:
        template = self.template_get(name, version)["template"]
        effective, bindings = bind(template["definition"], parameters)
        validate_definition(effective)
        if cluster not in self.jobs.clusters or self.jobs.clusters[cluster].scheduler != effective["scheduler"]:
            raise ValueError("template scheduler does not match the configured cluster")
        result = self.job_prepare_generated(cluster, input_dir, effective["spec"], effective["outputs"],
            project_root, effective.get("input_files"), effective.get("script_name", "hpc-mcp-job.sh"),
            effective.get("output_exclude"), effective.get("input_exclude"), effective.get("max_input_bytes"),
            {"name": name, "version": template["version"], "sha256": template["sha256"],
             "parameters": bindings, "definition": deepcopy(template["definition"])})
        result["plan_id"] = result["run"]["run_id"]
        return result

    def template_run(self, plan_id: str) -> dict:
        run = self.jobs.history.get(plan_id)
        if "template" not in run:
            raise ValueError("plan_id must reference a prepared template plan")
        # Submission uses the persisted version/parameters/snapshot, never the latest template.
        return self.jobs.job_submit(plan_id)
