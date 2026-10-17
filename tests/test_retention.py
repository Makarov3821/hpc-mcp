"""Shared registry across independent clients, real subprocesses, no live cluster."""

import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import shlex
import sqlite3
import time
import unittest
from unittest.mock import patch

import test_jobs
import test_sync_storage
import test_external_approvals
from hpc_mcp.jobs import JobService
from hpc_mcp.maintenance import MaintenanceService
from hpc_mcp.retention import RetentionService, registration_facts
from hpc_mcp.ssh import CommandResult


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_sync_storage.SyncStorageTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.jobs = self.fixture.jobs
        self.service = RetentionService(self.jobs)

    def synced(self):
        run = self.fixture.submitted()
        self.assertTrue(test_jobs.authorized_sync(self.jobs, run['run_id'])['ok'])
        return self.jobs.history.get(run['run_id'])

    def release(self, service, rid, holder='submission'):
        plan = service.release('lsf', [rid], holder, 'Synthetic phase complete')
        return service.release(dry_run=False, review_token=plan['review_token'],
                               confirmation_note='Synthetic user confirmation')

    def other(self):
        jobs = JobService(self.jobs.clusters, self.fixture.fixture.root / 'other-client',
            transport=self.jobs.transport, transfer=self.jobs.transfer)
        return RetentionService(jobs)

    def node(self, hostname, action='probe', **values):
        import hpc_mcp.registry_remote as helper
        source = Path(helper.__file__).read_text().replace('host = socket.gethostname()',
                                                         'host = ' + repr(hostname))
        data = {'root': self.jobs.clusters['lsf'].work_root, 'action': action, **values}
        command = 'python3 -c ' + shlex.quote(source) + ' ' + shlex.quote(
            base64.b64encode(json.dumps(data).encode()).decode())
        return self.jobs.transport.run(self.jobs.clusters['lsf'], command)

    def test_submission_and_sync_keep_default_hold_and_observe_receipts(self):
        run = self.synced()
        task = self.service.status('lsf', [run['run_id']])['tasks'][0]
        self.assertFalse(task['cleanup_allowed'])
        self.assertFalse(task['holds'][self.service.holder()]['released'])
        self.assertEqual(task['observations'][self.service.client_id]['sync_state'], 'complete')
        self.assertEqual(task['observations'][self.service.client_id]['job_id'], '4242')
        preview = MaintenanceService(self.jobs).cluster_work_cleanup('lsf', older_than_seconds=0)
        self.assertEqual(preview['candidate_count'], 0)
        local = MaintenanceService(self.jobs).local_cache_cleanup(categories=['snapshot'], older_than_seconds=0)
        self.assertEqual(local['candidate_count'], 0)

    def test_independent_clients_share_claims_and_cannot_release_each_other(self):
        run = self.synced()
        rid = run['run_id']
        other = self.other()
        other.hold('lsf', [rid], 'analysis', 'Needs checkpoint for a later stage')
        self.release(self.service, rid)
        self.assertFalse(self.service.status('lsf', [rid])['tasks'][0]['cleanup_allowed'])
        with self.assertRaisesRegex(ValueError, 'no claim'):
            self.service.release('lsf', [rid], 'analysis', 'Cannot impersonate another client')
        self.release(other, rid, 'analysis')
        self.assertTrue(self.service.status('lsf', [rid])['tasks'][0]['cleanup_allowed'])
        test_jobs.authorize(self.jobs, rid, remote_cleanup=True)
        self.assertTrue(self.jobs.job_remote_cleanup(rid, False)['ok'])
        self.assertFalse(Path(run['remote_dir']).exists())

    def test_same_installation_uses_distinct_labels_and_stable_identity_after_restart(self):
        run = self.synced()
        rid = run['run_id']
        self.service.hold('lsf', [rid], 'agent-a', 'Stage A')
        self.service.hold('lsf', [rid], 'agent-b', 'Stage B')
        self.release(self.service, rid)
        self.release(self.service, rid, 'agent-a')
        restarted = RetentionService(self.jobs)
        self.assertEqual(restarted.client_id, self.service.client_id)
        self.assertFalse(restarted.status('lsf', [rid])['tasks'][0]['cleanup_allowed'])
        self.release(restarted, rid, 'agent-b')
        self.assertTrue(restarted.status('lsf', [rid])['tasks'][0]['cleanup_allowed'])

    def test_release_is_explicit_frozen_expiring_and_idempotent(self):
        run = self.synced()
        rid = run['run_id']
        plan = self.service.release('lsf', [rid], reason='Stage done')
        with self.assertRaises(ValueError):
            self.service.release(dry_run=False, review_token=plan['review_token'])
        self.service.hold('lsf', [rid], 'other-stage', 'Still needed')
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.service.release(dry_run=False, review_token=plan['review_token'], confirmation_note='Test')
        plan = self.service.release('lsf', [rid], reason='Stage done')
        with patch('hpc_mcp.retention.time.time', return_value=plan['plan']['expires_at'] + 1):
            with self.assertRaisesRegex(ValueError, 'expired'):
                self.service.release(dry_run=False, review_token=plan['review_token'], confirmation_note='Test')
        result = self.service.release(dry_run=False, review_token=plan['review_token'], confirmation_note='Test')
        self.assertTrue(result['ok'])
        repeated = self.service.release(dry_run=False, review_token=plan['review_token'], confirmation_note='Test')
        self.assertEqual(result['tasks'], repeated['tasks'])
        self.service.hold('lsf', [rid], 'submission', 'Needs original files again')
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.service.release(dry_run=False, review_token=plan['review_token'], confirmation_note='Test')

    def test_new_hold_invalidates_remote_cleanup_preview(self):
        run = self.synced()
        rid = run['run_id']
        self.release(self.service, rid)
        maintenance = MaintenanceService(self.jobs)
        plan = maintenance.cluster_work_cleanup('lsf', older_than_seconds=0)
        # Age=0 is intentionally conservative at second boundaries.
        if not plan['candidate_count']:
            for path in Path(run['remote_dir']).rglob('*'):
                os.utime(path, (1, 1))
            os.utime(run['remote_dir'], (1, 1))
            plan = maintenance.cluster_work_cleanup('lsf', older_than_seconds=0)
        self.assertEqual(plan['candidate_count'], 1)
        other = self.other()
        other.hold('lsf', [rid], 'next-stage', 'Later work')
        self.release(other, rid, 'next-stage')
        result = maintenance.cluster_work_cleanup(dry_run=False, review_token=plan['review_token'],
                                                 confirmation_note='Synthetic cleanup confirmation')
        self.assertFalse(result['ok'])
        self.assertTrue(Path(run['remote_dir']).exists())

    def test_actual_host_conflict_is_persistent_and_second_host_does_not_open_db(self):
        run = self.synced()
        rid = run['run_id']
        self.release(self.service, rid)
        database = self.fixture.fixture.remote / '.hpc-mcp-registry/registry.sqlite3'
        metadata = database.stat().st_mtime_ns
        result = self.node('different-login-node')
        self.assertTrue(result.ok, result)
        self.assertTrue(json.loads(result.stdout)['node_conflict'])
        self.assertEqual(database.stat().st_mtime_ns, metadata)
        status = self.service.status('lsf', [rid])
        self.assertTrue(status['node_conflict'])
        self.assertFalse(status['tasks'][0]['cleanup_allowed'])
        with self.assertRaisesRegex(ValueError, 'conflict'):
            self.jobs.job_remote_cleanup(rid)
        foreign = self.node('different-login-node', 'status', run_ids=[rid])
        self.assertFalse(foreign.ok)
        self.assertTrue(Path(run['remote_dir']).exists())

    def test_conflict_reconciliation_requires_exact_confirmation_and_keeps_holds(self):
        run = self.synced()
        self.node('another-login')
        plan = self.service.reconcile('lsf')
        with self.assertRaises(ValueError):
            self.service.reconcile(dry_run=False, review_token=plan['review_token'])
        self.node('third-login')
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.service.reconcile(dry_run=False, review_token=plan['review_token'], confirmation_note='Test')
        plan = self.service.reconcile('lsf')
        self.assertTrue(self.service.reconcile(dry_run=False, review_token=plan['review_token'],
                                              confirmation_note='All clients unified on owner host')['ok'])
        status = self.service.status('lsf', [run['run_id']])
        self.assertFalse(status['node_conflict'])
        self.assertFalse(status['tasks'][0]['cleanup_allowed'])

    def test_readonly_probe_does_not_create_metadata_or_record_hosts(self):
        root = self.fixture.fixture.remote / '.hpc-mcp-registry'
        status = self.service.probe('lsf', initialize=False)
        self.assertFalse(status['initialized'])
        self.assertFalse(root.exists())
        self.service.probe('lsf')
        result = self.node('other-login', initialize=False)
        self.assertTrue(json.loads(result.stdout)['node_conflict'])
        self.assertFalse(list(root.glob('conflict-*.json')))

    def test_path_alias_hint_is_separate_from_remote_workspace_identity(self):
        self.jobs.clusters['alias'] = replace(self.jobs.clusters['lsf'], name='alias', ssh_host='other-alias')
        first = self.service.probe('lsf')
        second = self.service.probe('alias')
        self.assertEqual(first['workspace_id'], second['workspace_id'])
        self.assertIn('alias', first['configured_aliases'])
        self.assertFalse(first['node_conflict'])

    def test_missing_registry_and_legacy_tasks_are_protected_until_explicitly_adopted(self):
        run = self.synced()
        db = self.fixture.fixture.remote / '.hpc-mcp-registry/registry.sqlite3'
        with sqlite3.connect(db) as connection:
            connection.execute('DELETE FROM tasks')
        self.assertFalse(self.service.status('lsf', [run['run_id']])['tasks'][0]['registered'])
        with self.assertRaises(ValueError):
            self.jobs.job_remote_cleanup(run['run_id'])
        self.service.hold('lsf', [run['run_id']], 'submission', 'Explicitly adopt old task as held')
        self.assertFalse(self.service.status('lsf', [run['run_id']])['tasks'][0]['cleanup_allowed'])

    def test_registration_failure_blocks_upload_and_submission(self):
        run = self.fixture.fixture.prepare()
        test_jobs.authorize(self.jobs, run['run_id'])
        with patch.object(self.jobs.transport, 'run', return_value=CommandResult(127, stderr='python3 missing')), \
                patch.object(self.jobs.transfer, 'run') as transfer:
            result = self.jobs.job_submit(run['run_id'])
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'retention_registration_failed')
        transfer.assert_not_called()
        self.assertFalse(Path(run['remote_dir']).exists())

    def test_metadata_symlinks_are_refused(self):
        outside = self.fixture.fixture.root / 'outside-registry'
        outside.mkdir()
        (self.fixture.fixture.remote / '.hpc-mcp-registry').symlink_to(outside)
        with self.assertRaises(ValueError):
            self.service.probe('lsf')
        self.assertEqual(list(outside.iterdir()), [])

    def test_advanced_local_cleanup_cannot_bypass_retention(self):
        from hpc_mcp.submission import SubmissionError
        run = self.synced()
        rid = run['run_id']
        test_jobs.authorize(self.jobs, rid, cleanup_policy={'storage_categories': ['snapshot'],
            'storage_age_seconds': 0, 'sync_cache_age_seconds': 0})
        for cleanup in (lambda: self.jobs.job_storage_cleanup(rid, ['snapshot'], 0, False),
                        lambda: self.jobs.job_cache_cleanup(rid, 0, False)):
            with self.assertRaises(SubmissionError) as error:
                cleanup()
            self.assertEqual(error.exception.code, 'retention_required')
        self.assertTrue(Path(run['snapshot_dir']).exists())

    def test_completed_remote_receipt_recovers_lost_local_cleanup_result(self):
        run = self.synced()
        rid = run['run_id']
        released = self.service.release('lsf', [rid], reason='Synthetic complete')
        self.service.release(dry_run=False, review_token=released['review_token'], confirmation_note='Test')
        test_jobs.authorize(self.jobs, rid, remote_cleanup=True)
        self.assertTrue(self.jobs.job_remote_cleanup(rid, False)['ok'])
        self.jobs.history.update(rid, 'simulate_local_receipt_loss', remote_removed=False)
        recovered = self.jobs.job_remote_cleanup(rid, False)
        self.assertTrue(recovered['already_removed'])
        self.assertTrue(self.jobs.history.get(rid)['remote_removed'])
        self.assertTrue(self.service.release(dry_run=False, review_token=released['review_token'],
                                           confirmation_note='Test')['ok'])

    def test_cleanup_lock_and_deleting_state_prevent_late_hold(self):
        run = self.synced()
        rid = run['run_id']
        self.release(self.service, rid)
        marker = self.fixture.fixture.root / 'deleting-marker'
        command = 'touch ' + shlex.quote(str(marker)) + '\nsleep 0.4\nrm -rf -- ' + shlex.quote(run['remote_dir'])
        with ThreadPoolExecutor(max_workers=2) as pool:
            delete = pool.submit(self.service.remote.cleanup, self.jobs.clusters['lsf'], run, command)
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists())
            with self.assertRaisesRegex(ValueError, 'deleting/deleted'):
                self.service.hold('lsf', [rid], 'late', 'Concurrent late claim')
            self.assertTrue(delete.result().ok)
        self.assertFalse(Path(run['remote_dir']).exists())

    def test_interrupted_cleanup_leaves_protected_deleting_record(self):
        run = self.synced()
        rid = run['run_id']
        self.release(self.service, rid)
        with self.assertRaises(ValueError):
            self.service.remote.call(self.jobs.clusters['lsf'], 'cleanup', run_ids=[rid],
                facts=registration_facts(run), command='sleep 1', timeout=0.01)
        task = self.service.status('lsf', [rid])['tasks'][0]
        self.assertEqual(task['state'], 'deleting')
        self.assertFalse(task['cleanup_allowed'])
        self.assertTrue(Path(run['remote_dir']).exists())

    def test_cleanup_diagnostic_limit_leaves_protected_record(self):
        run = self.synced()
        rid = run['run_id']
        self.release(self.service, rid)
        with self.assertRaisesRegex(ValueError, 'limit exceeded'):
            self.service.remote.call(self.jobs.clusters['lsf'], 'cleanup', run_ids=[rid],
                facts=registration_facts(run), command="python3 -c 'print(\"x\" * 70000)'", timeout=5)
        self.assertEqual(self.service.status('lsf', [rid])['tasks'][0]['state'], 'deleting')
        self.assertTrue(Path(run['remote_dir']).exists())


