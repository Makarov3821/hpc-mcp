"""Gaussian preparation never modifies cards; application evidence is independent."""

import hashlib
from pathlib import Path
import tempfile
import unittest

import test_jobs
from hpc_mcp.gaussian import GaussianService, gaussian_inspect, memory_bytes


class GaussianTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_jobs.JobLifecycleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.jobs = self.fixture.jobs
        self.gaussian = GaussianService(self.jobs)
        self.card = self.fixture.source / "test.gjf"
        self.card.write_text("%chk=test.chk\n%nprocshared=2\n%mem=1GB\n# hf/sto-3g\n\ntitle\n\n0 1\nH 0 0 0\n\n")
        self.spec = {"command": ["bash", "-c", "printf 'Normal termination of Gaussian 16 at now\\n'; printf checkpoint > test.chk"],
                     "resources": {"cpus": 2}}

    def prepare(self, **options):
        return self.gaussian.prepare("lsf", str(self.card), str(self.fixture.root), self.spec,
                                     ["test.log", "test.chk"], **options)["run"]

    def test_generic_mpi_extension_does_not_enable_gaussian_linda(self):
        self.spec["resources"].update(tasks=2)
        self.spec["launcher"] = {"kind": "mpirun"}
        with self.assertRaisesRegex(ValueError, "MPI/Linda"):
            self.prepare()
        self.assertEqual(self.jobs.job_list()["runs"], [])

    def test_inspect_units_link1_and_unresolved_directives(self):
        self.card.write_text(self.card.read_text() + "--Link1--\n%oldchk=test.chk\n%chk=next.chk\n%mem=2MW\n%rwf=$SCRATCH/x\n# geom=allcheck\n\n")
        result = gaussian_inspect(str(self.card))["analysis"]
        self.assertEqual(len(result["sections"]), 2)
        self.assertEqual(result["required_checkpoints"], [])
        self.assertEqual(result["memory_bytes"], 1024 ** 3)
        self.assertEqual(next(f["bytes"] for f in result["fields"] if f["value"] == "2MW"), 2 * 1024 ** 2 * 8)
        self.assertEqual(result["fields"][0]["line"], 1)
        self.assertTrue(result["unresolved"])
        self.assertEqual(memory_bytes("128"), 1024)
        self.assertEqual(memory_bytes("1GB"), 1024 ** 3)
        for value in ("0", "1.2GB", "$MEM", True):
            with self.assertRaises(ValueError):
                memory_bytes(value)

    def test_snapshot_edits_preserve_original_and_resource_constraints(self):
        original = self.card.read_bytes()
        self.spec["resources"] = {"cpus": 4, "memory_mb": 2048, "memory_scope": "lsf_reservation"}
        run = self.prepare(changes={"cpus": 4, "memory": "2GB"})
        effective = (Path(run["snapshot_dir"]) / "test.gjf").read_text()
        self.assertIn("%NProcShared=4", effective)
        self.assertEqual(self.card.read_bytes(), original)
        self.assertEqual(Path(run["application"]["original_snapshot"]).read_bytes(), original)
        self.assertEqual(run["original_manifest"][0]["sha256"], hashlib.sha256(original).hexdigest())
        self.assertTrue(run["application"]["diff"])
        self.assertEqual(run["application"]["effective_sha256"], hashlib.sha256(effective.encode()).hexdigest())
        self.spec["resources"]["cpus"] = 1
        with self.assertRaisesRegex(ValueError, "CPUs"):
            self.prepare()

    def test_checkpoint_mapping_inside_project_and_external_dependency_rejection(self):
        seed = self.fixture.root / "seed.chk"
        seed.write_bytes(b"seed")
        self.card.write_text("%oldchk=../seed.chk\n%chk=/scratch/output.chk\n# geom=allcheck\n\n")
        run = self.prepare(changes={"paths": {"../seed.chk": "seed.chk", "/scratch/output.chk": "test.chk"}})
        self.assertEqual((Path(run["snapshot_dir"]) / "seed.chk").read_bytes(), b"seed")
        self.assertIn("%oldchk=seed.chk", (Path(run["snapshot_dir"]) / "test.gjf").read_text())
        self.assertIn("../seed.chk", self.card.read_text())
        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory) / "outside.chk"
            outside.write_bytes(b"outside")
            self.card.write_text(f"%oldchk={outside}\n%chk=test.chk\n# geom=allcheck\n\n")
            with self.assertRaisesRegex(ValueError, "project_root"):
                self.prepare(changes={"paths": {str(outside): "seed.chk"}})

    def test_checkpoint_produced_in_prior_link_is_not_required_as_local_input(self):
        self.card.write_text("%chk=first.chk\n# hf\n\ntitle\n\n0 1\n\n--Link1--\n%oldchk=first.chk\n%chk=test.chk\n# geom=allcheck\n\n")
        run = self.prepare()
        self.assertNotIn("first.chk", run["input_files"])

    def test_unknown_fields_restart_without_oldchk_and_input_output_conflicts(self):
        self.card.write_text("%chk=test.chk\n%cpu=0-3\n# hf\n\n")
        with self.assertRaisesRegex(ValueError, "unresolved"):
            self.prepare()
        self.prepare(allow_unresolved=True)
        self.card.write_text("%chk=test.chk\n# guess=read\n\n")
        with self.assertRaisesRegex(ValueError, "checkpoint-based"):
            self.prepare()
        (self.card.parent / "test.chk").write_text("seed")
        self.card.write_text("%oldchk=test.chk\n%chk=test.chk\n# hf\n\n")
        with self.assertRaisesRegex(ValueError, "overlap"):
            self.prepare()

    def test_application_failure_does_not_overwrite_scheduler_success(self):
        for text, expected in (("Normal termination of Gaussian 16 at now", "succeeded"),
                               ("Error termination via Lnk1e", "failed"), ("still incomplete", "unknown")):
            with self.subTest(text=text):
                self.spec["command"] = ["printf", "%s\\n", text]
                run = self.prepare()
                self.jobs.job_submit(run["run_id"])
                self.jobs.job_sync(run["run_id"], overwrite="merge")
                result = self.gaussian.result(run["run_id"])["application_result"]
                self.assertEqual(result["state"], expected)
                self.assertEqual(self.jobs.history.get(run["run_id"])["state"], "succeeded")
                (self.card.parent / "test.log").write_text("modified")
                with self.assertRaisesRegex(ValueError, "changed"):
                    self.gaussian.result(run["run_id"])

    def test_link1_requires_all_terminations_and_large_logs_use_bounded_scan(self):
        self.card.write_text("%chk=test.chk\n# hf\n\ntitle\n\n0 1\n\n--Link1--\n%chk=test.chk\n# hf\n\n")
        for count in (1, 2):
            self.spec["command"] = ["bash", "-c", "printf '%s\\n' 'Normal termination of Gaussian 16 at now'; "
                                   + ("printf '%s\\n' 'Normal termination of Gaussian 16 at now'" if count == 2 else "true")]
            run = self.prepare()
            self.jobs.job_submit(run["run_id"])
            self.jobs.job_sync(run["run_id"], overwrite="merge")
            state = self.gaussian.result(run["run_id"], max_bytes=1024)["application_result"]["state"]
            self.assertEqual(state, "succeeded" if count == 2 else "unknown")

    def test_large_logs_and_checkpoint_parent_directories(self):
        self.card.write_text("%chk=results/test.chk\n%nprocshared=2\n# hf\n\n")
        self.spec["command"] = ["bash", "-c", "head -c 2000000 /dev/zero | tr '\\0' x; printf '\\nNormal termination of Gaussian 16 at now\\n'; printf checkpoint > results/test.chk"]
        run = self.gaussian.prepare("lsf", str(self.card), str(self.project_root()), self.spec,
                                    ["test.log", "results/test.chk"])["run"]
        self.assertIn("mkdir -p -- results", run["generated_script"])
        self.jobs.job_submit(run["run_id"])
        self.assertTrue(self.jobs.job_sync(run["run_id"])["ok"])
        result = self.gaussian.result(run["run_id"], max_bytes=1024)["application_result"]
        self.assertEqual(result["state"], "succeeded")
        self.assertGreater(result["scanned_bytes"], 2000000)
        self.assertTrue((self.card.parent / "results/test.chk").exists())

    def project_root(self):
        return self.fixture.root

    def test_directional_oldchk_mapping_preserves_output_name(self):
        (self.card.parent / "test.chk").write_bytes(b"old seed")
        self.card.write_text("%oldchk=test.chk\n%chk=test.chk\n# geom=allcheck\n\n")
        run = self.prepare(changes={"paths": {"oldchk:test.chk": "seed.chk"}})
        text = (Path(run["snapshot_dir"]) / "test.gjf").read_text()
        self.assertIn("%oldchk=seed.chk", text)
        self.assertIn("%chk=test.chk", text)
        self.assertEqual((Path(run["snapshot_dir"]) / "seed.chk").read_bytes(), b"old seed")

    def test_cli_inspect_and_prepare_require_no_live_cluster(self):
        import json
        import subprocess
        import sys
        config = self.fixture.root / "clusters.toml"
        config.write_text(f'[clusters.lab]\nssh_host="offline-alias"\nscheduler="lsf"\nwork_root={json.dumps(str(self.fixture.remote))}\n')
        spec_file = self.fixture.root / "spec.json"
        spec_file.write_text(json.dumps(self.spec))
        command = [sys.executable, "-m", "hpc_mcp", "--config", str(config), "--state-dir", str(self.fixture.state)]
        result = subprocess.run(command + ["gaussian-inspect", str(self.card)], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["analysis"]["cpus"], 2)
        result = subprocess.run(command + ["gaussian-prepare", "lab", str(self.card), str(spec_file),
            "--project-root", str(self.fixture.root), "--output", "test.log", "--output", "test.chk",
            "--changes", '{"cpus":2}'], capture_output=True, text=True, check=True)
        run = json.loads(result.stdout)["run"]
        self.assertEqual(run["phase"], "prepared")
        self.assertEqual(run["application"]["kind"], "gaussian")
        self.assertFalse(Path(run["remote_dir"]).exists())
