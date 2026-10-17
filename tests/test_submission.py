"""Authorization gates with real Bash/local rsync; no SSH or genuine user approval."""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch, MagicMock

import test_jobs
from hpc_mcp.jobs import JobService
from hpc_mcp.monitor import MonitorService, MonitorSettings
from hpc_mcp.monitor_engine import MonitorEngine
from hpc_mcp.operations import operation_get
from hpc_mcp.submission import SubmissionError
from hpc_mcp.workflow import WorkflowService


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_jobs.JobLifecycleTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.jobs = self.fixture.jobs
        self.run = self.fixture.prepare(); self.rid = self.run['run_id']

    def preview(self, **permissions):
        return self.jobs.job_submission_plan(self.rid, permissions)

    def confirm(self, preview=None):
        plan = preview or self.preview()
        return self.jobs.job_authorize(self.rid, plan['review_token'], 'Synthetic explicit user approval for exact plan')

    def submitted(self, **permissions):
        self.confirm(self.preview(**permissions)); self.jobs.job_submit(self.rid)
        return self.jobs.history.get(self.rid)

    def test_unconfirmed_plan_has_no_remote_effects(self):
        for preview in (False, True):
            if preview: self.preview()
            with self.assertRaises(SubmissionError) as error: self.jobs.job_submit(self.rid)
            self.assertEqual(error.exception.code, 'submission_authorization_required')
        self.assertEqual(self.fixture.transport.commands, [])
        self.assertFalse(Path(self.run['remote_dir']).exists())

    def test_plan_contains_reviewable_script_full_settings_and_output_scope(self):
        plan = self.preview()['plan']
        self.assertEqual(plan['script_text'], (Path(self.run['snapshot_dir'])/'job.sh').read_text())
        self.assertEqual(plan['manifest'], self.run['manifest'])
        self.assertIn('max_output_bytes', plan['cluster_settings'])
        self.assertEqual(plan['sync']['destination_template'], str(self.jobs.history.root/self.rid/'outputs'/'<attempt_id>'))
        self.assertFalse(plan['automation']['auto_submit']); self.assertFalse(plan['automation']['auto_sync'])
        self.assertFalse(plan['automation']['remote_cleanup'])
        self.assertEqual(plan['automation']['cleanup_policy']['storage_categories'], [])

    def test_wrong_cross_run_and_empty_confirmation_are_rejected(self):
        plan = self.preview(); second = self.fixture.prepare()
        other = self.jobs.job_submission_plan(second['run_id'])
        for token in ('wrong', other['review_token']):
            with self.assertRaises(SubmissionError): self.jobs.job_authorize(self.rid, token, 'explicit approval')
        with self.assertRaises(ValueError): self.jobs.job_authorize(self.rid, plan['review_token'], '')
        self.assertIsNone(self.jobs.history.get(self.rid).get('submission_authorization'))

    def test_authorization_persists_across_restart_without_auto_execution(self):
        self.confirm()
        self.assertEqual(self.fixture.transport.commands, [])
        restarted = JobService(self.jobs.clusters, self.fixture.state, self.fixture.transport, self.fixture.transfer)
        self.assertTrue(restarted.job_submit(self.rid)['ok'])
        self.assertTrue(restarted.job_submit(self.rid)['already_processed'])
        self.assertEqual((self.fixture.root/'submissions').read_text(), '1')

    def test_full_configuration_change_rejects_before_confirmation_and_upload(self):
        preview = self.preview()
        original = self.jobs.clusters['lsf']
        self.jobs.clusters['lsf'] = replace(original, transfer_compress=True)
        with self.assertRaises(SubmissionError): self.confirm(preview)
        self.jobs.clusters['lsf'] = original; self.confirm(preview)
        self.jobs.clusters['lsf'] = replace(original, command_timeout=31)
        with self.assertRaises(SubmissionError): self.jobs.job_submit(self.rid)
        self.assertEqual(self.fixture.transport.commands, [])
        refreshed = self.preview(); self.assertNotEqual(refreshed['review_token'], preview['review_token'])
        self.assertFalse(refreshed['authorized'])
        self.confirm(refreshed); self.assertTrue(self.jobs.job_submit(self.rid)['ok'])

    def test_tampered_snapshot_and_command_never_execute(self):
        self.confirm()
        script = Path(self.run['snapshot_dir'])/'job.sh'; content = script.read_text()
        script.write_text(content+'echo altered\n')
        with self.assertRaises(SubmissionError): self.jobs.job_submit(self.rid)
        script.write_text(content)
        self.jobs.history.update(self.rid, 'test tamper', command='touch /tmp/unauthorized-command')
        with self.assertRaises(SubmissionError): self.jobs.job_submit(self.rid)
        self.assertEqual(self.fixture.transport.commands, [])

    def test_task_paths_outputs_and_provenance_are_bound(self):
        self.confirm()
        for key, value in [('outputs', ['**']), ('input_dir', str(self.fixture.root/'other')),
                ('output_exclude', ['*.log']), ('template', {'changed':True}), ('generation', {'changed':True})]:
            with self.subTest(key=key):
                original = self.jobs.history.get(self.rid).get(key)
                self.jobs.history.update(self.rid, 'test tamper', **{key:value})
                with self.assertRaises(SubmissionError): self.jobs.job_submit(self.rid)
                self.jobs.history.update(self.rid, 'restore', **{key:original})
        self.assertEqual(self.fixture.transport.commands, [])

    def test_copied_authorization_from_other_run_does_not_work(self):
        self.confirm(); approved = self.jobs.history.get(self.rid)['submission_authorization']
        second = self.fixture.prepare(); self.jobs.job_submission_plan(second['run_id'])
        self.jobs.history.update(second['run_id'], 'test replay', submission_authorization=approved)
        with self.assertRaises(SubmissionError) as error: self.jobs.job_submit(second['run_id'])
        self.assertEqual(error.exception.code,'submission_authorization_invalid')
        self.assertEqual(self.fixture.transport.commands, [])

    def test_changed_configuration_during_upload_never_reaches_scheduler(self):
        self.confirm(); transfer = self.fixture.transfer.run
        def change(*args, **kwargs):
            result = transfer(*args, **kwargs)
            self.jobs.clusters['lsf'] = replace(self.jobs.clusters['lsf'], transfer_compress=True)
            return result
        with patch.object(self.fixture.transfer,'run',side_effect=change):
            with self.assertRaises(SubmissionError): self.jobs.job_submit(self.rid)
        self.assertFalse((self.fixture.root/'submissions').exists())
        self.assertNotEqual(self.jobs.history.get(self.rid)['phase'], 'submitting')

    def test_lost_submission_response_recovers_even_after_revocation(self):
        self.confirm(); self.fixture.transport.lose_response=True
        self.assertFalse(self.jobs.job_submit(self.rid)['ok'])
        self.jobs.job_revoke_authorization(self.rid)
        self.assertTrue(self.jobs.job_recover(self.rid)['ok'])
        self.assertTrue(self.jobs.job_submit(self.rid)['already_processed'])
        self.assertEqual((self.fixture.root/'submissions').read_text(),'1')

    def test_sync_scope_denies_broader_selection_destination_and_overwrite(self):
        self.submitted(); count=len(self.fixture.transport.commands)
        for options in ({'includes':['**']},{'destination':str(self.fixture.root/'other')},
                        {'overwrite':'replace'},{'max_total_bytes':self.jobs.clusters['lsf'].max_output_bytes+1}):
            with self.subTest(options=options), self.assertRaises(SubmissionError): self.jobs.job_sync(self.rid, **options)
        self.assertEqual(len(self.fixture.transport.commands), count)
        result=self.jobs.job_sync(self.rid, includes=['results/value.txt'],stable_only=True)
        self.assertTrue(result['ok']); self.assertEqual([e['path'] for e in result['run']['output_manifest']], ['results/value.txt'])

    def test_explicit_new_output_plan_revokes_old_scope(self):
        self.submitted(); old=self.jobs.history.get(self.rid)['submission_plan_token']
        preview=self.preview(sync_options={'includes':['results/value.txt'],'destination':str(self.fixture.root/'chosen')})
        self.assertNotEqual(old,preview['review_token'])
        with self.assertRaises(SubmissionError): self.jobs.job_sync(self.rid)
        self.confirm(preview)
        self.assertTrue(self.jobs.job_sync(self.rid, includes=['results/value.txt'],destination=str(self.fixture.root/'chosen'))['ok'])

    def test_queued_sync_token_is_frozen_and_stale_worker_is_rejected(self):
        self.submitted()
        process=MagicMock();process.pid=99999999;process.wait.return_value=0
        with patch('hpc_mcp.operations.subprocess.Popen',return_value=process):
            operation=self.jobs.job_sync_start(self.rid)['operation']
        self.assertEqual(operation['authorization_token'],self.jobs.history.get(self.rid)['submission_plan_token'])
        self.confirm(self.preview(sync_options={'includes':['results/value.txt']}))
        count=len(self.fixture.transport.commands)
        from hpc_mcp.worker import main
        with patch('sys.argv',['worker',str(self.fixture.state),operation['operation_id']]), \
                patch('hpc_mcp.worker.JobService',return_value=self.jobs): main()
        stored=operation_get(self.jobs.history,operation['operation_id'])
        self.assertEqual(stored['state'],'failed');self.assertIn('stale',stored['error'])
        self.assertEqual(len(self.fixture.transport.commands),count)

    def test_cleanup_preview_is_read_only_but_apply_requires_scope(self):
        self.submitted();self.jobs.job_sync(self.rid)
        test_jobs.release_retention(self.jobs, self.rid)
        self.assertTrue(self.jobs.job_remote_cleanup(self.rid)['dry_run'])
        for operation in (lambda:self.jobs.job_remote_cleanup(self.rid,False),
                          lambda:self.jobs.job_storage_cleanup(self.rid,['snapshot'],0,False),
                          lambda:self.jobs.job_cache_cleanup(self.rid,0,False)):
            with self.assertRaises(SubmissionError) as error:operation()
            self.assertEqual(error.exception.code,'cleanup_authorization_required')
        self.assertTrue(Path(self.run['snapshot_dir']).exists());self.assertTrue(Path(self.run['remote_dir']).exists())
        self.confirm(self.preview(remote_cleanup=True,cleanup_policy={'storage_categories':['snapshot'],'storage_age_seconds':0}))
        self.assertTrue(self.jobs.job_remote_cleanup(self.rid,False)['ok'])
        self.jobs.job_storage_cleanup(self.rid,['snapshot'],0,False)
        self.assertFalse(Path(self.run['snapshot_dir']).exists())

    def test_monitor_needs_scope_and_revocation_blocks_automatic_actions(self):
        self.submitted();monitor=MonitorService(self.jobs)
        with self.assertRaises(SubmissionError):monitor.watch([self.rid])
        self.confirm(self.preview(auto_sync=True))
        monitor.watch([self.rid]);self.jobs.job_revoke_authorization(self.rid)
        engine=MonitorEngine(monitor,MonitorSettings())
        with patch.object(self.jobs,'job_sync_start') as sync: engine.tick()
        sync.assert_not_called();self.assertEqual(monitor.entry(self.rid)['stage'],'needs_attention')
        self.assertIn('job_authorize',monitor.entry(self.rid)['last_error'])

    def test_read_only_monitoring_and_history_survive_legacy_authorization_absence(self):
        self.submitted();self.jobs.history.update(self.rid,'legacy',submission_plan=None,submission_authorization=None)
        self.assertTrue(self.jobs.job_status(self.rid)['ok'])
        monitor=MonitorService(self.jobs);monitor.watch([self.rid],auto_sync=False)
        self.assertTrue(MonitorEngine(monitor,MonitorSettings()).tick())
        with self.assertRaises(SubmissionError):self.jobs.job_sync(self.rid)
        self.confirm();self.assertTrue(self.jobs.job_sync(self.rid)['ok'])

    def test_workflow_owned_run_cannot_be_individually_authorized_or_submitted(self):
        flow=WorkflowService(self.jobs);value=flow.plan('one',[self.rid])['workflow']
        preview=self.preview()
        with self.assertRaises(SubmissionError):self.confirm(preview)
        flow.start(value['workflow_id'],value['review_token'],'Synthetic user approval for this workflow')
        with self.assertRaises(ValueError):self.jobs.job_submit(self.rid)
        result=flow.tick(value['workflow_id']);self.assertEqual(result['submitted'],1)

    def test_workflow_detects_changed_scope_before_start_and_again_at_dispatch(self):
        flow=WorkflowService(self.jobs);value=flow.plan('one',[self.rid])['workflow']
        self.jobs.clusters['lsf']=replace(self.jobs.clusters['lsf'],sync_overwrite='merge')
        with self.assertRaises(ValueError):flow.start(value['workflow_id'],value['review_token'],'user confirms old plan')
        reviewed=flow.review(value['workflow_id'])['workflow']
        flow.start(value['workflow_id'],reviewed['review_token'],'user confirms new exact plan')
        self.jobs.history.update(self.rid,'test policy change',outputs=['**'])
        result=flow.tick(value['workflow_id'])
        self.assertEqual(result['workflow']['tasks'][self.rid]['stage'],'needs_attention')
        self.assertEqual(self.fixture.transport.commands,[])

    def test_cli_plan_authorize_and_revoke_do_not_submit(self):
        config=self.fixture.root/'clusters.toml'
        config.write_text(f'[clusters.lsf]\nssh_host="alias"\nscheduler="lsf"\nwork_root={json.dumps(str(self.fixture.remote))}\n')
        command=[sys.executable,'-m','hpc_mcp','--state-dir',str(self.fixture.state),'--config',str(config)]
        def call(*args):
            result=subprocess.run(command+list(args),text=True,capture_output=True,check=True)
            return json.loads(result.stdout)
        plan=call('submission-plan',self.rid)
        self.assertTrue(call('authorize',self.rid,plan['review_token'],'--confirmation-note','Synthetic CLI user approval')['ok'])
        self.assertTrue(call('revoke-authorization',self.rid)['ok'])
        self.assertFalse((self.fixture.root/'submissions').exists())

    def test_live_configuration_file_changes_invalidate_cached_service_permissions(self):
        from hpc_mcp.config import ConfigManager
        config = self.fixture.root/'live.toml'
        manager = ConfigManager(config, self.jobs.clusters)
        manager.set('lsf', {'ssh_host':'alias','scheduler':'lsf','work_root':str(self.fixture.remote)})
        self.jobs.config_path = config
        self.confirm()
        captured = JobService(dict(self.jobs.clusters),self.fixture.state,self.fixture.transport,self.fixture.transfer,config_path=config)
        manager.set('lsf', {'transfer_compress':True})
        with self.assertRaises(SubmissionError): captured.job_submit(self.rid)
        self.assertEqual(self.fixture.transport.commands,[])
        config.unlink()
        with self.assertRaises(FileNotFoundError): captured.job_submit(self.rid)
        self.assertFalse((self.fixture.root/'submissions').exists())

    def test_worker_uses_live_config_path_instead_of_stale_cluster_copy(self):
        from hpc_mcp.config import ConfigManager
        config = self.fixture.root/'live.toml'
        manager = ConfigManager(config,self.jobs.clusters)
        manager.set('lsf', {'ssh_host':'alias','scheduler':'lsf','work_root':str(self.fixture.remote)})
        self.jobs.config_path=config
        self.submitted()
        process=MagicMock();process.pid=99999999;process.wait.return_value=0
        with patch('hpc_mcp.operations.subprocess.Popen',return_value=process):
            operation=self.jobs.job_sync_start(self.rid)['operation']
        self.assertEqual(operation['config_path'],str(config))
        manager.set('lsf',{'transfer_compress':True})
        from hpc_mcp.config import Cluster
        worker=JobService({'lsf':Cluster(**operation['cluster'])},self.fixture.state,self.fixture.transport,self.fixture.transfer,config_path=config)
        count=len(self.fixture.transport.commands)
        with self.assertRaises(SubmissionError):worker.job_sync(self.rid,_authorization_token=operation['authorization_token'])
        self.assertEqual(len(self.fixture.transport.commands),count)

    def test_unconfirmed_or_legacy_enabled_workflow_cannot_sync_or_submit(self):
        flow=WorkflowService(self.jobs);value=flow.plan('one',[self.rid])['workflow']
        flow._control(value['workflow_id'],enabled=True)
        with patch.object(self.jobs,'job_sync_start') as sync, patch.object(self.jobs,'job_submit') as submit:
            result=flow.tick(value['workflow_id'])
        self.assertFalse(result['workflow']['enabled'])
        self.assertEqual(result['error'],'workflow_authorization_required')
        sync.assert_not_called();submit.assert_not_called()
        self.assertEqual(self.fixture.transport.commands,[])
