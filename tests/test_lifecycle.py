"""Durable lifecycle recovery over offline scheduler and real local rsync fixtures."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import test_jobs
import test_monitor
from hpc_mcp.applications import ApplicationService
from hpc_mcp.lifecycle import LifecycleService
from hpc_mcp.submission import SubmissionError


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_monitor.MonitorTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.jobs = self.fixture.jobs
        self.monitor = self.fixture.monitor
        self.engine = self.fixture.engine
        self.life = LifecycleService(self.jobs, clock=lambda: self.fixture.epoch[0])

    def prepare(self, directory=None):
        directory = directory or self.fixture.fixture.source
        return self.jobs.job_prepare('lsf', str(directory), 'job.sh',
            ['results/***', '*.log'], project_root=str(self.fixture.fixture.root),
            input_files=['job.sh', 'data.txt'])['run']['run_id']

    def approve(self, rid, cleanup=False, remote=False):
        return test_jobs.authorize(self.jobs, rid, auto_submit=True, auto_sync=True,
            cleanup_policy={'storage_categories': ['snapshot'] if cleanup else [],
                            'storage_age_seconds': 0}, remote_cleanup=remote)

    def criterion(self, rid, success='42', failure='BAD'):
        apps = ApplicationService(self.jobs)
        bundle = self.fixture.fixture.root / 'criterion-bundle'
        shutil.copytree(Path(__file__).resolve().parents[1] /
            'examples/applications/gaussian', bundle)
        manifest = json.loads((bundle / 'manifest.json').read_text())
        manifest['clusters'] = ['lsf']
        manifest['validation'] = {'log': 'results/value.txt',
            'success_marker': success, 'failure_marker': failure}
        (bundle / 'manifest.json').write_text(json.dumps(manifest))
        record = apps.install(str(bundle))['application']
        # No handler execution or reviewer claims: exercise retained-output checks
        # against an immutable installed manifest, using the offline fixture script.
        self.jobs.history.update(rid, 'test_validation_metadata', template={
            'application_plugin': {'application': 'gaussian', 'version': record['version'],
                'request': {'input_name': 'water.gjf'}, 'parameters': {}, 'cluster_settings': {}}})

    def cycle(self, rid):
        self.life.tick(rid, 5)
        with patch.object(self.jobs, 'job_sync_start', side_effect=self.fixture.sync_immediately):
            self.engine.tick()
        self.fixture.advance()

    def finish(self, rid, release=False):
        for _ in range(10):
            run = self.jobs.history.get(rid)
            if release and run['phase'] == 'submitted' and not run.get('retention', {}).get('released'):
                test_jobs.release_retention(self.jobs, rid)
            self.cycle(rid)
            if self.life.get(rid)['lifecycle']['stage'] in ('complete', 'needs_attention'):
                return self.life.get(rid)
        self.fail('lifecycle did not reach a terminal local stage')

    def test_approval_and_explicit_start_are_required_without_network_effects(self):
        rid = self.prepare()
        with patch.object(self.jobs.transport, 'run') as transport:
            self.assertEqual(self.life.get(rid)['lifecycle']['stage'], 'awaiting_authorization')
            self.life.tick(rid)
            with self.assertRaises(SubmissionError):
                self.life.start(rid)
            test_jobs.authorize(self.jobs, rid)
            with self.assertRaises(SubmissionError):
                self.life.start(rid)
            self.approve(rid)
            self.life.start(rid)
            self.assertEqual(self.jobs.history.get(rid)['phase'], 'prepared')
            transport.assert_not_called()

    def test_restart_finishes_once_and_unverified_results_preserve_caches(self):
        rid = self.prepare()
        self.approve(rid, cleanup=True, remote=True)
        self.life.start(rid)
        self.cycle(rid)
        self.life = LifecycleService(self.jobs, clock=lambda: self.fixture.epoch[0])
        with patch.object(self.jobs, 'job_submit', wraps=self.jobs.job_submit) as submit:
            result = self.finish(rid)
            self.life.start(rid)
            self.life.tick(rid)
            submit.assert_not_called()
        self.assertEqual(result['lifecycle']['stage'], 'complete')
        self.assertEqual(result['scheduler']['state'], 'succeeded')
        self.assertEqual(result['application_result']['status'], 'unverified')
        self.assertEqual(result['sync']['sync_state'], 'complete')
        self.assertTrue(result['cleanup']['deferred'])
        self.assertTrue(Path(self.jobs.history.get(rid)['snapshot_dir']).exists())
        self.assertFalse(result['cleanup']['remote_removed'])
        self.assertTrue((self.fixture.fixture.source / 'results/value.txt').is_file())

    def test_passed_markers_allow_approved_local_and_remote_cleanup(self):
        rid = self.prepare()
        self.criterion(rid)
        self.approve(rid, cleanup=True, remote=True)
        self.life.start(rid)
        result = self.finish(rid, release=True)
        self.assertEqual(result['lifecycle']['stage'], 'complete')
        self.assertEqual(result['application_result']['status'], 'passed')
        self.assertTrue(result['cleanup']['remote_removed'])
        self.assertFalse(Path(self.jobs.history.get(rid)['snapshot_dir']).exists())
        self.assertEqual((self.fixture.fixture.source / 'results/value.txt').read_text(), '42\n')

    def test_successful_calculation_defers_cleanup_while_retained(self):
        rid = self.prepare()
        self.criterion(rid)
        self.approve(rid, cleanup=True, remote=True)
        self.life.start(rid)
        result = self.finish(rid)
        self.assertEqual(result['lifecycle']['stage'], 'complete')
        self.assertIn('Retention', result['cleanup']['deferred'])
        run = self.jobs.history.get(rid)
        self.assertTrue(Path(run['remote_dir']).exists())
        self.assertTrue(Path(run['snapshot_dir']).exists())

    def test_failed_application_preserves_data_even_if_scheduler_succeeds(self):
        rid = self.prepare()
        self.criterion(rid, failure='42')
        self.approve(rid, cleanup=True, remote=True)
        self.life.start(rid)
        result = self.finish(rid)
        self.assertEqual(result['lifecycle']['stage'], 'needs_attention')
        self.assertEqual(result['application_result']['status'], 'failed')
        self.assertEqual(result['scheduler']['state'], 'succeeded')
        self.assertTrue(Path(self.jobs.history.get(rid)['snapshot_dir']).exists())
        self.assertTrue(Path(self.jobs.history.get(rid)['remote_dir']).exists())

    def test_missing_marker_stays_unproven_and_does_not_cleanup(self):
        rid = self.prepare()
        self.criterion(rid, success='never present')
        self.approve(rid, cleanup=True)
        self.life.start(rid)
        result = self.finish(rid)
        self.assertEqual(result['application_result']['status'], 'pending')
        self.assertEqual(result['lifecycle']['stage'], 'needs_attention')
        self.assertTrue(Path(self.jobs.history.get(rid)['snapshot_dir']).exists())

    def test_failed_scheduler_still_downloads_diagnostics_and_preserves_remote_data(self):
        rid = self.prepare()
        self.approve(rid, cleanup=True, remote=True)
        self.life.start(rid)
        self.life.tick(rid, 5)
        (self.fixture.fixture.bin / 'bjobs').write_text("#!/bin/bash\nprintf '4242|EXIT|1\\n'\n")
        result = self.finish(rid)
        self.assertEqual(result['lifecycle']['stage'], 'needs_attention')
        self.assertEqual(result['scheduler']['state'], 'failed')
        self.assertEqual(result['sync']['sync_state'], 'complete')
        self.assertTrue(Path(self.jobs.history.get(rid)['remote_dir']).exists())

    def test_remote_cleanup_failure_resumes_only_cleanup(self):
        rid = self.prepare()
        self.criterion(rid)
        self.approve(rid, cleanup=True, remote=True)
        self.life.start(rid)
        with patch.object(self.jobs, 'job_remote_cleanup', return_value={'ok': False, 'error': 'offline'}):
            result = self.finish(rid, release=True)
        self.assertEqual(result['lifecycle']['failed_step'], 'remote_cleanup')
        self.assertTrue(Path(self.jobs.history.get(rid)['remote_dir']).exists())
        self.life.start(rid, resume=True)
        with patch.object(self.jobs, 'job_submit') as submit, \
                patch.object(self.jobs, 'job_sync_start') as sync:
            self.assertEqual(self.life.tick(rid)['lifecycle']['stage'], 'complete')
            submit.assert_not_called()
            sync.assert_not_called()

    def test_sync_failure_resume_does_not_submit_again(self):
        rid = self.prepare()
        self.approve(rid)
        self.life.start(rid)
        self.jobs.transfer.fail_download = True
        result = self.finish(rid)
        self.assertEqual(result['lifecycle']['stage'], 'needs_attention')
        with self.assertRaises(ValueError):
            self.life.start(rid)
        self.jobs.transfer.fail_download = False
        self.life.start(rid, resume=True)
        with patch.object(self.jobs, 'job_submit') as submit:
            self.assertEqual(self.finish(rid)['lifecycle']['stage'], 'complete')
            submit.assert_not_called()

    def test_upload_failure_resume_retries_the_same_snapshot(self):
        rid = self.prepare()
        self.approve(rid)
        self.life.start(rid)
        self.jobs.transfer.fail_upload = True
        self.assertEqual(self.life.tick(rid)['lifecycle']['stage'], 'needs_attention')
        self.assertIsNone(self.jobs.history.get(rid)['job_id'])
        self.jobs.transfer.fail_upload = False
        self.life.start(rid, resume=True)
        self.assertEqual(self.finish(rid)['lifecycle']['stage'], 'complete')
        self.assertEqual(self.jobs.history.get(rid)['job_id'], '4242')

    def test_revoked_or_changed_authorization_stops_future_actions(self):
        rid = self.prepare()
        self.approve(rid)
        self.life.start(rid)
        self.jobs.job_revoke_authorization(rid)
        with patch.object(self.jobs, 'job_submit') as submit:
            self.assertEqual(self.life.tick(rid)['lifecycle']['failed_step'], 'authorization')
            submit.assert_not_called()
        self.approve(rid)
        self.life.start(rid, resume=True)
        self.jobs.job_submission_plan(rid, {'auto_submit': True, 'auto_sync': True,
            'remote_cleanup': True})
        with patch.object(self.jobs, 'job_submit') as submit:
            self.assertEqual(self.life.tick(rid)['lifecycle']['stage'], 'needs_attention')
            submit.assert_not_called()

    def test_pause_disables_monitor_and_explicit_unwatch_is_not_overridden(self):
        rid = self.prepare()
        self.approve(rid)
        self.life.start(rid)
        self.life.tick(rid, 5)
        self.monitor.unwatch(rid)
        self.fixture.advance()
        self.assertEqual(self.life.tick(rid)['lifecycle']['stage'], 'needs_attention')
        self.assertFalse(self.monitor.entry(rid)['enabled'])
        self.life.start(rid, resume=True)
        self.life.pause(rid)
        self.assertFalse(self.monitor.entry(rid)['enabled'])
        self.assertFalse(self.life.get(rid)['lifecycle']['enabled'])

    def test_two_task_directories_receive_their_own_outputs(self):
        root = self.fixture.fixture.root
        ids = []
        for name in ('B', 'D'):
            directory = root / name
            shutil.copytree(self.fixture.fixture.source, directory)
            rid = self.prepare(directory)
            self.approve(rid)
            self.life.start(rid)
            ids.append(rid)
        for rid in ids:
            self.assertEqual(self.finish(rid)['lifecycle']['stage'], 'complete')
        self.assertEqual({self.life.get(rid)['sync']['output_dir'] for rid in ids},
            {str(root / 'B'), str(root / 'D')})
        self.assertEqual(len(self.life.list()['lifecycles']), 2)

    def test_retained_output_tampering_blocks_cleanup_and_resume_does_not_resync(self):
        rid = self.prepare()
        self.approve(rid)
        self.life.start(rid)
        result = self.finish(rid)
        (Path(result['sync']['output_dir']) / 'results/value.txt').write_text('modified locally')
        self.life.start(rid, resume=True)
        with patch.object(self.jobs, 'job_sync_start') as sync:
            result = self.life.tick(rid)
            self.assertEqual(result['lifecycle']['failed_step'], 'retention')
            sync.assert_not_called()

    def test_workflow_owns_submission_while_lifecycle_handles_result_return(self):
        from hpc_mcp.workflow import WorkflowService
        rid = self.prepare()
        workflows = WorkflowService(self.jobs, clock=lambda: self.fixture.epoch[0])
        value = workflows.plan('full task', [rid], limits={'poll_interval_seconds': 5},
            automation={'auto_sync': True})['workflow']
        workflows.start(value['workflow_id'], value['review_token'], 'Synthetic exact workflow approval')
        self.life.start(rid)
        with patch.object(self.jobs, 'job_submit') as submit:
            self.assertEqual(self.life.tick(rid, 5)['lifecycle']['stage'], 'awaiting_workflow')
            submit.assert_not_called()
        workflows.tick(value['workflow_id'])
        self.fixture.advance()
        self.assertEqual(self.finish(rid)['lifecycle']['stage'], 'complete')
        self.assertEqual((self.fixture.fixture.root / 'submissions').read_text(), '1')

    def test_lost_submission_response_recovers_without_a_second_submission(self):
        rid = self.prepare()
        self.approve(rid)
        self.life.start(rid)
        self.jobs.transport.lose_response = True
        first = self.life.tick(rid)
        self.assertEqual(first['lifecycle']['stage'], 'needs_attention')
        self.assertEqual(self.jobs.history.get(rid)['phase'], 'submission_unknown')
        self.life.start(rid, resume=True)
        self.assertEqual(self.finish(rid)['lifecycle']['stage'], 'complete')
        self.assertEqual((self.fixture.fixture.root / 'submissions').read_text(), '1')

    def test_detached_coordinator_submits_and_returns_after_cli_client_exits(self):
        from dataclasses import asdict
        from hpc_mcp.config import ConfigManager
        fixture = self.fixture.fixture
        ssh = fixture.bin / 'ssh'
        ssh.write_text('#!/usr/bin/env python3\nimport os,sys\n'
            "os.execvp('bash',['bash','-c',' '.join(sys.argv[sys.argv.index('alias')+1:])])\n")
        ssh.chmod(0o755)
        config = fixture.root / 'clusters.toml'
        ConfigManager(config, {}).set('lsf', {
            'ssh_host': 'alias', 'scheduler': 'lsf', 'work_root': str(fixture.remote)})
        self.jobs.config_path = config
        rid = self.prepare()
        self.approve(rid)
        argv = [sys.executable, '-m', 'hpc_mcp', '--config', str(config),
            '--state-dir', str(self.jobs.history.root)]
        environment = {**os.environ, 'PATH': str(fixture.bin) + ':' + os.environ['PATH'],
            'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
        start = subprocess.run(argv + ['lifecycle-start', rid], env=environment,
            capture_output=True, text=True)
        self.assertEqual(start.returncode, 0, start.stderr)
        self.assertEqual(self.jobs.history.get(rid)['phase'], 'prepared')

        def stop_and_wait():
            self.monitor.stop()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and self.monitor.status()['runtime']['alive']:
                time.sleep(0.05)
        self.addCleanup(stop_and_wait)
        start = subprocess.run(argv + ['monitor-start', '--settings',
            json.dumps(asdict(self.fixture.settings))], env=environment,
            capture_output=True, text=True)
        self.assertEqual(start.returncode, 0, start.stderr)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline and self.life.get(rid)['lifecycle']['stage'] not in (
                'complete', 'needs_attention'):
            time.sleep(0.1)
        result = self.life.get(rid)
        self.assertEqual(result['lifecycle']['stage'], 'complete', result)
        self.assertEqual((fixture.root / 'submissions').read_text(), '1')
        self.assertTrue((fixture.source / 'results/value.txt').is_file())


if __name__ == '__main__':
    unittest.main()
