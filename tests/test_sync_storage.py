"""Real local rsync selection, recovery, storage isolation and detached workers."""

from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import time
import unittest
from unittest.mock import patch

import test_jobs
from hpc_mcp.ssh import CommandResult
from hpc_mcp.storage import InputCache


class SyncStorageTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_jobs.JobLifecycleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.jobs = self.fixture.jobs
        self.source = self.fixture.source
        self.project = self.fixture.root

    def submitted(self):
        run = self.jobs.job_prepare("lsf", str(self.source), "job.sh", ["results/***", "*.log", "*.chk"],
                                    project_root=str(self.project), input_files=["job.sh", "data.txt"])["run"]
        self.assertTrue(self.jobs.job_submit(run["run_id"])["ok"])
        return run

    def test_preview_exact_rules_limits_and_no_result_download(self):
        run = self.submitted()
        remote = Path(run["remote_dir"])
        (remote / "odd|name.chk").write_bytes(b"12345")
        (remote / "results" / "nested.chk").write_bytes(b"123")
        result = self.jobs.job_sync_preview(run["run_id"], includes=["*.chk"], excludes=["results/***"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["files"][0]["path"], "odd|name.chk")
        self.assertEqual(result["bytes"], 5)
        self.assertFalse((self.project / ".hpc-mcp-sync").exists())
        limited = self.jobs.job_sync(run["run_id"], includes=["*.chk"], max_file_bytes=2)
        self.assertFalse(limited["ok"])
        self.assertIn("max_file_bytes exceeded", limited["preview"]["blockers"])
        with patch("hpc_mcp.sync_support.shutil.disk_usage", return_value=shutil._ntuple_diskusage(100, 99, 1)):
            self.assertFalse(self.jobs.job_sync_preview(run["run_id"])["ok"])

    def test_resume_reuses_same_selection_and_changed_rules_isolate_staging(self):
        run = self.submitted()
        real = self.jobs.transfer.run
        attempts = []
        failed = [False]
        def lose_response(*args, **kwargs):
            if kwargs.get("download") and not kwargs.get("dry_run"):
                attempts.append(str(args[1]))
                result = real(*args, **kwargs)
                if not failed[0]:
                    failed[0] = True
                    return CommandResult(23, stderr="interrupted after partial download")
                return result
            return real(*args, **kwargs)
        with patch.object(self.jobs.transfer, "run", side_effect=lose_response):
            self.assertFalse(self.jobs.job_sync(run["run_id"])["ok"])
            result = self.jobs.job_sync(run["run_id"])
        self.assertTrue(result["ok"])
        self.assertTrue(result["resumed"])
        self.assertEqual(attempts[0], attempts[1])
        self.assertFalse((self.project / ".hpc-mcp-sync").exists())
        # Different parameters never reuse previous bytes, even if files happen to match.
        self.jobs.transfer.fail_download = True
        self.assertFalse(self.jobs.job_sync(run["run_id"], overwrite="merge")["ok"])
        prior = self.jobs.history.get(run["run_id"])["sync_options"]["staging_dir"]
        self.jobs.transfer.fail_download = False
        changed = self.jobs.job_sync(run["run_id"], overwrite="merge", checksum=False)
        self.assertTrue(changed["ok"])
        self.assertFalse(changed["resumed"])
        self.assertNotEqual(changed["run"]["sync_options"]["staging_dir"], prior)

    def test_source_change_during_download_is_not_installed(self):
        run = self.submitted()
        real = self.jobs.transfer.run
        def mutate(*args, **kwargs):
            result = real(*args, **kwargs)
            if kwargs.get("download") and not kwargs.get("dry_run"):
                (Path(run["remote_dir"]) / "results/value.txt").write_text("newer changed value")
            return result
        with patch.object(self.jobs.transfer, "run", side_effect=mutate):
            result = self.jobs.job_sync(run["run_id"])
        self.assertFalse(result["ok"])
        self.assertFalse((self.source / "results/value.txt").exists())
        self.assertTrue(Path(result["run"]["output_dir"]).exists())
        self.assertTrue(self.jobs.job_sync(run["run_id"])["ok"])

    def test_stable_only_rejects_running_and_failed_queries(self):
        run = self.submitted()
        bjobs = self.fixture.bin / "bjobs"
        bjobs.write_text("#!/bin/bash\nprintf '4242|RUN|0\\n'\n")
        with self.assertRaisesRegex(ValueError, "terminal"):
            self.jobs.job_sync(run["run_id"], stable_only=True)
        partial = self.jobs.job_sync(run["run_id"])
        self.assertTrue(partial["ok"])
        self.assertFalse(partial["final"])

    def test_input_cache_is_independent_and_capacity_cleanup_preserves_snapshots(self):
        self.jobs.clusters["lsf"] = replace(self.jobs.clusters["lsf"], input_cache=True)
        runs = [self.jobs.job_prepare("lsf", str(self.source), "job.sh")["run"] for _ in range(2)]
        first = Path(runs[0]["snapshot_dir"]) / "data.txt"
        second = Path(runs[1]["snapshot_dir"]) / "data.txt"
        first.write_text("tampered")
        self.assertEqual(second.read_text(), "input data")
        self.assertEqual((self.source / "data.txt").read_text(), "input data")
        preview = self.jobs.input_cache_cleanup(older_than_seconds=0, max_cache_bytes=0)
        self.assertGreater(preview["bytes"], 0)
        applied = self.jobs.input_cache_cleanup(older_than_seconds=0, max_cache_bytes=0, dry_run=False)
        self.assertEqual(applied["remaining_bytes"], 0)
        self.assertTrue(second.is_file())
        self.assertTrue(self.jobs.job_submit(runs[1]["run_id"])["ok"])

    def test_corrupt_cache_rebuilt_and_symlink_cache_rejected(self):
        cache = InputCache(self.jobs.history)
        source = self.source / "data.txt"
        target = self.project / "copied"
        cache.copy(source, target)
        blob = next(p for p in cache.root.iterdir() if len(p.name) == 64)
        blob.chmod(0o600)
        blob.write_bytes(b"corruption")
        cache.copy(source, target)
        self.assertEqual(target.read_text(), "input data")
        blob.unlink()
        blob.symlink_to(source)
        with self.assertRaisesRegex(ValueError, "symlink"):
            cache.copy(source, target)

    def test_local_cleanup_protects_latest_results_and_keeps_history(self):
        run = self.submitted()
        with self.assertRaisesRegex(ValueError, "complete"):
            self.jobs.job_storage_cleanup(run["run_id"])
        self.jobs.job_sync(run["run_id"])
        preview = self.jobs.job_storage_cleanup(run["run_id"], ["snapshot"], 0)
        self.assertTrue(Path(run["snapshot_dir"]).exists())
        self.assertEqual(len(preview["candidates"]), 1)
        self.jobs.job_storage_cleanup(run["run_id"], ["snapshot", "old_outputs"], 0, False)
        self.assertFalse(Path(run["snapshot_dir"]).exists())
        self.assertTrue((self.source / "results/value.txt").is_file())
        saved = self.jobs.job_get(run["run_id"])
        self.assertTrue(saved["run"]["snapshot_removed"])
        self.assertTrue(saved["run"]["manifest"])
        self.assertTrue(saved["events"])

    def test_remote_cleanup_validates_results_receipt_and_scope(self):
        run = self.submitted()
        self.jobs.job_sync(run["run_id"])
        directory = Path(run["remote_dir"])
        marker = self.fixture.remote / "outside.txt"
        marker.write_text("keep")
        preview = self.jobs.job_remote_cleanup(run["run_id"])
        self.assertTrue(preview["ok"], preview)
        self.assertTrue(directory.exists())
        response = directory / ".hpc-mcp-response"
        original = response.read_bytes()
        response.write_text("other job")
        self.assertFalse(self.jobs.job_remote_cleanup(run["run_id"], False)["ok"])
        self.assertTrue(directory.exists())
        response.write_bytes(original)
        self.assertTrue(self.jobs.job_remote_cleanup(run["run_id"], False)["ok"])
        self.assertFalse(directory.exists())
        self.assertEqual(marker.read_text(), "keep")
        self.assertTrue(self.jobs.job_get(run["run_id"])["run"]["remote_removed"])

    def test_detached_worker_uses_durable_operation_and_survives_service_recreation(self):
        run = self.submitted()
        ssh = self.fixture.bin / "ssh"
        ssh.write_text("#!/usr/bin/env python3\nimport os,sys\n"
                       "arguments=sys.argv[sys.argv.index('alias')+1:]\n"
                       "os.execvp('bash',['bash','-c',' '.join(arguments)])\n")
        ssh.chmod(0o755)
        with patch.dict(os.environ, PATH=str(self.fixture.bin) + ":" + os.environ["PATH"]):
            result = self.jobs.job_sync_start(run["run_id"])
        operation_id = result["operation"]["operation_id"]
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            result = self.jobs.job_sync_operation(operation_id)["operation"]
            if result["state"] not in ("running", "queued"):
                break
            time.sleep(0.1)
        self.assertEqual(result["state"], "succeeded", result)
        from hpc_mcp.jobs import JobService
        restarted = JobService(self.jobs.clusters, self.jobs.history.root)
        self.assertEqual(restarted.job_sync_operation(operation_id)["operation"]["state"], "succeeded")
        self.assertTrue((self.source / "results/value.txt").is_file())

    def test_partial_file_is_used_as_delta_basis_on_retry(self):
        run = self.submitted()
        data = b"0123456789" * 200000
        (Path(run["remote_dir"]) / "big.chk").write_bytes(data)
        real = self.jobs.transfer.run
        failed = [False]
        def partial(*args, **kwargs):
            if kwargs.get("download") and not kwargs.get("dry_run") and not failed[0]:
                failed[0] = True
                private = Path(args[1]) / ".hpc-mcp-transfer-partial"
                private.mkdir()
                (private / "big.chk").write_bytes(data[:1000000])
                return CommandResult(23, stderr="partial checkpoint")
            return real(*args, **kwargs)
        with patch.object(self.jobs.transfer, "run", side_effect=partial):
            self.assertFalse(self.jobs.job_sync(run["run_id"])["ok"])
            result = self.jobs.job_sync(run["run_id"])
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["resumed"])
        self.assertGreater(result["run"]["sync_transfer_stats"]["reused_bytes"], 0)
        self.assertEqual((self.source / "big.chk").read_bytes(), data)
        self.assertFalse((self.source / ".hpc-mcp-transfer-partial").exists())

    def test_expired_archives_and_old_outputs_preserve_latest(self):
        run = self.fixture.prepare()
        self.jobs.job_submit(run["run_id"])
        first = self.jobs.job_sync(run["run_id"])["run"]
        latest = self.jobs.job_sync(run["run_id"])["run"]
        candidates = self.jobs.job_storage_cleanup(run["run_id"], ["old_outputs"], 0)
        self.assertEqual([c["path"] for c in candidates["candidates"]], [first["output_dir"]])
        self.jobs.job_storage_cleanup(run["run_id"], ["old_outputs"], 0, False)
        self.assertFalse(Path(first["output_dir"]).exists())
        self.assertTrue(Path(latest["output_dir"]).is_dir())
        direct = self.jobs.job_sync(run["run_id"], layout="direct", overwrite="merge")["run"]
        self.assertEqual(self.jobs.job_storage_cleanup(run["run_id"], ["old_outputs"], 0)["candidates"], [])
        self.jobs.job_sync(run["run_id"], layout="direct", overwrite="replace")
        archives = self.jobs.job_storage_cleanup(run["run_id"], ["sync_history"], 0)
        self.assertTrue(archives["candidates"])
        self.jobs.job_storage_cleanup(run["run_id"], ["sync_history"], 0, False)
        self.assertTrue(Path(direct["output_dir"]).exists())

    def test_interrupted_operation_and_invalid_options_are_reported(self):
        from hpc_mcp.operations import initialize
        from hpc_mcp.history import now
        run = self.submitted()
        with self.assertRaisesRegex(ValueError, "unknown"):
            self.jobs.job_sync_start(run["run_id"], {"unknown": 1})
        initialize(self.jobs.history)
        value = {"operation_id": "op_interrupted", "run_id": run["run_id"], "state": "running", "pid": 999999,
                 "created_at": now()}
        with self.jobs.history.connect() as db:
            db.execute("INSERT INTO operations VALUES (?,?,?)", (value["operation_id"], run["run_id"], json.dumps(value)))
        self.assertEqual(self.jobs.job_sync_operation(value["operation_id"])["operation"]["state"], "interrupted")
        # Value validation happens in the worker and is persisted without any SSH call.
        result = self.jobs.job_sync_start(run["run_id"], {"resume": "invalid"})
        operation_id = result["operation"]["operation_id"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = self.jobs.job_sync_operation(operation_id)["operation"]
            if result["state"] not in ("running", "queued"):
                break
            time.sleep(0.1)
        self.assertEqual(result["state"], "failed")
        self.assertIn("boolean", result["error"])

    def test_cleanup_rejects_moved_run_directory_and_changed_local_outputs(self):
        run = self.submitted()
        self.jobs.job_sync(run["run_id"])
        file = self.source / "results/value.txt"
        file.write_text("modified local result")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.jobs.job_remote_cleanup(run["run_id"], False)
        self.assertTrue(Path(run["remote_dir"]).exists())
        root = self.jobs.history.root / run["run_id"]
        moved = self.project / "moved-run"
        root.rename(moved)
        root.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.jobs.job_storage_cleanup(run["run_id"], ["snapshot"], 0, False)
        self.assertTrue((moved / "input").is_dir())

    def test_nonproject_snapshot_retry_preserves_target_and_partial_staging(self):
        run = self.fixture.prepare()
        self.jobs.job_submit(run["run_id"])
        self.jobs.transfer.fail_download = True
        first = self.jobs.job_sync(run["run_id"])
        self.assertFalse(first["ok"])
        self.jobs.transfer.fail_download = False
        retried = self.jobs.job_sync(run["run_id"])
        self.assertTrue(retried["resumed"])
        self.assertEqual(retried["run"]["output_dir"], first["run"]["sync_options"]["destination"])
        self.assertTrue(Path(retried["run"]["output_dir"]).exists())

    def test_physical_growth_monitor_terminates_and_reaps_transfer(self):
        import subprocess
        import sys
        from hpc_mcp.transfer import Transfer
        staged = self.project / "growth"
        staged.mkdir()
        real_popen = subprocess.Popen
        processes = []
        def sender(*args, **kwargs):
            process = real_popen([sys.executable, "-c",
                "from pathlib import Path; import time; "
                f"Path({str(staged / 'growing.chk')!r}).write_bytes(b'x' * 100); time.sleep(5)"], **kwargs)
            processes.append(process)
            return process
        with patch("hpc_mcp.transfer.subprocess.Popen", side_effect=sender):
            result = Transfer().run(self.jobs.clusters["lsf"], staged, "/remote", download=True,
                                    max_total_bytes=10)
        self.assertEqual(result.error, "transfer_size_limit")
        self.assertIsNotNone(processes[0].returncode)
