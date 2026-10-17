"""Scoped cache/work-directory retention using local Bash/rsync and exact confirmations."""

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import test_jobs
import test_sync_storage
import test_external_approvals
from hpc_mcp.maintenance import MaintenanceService
from hpc_mcp.storage import InputCache
from hpc_mcp.approvals import ApprovalError


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_sync_storage.SyncStorageTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.jobs = self.fixture.jobs
        self.service = MaintenanceService(self.jobs)

    def synced(self):
        run = self.fixture.submitted()
        self.assertTrue(test_jobs.authorized_sync(self.jobs, run['run_id'])['ok'])
        test_jobs.release_retention(self.jobs, run['run_id'])
        return self.jobs.history.get(run['run_id'])

    def old(self, path):
        path = Path(path)
        if path.is_dir():
            for item in path.rglob('*'):
                os.utime(item, (1, 1))
        os.utime(path, (1, 1))

    def apply_local(self, preview):
        return self.service.local_cache_cleanup(dry_run=False,
            review_token=preview['review_token'], confirmation_note='Synthetic exact cleanup approval')

    def apply_remote(self, preview):
        return self.service.cluster_work_cleanup(dry_run=False,
            review_token=preview['review_token'], confirmation_note='Synthetic exact cleanup approval')

    def test_local_snapshot_preview_apply_and_repeat_preserve_outputs_history_authorization(self):
        run = self.synced()
        self.old(run['snapshot_dir'])
        approval = run['submission_authorization']
        preview = self.service.local_cache_cleanup(categories=['snapshot'])
        self.assertEqual(preview['candidate_count'], 1, preview)
        self.assertTrue(Path(run['snapshot_dir']).exists())
        self.assertTrue(self.apply_local(preview)['ok'])
        self.assertFalse(Path(run['snapshot_dir']).exists())
        self.assertTrue((Path(run['output_dir']) / 'results/value.txt').exists())
        self.assertEqual(self.jobs.history.get(run['run_id'])['submission_authorization'], approval)
        self.assertTrue(self.jobs.history.events(run['run_id']))
        self.assertTrue(self.apply_local(preview)['ok'])

    def test_shared_cache_cleanup_keeps_independent_active_snapshots(self):
        run = self.fixture.fixture.prepare()
        cache = InputCache(self.jobs.history)
        destination = self.fixture.project / 'copied'
        cache.copy(self.fixture.source / 'data.txt', destination)
        preview = self.service.local_cache_cleanup(categories=['input_cache'],
            older_than_seconds=0, max_input_cache_bytes=0)
        self.assertEqual(preview['candidate_count'], 1)
        self.assertTrue(self.apply_local(preview)['ok'])
        self.assertTrue(Path(run['snapshot_dir']).exists())
        self.assertEqual(destination.read_text(), 'input data')

    def test_default_categories_exclude_snapshot_and_latest_outputs(self):
        run = self.synced()
        self.old(run['snapshot_dir'])
        preview = self.service.local_cache_cleanup(older_than_seconds=0)
        self.assertFalse(any(c['category'] == 'snapshot' for c in preview['plan']['candidates']))
        self.apply_local(preview)
        self.assertTrue(Path(run['snapshot_dir']).exists())

    def test_active_unsynced_and_pending_installations_are_protected(self):
        active = self.fixture.fixture.prepare()
        unsynced = self.fixture.submitted()
        pending = self.synced()
        self.jobs.history.update(pending['run_id'], 'test_pending',
            sync_installation={'phase': 'pending', 'staging': '/tmp/not-owned'})
        preview = self.service.local_cache_cleanup(categories=['snapshot'], older_than_seconds=0)
        self.assertEqual(preview['candidate_count'], 0)
        self.assertEqual(len(preview['skipped']), 3)
        self.assertTrue(any('pending' in s['reason'] for s in preview['skipped']))
        self.apply_local(preview)
        for run in (active, unsynced, pending):
            self.assertTrue(Path(run['snapshot_dir']).exists())

    def test_plan_token_note_expiry_and_cross_tool_scope_are_required(self):
        run = self.synced()
        self.old(run['snapshot_dir'])
        preview = self.service.local_cache_cleanup(categories=['snapshot'])
        with self.assertRaises(ValueError):
            self.service.local_cache_cleanup(dry_run=False, review_token=preview['review_token'])
        with self.assertRaises(ValueError):
            self.service.local_cache_cleanup(dry_run=False, review_token='0' * 64, confirmation_note='yes')
        with self.assertRaises(ValueError):
            self.apply_remote(preview)
        with patch('hpc_mcp.maintenance.time.time', return_value=time.time() + 4000):
            with self.assertRaisesRegex(ValueError, 'expired'):
                self.apply_local(preview)
        self.assertTrue(Path(run['snapshot_dir']).exists())

    def test_changed_cache_or_output_is_not_deleted(self):
        run = self.synced()
        self.old(run['snapshot_dir'])
        preview = self.service.local_cache_cleanup(categories=['snapshot'])
        (Path(run['snapshot_dir']) / 'data.txt').write_text('changed')
        result = self.apply_local(preview)
        self.assertFalse(result['ok'])
        self.assertIn('changed', result['results'][0]['error'])
        self.old(run['snapshot_dir'])
        preview = self.service.local_cache_cleanup(categories=['snapshot'])
        (Path(run['output_dir']) / 'results/value.txt').write_text('changed retained output')
        self.assertFalse(self.apply_local(preview)['ok'])
        self.assertTrue(Path(run['snapshot_dir']).exists())

    def test_symlink_cache_entry_never_deletes_external_files(self):
        run = self.synced()
        snapshot = Path(run['snapshot_dir'])
        (snapshot / 'data.txt').unlink()
        (snapshot / 'data.txt').symlink_to(self.fixture.source / 'data.txt')
        preview = self.service.local_cache_cleanup(categories=['snapshot'], older_than_seconds=0)
        self.assertEqual(preview['candidate_count'], 0)
        self.assertTrue(preview['skipped'])
        self.assertTrue((self.fixture.source / 'data.txt').is_file())

    def test_local_cleanup_is_offline_and_filters_page_without_expanding_shared_cache(self):
        one = self.synced()
        with patch.object(self.jobs.transport, 'run') as network:
            preview = self.service.local_cache_cleanup(run_ids=[one['run_id']],
                older_than_seconds=0, limit=1)
            self.apply_local(preview)
            network.assert_not_called()
        self.assertNotIn('input_cache', preview['plan']['request']['categories'])
        with self.assertRaises(ValueError):
            self.service.local_cache_cleanup(run_ids=[one['run_id']], categories=['input_cache'])

    def test_remote_preview_and_apply_delete_only_registered_aged_directory(self):
        run = self.synced()
        self.old(run['remote_dir'])
        unknown = self.fixture.fixture.remote / 'user-directory'
        unknown.mkdir()
        (unknown / 'important').write_text('keep')
        self.old(unknown)
        authorization = self.jobs.history.get(run['run_id'])['submission_authorization']
        preview = self.service.cluster_work_cleanup('lsf')
        self.assertEqual(preview['candidate_count'], 1, preview)
        self.assertTrue(Path(run['remote_dir']).exists())
        result = self.apply_remote(preview)
        self.assertTrue(result['ok'], result)
        self.assertFalse(Path(run['remote_dir']).exists())
        self.assertTrue(unknown.is_dir())
        self.assertEqual(self.jobs.history.get(run['run_id'])['submission_authorization'], authorization)
        self.assertTrue(self.apply_remote(preview)['ok'])

    def test_remote_young_directory_and_new_files_after_preview_are_retained(self):
        run = self.synced()
        preview = self.service.cluster_work_cleanup('lsf')
        self.assertEqual(preview['candidate_count'], 0)
        self.old(run['remote_dir'])
        preview = self.service.cluster_work_cleanup('lsf')
        (Path(run['remote_dir']) / 'new-checkpoint.chk').write_text('not retained')
        result = self.apply_remote(preview)
        self.assertFalse(result['ok'])
        self.assertTrue(Path(run['remote_dir']).exists())

    def test_remote_retention_scan_failure_never_deletes(self):
        run = self.synced()
        self.old(run['remote_dir'])
        preview = self.service.cluster_work_cleanup('lsf')
        self.assertEqual(preview['candidate_count'], 1)
        find = self.fixture.fixture.bin / 'find'
        find.write_text('#!/bin/bash\nexit 7\n')
        find.chmod(0o755)
        self.assertFalse(self.apply_remote(preview)['ok'])
        self.assertTrue(Path(run['remote_dir']).is_dir())

    def test_remote_running_state_changed_results_and_config_drift_are_refused(self):
        run = self.synced()
        self.old(run['remote_dir'])
        preview = self.service.cluster_work_cleanup('lsf')
        self.jobs.clusters['lsf'] = replace(self.jobs.clusters['lsf'], command_timeout=50)
        self.assertFalse(self.apply_remote(preview)['ok'])
        self.jobs.clusters['lsf'] = replace(self.jobs.clusters['lsf'], command_timeout=30)
        (Path(run['remote_dir']) / 'results/value.txt').write_text('remote changed')
        self.old(run['remote_dir'])
        self.assertFalse(self.apply_remote(preview)['ok'])

    def test_failed_tasks_require_explicit_inclusion_and_no_compute_is_reauthorized(self):
        run = self.synced()
        self.old(run['snapshot_dir'])
        self.jobs.history.update(run['run_id'], 'test_failed', state='failed')
        self.assertEqual(self.service.local_cache_cleanup(categories=['snapshot'])['candidate_count'], 0)
        preview = self.service.local_cache_cleanup(categories=['snapshot'], include_failed=True)
        self.assertEqual(preview['candidate_count'], 1)
        self.assertTrue(self.apply_local(preview)['ok'])

    def test_revocation_after_preview_invalidates_maintenance_without_restoring_job_permission(self):
        run = self.synced()
        self.old(run['snapshot_dir'])
        preview = self.service.local_cache_cleanup(categories=['snapshot'])
        self.jobs.job_revoke_authorization(run['run_id'])
        self.assertFalse(self.apply_local(preview)['ok'])
        self.assertFalse(self.jobs.history.get(run['run_id'])['submission_authorization'])

    def test_cli_preview_and_apply_share_exact_persisted_plan(self):
        run = self.synced()
        self.old(run['snapshot_dir'])
        args = [sys.executable, '-m', 'hpc_mcp', '--state-dir', str(self.jobs.history.root)]
        env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
        result = subprocess.run(args + ['local-cache-cleanup', '--categories', 'snapshot'],
            capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        preview = json.loads(result.stdout)
        result = subprocess.run(args + ['local-cache-cleanup', '--apply',
            '--review-token', preview['review_token'], '--confirmation-note', 'Synthetic CLI approval'],
            capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(Path(run['snapshot_dir']).exists())


@unittest.skipUnless(test_external_approvals.Ed25519PrivateKey, 'cryptography required')
class MaintenanceSignedTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_external_approvals.ExternalApprovalTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.jobs = self.fixture.jobs
        self.service = MaintenanceService(self.jobs)
        rid = self.fixture.fixture.prepare()['run_id']
        plan = self.jobs.job_submission_plan(rid)
        self.fixture.endorse('job_authorize', rid, plan['review_token'])
        self.jobs.job_authorize(rid, plan['review_token'], 'Synthetic signed computation approval')
        self.jobs.job_submit(rid)
        self.jobs.job_sync(rid)
        from hpc_mcp.retention import RetentionService
        retention = RetentionService(self.jobs)
        released = retention.release('lsf', [rid], reason='Synthetic signed release')
        self.fixture.endorse('retention_release', 'lsf', released['review_token'])
        retention.release(dry_run=False, review_token=released['review_token'],
                          confirmation_note='Synthetic signed retention release')
        self.rid = rid

    def test_independent_signature_required_for_cleanup_and_wrong_scope_rejected(self):
        preview = self.service.local_cache_cleanup(categories=['snapshot'], older_than_seconds=0)
        self.assertEqual(preview['candidate_count'], 1)
        with self.assertRaises(ApprovalError):
            self.service.local_cache_cleanup(dry_run=False, review_token=preview['review_token'],
                confirmation_note='fabricated note cannot replace signature')
        self.fixture.endorse('local_cache_cleanup', 'local-cache', preview['review_token'])
        result = self.service.local_cache_cleanup(dry_run=False, review_token=preview['review_token'],
            confirmation_note='Synthetic separately signed cleanup')
        self.assertTrue(result['ok'], result)
        self.assertTrue(self.jobs.history.get(self.rid)['submission_authorization'])


if __name__ == '__main__':
    unittest.main()