@unittest.skipUnless(test_external_approvals.Ed25519PrivateKey, 'cryptography required')
class SignedRetentionTests(unittest.TestCase):
    def test_release_and_conflict_reconcile_require_separate_signatures(self):
        fixture = test_external_approvals.ExternalApprovalTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        jobs = fixture.jobs
        run = fixture.fixture.prepare()
        plan = jobs.job_submission_plan(run['run_id'])
        fixture.endorse('job_authorize', run['run_id'], plan['review_token'])
        jobs.job_authorize(run['run_id'], plan['review_token'], 'Synthetic signed computation')
        self.assertTrue(jobs.job_submit(run['run_id'])['ok'])
        service = RetentionService(jobs)
        plan = service.release('lsf', [run['run_id']], reason='Synthetic done')
        with self.assertRaises(test_external_approvals.ApprovalError):
            service.release(dry_run=False, review_token=plan['review_token'], confirmation_note='Test')
        fixture.endorse('retention_release', 'lsf', plan['review_token'])
        self.assertTrue(service.release(dry_run=False, review_token=plan['review_token'],
                                       confirmation_note='Synthetic signed release')['ok'])
        reconcile = service.reconcile('lsf')
        with self.assertRaises(test_external_approvals.ApprovalError):
            service.reconcile(dry_run=False, review_token=reconcile['review_token'], confirmation_note='Test')
        fixture.endorse('cluster_registry_reconcile', 'lsf', reconcile['review_token'])
        self.assertTrue(service.reconcile(dry_run=False, review_token=reconcile['review_token'],
                                         confirmation_note='Synthetic signed reconcile')['ok'])
