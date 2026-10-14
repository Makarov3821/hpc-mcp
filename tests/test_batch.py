"""Offline batch planning, authorization, crash recovery and output return."""

from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import test_monitor
import test_applications
from hpc_mcp.batch import BatchError, BatchService
from hpc_mcp.workflow import WorkflowService


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_monitor.MonitorTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.jobs = self.fixture.jobs
        self.root = self.fixture.fixture.root
        self.batches = BatchService(self.jobs)
        self.directories = [self.root / name for name in ('B', 'D')]
        for directory in self.directories:
            shutil.copytree(self.fixture.fixture.source, directory)
        self.tasks = [{'task_id': name, 'input_dir': str(directory), 'script': 'job.sh',
            'outputs': ['results/***', '*.log'], 'input_files': ['job.sh', 'data.txt']}
            for name, directory in zip(('one', 'two'), self.directories)]

    def plan(self, **options):
        args = dict(request_key='test-request', name='Test batch', cluster='lsf',
            project_root=str(self.root), tasks=self.tasks, limits={'poll_interval_seconds': 5},
            start_monitor=False)
        args.update(options)
        return self.batches.plan(**args)

    def start(self, value):
        return self.batches.start(value['batch_id'], value['review_token'],
            'Synthetic user confirmation of this exact complete batch')

    def advance(self, bid, cycles=6):
        workflows = WorkflowService(self.jobs, clock=lambda: self.fixture.epoch[0])
        wf = self.batches.get(bid)['workflow_id']
        self.batches.lifecycles.clock = lambda: self.fixture.epoch[0]
        for _ in range(cycles):
            workflows.tick(wf)
            for item in self.batches.get(bid)['tasks'].values():
                self.batches.lifecycles.tick(item['run_id'], 5)
            with patch.object(self.jobs, 'job_sync_start', side_effect=self.fixture.sync_immediately):
                self.fixture.engine.tick()
            self.fixture.advance()
        return self.batches.status(bid, include_completed=True)

    def test_one_plan_one_start_and_summary_returns_two_independent_directories(self):
        with patch.object(self.jobs.transport, 'run') as transport:
            value = self.plan(compact=False)
            self.assertTrue(value['ok'], value)
            self.assertEqual(len(value['plan']['workflow_plan']['runs']), 2)
            self.start(value)
            transport.assert_not_called()
        result = self.advance(value['batch_id'])
        self.assertEqual(result['stage'], 'complete', result)
        self.assertEqual(result['counts'], {'complete': 2})
        self.assertEqual({t['output_dir'] for t in result['tasks']},
            {str(d) for d in self.directories})
        self.assertEqual((self.root / 'submissions').read_text(), '2')
        with patch.object(self.jobs, 'job_submit') as submit:
            self.start(value)
            submit.assert_not_called()
        self.assertEqual(self.batches.status(value['batch_id'])['tasks'], [])

    def test_request_key_is_idempotent_and_conflicts_are_rejected(self):
        value = self.plan()
        self.batches = BatchService(self.jobs)
        with patch.object(self.jobs, 'job_prepare') as prepare:
            again = self.plan()
            self.assertEqual(value['batch_id'], again['batch_id'])
            self.assertEqual(value['review_token'], again['review_token'])
            prepare.assert_not_called()
            with self.assertRaises(BatchError):
                self.plan(name='Changed intent')

    def test_slurm_batch_uses_the_same_authorization_and_return_pipeline(self):
        value = self.plan(cluster='slurm')
        self.assertTrue(value['ok'], value)
        self.start(value)
        result = self.advance(value['batch_id'])
        self.assertEqual(result['counts'], {'complete': 2}, result)
        self.assertEqual((self.root / 'submissions').read_text(), '2')

    def test_prepare_failure_blocks_whole_batch_and_retry_reuses_successful_snapshot(self):
        (self.directories[1] / 'job.sh').unlink()
        value = self.plan()
        self.assertFalse(value['ok'])
        self.assertIsNone(value['review_token'])
        successful = value['tasks'][0]['run_id']
        self.assertIsNone(self.jobs.history.get(successful)['submission_authorization']
                          if 'submission_authorization' in self.jobs.history.get(successful) else None)
        with self.assertRaises(BatchError):
            self.start(value)
        (self.directories[1] / 'job.sh').write_text((self.directories[0] / 'job.sh').read_text())
        with patch.object(self.jobs, 'job_prepare', wraps=self.jobs.job_prepare) as prepare:
            recovered = self.plan()
            self.assertTrue(recovered['ok'], recovered)
            self.assertEqual(prepare.call_count, 1)
            self.assertEqual(successful, recovered['tasks'][0]['run_id'])

    def test_crash_after_history_insert_adopts_the_reserved_run(self):
        original = self.batches.prepare
        def crash(*args):
            original(*args)
            raise SystemExit('client died after snapshot commit')
        with patch.object(self.batches, 'prepare', side_effect=crash), self.assertRaises(SystemExit):
            self.plan()
        with patch.object(self.jobs, 'job_prepare', wraps=self.jobs.job_prepare) as prepare:
            recovered = self.plan()
            self.assertTrue(recovered['ok'], recovered)
            self.assertEqual(prepare.call_count, 1)

    def test_orphan_snapshot_is_rebuilt_only_with_matching_ownership(self):
        with patch.object(self.jobs.history, 'add', side_effect=SystemExit('before database commit')):
            with self.assertRaises(SystemExit):
                self.plan()
        recovered = self.plan()
        self.assertTrue(recovered['ok'], recovered)
        self.assertEqual(len(self.jobs.history.list()), 2)

    def test_crash_after_workflow_registration_reuses_the_reserved_workflow(self):
        with patch.object(self.batches, 'freeze', side_effect=SystemExit('after workflow commit')):
            with self.assertRaises(SystemExit):
                self.plan()
        with patch.object(self.batches.workflows, 'plan') as register:
            recovered = self.plan()
            self.assertTrue(recovered['ok'], recovered)
            register.assert_not_called()

    def test_start_crash_keeps_workflow_disabled_until_all_tasks_are_enrolled(self):
        value = self.plan()
        original = self.batches.lifecycles.start
        def crash(rid, **options):
            original(rid, **options)
            raise SystemExit('after one lifecycle enrollment')
        with patch.object(self.batches.lifecycles, 'start', side_effect=crash):
            with self.assertRaises(SystemExit):
                self.start(value)
        workflow = self.batches.workflows.get(self.batches.get(value['batch_id'])['workflow_id'])['workflow']
        self.assertFalse(workflow['enabled'])
        self.assertFalse((self.root / 'submissions').exists())
        self.start(value)
        self.assertEqual(self.advance(value['batch_id'])['counts'], {'complete': 2})

    def test_wrong_and_cross_batch_tokens_never_upload(self):
        one = self.plan()
        two = self.plan(request_key='another-request')
        with patch.object(self.jobs.transfer, 'run') as transfer:
            for token in ('wrong', one['review_token']):
                with self.assertRaises(BatchError):
                    self.batches.start(two['batch_id'], token, 'Synthetic exact approval')
            transfer.assert_not_called()

    def test_one_changed_snapshot_prevents_authorization_for_every_task(self):
        value = self.plan()
        rid = value['tasks'][1]['run_id']
        (Path(self.jobs.history.get(rid)['snapshot_dir']) / 'data.txt').write_text('changed')
        with self.assertRaises(ValueError):
            self.start(value)
        for task in value['tasks']:
            self.assertFalse(self.jobs.history.get(task['run_id']).get('submission_authorization'))
        self.assertFalse((self.root / 'submissions').exists())

    def test_configuration_change_requires_review_and_new_confirmation(self):
        value = self.plan()
        self.jobs.clusters['lsf'] = replace(self.jobs.clusters['lsf'], transfer_timeout=600)
        with self.assertRaises(ValueError):
            self.start(value)
        reviewed = self.batches.review(value['batch_id'])
        self.assertNotEqual(value['review_token'], reviewed['review_token'])
        with self.assertRaises(BatchError):
            self.start(value)
        self.start(reviewed)

    def test_revocation_is_not_undone_by_duplicate_start_or_resume(self):
        value = self.plan()
        self.start(value)
        self.jobs.job_revoke_authorization(value['tasks'][0]['run_id'])
        with self.assertRaises(ValueError):
            self.start(value)
        with self.assertRaises(ValueError):
            self.batches.resume(value['batch_id'], ['one'])
        self.assertFalse(self.jobs.history.get(value['tasks'][0]['run_id'])['submission_authorization'])

    def test_pause_then_resume_one_task_does_not_release_other_tasks(self):
        value = self.plan()
        self.start(value)
        self.batches.pause(value['batch_id'])
        self.batches.resume(value['batch_id'], ['one'])
        result = self.advance(value['batch_id'])
        self.assertEqual((self.root / 'submissions').read_text(), '1')
        self.assertEqual({t['task_id']: t['stage'] for t in result['tasks']},
            {'one': 'complete', 'two': 'paused'})
        self.batches.resume(value['batch_id'], ['two'])
        self.assertEqual(self.advance(value['batch_id'])['counts'], {'complete': 2})

    def test_failed_upload_retries_only_selected_task_without_resubmitting_successes(self):
        value = self.plan()
        self.start(value)
        self.jobs.transfer.fail_upload = True
        self.advance(value['batch_id'], cycles=1)
        self.jobs.transfer.fail_upload = False
        self.batches.resume(value['batch_id'], ['one'])
        self.assertEqual(self.advance(value['batch_id'])['counts'], {'complete': 2})
        self.assertEqual((self.root / 'submissions').read_text(), '2')

    def test_same_directory_colliding_outputs_are_blocked_before_registration(self):
        self.tasks[1]['automation'] = {'sync_options': {'destination': str(self.directories[0])}}
        value = self.plan()
        self.assertFalse(value['ok'])
        self.assertIn('overlap', value['last_error'])
        self.assertFalse((self.root / 'submissions').exists())

    def test_output_that_matches_another_tasks_input_is_blocked(self):
        self.tasks[0]['outputs'] = ['data.txt']
        value = self.plan()
        self.assertFalse(value['ok'])
        self.assertIn('overwrite', value['last_error'])

    def test_duplicate_outside_project_and_unknown_task_fields_are_rejected(self):
        for tasks in ([self.tasks[0], self.tasks[0]],
                      [{**self.tasks[0], 'input_dir': '/tmp'}],
                      [{**self.tasks[0], 'guess_software': True}]):
            with self.subTest(tasks=tasks), self.assertRaises(ValueError):
                self.plan(tasks=tasks)

    def test_unknown_application_needs_a_template_and_never_falls_back(self):
        (self.directories[0] / 'card.gjf').write_text('input')
        value = self.plan(tasks=[{'application': 'unregistered',
            'input_path': str(self.directories[0] / 'card.gjf')}])
        self.assertFalse(value['ok'])
        self.assertEqual(value['tasks'][0]['error_code'], 'application_not_registered')
        self.assertFalse(self.jobs.history.list())

    def test_status_is_local_read_only_paginated_and_contains_all_counts(self):
        value = self.plan()
        with patch.object(self.jobs.transport, 'run') as transport:
            first = self.batches.status(value['batch_id'], limit=1)
            second = self.batches.status(value['batch_id'], limit=1, offset=1)
            self.assertEqual(sum(first['counts'].values()), 2)
            self.assertEqual(first['next_offset'], 1)
            self.assertNotEqual(first['tasks'][0]['task_id'], second['tasks'][0]['task_id'])
            transport.assert_not_called()

    def test_cli_plan_and_status_share_persisted_batch(self):
        from hpc_mcp.config import ConfigManager
        config = self.root / 'clusters.toml'
        ConfigManager(config, {}).set('lsf', {'ssh_host': 'alias', 'scheduler': 'lsf',
            'work_root': str(self.fixture.fixture.remote)})
        task_file = self.root / 'tasks.json'
        task_file.write_text(json.dumps(self.tasks))
        argv = [sys.executable, '-m', 'hpc_mcp', '--config', str(config),
            '--state-dir', str(self.jobs.history.root)]
        env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
        planned = subprocess.run(argv + ['batch-plan', 'cli-key', 'CLI batch', 'lsf',
            '--project-root', str(self.root), '--tasks-file', str(task_file), '--no-start-monitor'],
            capture_output=True, text=True, env=env)
        self.assertEqual(planned.returncode, 0, planned.stderr)
        value = json.loads(planned.stdout)
        result = subprocess.run(argv + ['batch-status', value['batch_id']],
            capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['task_count'], 2)

    def test_default_start_runs_detached_batch_without_per_task_client_calls(self):
        from dataclasses import asdict
        from hpc_mcp.config import ConfigManager
        fixture = self.fixture.fixture
        ssh = fixture.bin / 'ssh'
        ssh.write_text('#!/usr/bin/env python3\nimport os,sys\n'
            "os.execvp('bash',['bash','-c',' '.join(sys.argv[sys.argv.index('alias')+1:])])\n")
        ssh.chmod(0o755)
        config = self.root / 'clusters.toml'
        ConfigManager(config, {}).set('lsf', {'ssh_host': 'alias', 'scheduler': 'lsf',
            'work_root': str(fixture.remote)})
        self.jobs.config_path = config
        value = self.plan(start_monitor=True, monitor_settings=asdict(self.fixture.settings))
        def stop_and_wait():
            self.batches.monitor.stop()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and self.batches.monitor.status()['runtime']['alive']:
                time.sleep(0.05)
        self.addCleanup(stop_and_wait)
        environment = {**os.environ, 'PATH': str(fixture.bin) + ':' + os.environ['PATH']}
        with patch.dict(os.environ, environment):
            self.start(value)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            status = self.batches.status(value['batch_id'], include_completed=True)
            if status['stage'] == 'complete' or status['counts'].get('needs_attention'):
                break
            time.sleep(0.1)
        self.assertEqual(status['stage'], 'complete', status)
        self.assertEqual((self.root / 'submissions').read_text(), '2')
        self.assertTrue(all((d / 'results/value.txt').is_file() for d in self.directories))


class BatchApplicationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_applications.ApplicationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.register()
        self.jobs = self.fixture.jobs
        self.batch = BatchService(self.jobs)
        self.second = self.fixture.card.with_name('methane.gjf')
        self.second.write_text(self.fixture.card.read_text())
        self.tasks = [{'task_id': name, 'application': 'gaussian', 'input_path': str(path),
            'parameters': {'cpus': 28}} for name, path in
            (('water', self.fixture.card), ('methane', self.second))]

    def plan(self):
        return self.batch.plan('gaussian-batch', 'Two cards', 'lsf',
            str(self.fixture.fixture.root), self.tasks, start_monitor=False, compact=False)

    def test_registered_cards_in_same_directory_keep_distinct_outputs_and_original_style(self):
        value = self.plan()
        self.assertTrue(value['ok'], value)
        self.assertEqual(len(value['policies']), 1)
        for task in value['tasks']:
            run = self.jobs.history.get(task['run_id'])
            self.assertEqual(run['template']['application_plugin']['version'], 1)
            self.assertIn('#BSUB -R span[ptile=28]', run['generated_script'])
            self.assertEqual(task['outputs'], [task['task_id'] + '.log', task['task_id'] + '.chk'])
        with patch.object(self.batch.apps, 'prepare') as prepare:
            self.assertEqual(self.plan()['review_token'], value['review_token'])
            prepare.assert_not_called()

    def test_interruption_pins_application_version_for_remaining_tasks(self):
        original = self.batch.apps.prepare
        def crash(*args, **kwargs):
            original(*args, **kwargs)
            raise SystemExit('after first generated input snapshot')
        with patch.object(self.batch.apps, 'prepare', side_effect=crash), self.assertRaises(SystemExit):
            self.plan()
        self.fixture.register()  # Version 2 becomes the current binding.
        value = self.plan()
        self.assertTrue(value['ok'], value)
        self.assertEqual([t['version'] for t in value['tasks']], [1, 1])


if __name__ == '__main__':
    unittest.main()
