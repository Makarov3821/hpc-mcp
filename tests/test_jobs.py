"""Offline batch-job lifecycle tests using real Bash and local rsync."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from hpc_mcp.config import Cluster
from hpc_mcp.job_scheduler import parse_job_id, parse_status, query_commands, submit_command
from hpc_mcp.jobs import JobService
from hpc_mcp.ssh import CommandResult
from hpc_mcp.transfer import Transfer


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
        if self.lose_response and any(f"mkdir {prefix}-submit-lock" in command
                                     for prefix in (".hpc-mcp", ".xn02")):
            self.lose_response = False
            return CommandResult(-1, error="timeout")
        return CommandResult(result.returncode, result.stdout, result.stderr)


class LocalTransfer:
    def __init__(self):
        self.fail_upload = False
        self.fail_download = False

    def run(self, cluster, local, remote, download=False, patterns=None, **options):
        if (download and self.fail_download and not options.get("dry_run")) or (not download and self.fail_upload):
            return CommandResult(23, stderr="simulated transfer failure")
        real_run = subprocess.Popen

        def localize(argv, **kwargs):
            args = list(argv)
            index = args.index("-e")
            del args[index:index + 2]
            prefix = cluster.ssh_host + ":"
            args = [a[len(prefix):] if a.startswith(prefix) else a for a in args]
            return real_run(args, **kwargs)

        with patch("hpc_mcp.transfer.subprocess.Popen", side_effect=localize):
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
                self.assertFalse((output / ".hpc-mcp-response").exists())
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

    def test_legacy_submission_recovery_and_receipt_filtering(self):
        run = self.prepare()
        # Old records do not carry the new receipt protocol field.
        with self.jobs.history.connect() as db:
            db.execute("UPDATE runs SET data=json_remove(data, '$.internal_prefix') WHERE run_id=?",
                       (run["run_id"],))
        self.transport.lose_response = True
        result = self.jobs.job_submit(run["run_id"])
        self.assertEqual(result["run"]["phase"], "submission_unknown")
        self.assertTrue((Path(run["remote_dir"]) / ".xn02-response").exists())
        restarted = JobService(self.config, self.state, self.transport, self.transfer)
        self.assertTrue(restarted.job_recover(run["run_id"])["ok"])
        self.assertEqual((self.root / "submissions").read_text(), "1")
        synced = restarted.job_sync(run["run_id"], mode="all")
        self.assertTrue(synced["ok"], synced)
        self.assertFalse(any(p.name.startswith((".xn02-", ".hpc-mcp-"))
                             for p in Path(synced["run"]["output_dir"]).iterdir()))

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

    def test_lsf_cancel_request_does_not_determine_final_state(self):
        run = self.prepare()
        self.jobs.job_submit(run["run_id"])
        self.jobs.job_cancel(run["run_id"])
        replies = [CommandResult(0, "4242|EXIT|130|\n"),
                   CommandResult(0, "4242|EXIT|130\n"),
                   *[CommandResult(1, stderr="not found") for _ in range(3)]]
        with patch.object(self.transport, "run", side_effect=replies):
            result = self.jobs.job_status(run["run_id"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["run"]["state"], "failed")
        self.assertTrue(result["run"]["cancel_requested"])
        self.assertIsNone(result["run"]["termination_reason"])

    def test_lsf_exit_reason_comes_from_scheduler(self):
        run = self.prepare()
        self.jobs.job_submit(run["run_id"])
        self.jobs.job_cancel(run["run_id"])
        for keyword, expected in (("TERM_OWNER", "cancelled"), ("TERM_RUNLIMIT", "failed")):
            with self.subTest(keyword=keyword), patch.object(self.transport, "run",
                return_value=CommandResult(0, f"4242|EXIT|130|{keyword}: scheduler reason\n")):
                result = self.jobs.job_status(run["run_id"])
            self.assertEqual(result["run"]["state"], expected)
            self.assertEqual(result["run"]["termination_reason"], keyword)
            self.assertEqual(result["run"]["status_source"], "bjobs")

    def test_lsf_conflicting_history_does_not_reclassify_current_failure(self):
        run = self.prepare()
        self.jobs.job_submit(run["run_id"])
        output = ("Job <4242>, Status <EXIT>, Command <job.sh>\n"
                  "Thu Oct 8 12:00:00 2026: Exited with exit code 130.\n"
                  "Thu Oct 8 12:00:00 2026: Completed <exit>; TERM_OWNER: job killed by owner.\n")
        with patch.object(self.transport, "run", side_effect=[
            CommandResult(0, "4242|EXIT|7|\n"), CommandResult(0, "4242|EXIT|7\n"),
            CommandResult(1), CommandResult(0, output), CommandResult(1)]):
            result = self.jobs.job_status(run["run_id"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["run"]["state"], "failed")
        self.assertEqual(result["run"]["exit_code"], "7")
        self.assertIsNone(result["run"]["termination_reason"])
        self.assertIn("ignored", result["run"]["status_diagnostics"][3])

    def test_lsf_older_bjobs_format_remains_supported(self):
        run = self.prepare()
        self.jobs.job_submit(run["run_id"])
        with patch.object(self.transport, "run", side_effect=[
            CommandResult(1, stderr="unsupported exit_reason field"),
            CommandResult(0, "4242|RUN|-\n")]):
            result = self.jobs.job_status(run["run_id"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["run"]["state"], "running")

    def test_lsf_long_output_enriches_exit_without_losing_code(self):
        run = self.prepare()
        self.jobs.job_submit(run["run_id"])
        output = ("Job <4242>, User <user>, Status <EXIT>, Command <job.sh>\n"
                  "Thu Oct 8 12:00:00 2026: Completed <exit>; TERM_OWNER: job killed by owner.\n")
        with patch.object(self.transport, "run", side_effect=[
            CommandResult(0, "4242|EXIT|130|\n"), CommandResult(0, "4242|EXIT|130\n"),
            CommandResult(0, output)]):
            result = self.jobs.job_status(run["run_id"])
        self.assertEqual(result["run"]["state"], "cancelled")
        self.assertEqual(result["run"]["exit_code"], "130")
        self.assertEqual(result["run"]["status_source"], "bjobs_long")

    def test_lsf_expired_record_falls_back_to_accounting_then_history(self):
        run = self.prepare()
        self.jobs.job_submit(run["run_id"])
        for source in ("bacct", "bhist"):
            output = (f"Job <4242>, User <user>, Job Name <{run['run_id']}>, Command <job.sh>\n"
                      "Thu Oct 8 12:00:00 2026: Done successfully. The CPU time used is 0.4 seconds;\n")
            replies = [CommandResult(1, stderr="not found") for _ in range(3)]
            if source == "bhist":
                replies.append(CommandResult(1, stderr="accounting log unavailable"))
            replies.append(CommandResult(0, output))
            with self.subTest(source=source), patch.object(self.transport, "run", side_effect=replies):
                result = self.jobs.job_status(run["run_id"])
            self.assertTrue(result["ok"])
            self.assertEqual(result["run"]["state"], "succeeded")
            self.assertEqual(result["run"]["status_source"], source)
            self.assertEqual(result["run"]["exit_code"], "0")

    def test_lsf_malformed_fallback_keeps_last_observation(self):
        run = self.prepare()
        self.jobs.job_submit(run["run_id"])
        self.jobs.job_status(run["run_id"])
        output = "Job <4242>, Status <DONE>\nJob <4242>, Status <EXIT>\n"
        with patch.object(self.transport, "run", side_effect=[
            CommandResult(1), CommandResult(1), CommandResult(1),
            CommandResult(0, output), CommandResult(0, "Summary only")]):
            result = self.jobs.job_status(run["run_id"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["run"]["state"], "succeeded")
        self.assertIn("parse_error", result["run"]["status_diagnostics"][-1])

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
        self.assertFalse((output / ".hpc-mcp-response").exists())

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

    def project_task(self, name="test1", outputs=None):
        (self.source / f"{name}.gjf").write_text("input card")
        (self.source / "job.sh").write_text(
            f"#!/bin/bash\nprintf 'result' > {name}.log\nprintf 'checkpoint' > {name}.chk\n")
        return self.jobs.job_prepare("lsf", str(self.source), "job.sh",
            outputs=outputs or [f"{name}.log", f"{name}.chk"], project_root=str(self.root),
            input_files=[f"{name}.gjf"])["run"]

    def test_project_tasks_select_inputs_and_return_beside_cards(self):
        runs = [self.project_task(name) for name in ("test1", "test2")]
        self.assertNotEqual(runs[0]["remote_dir"], runs[1]["remote_dir"])
        for index, run in enumerate(runs, 1):
            self.assertEqual({f["path"] for f in run["manifest"]}, {"job.sh", f"test{index}.gjf"})
            self.assertTrue(self.jobs.job_submit(run["run_id"])["ok"])
            result = self.jobs.job_sync(run["run_id"])
            self.assertTrue(result["ok"], result)
            self.assertEqual(Path(result["run"]["output_dir"]), self.source)
            self.assertEqual((self.source / f"test{index}.chk").read_text(), "checkpoint")
            self.assertFalse((self.root / ".hpc-mcp-sync").exists())
            self.assertFalse((self.state / run["run_id"] / "outputs").exists())

    def test_project_return_preserves_inputs_and_handles_output_collisions(self):
        run = self.project_task()
        self.jobs.job_submit(run["run_id"])
        (self.source / "test1.log").write_text("previous result")
        result = self.jobs.job_sync(run["run_id"])
        self.assertFalse(result["ok"])
        self.assertEqual((self.source / "test1.log").read_text(), "previous result")
        self.assertFalse((self.source / "test1.chk").exists())
        self.assertTrue(self.jobs.job_sync(run["run_id"], overwrite="merge")["ok"])
        result = self.jobs.job_sync(run["run_id"], includes=["test1.gjf"], overwrite="merge")
        self.assertFalse(result["ok"])
        self.assertIn("overwrite an input", result["error"])
        self.assertEqual((self.source / "test1.gjf").read_text(), "input card")
        with self.assertRaises(ValueError):
            self.jobs.job_sync(run["run_id"], overwrite="replace")

    def test_project_failed_cache_cleanup_is_scoped_and_locked(self):
        run = self.project_task()
        self.jobs.job_submit(run["run_id"])
        self.transfer.fail_download = True
        failed = self.jobs.job_sync(run["run_id"])
        attempt = Path(failed["run"]["output_dir"])
        self.assertTrue(attempt.is_relative_to(self.root / ".hpc-mcp-sync"))
        (attempt / "partial.log").write_text("partial")
        preview = self.jobs.job_cache_cleanup(run["run_id"], 0)
        self.assertEqual(preview["bytes"], 7)
        self.assertTrue(attempt.exists())
        with self.jobs.history.lock(run["run_id"]), self.assertRaisesRegex(ValueError, "another"):
            self.jobs.job_cache_cleanup(run["run_id"], 0, False)
        self.jobs.job_cache_cleanup(run["run_id"], 0, False)
        self.assertFalse((self.root / ".hpc-mcp-sync").exists())
        self.assertTrue(Path(run["snapshot_dir"]).exists())
        self.assertTrue((self.source / "test1.gjf").exists())
        self.assertIsNone(self.jobs.job_get(run["run_id"])["run"]["output_dir"])

    def test_project_cache_and_result_symlinks_rejected(self):
        run = self.project_task(outputs=["nested/result.log"])
        self.jobs.job_submit(run["run_id"])
        remote = Path(run["remote_dir"])
        (remote / "nested").mkdir()
        (remote / "nested/result.log").write_text("result")
        outside = self.root / "outside"
        outside.mkdir()
        (self.source / "nested").symlink_to(outside, target_is_directory=True)
        result = self.jobs.job_sync(run["run_id"])
        self.assertFalse(result["ok"])
        self.assertFalse((outside / "result.log").exists())
        self.jobs.job_cache_cleanup(run["run_id"], 0, False)
        (self.root / ".hpc-mcp-sync").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.jobs.job_sync(run["run_id"])
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.jobs.job_cache_cleanup(run["run_id"], 0, False)

    def test_project_preparation_requires_explicit_output_selection(self):
        for options in ({}, {"outputs": []}, {"outputs": ["*.log"], "output_mode": "all"}):
            with self.assertRaisesRegex(ValueError, "explicit filtered outputs"):
                self.jobs.job_prepare("lsf", str(self.source), "job.sh",
                                      project_root=str(self.root), **options)
        with self.assertRaisesRegex(ValueError, "within"):
            self.jobs.job_prepare("lsf", str(self.source), "job.sh", outputs=["*.log"],
                                  project_root=str(self.remote))


class SchedulerTests(unittest.TestCase):
    def test_lsf_long_history_ignores_other_jobs_and_post_processing(self):
        text = ("Job <9>, Status <DONE>, Command <another job>\n"
                "Job <8>, User <user>, Command <echo TERM_OWNER>\n"
                "Thu Oct 8 12:00:00 2026: Exited with exit code 7. The CPU time used is 1 seconds;\n"
                "Thu Oct 8 12:00:01 2026: Post job process done successfully;\n")
        detail = parse_status("lsf", text, "8", source="bhist")
        self.assertEqual(detail["state"], "failed")
        self.assertEqual(detail["exit_code"], "7")
        self.assertIsNone(detail["termination_reason"])
        self.assertIsNone(parse_status("lsf", text, "10", source="bacct"))

    def test_lsf_long_cancellation_signal_limits_and_rerun(self):
        header = "Job <8>, User <user>, Command <job.sh>\n"
        for reason, expected in (("TERM_OWNER", "cancelled"), ("TERM_FORCE_ADMIN", "cancelled"),
                                 ("TERM_RUNLIMIT", "failed"), ("TERM_EXTERNAL_SIGNAL", "failed")):
            text = header + ("Thu Oct 8 12:00:00 2026: Exited by signal 9.\n"
                             f"Thu Oct 8 12:00:00 2026: Completed <exit>; {reason}: description.\n")
            with self.subTest(reason=reason):
                detail = parse_status("lsf", text, "8", source="bacct")
                self.assertEqual(detail["state"], expected)
                self.assertEqual(detail["signal"], "9")
                self.assertIsNone(detail["exit_code"])
                self.assertEqual(detail["termination_reason"], reason)
        text += "Thu Oct 8 12:01:00 2026: Requeued to queue <normal>;\n"
        detail = parse_status("lsf", text, "8", source="bhist")
        self.assertEqual(detail["state"], "pending")
        self.assertIsNone(detail["termination_reason"])

    def test_lsf_history_rejects_ambiguous_or_recycled_identity(self):
        for text in ("Job <8>, Status <DONE>\nJob <8>, Status <EXIT>\n",
                     "Job <8>, Job Name <different_run>, Status <DONE>\n"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_status("lsf", text, "8", source="bacct", expected_name="r_expected")
        commands = query_commands("lsf", "8", "2026-10-08T09:00:00+00:00")
        self.assertIn("-S 2026/10/07/00:00,", commands[3])
        self.assertIn("-n 0", commands[4])

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
