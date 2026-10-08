"""Offline batch-job lifecycle tests using real Bash and local rsync."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from xn02_mcps.config import Cluster
from xn02_mcps.job_scheduler import parse_job_id, parse_status, query_commands, submit_command
from xn02_mcps.jobs import JobService
from xn02_mcps.ssh import CommandResult
from xn02_mcps.transfer import Transfer


class LocalSSH:
    def __init__(self, bin_dir):
        self.bin_dir = bin_dir
        self.commands = []
        self.lose_response = False

    def run(self, cluster, command):
        self.commands.append(command)
        env = dict(os.environ, PATH=str(self.bin_dir) + ":" + os.environ["PATH"])
        result = subprocess.run(["bash", "-e", "-c", command], capture_output=True,
                                text=True, env=env)
        if self.lose_response and "mkdir .xn02-submit-lock" in command:
            self.lose_response = False
            return CommandResult(-1, error="timeout")
        return CommandResult(result.returncode, result.stdout, result.stderr)


class LocalTransfer:
    def __init__(self):
        self.fail_upload = False
        self.fail_download = False

    def run(self, cluster, local, remote, download=False, patterns=None, **options):
        if (download and self.fail_download) or (not download and self.fail_upload):
            return CommandResult(23, stderr="simulated transfer failure")
        real_run = subprocess.run

        def localize(argv, **kwargs):
            args = list(argv)
            index = args.index("-e")
            del args[index:index + 2]
            prefix = cluster.ssh_host + ":"
            args = [a[len(prefix):] if a.startswith(prefix) else a for a in args]
            return real_run(args, **kwargs)

        with patch("xn02_mcps.transfer.subprocess.run", side_effect=localize):
            return Transfer().run(cluster, local, remote, download, patterns, **options)


@unittest.skipUnless(shutil.which("rsync"), "local rsync required for lifecycle tests")
class JobLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "inputs with spaces"
        self.source.mkdir()
        (self.source / "job.sh").write_text("#!/bin/bash\nprintf 'computed result\\n'\n"
                                           "mkdir results\nprintf '42\\n' > results/value.txt\n")
        (self.source / "data.txt").write_text("input data")
        self.remote = self.root / "remote with spaces"
        self.remote.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        # These stub commands accept the real scheduler argv and execute the batch locally.
        for scheduler in ("bsub", "sbatch"):
            command = self.bin / scheduler
            command.write_text("#!/usr/bin/env python3\n"
                "import pathlib, subprocess, sys\n"
                f"counter = pathlib.Path({str(self.root / 'submissions')!r})\n"
                "count = int(counter.read_text()) if counter.exists() else 0\n"
                "counter.write_text(str(count + 1))\n"
                "text = pathlib.Path(sys.argv[-1]).read_text() if "
                "pathlib.Path(sys.argv[0]).name == 'sbatch' else sys.stdin.read()\n"
                "with open('stdout.log','w') as out, open('stderr.log','w') as err:\n"
                "    result = subprocess.run(['bash'], input=text, text=True, stdout=out, stderr=err)\n"
                "pathlib.Path('.stub-exit').write_text(str(result.returncode))\n"
                "print('4242' if pathlib.Path(sys.argv[0]).name == 'sbatch' "
                "else 'Job <4242> is submitted to queue <test>.')\n")
            command.chmod(0o755)
        for name, content in {
            "bjobs": "printf '4242|DONE|0\\n'",
            "squeue": "true", "sacct": "printf '4242|COMPLETED|0:0\\n'",
            "bkill": "true", "scancel": "true",
        }.items():
            command = self.bin / name
            command.write_text("#!/bin/bash\n" + content + "\n")
            command.chmod(0o755)
        self.transport = LocalSSH(self.bin)
        self.transfer = LocalTransfer()
        self.config = {name: Cluster(name, "alias", name, str(self.remote))
                       for name in ("lsf", "slurm")}
        self.state = self.root / "state"
        self.jobs = JobService(self.config, self.state, self.transport, self.transfer)

    def prepare(self, scheduler="lsf"):
        return self.jobs.job_prepare(scheduler, str(self.source), "job.sh",
                                    ["*.log", "results/***"])["run"]

    def test_full_lifecycle_both_schedulers(self):
        for scheduler in ("lsf", "slurm"):
            with self.subTest(scheduler=scheduler):
                run = self.prepare(scheduler)
                self.assertFalse((Path(run["remote_dir"])).exists())
                submitted = self.jobs.job_submit(run["run_id"])
                self.assertTrue(submitted["ok"], submitted)
                self.assertEqual(submitted["run"]["job_id"], "4242")
                status = self.jobs.job_status(run["run_id"])
                self.assertEqual(status["run"]["state"], "succeeded")
                logs = self.jobs.job_logs(run["run_id"])
                self.assertEqual(logs["text"], "computed result\n")
                synced = self.jobs.job_sync(run["run_id"])
                self.assertTrue(synced["ok"], synced)
                self.assertTrue(synced["final"])
                output = Path(synced["run"]["output_dir"])
                self.assertEqual((output / "results/value.txt").read_text(), "42\n")
                self.assertFalse((output / "data.txt").exists())
                self.assertFalse((output / ".xn02-response").exists())
                self.assertTrue(self.jobs.job_cancel(run["run_id"])["ok"])
                self.assertTrue(self.jobs.job_submit(run["run_id"])["already_processed"])
        self.assertEqual((self.root / "submissions").read_text(), "2")
        restarted = JobService(self.config, self.state, self.transport, self.transfer)
        self.assertEqual(len(restarted.job_list()["runs"]), 2)
        self.assertEqual(len(restarted.job_list(limit=1, offset=1)["runs"]), 1)
        self.assertTrue(restarted.job_get(run["run_id"])["events"])

    def test_lost_response_recovered_without_duplicate(self):
        run = self.prepare()
        self.transport.lose_response = True
        result = self.jobs.job_submit(run["run_id"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["run"]["phase"], "submission_unknown")
        restarted = JobService(self.config, self.state, self.transport, self.transfer)
        recovered = restarted.job_submit(run["run_id"])
        self.assertTrue(recovered["ok"], recovered)
        self.assertEqual(recovered["run"]["job_id"], "4242")
        self.assertEqual((self.root / "submissions").read_text(), "1")

    def test_missing_receipt_never_resubmits(self):
        run = self.prepare()
        self.jobs.history.update(run["run_id"], "interrupted", phase="submitting")
        result = self.jobs.job_submit(run["run_id"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["run"]["phase"], "submission_unknown")
        self.assertFalse((self.root / "submissions").exists())

    def test_changed_source_does_not_change_prepared_snapshot(self):
        run = self.prepare()
        (self.source / "job.sh").write_text("exit 99")
        self.assertTrue(self.jobs.job_submit(run["run_id"])["ok"])
        self.assertEqual(self.jobs.job_logs(run["run_id"])["text"], "computed result\n")

    def test_snapshot_tampering_blocks_submission(self):
        run = self.prepare()
        (Path(run["snapshot_dir"]) / "job.sh").write_text("exit 99")
        with self.assertRaisesRegex(ValueError, "snapshot changed"):
            self.jobs.job_submit(run["run_id"])
        self.assertFalse((self.root / "submissions").exists())

    def test_failed_upload_can_retry_without_submission(self):
        run = self.prepare()
        self.transfer.fail_upload = True
        result = self.jobs.job_submit(run["run_id"])
        self.assertEqual(result["run"]["phase"], "upload_failed")
        self.assertFalse((self.root / "submissions").exists())
        self.transfer.fail_upload = False
        self.assertTrue(self.jobs.job_submit(run["run_id"])["ok"])

    def test_sync_failure_can_retry_and_does_not_overwrite(self):
        run = self.prepare()
        self.jobs.job_submit(run["run_id"])
        self.transfer.fail_download = True
        self.assertFalse(self.jobs.job_sync(run["run_id"])["ok"])
        self.transfer.fail_download = False
        first = self.jobs.job_sync(run["run_id"])
        second = self.jobs.job_sync(run["run_id"])
        self.assertNotEqual(first["run"]["output_dir"], second["run"]["output_dir"])
        self.assertEqual((self.root / "submissions").read_text(), "1")

    def test_missing_status_retains_last_observation_and_sync_is_partial(self):
        run = self.prepare()
        self.jobs.job_submit(run["run_id"])
        self.jobs.job_status(run["run_id"])
        (self.bin / "bjobs").write_text("#!/bin/bash\nexit 1\n")
        result = self.jobs.job_status(run["run_id"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["run"]["state"], "succeeded")
        synced = self.jobs.job_sync(run["run_id"])
        self.assertFalse(synced["final"])
        self.assertEqual(synced["run"]["sync_state"], "partial")

    def test_symlinks_and_unsafe_paths_rejected(self):
        (self.source / "linked").symlink_to(self.root)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            self.prepare()
        (self.source / "linked").unlink()
        for script in ("../job.sh", "/job.sh"):
            with self.assertRaises(ValueError):
                self.jobs.job_prepare("lsf", str(self.source), script)
        with self.assertRaises(ValueError):
            self.jobs.job_prepare("lsf", str(self.source), "job.sh", ["../secret"])

    def test_environment_change_blocks_remote_actions(self):
        run = self.prepare()
        changed = {"lsf": Cluster("lsf", "different-host", "lsf", str(self.remote))}
        jobs = JobService(changed, self.state, self.transport, self.transfer)
        with self.assertRaisesRegex(ValueError, "changed since preparation"):
            jobs.job_submit(run["run_id"])

    def test_concurrent_operations_rejected(self):
        run = self.prepare()
        with self.jobs.history.lock(run["run_id"]):
            with self.assertRaisesRegex(ValueError, "another"):
                self.jobs.job_submit(run["run_id"])

    def test_cancel_is_a_request_not_a_final_state(self):
        run = self.prepare()
        self.jobs.job_submit(run["run_id"])
        result = self.jobs.job_cancel(run["run_id"])
        self.assertTrue(result["run"]["cancel_requested"])
        self.assertEqual(result["run"]["state"], "pending")

    def test_rejected_submission_is_not_retried(self):
        run = self.prepare()
        (self.bin / "bsub").write_text("#!/bin/bash\nprintf 'queue closed\\n' >&2\nexit 1\n")
        result = self.jobs.job_submit(run["run_id"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["run"]["phase"], "rejected")
        count = len(self.transport.commands)
        self.assertFalse(self.jobs.job_submit(run["run_id"])["ok"])
        self.assertEqual(len(self.transport.commands), count)

    def test_default_sync_all_and_per_call_exclusions(self):
        run = self.jobs.job_prepare("lsf", str(self.source), "job.sh")["run"]
        self.assertEqual(run["output_mode"], "all")
        self.jobs.job_submit(run["run_id"])
        synced = self.jobs.job_sync(run["run_id"], excludes=["data.txt"])
        output = Path(synced["run"]["output_dir"])
        self.assertTrue((output / "job.sh").exists())
        self.assertFalse((output / "data.txt").exists())
        self.assertFalse((output / ".xn02-response").exists())

    def test_sync_overrides_selection_and_direct_layout(self):
        run = self.prepare()
        self.jobs.job_submit(run["run_id"])
        first = self.jobs.job_sync(run["run_id"], mode="all", layout="direct")
        output = Path(first["run"]["output_dir"])
        self.assertEqual(output, self.state / run["run_id"] / "outputs")
        self.assertTrue((output / "data.txt").exists())
        with self.assertRaisesRegex(ValueError, "destination exists"):
            self.jobs.job_sync(run["run_id"], layout="direct")
        replaced = self.jobs.job_sync(run["run_id"], includes=["results/***"],
                                      layout="direct", overwrite="replace")
        self.assertFalse((output / "data.txt").exists())
        self.assertTrue((Path(replaced["run"]["sync_backup"]) / "data.txt").exists())

    def test_sync_destination_merge_and_excludes_win(self):
        run = self.prepare()
        self.jobs.job_submit(run["run_id"])
        output = self.root / "chosen-output"
        output.mkdir()
        (output / "keep.txt").write_text("keep")
        result = self.jobs.job_sync(run["run_id"], includes=["*.log", "results/***"],
            excludes=["stderr.log"], destination=str(output), overwrite="merge",
            compress=True, checksum=False, timeout=45)
        self.assertTrue(result["ok"])
        self.assertTrue((output / "keep.txt").exists())
        self.assertTrue((output / "stdout.log").exists())
        self.assertFalse((output / "stderr.log").exists())
        self.assertEqual(result["run"]["sync_options"]["timeout"], 45)

    def test_prepare_input_exclusions_and_size_limit(self):
        run = self.jobs.job_prepare("lsf", str(self.source), "job.sh",
                                    input_exclude=["*.txt"])["run"]
        self.assertEqual([f["path"] for f in run["manifest"]], ["job.sh"])
        with self.assertRaisesRegex(ValueError, "max_input_bytes"):
            self.jobs.job_prepare("lsf", str(self.source), "job.sh", max_input_bytes=1)

    def test_sync_invalid_options_and_input_destination_rejected(self):
        run = self.prepare()
        self.jobs.job_submit(run["run_id"])
        for options in [{"mode": "bad"}, {"timeout": 0}, {"checksum": "yes"},
                        {"excludes": ["../outside"]}, {"destination": str(self.source)}]:
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.jobs.job_sync(run["run_id"], **options)

    def test_arrays_and_state_directory_as_input_rejected(self):
        (self.source / "job.sh").write_text("#!/bin/bash\n#SBATCH --array=0-10\ntrue\n")
        with self.assertRaisesRegex(ValueError, "arrays"):
            self.prepare("slurm")
        with self.assertRaisesRegex(ValueError, "state directory"):
            self.jobs.job_prepare("lsf", str(self.state), "job.sh")


class SchedulerTests(unittest.TestCase):
    def test_submission_ids(self):
        self.assertEqual(parse_job_id("lsf", "Job <123> is submitted to queue <q>.\n"), "123")
        self.assertEqual(parse_job_id("slurm", "123\n"), "123")
        self.assertEqual(parse_job_id("slurm", "123;cluster-a\n"), "123")
        with self.assertRaises(ValueError):
            parse_job_id("slurm", "123\n456\n")

    def test_scheduler_scope_and_working_directory(self):
        commands = query_commands("slurm", "123", "2026-10-08", "cluster-a")
        self.assertTrue(all("--clusters=cluster-a" in c for c in commands))
        command = submit_command("lsf", "r_example", "job.sh", "/shared/jobs with spaces")
        self.assertIn("-cwd '/shared/jobs with spaces'", command)
        self.assertIn("'/shared/jobs with spaces/stdout.log'", command)
        command = submit_command("slurm", "r_example", "--wrap=unexpected", "/jobs")
        self.assertTrue(command.endswith("./--wrap=unexpected"))

    def test_status_matches_requested_job_and_preserves_exit(self):
        self.assertIsNone(parse_status("slurm", "9|RUNNING|none", "8"))
        self.assertEqual(parse_status("slurm", "8|FAILED|1:0", "8", True)["state"], "failed")
        self.assertEqual(parse_status("lsf", "8|EXIT|7", "8")["exit_code"], "7")


if __name__ == "__main__":
    unittest.main()
