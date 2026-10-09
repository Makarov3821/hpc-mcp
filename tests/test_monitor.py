"""Durable opt-in monitoring, query budgets, retry safety and detached lifecycle."""

from dataclasses import asdict
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import test_jobs
from hpc_mcp.history import now
from hpc_mcp.jobs import JobService
from hpc_mcp.monitor import MonitorService, MonitorSettings, state_lock
from hpc_mcp.monitor_engine import MonitorEngine
from hpc_mcp.ssh import CommandResult


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_jobs.JobLifecycleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.jobs = self.fixture.jobs
        self.monitor = MonitorService(self.jobs)
        self.epoch = [time.time()]
        self.settings = MonitorSettings(poll_interval_seconds=5, retry_initial_seconds=5,
                                       retry_max_seconds=20, sync_max_attempts=2)
        self.engine = MonitorEngine(self.monitor, self.settings, lambda: self.epoch[0])
        self.counter = 0

    def submit(self, scheduler='lsf'):
        run = self.jobs.job_prepare(scheduler, str(self.fixture.source), 'job.sh',
            ['results/***', '*.log'], project_root=str(self.fixture.root), input_files=['job.sh', 'data.txt'])['run']
        self.assertTrue(self.jobs.job_submit(run['run_id'])['ok'])
        return self.jobs.history.get(run['run_id'])

    def advance(self, seconds=5):
        self.epoch[0] += seconds

    def sync_immediately(self, run_id, options):
        result = self.jobs.job_sync(run_id, **options)
        return self.operation(run_id, 'succeeded' if result['ok'] else 'failed', result=result)

    def operation(self, run_id, state, **values):
        self.counter += 1
        operation_id = 'op_test_' + str(self.counter)
        value = {'operation_id': operation_id, 'run_id': run_id, 'created_at': now(),
                 'state': state, 'options': {}, 'progress': {}, **values}
        with self.jobs.history.connect() as db:
            db.execute('INSERT INTO operations VALUES (?,?,?)', (operation_id, run_id, json.dumps(value)))
        self.jobs.history.update(run_id, 'sync_operation_registered', last_sync_operation=operation_id)
        return {'ok': True, 'operation': value}

    def test_watch_is_atomic_idempotent_and_does_not_submit_or_start_daemon(self):
        run = self.submit()
        with patch.object(self.jobs, 'job_submit') as submit, patch('hpc_mcp.monitor.subprocess.Popen') as process:
            first = self.monitor.watch([run['run_id']])['monitors'][0]
            second = self.monitor.watch([run['run_id']])['monitors'][0]
            self.assertEqual(first['generation'], second['generation'])
            self.assertTrue(first['policy']['sync_options']['stable_only'])
            self.assertEqual(first['policy']['sync_options']['destination'], str(self.fixture.source))
            submit.assert_not_called()
            process.assert_not_called()
        prepared = self.fixture.prepare()
        with self.assertRaises(ValueError):
            self.monitor.watch([run['run_id'], prepared['run_id']], reset=True)
        self.assertEqual(first['generation'], self.monitor.entry(run['run_id'])['generation'])
        for options in ({'stable_only': False}, {'unknown': True}, {'overwrite': 'replace'}, {'resume': 'yes'}, {'max_file_bytes': 0}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.monitor.watch([run['run_id']], sync_options=options)

    def test_terminal_sync_once_then_restart_preserves_completion_and_notifications(self):
        run = self.submit()
        self.monitor.watch([run['run_id']])
        with patch.object(self.jobs, 'job_sync_start', side_effect=self.sync_immediately) as start:
            self.engine.tick()
            self.advance()
            self.engine.tick()
            self.assertEqual(start.call_count, 1)
        self.assertEqual(self.monitor.entry(run['run_id'])['stage'], 'complete')
        self.assertTrue((self.fixture.source / 'results/value.txt').is_file())
        restarted = MonitorService(self.jobs)
        MonitorEngine(restarted, self.settings, lambda: self.epoch[0]).tick()
        self.assertEqual(restarted.entry(run['run_id'])['stage'], 'complete')
        events = restarted.notifications()['notifications']
        complete = [e for e in events if e['kind'] == 'complete']
        self.assertEqual(len(complete), 1)
        self.assertEqual(complete[0]['detail']['scheduler_state'], 'succeeded')
        self.assertEqual(restarted.notifications(complete[0]['id'])['notifications'], [])

    def test_failed_queries_never_use_stale_terminal_state_for_sync_or_cleanup(self):
        run = self.submit()
        self.jobs.history.update(run['run_id'], 'old_terminal', state='succeeded')
        self.monitor.watch([run['run_id']], cleanup_policy={'storage_categories': ['snapshot'], 'storage_age_seconds': 0})
        with patch.object(self.jobs.transport, 'run', return_value=CommandResult(-1, error='ssh_connection_failed')), \
             patch.object(self.jobs, 'job_sync_start') as start, patch.object(self.jobs, 'job_storage_cleanup') as cleanup:
            self.engine.tick()
            first = self.monitor.entry(run['run_id'])
            self.assertEqual(first['query_failures'], 1)
            self.assertEqual(first['next_check_at'], self.epoch[0] + 5)
            self.advance()
            self.engine.tick()
            self.assertEqual(self.monitor.entry(run['run_id'])['next_check_at'], self.epoch[0] + 10)
            start.assert_not_called()
            cleanup.assert_not_called()
        self.assertTrue(Path(run['snapshot_dir']).exists())

    def test_retries_are_bounded_and_same_policy_does_not_accidentally_rearm(self):
        run = self.submit()
        self.monitor.watch([run['run_id']])
        def fail(run_id, options):
            return self.operation(run_id, 'failed', error='download conflict')
        with patch.object(self.jobs, 'job_sync_start', side_effect=fail) as start:
            self.engine.tick()
            self.advance()
            self.engine.tick()  # Observe failure, wait before retry.
            self.advance()
            self.engine.tick()
            self.advance()
            self.engine.tick()
            self.assertEqual(start.call_count, 2)
        self.assertEqual(self.monitor.entry(run['run_id'])['stage'], 'needs_attention')
        self.monitor.watch([run['run_id']])
        self.assertEqual(self.monitor.entry(run['run_id'])['stage'], 'needs_attention')
        self.monitor.watch([run['run_id']], reset=True)
        self.assertEqual(self.monitor.entry(run['run_id'])['sync_failures'], 0)

    def test_existing_worker_is_adopted_and_dead_worker_does_not_resubmit(self):
        run = self.submit()
        self.monitor.watch([run['run_id']])
        op = self.operation(run['run_id'], 'queued')['operation']
        with patch.object(self.jobs, 'job_sync_start') as start:
            self.engine.tick()
            self.assertEqual(self.monitor.entry(run['run_id'])['operation_id'], op['operation_id'])
            start.assert_not_called()
        from hpc_mcp.operations import operation_update
        operation_update(self.jobs.history, op['operation_id'], state='running', pid=999999)
        self.advance()
        self.engine.tick()
        self.assertEqual(self.monitor.entry(run['run_id'])['sync_failures'], 1)
        self.assertEqual(self.jobs.history.get(run['run_id'])['job_id'], '4242')

    def test_unwatch_during_query_prevents_new_actions(self):
        run = self.submit()
        self.monitor.watch([run['run_id']])
        original = self.jobs.transport.run
        def disable(*arguments):
            self.monitor.unwatch(run['run_id'])
            return original(*arguments)
        with patch.object(self.jobs.transport, 'run', side_effect=disable), patch.object(self.jobs, 'job_sync_start') as start:
            self.engine.tick()
            start.assert_not_called()
        self.assertFalse(self.monitor.entry(run['run_id'])['enabled'])

    def test_stop_signal_during_query_does_not_schedule_a_new_sync(self):
        run = self.submit()
        self.monitor.watch([run['run_id']])
        stopped = [False]
        engine = MonitorEngine(self.monitor, self.settings, stopping=lambda: stopped[0])
        original = self.jobs.transport.run
        def stop_during_query(*arguments):
            result = original(*arguments)
            stopped[0] = True
            return result
        with patch.object(self.jobs.transport, 'run', side_effect=stop_during_query), patch.object(self.jobs, 'job_sync_start') as start:
            engine.tick()
            start.assert_not_called()
        self.assertEqual(self.monitor.entry(run['run_id'])['stage'], 'watching')

    def test_cleanup_requires_verified_retained_results_and_preserves_project_outputs(self):
        run = self.submit()
        self.monitor.watch([run['run_id']], cleanup_policy={'storage_categories': ['snapshot'], 'storage_age_seconds': 0,
                                                          'sync_cache_age_seconds': 0})
        with patch.object(self.jobs, 'job_sync_start', side_effect=self.sync_immediately):
            self.engine.tick()
            self.advance()
            self.engine.tick()
        self.assertFalse(Path(run['snapshot_dir']).exists())
        output = self.fixture.source / 'results/value.txt'
        self.assertTrue(output.exists())
        self.assertTrue(self.jobs.history.get(run['run_id'])['manifest'])
        output.write_text('locally modified result')
        self.advance(self.settings.cleanup_interval_seconds)
        with patch.object(self.jobs, 'job_sync_start') as start:
            self.engine.tick()
            start.assert_not_called()
        self.assertEqual(self.monitor.entry(run['run_id'])['stage'], 'needs_attention')
        self.assertEqual(output.read_text(), 'locally modified result')

    def test_coalesced_statuses_and_fallback_preserve_exit_reason(self):
        one = self.submit()
        two = self.submit()
        self.jobs.history.update(two['run_id'], 'test_identity', job_id='4243')
        self.monitor.watch([one['run_id'], two['run_id']], auto_sync=False)
        with patch.object(self.jobs.transport, 'run', return_value=CommandResult(0, '4242|RUN|-|\n4243|EXIT|130|TERM_OWNER: killed\n')) as query:
            summary = self.engine.tick()
            self.assertEqual(query.call_count, 1)
            self.assertIn('4242 4243', query.call_args.args[1])
            self.assertEqual(summary['status_commands'], 1)
        self.assertEqual(self.jobs.history.get(two['run_id'])['state'], 'cancelled')
        self.assertEqual(self.monitor.entry(two['run_id'])['stage'], 'complete')
        self.assertEqual(self.monitor.entry(one['run_id'])['stage'], 'watching')

    def test_slurm_batches_missing_jobs_fall_back_to_accounting(self):
        one = self.submit('slurm')
        two = self.submit('slurm')
        self.jobs.history.update(two['run_id'], 'test_identity', job_id='4243')
        self.monitor.watch([one['run_id'], two['run_id']], auto_sync=False)
        def query(cluster, command):
            if '--jobs=4242,4243' in command:
                return CommandResult(0, '4242|RUNNING|None\n')
            if 'sacct' in command:
                return CommandResult(0, '4243|COMPLETED|0:0\n')
            return CommandResult(0, '')
        with patch.object(self.jobs.transport, 'run', side_effect=query) as transport:
            self.engine.tick()
            self.assertEqual(transport.call_count, 3)
        self.assertEqual(self.monitor.entry(two['run_id'])['stage'], 'complete')

    def test_strict_command_budget_and_fair_progress(self):
        one = self.submit()
        two = self.submit()
        self.jobs.history.update(two['run_id'], 'test_identity', job_id='4243')
        self.monitor.watch([one['run_id'], two['run_id']], auto_sync=False)
        settings = MonitorSettings(poll_interval_seconds=5, max_status_commands_per_cycle=1, batch_queries=False)
        engine = MonitorEngine(self.monitor, settings, lambda: self.epoch[0])
        def query(cluster, command):
            identity = '4242' if '4242' in command else '4243'
            return CommandResult(0, identity + '|RUN|-|\n')
        with patch.object(self.jobs.transport, 'run', side_effect=query) as transport:
            engine.tick()
            self.assertEqual(transport.call_count, 1)
            self.advance()
            engine.tick()
            self.assertEqual(transport.call_count, 2)
        self.assertTrue(all(self.monitor.entry(r['run_id']).get('last_query_attempt_at') for r in (one, two)))

    def test_poll_window_bounds_newly_registered_jobs_as_well_as_repeated_ticks(self):
        one = self.submit()
        two = self.submit()
        self.monitor.watch([one['run_id']], auto_sync=False)
        with patch.object(self.jobs.transport, 'run', return_value=CommandResult(0, '4242|RUN|-|\n')) as query:
            self.engine.tick()
            self.monitor.watch([two['run_id']], auto_sync=False)
            before = query.call_count
            self.engine.tick()
            self.assertEqual(query.call_count, before)
            self.advance()
            self.engine.tick()
            self.assertGreater(query.call_count, before)

    def test_empty_output_selection_is_not_successful_retention_or_infinite_retry(self):
        run = self.submit()
        self.monitor.watch([run['run_id']], sync_options={'includes': ['missing.chk']},
                           cleanup_policy={'storage_categories': ['snapshot'], 'storage_age_seconds': 0})
        with patch.object(self.jobs, 'job_sync_start', side_effect=self.sync_immediately) as start:
            self.engine.tick()
            self.advance()
            self.engine.tick()
            self.advance()
            self.engine.tick()
            self.advance()
            self.engine.tick()
            self.assertEqual(start.call_count, 2)
        self.assertEqual(self.monitor.entry(run['run_id'])['stage'], 'needs_attention')
        self.assertTrue(Path(run['snapshot_dir']).exists())

    def test_global_sync_capacity_includes_workers_started_outside_monitor(self):
        one = self.submit()
        two = self.submit()
        self.monitor.watch([two['run_id']])
        self.operation(one['run_id'], 'queued')
        settings = MonitorSettings(max_sync_operations=1)
        with patch.object(self.jobs, 'job_sync_start') as start:
            MonitorEngine(self.monitor, settings).tick()
            start.assert_not_called()
        self.assertEqual(self.monitor.entry(two['run_id'])['deferred'], 'sync operation capacity')

    def test_notification_retention_reports_cursor_gaps_without_deleting_history(self):
        run = self.submit()
        entry = self.monitor.watch([run['run_id']], auto_sync=False)['monitors'][0]
        for index in range(25):
            self.monitor.update(entry, ('test', 'event_' + str(index), {'index': index}), next_check_at=None)
        MonitorEngine(self.monitor, MonitorSettings(notifications_limit=10)).tick()
        events = self.monitor.notifications(after_id=1)
        self.assertTrue(events['cursor_gap'])
        self.assertEqual(len(events['notifications']), 10)
        self.assertTrue(self.jobs.history.events(run['run_id']))

    def test_pause_and_retention_settings_validate_without_side_effects(self):
        for values in ({'query_concurrency': True}, {'poll_interval_seconds': 1}, {'unknown': 1},
                       {'retry_initial_seconds': 100, 'retry_max_seconds': 10}, {'input_cache_cleanup': {'dry_run': False}}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                from hpc_mcp.monitor import settings_from
                settings_from(values)
        with state_lock(self.jobs.history, 'monitor-daemon.lock'):
            with self.assertRaises(ValueError):
                with state_lock(self.jobs.history, 'monitor-daemon.lock', blocking=False):
                    pass
        result = subprocess.run([sys.executable, '-m', 'hpc_mcp', '--state-dir', str(self.jobs.history.root),
                                 'monitor-status'], text=True, capture_output=True, cwd=self.fixture.root,
            env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)['runtime']['alive'])

    def test_shared_input_cache_policy_is_independent_and_opt_in(self):
        from hpc_mcp.storage import InputCache
        cache = InputCache(self.jobs.history)
        target = self.fixture.root / 'cached-copy'
        cache.copy(self.fixture.source / 'data.txt', target)
        settings = MonitorSettings(input_cache_cleanup={'older_than_seconds': 0, 'max_cache_bytes': 0})
        MonitorEngine(self.monitor, settings).tick()
        self.assertEqual(target.read_text(), 'input data')
        self.assertFalse(any(len(path.name) == 64 for path in cache.root.iterdir()))

    def test_foreground_service_signal_stop_and_crash_recovery(self):
        run = self.submit()
        (self.fixture.bin / 'bjobs').write_text("#!/bin/bash\nprintf '4242|RUN|-|\\n'\n")
        ssh = self.fixture.bin / 'ssh'
        ssh.write_text('#!/usr/bin/env python3\nimport os,sys\n'
            "os.execvp('bash',['bash','-c',' '.join(sys.argv[sys.argv.index('alias')+1:])])\n")
        ssh.chmod(0o755)
        config = self.fixture.root / 'clusters.toml'
        from hpc_mcp.config import ConfigManager
        ConfigManager(config, {}).set('lsf', {'ssh_host': 'alias', 'scheduler': 'lsf', 'work_root': str(self.fixture.remote)})
        self.monitor.watch([run['run_id']], auto_sync=False)
        env = {**os.environ, 'PATH': str(self.fixture.bin) + ':' + os.environ['PATH'],
               'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
        argv = [sys.executable, '-m', 'hpc_mcp', '--config', str(config), '--state-dir', str(self.jobs.history.root),
                'monitor-run', '--settings', json.dumps(asdict(self.settings))]
        with subprocess.Popen(argv, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as process:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and self.monitor.status()['runtime'].get('state') != 'running':
                time.sleep(0.05)
            self.assertEqual(self.monitor.status()['runtime']['pid'], process.pid)
            self.assertTrue(self.monitor.status()['runtime']['alive'])
            process.kill()
            process.wait(timeout=5)
        self.assertEqual(self.monitor.status()['runtime']['state'], 'interrupted')
        self.assertTrue(self.monitor.entry(run['run_id'])['enabled'])
        self.assertEqual(self.jobs.history.get(run['run_id'])['job_id'], '4242')
        with subprocess.Popen(argv, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as process:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and self.monitor.status()['runtime'].get('state') != 'running':
                time.sleep(0.05)
            self.assertTrue(self.monitor.status()['runtime']['alive'])
            process.terminate()
            self.assertEqual(process.wait(timeout=5), 0)
        self.assertEqual(self.monitor.status()['runtime']['state'], 'stopped')

    def test_detached_coordinator_survives_client_exit_restart_and_singleton(self):
        run = self.submit()
        ssh = self.fixture.bin / 'ssh'
        ssh.write_text('#!/usr/bin/env python3\nimport os,sys\n'
            "arguments=sys.argv[sys.argv.index('alias')+1:]\n"
            "os.execvp('bash',['bash','-c',' '.join(arguments)])\n")
        ssh.chmod(0o755)
        config = self.fixture.root / 'clusters.toml'
        from hpc_mcp.config import ConfigManager
        ConfigManager(config, {}).set('lsf', {'ssh_host': 'alias', 'scheduler': 'lsf', 'work_root': str(self.fixture.remote)})
        self.monitor.watch([run['run_id']])
        def stop_and_wait():
            self.monitor.stop()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and self.monitor.status()['runtime']['alive']:
                time.sleep(0.05)
        self.addCleanup(stop_and_wait)
        argv = [sys.executable, '-m', 'hpc_mcp', '--config', str(config), '--state-dir', str(self.jobs.history.root)]
        environment = {**os.environ, 'PATH': str(self.fixture.bin) + ':' + os.environ['PATH']}
        started = subprocess.run(argv + ['monitor-start', '--settings', json.dumps(asdict(self.settings))],
                                 env=environment, text=True, capture_output=True)
        self.assertEqual(started.returncode, 0, started.stderr)
        first = json.loads(started.stdout)['runtime']['instance_id']
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and self.monitor.entry(run['run_id'])['stage'] != 'complete':
            time.sleep(0.1)
        self.assertEqual(self.monitor.entry(run['run_id'])['stage'], 'complete', self.monitor.status())
        self.assertTrue(self.monitor.status()['runtime']['alive'])
        repeated = self.monitor.start()
        self.assertTrue(repeated['already_running'])
        self.assertEqual(repeated['runtime']['instance_id'], first)
        with self.assertRaises(ValueError):
            self.monitor.start({'poll_interval_seconds': 10})
        self.monitor.stop()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.monitor.status()['runtime']['alive']:
            time.sleep(0.05)
        self.assertFalse(self.monitor.status()['runtime']['alive'])
        with patch.dict(os.environ, environment):
            restarted = self.monitor.start()
        self.assertNotEqual(restarted['runtime']['instance_id'], first)
        self.assertEqual(self.monitor.entry(run['run_id'])['stage'], 'complete')
        self.monitor.stop()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.monitor.status()['runtime']['alive']:
            time.sleep(0.05)
        self.assertFalse(self.monitor.status()['runtime']['alive'])


if __name__ == '__main__':
    unittest.main()
