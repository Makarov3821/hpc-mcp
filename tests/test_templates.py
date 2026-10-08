"""Script rendering and template lifecycle tests using real Bash and local scheduler stubs."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from hpc_mcp.scripts import script_generate
from hpc_mcp.templates import TemplateService
import test_jobs


class ScriptTests(unittest.TestCase):
    def test_scheduler_directives_and_memory_scopes(self):
        for scheduler, scope in (("lsf", "lsf_reservation"), ("slurm", "job")):
            spec = {"command": ["g16"], "resources": {"cpus": 4, "queue": "compute",
                    "memory_mb": 2048, "memory_scope": scope, "time_minutes": 1501}}
            rendered = script_generate(scheduler, spec)
            text = rendered["script"]
            if scheduler == "lsf":
                self.assertIn("#BSUB -n 4", text)
                self.assertIn("span[hosts=1] rusage[mem=2048MB]", text)
                self.assertIn("#BSUB -W 25:01", text)
                self.assertTrue(rendered["warnings"])
            else:
                self.assertIn("#SBATCH --cpus-per-task=4", text)
                self.assertIn("#SBATCH --mem=2048M", text)
                self.assertIn("#SBATCH --time=1-01:01:00", text)
        text = script_generate("slurm", {"command": ["true"], "resources": {
            "memory_mb": 512, "memory_scope": "per_cpu", "account": "lab", "qos": "normal"}})["script"]
        self.assertIn("--mem-per-cpu=512M", text)
        self.assertIn("--account=lab", text)

    def test_quoted_arguments_environment_and_redirection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "input name.gjf").write_text("card content")
            (root / "init script.sh").write_text("export INIT_RESULT=initialized\n")
            literal = "$(touch unwanted); ' quoted"
            spec = {"command": [sys.executable, "-c",
                "import os,sys;print(sys.argv[1]);print(os.environ['TOKEN']);"
                "print(os.environ['INIT_RESULT']);print(sys.stdin.read())", literal],
                "environment": {"TOKEN": literal}, "init_scripts": [str(root / "init script.sh")],
                "stdin": "input name.gjf", "stdout": "results with spaces/result name.log"}
            original = deepcopy(spec)
            script = script_generate("lsf", spec)["script"]
            result = subprocess.run(["bash"], input=script, text=True, cwd=root, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / "results with spaces/result name.log").read_text(),
                             f"{literal}\n{literal}\ninitialized\ncard content\n")
            self.assertFalse((root / "unwanted").exists())
            self.assertEqual(spec, original)

    def test_program_and_initialization_failures_preserve_exit_status(self):
        script = script_generate("slurm", {"command": ["bash", "-c", "exit 7"]})["script"]
        self.assertEqual(subprocess.run(["bash"], input=script, text=True).returncode, 7)
        script = script_generate("lsf", {"command": ["true"],
            "init_scripts": ["/nonexistent-hpc-mcp-init"]})["script"]
        self.assertNotEqual(subprocess.run(["bash"], input=script, text=True, capture_output=True).returncode, 0)

    def test_invalid_resources_and_specs_are_rejected(self):
        invalid = [{"command": "g16"}, {"command": ["true\nfalse"]},
            {"command": ["true"], "resources": {"cpus": None}},
            {"command": ["true"], "resources": {"cpus": True}},
            {"command": ["true"], "resources": {"nodes": 2}},
            {"command": ["true"], "resources": {"queue": "normal\n#BSUB -q other"}},
            {"command": ["true"], "resources": {"memory_mb": 1024}},
            {"command": ["true"], "stdin": "../outside"},
            {"command": ["true"], "stdin": "card", "stdout": "card"},
            {"command": ["true"], "environment": {"BAD-NAME": "value"}},
            {"command": ["true"], "init_scripts": ["relative-init.sh"]}]
        for spec in invalid:
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                script_generate("lsf", spec)
        for scheduler, resources in (("lsf", {"memory_mb": 1024, "memory_scope": "job"}),
                                      ("lsf", {"account": "lab"}),
                                      ("slurm", {"lsf_resource_requirement": "select[gpu]"})):
            with self.assertRaises(ValueError):
                script_generate(scheduler, {"command": ["true"], "resources": resources})


@unittest.skipUnless(shutil.which("rsync"), "local rsync required for template lifecycle")
class TemplateTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_jobs.JobLifecycleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.jobs = self.fixture.jobs
        self.templates = TemplateService(self.jobs)
        self.source = self.fixture.source
        (self.source / "test1.gjf").write_text("original card")
        self.definition = {"scheduler": "lsf", "spec": {
            "command": ["bash", "-c", "cat; printf checkpoint > '{{stem}}.chk'"],
            "stdin": "{{input}}", "stdout": "{{stem}}.log",
            "resources": {"cpus": "{{cpus}}"}},
            "parameters": {"input": {"type": "string"}, "stem": {"type": "string"},
                           "cpus": {"type": "integer", "default": 2}},
            "input_files": ["{{input}}"], "outputs": ["{{stem}}.log", "{{stem}}.chk"]}
        self.bindings = {"input": "test1.gjf", "stem": "test1"}

    def plan(self, **options):
        return self.templates.template_plan("example", "lsf", str(self.source), self.bindings,
                                           project_root=str(self.fixture.root), **options)

    def test_versioned_plan_submission_sync_and_source_preservation(self):
        self.templates.template_import("example", self.definition)
        plan = self.plan()
        self.assertEqual(self.fixture.transport.commands, [])
        self.assertFalse((self.source / "hpc-mcp-job.sh").exists())
        run = plan["run"]
        self.assertEqual(run["template"]["version"], 1)
        self.assertEqual(run["template"]["parameters"]["cpus"], 2)
        changed = deepcopy(self.definition)
        changed["spec"]["command"] = ["false"]
        self.templates.template_import("example", changed)
        (self.source / "test1.gjf").write_text("later edit")
        restarted = TemplateService(self.jobs)
        submitted = restarted.template_run(plan["plan_id"])
        self.assertTrue(submitted["ok"], submitted)
        self.assertEqual(submitted["run"]["template"]["version"], 1)
        self.assertTrue(restarted.template_run(plan["plan_id"])["already_processed"])
        self.assertEqual((self.fixture.root / "submissions").read_text(), "1")
        synced = self.jobs.job_sync(run["run_id"])
        self.assertTrue(synced["ok"], synced)
        self.assertEqual((self.source / "test1.log").read_text(), "original card")
        self.assertEqual((self.source / "test1.chk").read_text(), "checkpoint")
        self.assertEqual((self.source / "test1.gjf").read_text(), "later edit")
        self.assertEqual(restarted.template_get("example")["template"]["version"], 2)
        self.assertEqual(restarted.template_get("example", 1)["template"]["definition"], self.definition)
        self.assertEqual(restarted.template_list()["templates"][0]["version"], 2)

    def test_generated_preparation_both_schedulers_and_stdin_dependency(self):
        for scheduler in ("lsf", "slurm"):
            prepared = self.templates.job_prepare_generated(scheduler, str(self.source),
                {"command": ["cat"], "stdin": "test1.gjf", "stdout": f"{scheduler}.log"},
                [f"{scheduler}.log"], str(self.fixture.root), ["data.txt"])
            self.assertEqual({f["path"] for f in prepared["run"]["manifest"]},
                             {"data.txt", "test1.gjf", "hpc-mcp-job.sh"})
            self.assertTrue(self.jobs.job_submit(prepared["run"]["run_id"])["ok"])
            self.assertTrue(self.jobs.job_sync(prepared["run"]["run_id"])["ok"])
            self.assertEqual((self.source / f"{scheduler}.log").read_text(), "original card")

    def test_parameters_schema_scheduler_and_paths_validated_before_preparation(self):
        self.templates.template_import("example", self.definition)
        for bindings in ({}, {**self.bindings, "cpus": True}, {**self.bindings, "unknown": 1},
                         {**self.bindings, "input": "../outside"}, []):
            with self.subTest(bindings=bindings), self.assertRaises(ValueError):
                self.templates.template_plan("example", "lsf", str(self.source), bindings)
        with self.assertRaisesRegex(ValueError, "scheduler"):
            self.templates.template_plan("example", "slurm", str(self.source), self.bindings)
        self.assertEqual(self.jobs.job_list()["runs"], [])
        bad = deepcopy(self.definition)
        bad["spec"]["stdin"] = "{{typo}}"
        with self.assertRaisesRegex(ValueError, "undeclared"):
            self.templates.template_import("invalid", bad)

    def test_collision_snapshot_changes_and_non_template_execution_rejected(self):
        self.templates.template_import("example", self.definition)
        (self.source / "hpc-mcp-job.sh").write_text("existing script")
        with self.assertRaisesRegex(ValueError, "conflicts"):
            self.plan()
        (self.source / "hpc-mcp-job.sh").unlink()
        plan = self.plan()
        (Path(plan["run"]["snapshot_dir"]) / "hpc-mcp-job.sh").write_text("tampered")
        with self.assertRaisesRegex(ValueError, "snapshot changed"):
            self.templates.template_run(plan["plan_id"])
        ordinary = self.fixture.prepare()
        with self.assertRaisesRegex(ValueError, "template plan"):
            self.templates.template_run(ordinary["run_id"])

    def test_import_versions_are_transactional_and_listing_paginated(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            versions = list(pool.map(lambda _: self.templates.template_import(
                "example", self.definition)["template"]["version"], range(6)))
        self.assertEqual(sorted(versions), list(range(1, 7)))
        self.templates.template_import("another", self.definition)
        self.assertEqual(self.templates.template_list(1, 1)["templates"][0]["name"], "example")
        with self.assertRaises(ValueError):
            self.templates.template_get("../escape")
        with self.assertRaises(ValueError):
            self.templates.template_get("example", 999)

    def test_cli_preview_import_and_plan(self):
        definition_file = self.fixture.root / "template.json"
        definition_file.write_text(json.dumps(self.definition))
        spec_file = self.fixture.root / "spec.json"
        spec_file.write_text(json.dumps({"command": ["true"]}))
        source_path = str(Path(__file__).resolve().parents[1] / "src")
        import os
        command = [sys.executable, "-m", "hpc_mcp", "--config", str(self.fixture.root / "absent.toml"),
                   "--state-dir", str(self.fixture.state)]
        env = dict(os.environ, PYTHONPATH=source_path)
        for arguments in (["script-generate", "lsf", str(spec_file)],
                          ["template-import", "cli-template", str(definition_file)], ["template-list"],
                          ["config-set", "lsf", json.dumps({"ssh_host": "alias", "scheduler": "lsf",
                                                            "work_root": str(self.fixture.remote)})],
                          ["template-plan", "cli-template", "lsf", str(self.source),
                           "--parameters", json.dumps(self.bindings), "--project-root", str(self.fixture.root)],
                          ["prepare-generated", "lsf", str(self.source), str(spec_file),
                           "--output", "result.log", "--input-file", "test1.gjf"]):
            result = subprocess.run(command + arguments, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertTrue(json.loads(result.stdout)["ok"])
        self.assertFalse((self.source / "hpc-mcp-job.sh").exists())

    def test_example_templates_are_valid(self):
        examples = Path(__file__).resolve().parents[1] / "examples/templates"
        for path in examples.glob("*.json"):
            definition = json.loads(path.read_text())
            self.templates.template_import(path.stem, definition)
            scheduler = definition["scheduler"]
            plan = self.templates.template_plan(path.stem, scheduler, str(self.source),
                {"input": "test1.gjf", "stem": "test1", "checkpoint": "test1.chk", "queue": "compute"})
            self.assertEqual(plan["run"]["scheduler"], scheduler)
