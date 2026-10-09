"""Workflow limits, recovery and dependency release using real Bash/local rsync."""

import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

import test_jobs
from hpc_mcp.history import now
from hpc_mcp.operations import initialize
from hpc_mcp.ssh import CommandResult
from hpc_mcp.workflow import WorkflowService


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_jobs.JobLifecycleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.jobs = self.fixture.jobs
        self.epoch = [time.time()]
        self.flow = WorkflowService(self.jobs, clock=lambda: self.epoch[0])
        self.op_count = 0
        initialize(self.jobs.history)

    def prepare(self):
        return self.fixture.prepare()['run_id']

    def plan(self, runs, dependencies=None, **limits):
        return self.flow.plan('project', runs, dependencies,
            dict(poll_interval_seconds=5, **limits))['workflow']

    def start(self, value):
        return self.flow.start(value['workflow_id'], value['review_token'], 'User explicitly authorized these runs and limits')

    def tick(self, value, seconds=5):
        self.epoch[0] += seconds
        return self.flow.tick(value['workflow_id'])

    def count(self):
        path = self.fixture.root / 'submissions'
        return int(path.read_text()) if path.exists() else 0

    def sync_immediately(self, run_id, options):
        result = self.jobs.job_sync(run_id, **options)
        self.op_count += 1
        op = {'operation_id': 'op_workflow_' + str(self.op_count), 'run_id': run_id,
              'created_at': now(), 'state': 'succeeded' if result['ok'] else 'failed',
              'options': options, 'progress': {}, 'result': result}
        with self.jobs.history.connect() as db:
            db.execute('INSERT INTO operations VALUES (?,?,?)', (op['operation_id'], run_id, json.dumps(op)))
        return {'ok': True, 'operation': op}

    def test_exact_plan_authorization_claims_and_pause(self):
        first = self.prepare()
        value = self.plan([first])
        self.assertFalse(value['enabled'])
        self.assertTrue(self.tick(value)['deferred'])
        self.assertEqual(self.count(), 0)
        with self.assertRaises(ValueError):
            self.jobs.job_submit(first)
        with self.assertRaises(ValueError):
            self.flow.start(value['workflow_id'], 'wrong', 'user confirmed')
        self.start(value)
        self.flow.pause(value['workflow_id'])
        self.assertTrue(self.tick(value)['deferred'])
        self.assertEqual(self.count(), 0)
        with self.assertRaises(ValueError):
            self.jobs.job_submit(first)
        self.start(value)
        self.tick(value)
        self.assertEqual(self.count(), 1)

    def test_in_flight_pending_unknown_and_rate_survive_restart(self):
        ids = [self.prepare() for _ in range(4)]
        value = self.plan(ids, max_in_flight=2, max_submissions_per_tick=2, submissions_per_minute=2)
        self.start(value)
        self.tick(value)
        self.assertEqual(self.count(), 2)
        original = self.jobs.transport.run
        def running(cluster, command):
            if 'bjobs ' in command:
                return CommandResult(0, '4242|RUN|-\n')
            return original(cluster, command)
        with patch.object(self.jobs.transport, 'run', side_effect=running):
            self.tick(value)
            self.assertEqual(self.count(), 2)
        self.tick(value)
        self.assertEqual(self.count(), 2)  # rate window still full after terminal confirmation
        self.flow = WorkflowService(self.jobs, clock=lambda: self.epoch[0])
        self.tick(value, seconds=61)
        self.assertEqual(self.count(), 4)
        self.tick(value)
        self.assertEqual(self.count(), 4)
        self.assertFalse(self.flow.get(value['workflow_id'])['workflow']['enabled'])

    def test_unknown_status_does_not_free_capacity(self):
        value = self.plan([self.prepare(), self.prepare()], max_in_flight=1)
        self.start(value)
        self.tick(value)
        with patch.object(self.jobs.transport, 'run', return_value=CommandResult(-1, error='offline')):
            result = self.tick(value)
        self.assertEqual(result['in_flight'], 1)
        self.assertEqual(self.count(), 1)

    def test_ambiguous_submission_recovers_without_duplicate(self):
        value = self.plan([self.prepare(), self.prepare()], max_in_flight=1)
        self.start(value)
        self.fixture.transport.lose_response = True
        self.tick(value)
        self.assertEqual(self.count(), 1)
        self.tick(value)
        self.assertEqual(self.count(), 1)
        self.tick(value)
        self.assertEqual(self.count(), 2)

    def test_failed_parent_blocks_child_and_cycle_is_rejected(self):
        parent, child = self.prepare(), self.prepare()
        with self.assertRaises(ValueError):
            self.plan([parent, child], [{'run_id': child, 'parent_run_id': parent},
                                      {'run_id': parent, 'parent_run_id': child}])
        value = self.plan([parent, child], [{'run_id': child, 'parent_run_id': parent}], max_in_flight=1)
        self.start(value)
        self.tick(value)
        original = self.jobs.transport.run
        def failed(cluster, command):
            if 'bjobs ' in command:
                return CommandResult(0, '4242|EXIT|7|TERM_RUNLIMIT: time limit\n')
            return original(cluster, command)
        with patch.object(self.jobs.transport, 'run', side_effect=failed):
            result = self.tick(value)
        self.assertEqual(result['workflow']['tasks'][child]['stage'], 'blocked')
        self.assertEqual(self.count(), 1)

    def test_checkpoint_handoff_creates_new_frozen_run_and_preserves_originals(self):
        parent = self.prepare()
        (self.fixture.source / 'child.sh').write_text('#!/bin/bash\ncat restart.dat > child.out\n')
        child = self.jobs.job_prepare('lsf', str(self.fixture.source), 'child.sh', ['child.out'],
                                     input_files=['child.sh'])['run']['run_id']
        value = self.plan([parent, child], [{'run_id': child, 'parent_run_id': parent,
            'condition': 'files_ready', 'files': [{'source': 'results/value.txt', 'target': 'restart.dat'}]}], max_in_flight=1)
        self.start(value)
        with patch.object(self.jobs, 'job_sync_start', side_effect=self.sync_immediately):
            self.tick(value)
            self.tick(value)
            result = self.tick(value)
        derived_id = result['workflow']['tasks'][child]['run_id']
        self.assertNotEqual(derived_id, child)
        derived = self.jobs.history.get(derived_id)
        self.assertEqual(derived['phase'], 'submitted')
        self.assertEqual((Path(derived['remote_dir']) / 'child.out').read_text(), '42\n')
        self.assertFalse((Path(self.jobs.history.get(child)['snapshot_dir']) / 'restart.dat').exists())
        self.assertFalse((self.fixture.source / 'restart.dat').exists())
        self.assertEqual(derived['source_run_id'], child)
        self.assertEqual(derived['dependency_materialization']['sources'][0]['parent_run_id'], parent)
        self.assertEqual(self.count(), 2)

    def test_materialization_crash_recovers_draft_and_original_result_directory(self):
        parent = self.prepare()
        (self.fixture.source / 'child.sh').write_text('#!/bin/bash\ncat restart.dat > child.out\n')
        child = self.jobs.job_prepare('lsf', str(self.fixture.source), 'child.sh', ['child.out'],
                                     input_files=['child.sh'])['run']['run_id']
        value = self.plan([parent, child], [{'run_id': child, 'parent_run_id': parent,
            'condition': 'files_ready', 'files': [{'source': 'results/value.txt', 'target': 'restart.dat'}]}])
        self.start(value)
        update = self.jobs.history.update

        def interrupted(run_id, event, **fields):
            if event == 'dependency_snapshot_created':
                raise OSError('simulated interruption after draft creation')
            return update(run_id, event, **fields)

        with patch.object(self.jobs, 'job_sync_start', side_effect=self.sync_immediately):
            self.tick(value)
            self.tick(value)
            with patch.object(self.jobs.history, 'update', side_effect=interrupted):
                result = self.tick(value)
        self.assertEqual(result['workflow']['tasks'][child]['stage'], 'needs_attention')
        self.assertEqual(self.count(), 1)
        with self.jobs.history.connect() as db:
            before = db.execute('SELECT COUNT(*) FROM runs').fetchone()[0]
        self.flow.retry(value['workflow_id'], [child])
        self.start(value)
        result = self.tick(value)
        derived = self.jobs.history.get(result['workflow']['tasks'][child]['run_id'])
        self.assertEqual(derived['input_dir'], str(self.fixture.source))
        self.assertEqual(derived['source_run_id'], child)
        self.assertEqual(self.count(), 2)
        with self.jobs.history.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM runs').fetchone()[0], before)

    def test_missing_handoff_and_application_failure_never_release_child(self):
        parent, child = self.prepare(), self.prepare()
        value = self.plan([parent, child], [{'run_id': child, 'parent_run_id': parent,
            'condition': 'files_ready', 'files': [{'source': 'missing.chk', 'target': 'old.chk'}]}])
        self.start(value)
        with patch.object(self.jobs, 'job_sync_start', side_effect=self.sync_immediately):
            self.tick(value)
            self.tick(value)
            result = self.tick(value)
        self.assertEqual(result['workflow']['tasks'][child]['stage'], 'needs_attention')
        self.assertEqual(self.count(), 1)
        # Independent workflow with a scheduler-success / application-failure parent.
        (self.fixture.source / 'job.sh').write_text('#!/bin/bash\nprintf "Error termination of Gaussian 16\\n"\n')
        parent, child = self.prepare(), self.prepare()
        self.jobs.history.update(parent, 'gaussian', application={'kind': 'gaussian', 'log': 'stdout.log', 'expected_sections': 1})
        value = self.plan([parent, child], [{'run_id': child, 'parent_run_id': parent, 'condition': 'application_succeeded'}])
        self.start(value)
        with patch.object(self.jobs, 'job_sync_start', side_effect=self.sync_immediately):
            self.tick(value)
            self.tick(value)
            result = self.tick(value)
        self.assertEqual(result['workflow']['tasks'][child]['stage'], 'blocked')
        self.assertEqual(self.count(), 2)

    def test_overwrite_and_normalized_duplicate_mappings_are_rejected(self):
        parent, child = self.prepare(), self.prepare()
        for files in ([{'source': 'result.chk', 'target': 'job.sh'}],
                      [{'source': 'a.chk', 'target': 'a//old.chk'}, {'source': 'b.chk', 'target': 'a/old.chk'}],
                      [{'source': '*.chk', 'target': 'old.chk'}]):
            with self.subTest(files=files), self.assertRaises(ValueError):
                self.plan([parent, child], [{'run_id': child, 'parent_run_id': parent, 'condition': 'files_ready', 'files': files}])

    def test_query_window_rotates_completed_tasks_do_not_hold_capacity_forever(self):
        ids = [self.prepare() for _ in range(5)]
        value = self.plan(ids, max_in_flight=1, max_status_jobs=1)
        self.start(value)
        for _ in range(20):
            self.tick(value)
        self.assertEqual(self.count(), 5)

    def test_failed_upload_requires_explicit_retry_and_pause_survives_save(self):
        value = self.plan([self.prepare()])
        self.start(value)
        self.fixture.transfer.fail_upload = True
        result = self.tick(value)
        self.assertEqual(next(iter(result['workflow']['tasks'].values()))['stage'], 'needs_attention')
        self.fixture.transfer.fail_upload = False
        self.flow.retry(value['workflow_id'], value['plan']['run_ids'])
        self.start(value)
        self.tick(value)
        self.assertEqual(self.count(), 1)
        stale = self.flow.get(value['workflow_id'])['workflow']
        self.flow.pause(value['workflow_id'])
        self.flow._save(stale)
        self.assertFalse(self.flow.get(value['workflow_id'])['workflow']['enabled'])
