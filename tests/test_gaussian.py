"""Read-only Gaussian input analysis and stable application log evidence."""

from pathlib import Path
import unittest

import test_jobs
from hpc_mcp.gaussian import GaussianService, gaussian_inspect, memory_bytes
from hpc_mcp.templates import TemplateService


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

    def prepare(self, expected_sections=1):
        spec = dict(self.spec, stdin=self.card.name, stdout='test.log')
        return TemplateService(self.jobs).job_prepare_generated(
            'lsf', str(self.card.parent), spec, ['test.log', 'results/test.chk'],
            str(self.fixture.root), input_files=[self.card.name],
            application_context={'kind': 'gaussian', 'log': 'test.log',
                                 'expected_sections': expected_sections})['run']

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

    def test_checkpoint_produced_in_prior_link_is_not_required_as_local_input(self):
        self.card.write_text("%chk=first.chk\n# hf\n\ntitle\n\n0 1\n\n--Link1--\n%oldchk=first.chk\n%chk=test.chk\n# geom=allcheck\n\n")
        analysis = gaussian_inspect(str(self.card))["analysis"]
        self.assertEqual(analysis["required_checkpoints"], [])

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
            run = self.prepare(expected_sections=2)
            self.jobs.job_submit(run["run_id"])
            self.jobs.job_sync(run["run_id"], overwrite="merge")
            state = self.gaussian.result(run["run_id"], max_bytes=1024)["application_result"]["state"]
            self.assertEqual(state, "succeeded" if count == 2 else "unknown")

    def test_large_logs_and_checkpoint_parent_directories(self):
        self.card.write_text("%chk=results/test.chk\n%nprocshared=2\n# hf\n\n")
        self.spec["command"] = ["bash", "-c", "head -c 2000000 /dev/zero | tr '\\0' x; printf '\\nNormal termination of Gaussian 16 at now\\n'; printf checkpoint > results/test.chk"]
        self.spec["output_directories"] = ["results"]
        run = self.prepare()
        self.assertIn("mkdir -p -- results", run["generated_script"])
        self.jobs.job_submit(run["run_id"])
        self.assertTrue(self.jobs.job_sync(run["run_id"])["ok"])
        result = self.gaussian.result(run["run_id"], max_bytes=1024)["application_result"]
        self.assertEqual(result["state"], "succeeded")
        self.assertGreater(result["scanned_bytes"], 2000000)
        self.assertTrue((self.card.parent / "results/test.chk").exists())

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
            "--project-root", str(self.fixture.root), "--output", "test.log"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('unrecognized arguments', result.stderr)
        self.assertFalse((self.fixture.root / 'submissions').exists())
