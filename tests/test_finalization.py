"""Approved completion contracts over real local Bash/rsync; no model or live cluster."""

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import unittest
from unittest.mock import patch

import test_jobs
import test_lifecycle
import test_workflow
import test_external_approvals
from hpc_mcp.batch import BatchService
from hpc_mcp.finalization import FinalizationService, matches
from hpc_mcp.jobs import JobService
from hpc_mcp.maintenance import MaintenanceService
from hpc_mcp.retention import RetentionService
from hpc_mcp.ssh import CommandResult
from hpc_mcp.workflow import WorkflowService


class FinalizationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_jobs.JobLifecycleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.jobs = self.fixture.jobs
        self.finalizer = FinalizationService(self.jobs)
        self.retention = RetentionService(self.jobs)

    def rule(self, **changes):
        return {'strategy': 'after_verified_sync', 'required_outputs': ['results/value.txt'],
                'optional_outputs': ['stderr.log'], 'allow_unverified_application': True, **changes}

    def approve(self, run, rule=None):
        return test_jobs.authorize(self.jobs, run['run_id'], auto_submit=True, auto_sync=True,
                                   release_policy=self.rule() if rule is None else rule)

    def synced(self, rule=None):
        run = self.fixture.prepare()
        self.approve(run, rule)
        self.assertTrue(self.jobs.job_submit(run['run_id'])['ok'])
        self.assertTrue(self.jobs.job_sync(run['run_id'], stable_only=True)['ok'])
        return self.jobs.history.get(run['run_id'])

    def test_successful_sync_releases_only_approved_holder_and_records_evidence(self):
        run = self.synced()
        result = self.finalizer.advance(run['run_id'])
        self.assertTrue(result['normal_ready'], result)
        self.assertTrue(result['submission_released'], result)
        self.assertTrue(result['cleanup_ready'], result)
        self.assertEqual(result['application']['status'], 'unverified')
        claim = self.retention.status('lsf', [run['run_id']])['tasks'][0]
        self.assertTrue(claim['cleanup_ready'])
        self.assertEqual(claim['holds'][self.retention.holder()]['release_source'], 'automatic')
        self.assertEqual(claim['holds'][self.retention.holder()]['automatic_evidence']['plan_token'],
                         run['submission_plan_token'])
        with sqlite3.connect(self.fixture.remote / '.hpc-mcp-registry/registry.sqlite3') as db:
            detail = json.loads(db.execute("SELECT detail FROM events WHERE action='auto_release'").fetchone()[0])
        self.assertEqual(detail['evidence']['plan_token'], run['submission_plan_token'])
        again = self.finalizer.advance(run['run_id'])
        self.assertEqual(again['retention_revision'], result['retention_revision'])
        with self.jobs.history.connect() as db:
            rows = db.execute("SELECT detail FROM events WHERE run_id=? AND kind='finalization_checked'",
                              (run['run_id'],)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertNotIn('required_outputs', json.loads(rows[0][0]))
        self.assertTrue(Path(run['remote_dir']).exists())  # Release never deletes.

    def test_required_output_missing_and_optional_missing_are_different(self):
        run = self.synced(self.rule(required_outputs=['missing.log'], optional_outputs=['optional.log']))
        result = self.finalizer.advance(run['run_id'])
        self.assertFalse(result['normal_ready'])
        self.assertIn('necessary_outputs_missing', result['blockers'])
        run = self.synced(self.rule(optional_outputs=['optional.log']))
        result = self.finalizer.advance(run['run_id'])
        self.assertTrue(result['cleanup_ready'], result)
        self.assertEqual(result['optional_outputs'], [])

    def test_glob_minimum_and_root_relative_matching(self):
        self.assertFalse(matches('nested/a.log', '*.log'))
        self.assertTrue(matches('nested/a.log', '**/*.log'))
        self.assertTrue(matches('a.log', '**/*.log'))
        run = self.synced(self.rule(required_outputs=[{'pattern': 'results/***', 'min_matches': 2}]))
        self.assertIn('necessary_outputs_missing', self.finalizer.advance(run['run_id'])['blockers'])

    def test_invalid_or_unapproved_contracts_fail_before_network(self):
        run = self.fixture.prepare()
        invalid = [self.rule(required_outputs=[]), self.rule(required_outputs=['../bad']),
            self.rule(required_outputs=['data.chk']), self.rule(allow_unverified_application=False),
            self.rule(required_outputs=[{'pattern': '*.log', 'min_matches': 0}]),
            self.rule(required_outputs=[{'pattern': 'stdout.log', 'min_matches': 2}])]
        with patch.object(self.jobs.transport, 'run') as network:
            for rule in invalid:
                with self.subTest(rule=rule), self.assertRaises(ValueError):
                    self.approve(run, rule)
            network.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'outside approved sync'):
            test_jobs.authorize(self.jobs, run['run_id'], release_policy=self.rule(),
                sync_options={'excludes': ['results/']})
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            test_jobs.authorize(self.jobs, run['run_id'],
                release_policy=self.rule(required_outputs=['results/***']),
                sync_options={'excludes': ['*.tmp']})

    def test_legacy_and_manual_tasks_do_not_automatically_release(self):
        run = self.fixture.prepare()
        test_jobs.authorized_submit(self.jobs, run['run_id'])
        test_jobs.authorized_sync(self.jobs, run['run_id'])
        self.assertIn('no_approved_release_rule', self.finalizer.advance(run['run_id'])['blockers'])
        self.approve(self.jobs.history.get(run['run_id']), self.rule(strategy='manual'))
        result = self.finalizer.advance(run['run_id'])
        self.assertTrue(result['normal_ready'])
        self.assertFalse(result['cleanup_ready'])
        self.assertIn('manual_release_required', result['blockers'])

    def test_other_clients_keep_independent_holds(self):
        run = self.synced()
        other = RetentionService(JobService(self.jobs.clusters, self.fixture.root / 'other',
            transport=self.jobs.transport, transfer=self.jobs.transfer))
        other.hold('lsf', [run['run_id']], 'later-stage', 'Checkpoint still needed')
        result = self.finalizer.advance(run['run_id'])
        self.assertTrue(result['submission_released'])
        self.assertFalse(result['cleanup_ready'])
        self.assertIn('other_retention_claims_active', result['blockers'])
        plan = other.release('lsf', [run['run_id']], 'later-stage', 'Done')
        other.release(dry_run=False, review_token=plan['review_token'], confirmation_note='Synthetic approval')
        self.assertTrue(self.finalizer.advance(run['run_id'])['cleanup_ready'])

    def test_explicit_rehold_is_never_overridden_by_automatic_rule(self):
        run = self.synced()
        self.assertTrue(self.finalizer.advance(run['run_id'])['cleanup_ready'])
        with self.jobs.history.lock(run['run_id']), patch.object(self.jobs.transport, 'run') as network:
            with self.assertRaisesRegex(ValueError, 'another'):
                self.retention.hold('lsf', [run['run_id']], 'submission', 'Concurrent hold')
            network.assert_not_called()
        self.retention.hold('lsf', [run['run_id']], 'submission', 'User needs these again')
        result = self.finalizer.advance(run['run_id'])
        self.assertFalse(result['cleanup_ready'])
        self.assertIn('explicit_hold_requires_manual_release', result['blockers'])

    def test_foreign_hold_between_check_and_release_is_detected_atomically(self):
        run = self.synced()
        other = RetentionService(JobService(self.jobs.clusters, self.fixture.root / 'racing-client',
            transport=self.jobs.transport, transfer=self.jobs.transfer))
        original = self.finalizer.retention.automatic
        def raced(current, task, action, evidence, *args):
            if action == 'auto_release':
                other.hold('lsf', [run['run_id']], 'concurrent', 'Still needed')
            return original(current, task, action, evidence, *args)
        with patch.object(self.finalizer.retention, 'automatic', side_effect=raced):
            result = self.finalizer.advance(run['run_id'])
        self.assertFalse(result['cleanup_ready'])
        task = self.retention.status('lsf', [run['run_id']])['tasks'][0]
        self.assertFalse(task['holds'][self.retention.holder()]['released'])
        self.assertFalse(task['holds'][other.holder('concurrent')]['released'])

    def test_config_changes_during_checks_are_revalidated_before_release(self):
        run = self.synced()
        original = self.finalizer.retention.status
        def changed(*args):
            result = original(*args)
            self.jobs.clusters['lsf'] = replace(self.jobs.clusters['lsf'], command_timeout=29)
            return result
        with patch.object(self.finalizer.retention, 'status', side_effect=changed):
            result = self.finalizer.advance(run['run_id'])
        self.assertFalse(result['cleanup_ready'])
        self.assertFalse(self.retention.status('lsf', [run['run_id']])['tasks'][0]['cleanup_ready'])

    def test_changed_output_or_revoked_approval_revokes_automatic_readiness(self):
        run = self.synced()
        self.assertTrue(self.finalizer.advance(run['run_id'])['cleanup_ready'])
        (Path(run['output_dir']) / 'results/value.txt').write_text('changed')
        result = self.finalizer.advance(run['run_id'])
        self.assertFalse(result['cleanup_ready'])
        self.assertFalse(self.retention.status('lsf', [run['run_id']])['tasks'][0]['cleanup_ready'])
        run = self.synced()
        self.assertTrue(self.finalizer.advance(run['run_id'])['cleanup_ready'])
        self.jobs.job_revoke_authorization(run['run_id'])
        self.assertFalse(self.finalizer.advance(run['run_id'])['cleanup_ready'])
        self.assertFalse(self.retention.status('lsf', [run['run_id']])['tasks'][0]['cleanup_ready'])

    def test_pending_installation_and_active_worker_block_completion(self):
        run = self.synced()
        self.jobs.history.update(run['run_id'], 'pending_test', sync_installation={'phase': 'pending'})
        self.assertIn('pending_result_installation', self.finalizer.advance(run['run_id'])['blockers'])
        run = self.synced()
        from hpc_mcp.operations import initialize
        initialize(self.jobs.history)
        with self.jobs.history.connect() as db:
            db.execute('INSERT INTO operations VALUES (?,?,?)',
                ('test_active', run['run_id'], json.dumps({'state': 'running'})))
        self.assertIn('active_sync_operation', self.finalizer.advance(run['run_id'])['blockers'])

    def test_failed_unknown_and_nonzero_status_never_release(self):
        for command in ("printf '4242|EXIT|1\\n'", "printf '4242|DONE|7\\n'", 'exit 1'):
            run = self.synced()
            (self.fixture.bin / 'bjobs').write_text('#!/bin/bash\n' + command + '\n')
            result = self.finalizer.advance(run['run_id'])
            self.assertFalse(result['cleanup_ready'], result)
            self.assertFalse(self.retention.status('lsf', [run['run_id']])['tasks'][0]['cleanup_ready'])
            (self.fixture.bin / 'bjobs').write_text("#!/bin/bash\nprintf '4242|DONE|0\\n'\n")

    def test_restart_reuses_exact_rule_without_extra_release_confirmation(self):
        run = self.synced()
        restarted = JobService(self.jobs.clusters, self.jobs.history.root,
                               transport=self.jobs.transport, transfer=self.jobs.transfer)
        self.assertTrue(FinalizationService(restarted).advance(run['run_id'])['cleanup_ready'])

    def test_cleanup_revalidates_evidence_and_does_not_trust_ready_flag(self):
        run = self.synced()
        self.assertTrue(self.finalizer.advance(run['run_id'])['cleanup_ready'])
        preview = MaintenanceService(self.jobs).local_cache_cleanup(categories=['snapshot'], older_than_seconds=0)
        self.assertEqual(preview['candidate_count'], 1, preview)
        (Path(run['output_dir']) / 'results/value.txt').write_text('changed')
        result = MaintenanceService(self.jobs).local_cache_cleanup(dry_run=False,
            review_token=preview['review_token'], confirmation_note='Synthetic exact cleanup')
        self.assertFalse(result['ok'])
        self.assertTrue(Path(run['snapshot_dir']).exists())

    def test_bulk_invalid_identity_fails_before_releasing_any_task(self):
        run = self.synced()
        with self.assertRaises(ValueError):
            self.finalizer.many([run['run_id'], 'r_' + '0' * 32])
        self.assertFalse(self.retention.status('lsf', [run['run_id']])['tasks'][0]['cleanup_ready'])

    def test_manual_release_after_explicit_rehold_is_a_valid_override(self):
        run = self.synced()
        self.retention.hold('lsf', [run['run_id']], 'submission', 'Explicit hold')
        test_jobs.release_retention(self.jobs, run['run_id'])
        self.assertTrue(self.finalizer.advance(run['run_id'])['cleanup_ready'])

    def test_remote_automatic_receipt_does_not_bypass_missing_local_completion_witness(self):
        run = self.synced()
        test_jobs.authorize(self.jobs, run['run_id'], auto_sync=True,
                            remote_cleanup=True, release_policy=self.rule())
        self.assertTrue(self.finalizer.advance(run['run_id'])['cleanup_ready'])
        cached = dict(self.jobs.history.get(run['run_id'])['retention'], automatic_release=False)
        self.jobs.history.update(run['run_id'], 'simulate_lost_witness', retention=cached, finalization=None)
        with self.assertRaisesRegex(ValueError, 'evidence changed'):
            self.jobs.job_remote_cleanup(run['run_id'], False)
        self.assertTrue(Path(run['remote_dir']).exists())
        self.assertTrue(self.finalizer.advance(run['run_id'])['cleanup_ready'])
        self.assertTrue(self.jobs.job_remote_cleanup(run['run_id'], False)['ok'])

    def test_cli_retries_authorized_rule_without_separate_release_approval(self):
        from hpc_mcp.config import ConfigManager
        config = self.fixture.root / 'clusters.toml'
        ConfigManager(config, {}).set('lsf', {'ssh_host': 'alias', 'scheduler': 'lsf',
                                            'work_root': str(self.fixture.remote)})
        self.jobs.config_path = config
        run = self.synced()
        ssh = self.fixture.bin / 'ssh'
        ssh.write_text('#!/usr/bin/env python3\nimport os,sys\n'
            "os.execvp('bash',['bash','-c',' '.join(sys.argv[sys.argv.index('alias')+1:])])\n")
        ssh.chmod(0o755)
        result = subprocess.run([sys.executable, '-m', 'hpc_mcp', '--config', str(config),
            '--state-dir', str(self.jobs.history.root), 'retention-finalize', '--run-ids', run['run_id']],
            capture_output=True, text=True, env={**os.environ,
                'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src'),
                'PATH': str(self.fixture.bin) + ':' + os.environ['PATH']})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['tasks'][0]['cleanup_ready'], result.stdout)


class FinalizationLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_lifecycle.LifecycleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.jobs = self.fixture.jobs

    def test_registered_checks_normal_finish_releases_and_runs_preapproved_cleanup(self):
        rid = self.fixture.prepare()
        self.fixture.criterion(rid)
        test_jobs.authorize(self.jobs, rid, auto_submit=True, auto_sync=True,
            release_policy={'strategy': 'after_verified_sync', 'required_outputs': ['results/value.txt']},
            remote_cleanup=True, cleanup_policy={'storage_categories': ['snapshot'], 'storage_age_seconds': 0})
        self.fixture.life.start(rid)
        result = self.fixture.finish(rid)
        self.assertEqual(result['lifecycle']['stage'], 'complete', result)
        self.assertTrue(result['finalization']['cleanup_ready'], result)
        self.assertTrue(result['cleanup']['remote_removed'])
        self.assertFalse(Path(self.jobs.history.get(rid)['snapshot_dir']).exists())

    def test_registered_application_failure_never_releases(self):
        rid = self.fixture.prepare()
        self.fixture.criterion(rid, failure='42')
        test_jobs.authorize(self.jobs, rid, auto_submit=True, auto_sync=True,
            release_policy={'strategy': 'after_verified_sync', 'required_outputs': ['results/value.txt']})
        self.fixture.life.start(rid)
        result = self.fixture.finish(rid)
        self.assertEqual(result['lifecycle']['stage'], 'needs_attention')
        self.assertFalse(result['finalization']['cleanup_ready'])

    def test_batch_confirmation_covers_rule_and_status_reports_ready(self):
        batches = BatchService(self.jobs)
        fixture = self.fixture.fixture.fixture
        plan = batches.plan('auto-finish', 'One normal task', 'lsf', str(fixture.root), [{
            'input_dir': str(fixture.source), 'script': 'job.sh',
            'input_files': ['job.sh', 'data.txt'], 'outputs': ['results/***', '*.log']}],
            automation={'release_policy': {'strategy': 'after_verified_sync',
                'required_outputs': ['results/value.txt'], 'optional_outputs': ['optional.log'],
                'allow_unverified_application': True}}, start_monitor=False)
        self.assertTrue(plan['ok'], plan)
        policy = next(iter(plan['policies'].values()))
        self.assertIn('release_contract', policy)
        self.assertTrue(policy['automation']['release_policy']['allow_unverified_application'])
        batches.start(plan['batch_id'], plan['review_token'], 'Synthetic single batch approval')
        value = batches.get(plan['batch_id'])
        batches.workflows.tick(value['workflow_id'])
        rid = next(iter(value['tasks'].values()))['run_id']
        result = self.fixture.finish(rid)
        self.assertEqual(result['lifecycle']['stage'], 'complete', result)
        summary = batches.status(plan['batch_id'], include_completed=True)
        self.assertTrue(summary['tasks'][0]['finalization']['cleanup_ready'], summary)
        self.assertTrue(summary['tasks'][0]['finalization']['cached'])


class FinalizationDependencyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_workflow.WorkflowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.jobs = self.fixture.jobs
        self.flow = self.fixture.flow
        self.retention = RetentionService(self.jobs)
        self.finalizer = FinalizationService(self.jobs)

    def scenario(self, strategy):
        parent = self.fixture.prepare()
        (self.fixture.fixture.source / 'child.sh').write_text('#!/bin/bash\ncat restart.dat > child.out\n')
        child = self.jobs.job_prepare('lsf', str(self.fixture.fixture.source), 'child.sh', ['child.out'],
                                     input_files=['child.sh'])['run']['run_id']
        rule = {'strategy': strategy, 'required_outputs': ['results/value.txt'],
                'allow_unverified_application': True}
        test_jobs.authorize(self.jobs, parent, auto_sync=True, release_policy=rule)
        self.assertTrue(self.jobs.job_submit(parent)['ok'])
        self.assertTrue(self.jobs.job_sync(parent, stable_only=True)['ok'])
        value = self.flow.plan('dependent', [child], [{'run_id': child, 'parent_run_id': parent,
            'condition': 'files_ready', 'files': [{'source': 'results/value.txt', 'target': 'restart.dat'}]}],
            {'poll_interval_seconds': 5}, automation={'auto_sync': True, 'release_policy': {
                'strategy': 'manual', 'required_outputs': ['child.out'], 'allow_unverified_application': True}})['workflow']
        self.fixture.start(value)
        return parent, child, value

    def finish_consumer(self, child, value):
        with patch.object(self.jobs, 'job_sync_start', side_effect=self.fixture.sync_immediately):
            result = self.fixture.tick(value)
            if result['workflow']['tasks'][child]['run_id'] == child:
                result = self.fixture.tick(value)
        actual = result['workflow']['tasks'][child]['run_id']
        self.assertNotEqual(actual, child)
        reviewed = self.flow.review(value['workflow_id'])['workflow']
        self.fixture.start(reviewed)
        self.fixture.tick(reviewed)
        self.assertTrue(self.jobs.job_sync(actual, stable_only=True)['ok'])
        # Consumer's own manual hold remains active; its normal-completion proof suffices.
        result = self.finalizer.advance(actual)
        self.assertTrue(result['normal_ready'], result)
        self.assertFalse(result['submission_released'])
        return actual

    def test_after_consumers_waits_without_waiting_for_consumers_own_holders(self):
        parent, child, value = self.scenario('after_consumers_complete')
        result = self.finalizer.advance(parent)
        self.assertFalse(result['submission_released'])
        self.assertIn('dependent_stages_still_need_remote_files', result['blockers'])
        actual = self.finish_consumer(child, value)
        result = self.finalizer.advance(parent)
        self.assertTrue(result['cleanup_ready'], result)
        self.assertFalse(self.retention.status('lsf', [actual])['tasks'][0]['cleanup_ready'])

    def test_after_sync_releases_self_but_dependency_still_blocks_cleanup(self):
        parent, child, value = self.scenario('after_verified_sync')
        result = self.finalizer.advance(parent)
        self.assertTrue(result['submission_released'])
        self.assertFalse(result['cleanup_ready'])
        self.assertIn('other_retention_claims_active', result['blockers'])
        self.finish_consumer(child, value)
        self.assertTrue(self.finalizer.advance(parent)['cleanup_ready'])

    def test_explicit_dependency_release_can_end_a_no_longer_needed_stage(self):
        parent, _, _ = self.scenario('after_consumers_complete')
        task = self.retention.status('lsf', [parent])['tasks'][0]
        holder = next(k for k, h in task['holds'].items() if h.get('dependency'))
        plan = self.retention.release('lsf', [parent], holder.split(':', 1)[1],
                                     'User explicitly no longer needs this dependent stage')
        self.retention.release(dry_run=False, review_token=plan['review_token'],
                               confirmation_note='Synthetic explicit dependency release')
        self.assertTrue(self.finalizer.advance(parent)['cleanup_ready'])


@unittest.skipUnless(test_external_approvals.Ed25519PrivateKey, 'cryptography required')
class FinalizationSignedTests(unittest.TestCase):
    def test_exact_signed_compute_plan_also_authorizes_its_automatic_release_rule(self):
        fixture = test_external_approvals.ExternalApprovalTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        jobs = fixture.jobs
        run = fixture.fixture.prepare()
        plan = jobs.job_submission_plan(run['run_id'], {'auto_sync': True,
            'release_policy': {'strategy': 'after_verified_sync',
                              'required_outputs': ['results/value.txt'], 'allow_unverified_application': True}})
        fixture.endorse('job_authorize', run['run_id'], plan['review_token'])
        jobs.job_authorize(run['run_id'], plan['review_token'], 'Synthetic exact signed policy')
        self.assertTrue(jobs.job_submit(run['run_id'])['ok'])
        self.assertTrue(jobs.job_sync(run['run_id'], stable_only=True)['ok'])
        self.assertTrue(FinalizationService(jobs).advance(run['run_id'])['cleanup_ready'])
