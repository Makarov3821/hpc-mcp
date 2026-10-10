"""Offline ownership, durable transitions and recovery for setup-flow workspaces."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from hpc_mcp.jobs import JobService
from hpc_mcp.onboarding_store import OnboardingStore
from hpc_mcp.setup_flows import SetupFlowService, SetupFlowError

ROOT = Path(__file__).resolve().parents[1]


class SetupFlowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.jobs = JobService({}, self.root/'state')
        self.flows = SetupFlowService(self.jobs)

    def create(self, key='first'):
        return self.flows.create('application_adaptation', key, 'gaussian')['flow']

    def abandon(self, record):
        return self.flows.close(record['flow_id'], record['revision'], 'abandoned')['flow']

    def assert_code(self, code, call):
        with self.assertRaises(SetupFlowError) as error: call()
        self.assertEqual(error.exception.code, code)

    def test_idempotency_restart_events_and_pagination(self):
        record=self.create()
        workspace=Path(record['workspace']); self.assertTrue(workspace.is_dir())
        self.assertTrue(workspace.is_relative_to(self.root/'state/onboarding'))
        self.assertEqual(self.create(),record)
        restarted=SetupFlowService(JobService({},self.root/'state'))
        self.assertEqual(restarted.get(record['flow_id'])['flow'],record)
        self.assertEqual(restarted.resume(record['flow_id'])['flow'],record)
        events=restarted.events(record['flow_id'])['events']
        self.assertEqual([e['kind'] for e in events],['flow_created','workspace_allocated'])
        self.assertEqual(restarted.events(record['flow_id'],after_id=events[0]['id'])['events'],events[1:])
        self.create('second')
        self.assertEqual(len(self.flows.list(1)['flows']),1)
        self.assertEqual(len(self.flows.list(1,1)['flows']),1)
        self.assert_code('setup_flow_conflict', lambda:self.flows.create('cluster_setup','first','gaussian'))

    def test_active_flow_and_stale_revision_are_protected(self):
        record=self.create()
        self.assert_code('setup_flow_in_use',lambda:self.flows.cleanup(record['flow_id'],record['revision'],False))
        self.assert_code('setup_flow_conflict',lambda:self.flows.close(record['flow_id'],0,'abandoned'))
        self.assertTrue(Path(record['workspace']).exists())
        with self.assertRaises(ValueError): self.flows.close(record['flow_id'],record['revision'],'completed',[])
        with self.assertRaises(ValueError): self.flows.close(record['flow_id'],record['revision'],'completed',[{'kind':'file','path':'/tmp/a'}])
        self.assertEqual(self.flows.get(record['flow_id'])['flow']['state'],'open')

    def test_complete_requires_persisted_evidence_and_preserves_it(self):
        record=self.create(); store=OnboardingStore(self.jobs.history)
        report=store.save('probe',{'hostname':'example'})
        with self.assertRaises(ValueError): self.flows.close(record['flow_id'],record['revision'],'completed',[{'kind':'onboarding_report','report_id':'missing'}])
        closed=self.flows.close(record['flow_id'],record['revision'],'completed',[{'kind':'onboarding_report','report_id':report['report_id']}])['flow']
        self.assertIn('sha256',closed['artifacts'][0])
        self.flows.cleanup(closed['flow_id'],closed['revision'],False)
        self.assertEqual(store.get(report['report_id']),report)
        self.assertEqual(self.flows.get(closed['flow_id'])['flow']['artifacts'],closed['artifacts'])

    def test_installed_bundle_reference_survives_workspace_cleanup(self):
        from hpc_mcp.applications import ApplicationService
        record=self.create();workspace=Path(record['workspace'])
        shutil.copytree(ROOT/'examples/applications/hello_lsf', workspace/'bundle')
        apps=ApplicationService(self.jobs);installed=apps.install(str(workspace/'bundle'))['application']
        closed=self.flows.close(record['flow_id'],record['revision'],'completed',[
            {'kind':'application_version','application':'hello_lsf','version':installed['version']}])['flow']
        self.assertEqual(closed['artifacts'][0]['review_token'],installed['review_token'])
        self.flows.cleanup(closed['flow_id'],closed['revision'],False)
        self.assertEqual(apps.get('hello_lsf',1)['application']['status'],'draft')

    def test_preview_cleanup_scope_repetition_and_abandon(self):
        record=self.create();workspace=Path(record['workspace']);(workspace/'review.json').write_text('{}')
        project=self.root/'project';project.mkdir();(project/'water.gjf').write_text('input')
        other=self.create('other');closed=self.abandon(record)
        preview=self.flows.cleanup(closed['flow_id'],closed['revision'])
        self.assertEqual(preview['bytes'],2);self.assertTrue(workspace.exists())
        self.assertEqual(preview['paths'],[str(workspace)])
        result=self.flows.cleanup(closed['flow_id'],closed['revision'],False)
        self.assertEqual(result['flow']['state'],'cleaned');self.assertFalse(workspace.exists())
        self.assertTrue(Path(other['workspace']).exists());self.assertTrue((project/'water.gjf').exists())
        self.assertEqual(self.flows.cleanup(closed['flow_id'],closed['revision'],False)['status'],'already_cleaned')
        self.assertEqual(self.create()['state'],'cleaned')
        self.assertEqual(self.flows.close(closed['flow_id'],0,'abandoned')['flow']['state'],'cleaned')
        self.assert_code('setup_flow_conflict',lambda:self.flows.close(closed['flow_id'],0,'completed'))

    def test_symlinks_owner_tampering_and_database_path_tampering_fail_closed(self):
        record=self.create();workspace=Path(record['workspace']);outside=self.root/'outside';outside.mkdir();(outside/'keep').write_text('safe')
        closed=self.abandon(record)
        (workspace/'escape').symlink_to(outside,target_is_directory=True)
        with self.assertRaises(ValueError): self.flows.cleanup(closed['flow_id'],closed['revision'],False)
        self.assertTrue((outside/'keep').exists());(workspace/'escape').unlink()
        marker=workspace.parent/'owner.json';saved=marker.read_text();marker.write_text('{}')
        self.assert_code('setup_flow_path_unsafe',lambda:self.flows.cleanup(closed['flow_id'],closed['revision'],False))
        marker.write_text(saved);workspace.rmdir();workspace.symlink_to(outside,target_is_directory=True)
        self.assert_code('setup_flow_path_unsafe',lambda:self.flows.cleanup(closed['flow_id'],closed['revision'],False))
        with self.jobs.history.connect() as db:
            data=dict(closed,workspace=str(outside));db.execute('UPDATE setup_flows SET data=? WHERE id=?',(json.dumps(data),closed['flow_id']))
        self.assert_code('setup_flow_path_unsafe',lambda:self.flows.get(closed['flow_id']))

    def test_root_symlink_rejected(self):
        self.flows.root.rmdir();self.flows.root.symlink_to(self.root,target_is_directory=True)
        self.assert_code('setup_flow_path_unsafe',lambda:SetupFlowService(self.jobs))

    def test_interrupted_allocation_recovers_with_same_id(self):
        original=Path.mkdir
        def interrupted(path,*args,**kwargs):
            if path.name=='workspace': raise OSError('allocation interrupted')
            return original(path,*args,**kwargs)
        with patch('hpc_mcp.setup_flows.Path.mkdir',interrupted):
            with self.assertRaises(OSError):self.create()
        stored=self.flows.list()['flows'][0];self.assertEqual(stored['state'],'allocating')
        self.assertIn('interrupted',stored['last_error'])
        resumed=self.create();self.assertEqual(resumed['flow_id'],stored['flow_id']);self.assertEqual(resumed['state'],'open')
        self.assertIsNone(resumed['last_error'])

    def test_interrupted_close_and_partial_cleanup_resume(self):
        record=self.create();original=self.flows._save
        def interrupted(value,kind,**changes):
            if kind=='flow_closed':raise OSError('close interrupted')
            return original(value,kind,**changes)
        with patch.object(self.flows,'_save',interrupted):
            with self.assertRaises(OSError):self.abandon(record)
        closing=self.flows.get(record['flow_id'])['flow'];self.assertEqual(closing['state'],'closing')
        closed=self.flows.resume(record['flow_id'])['flow'];self.assertEqual(closed['state'],'abandoned')
        workspace=Path(closed['workspace']);(workspace/'partial').write_text('temp')
        def partial(path):
            (path/'partial').unlink();raise OSError('delete interrupted')
        with patch('hpc_mcp.setup_flows.shutil.rmtree',partial):
            with self.assertRaises(OSError):self.flows.cleanup(closed['flow_id'],closed['revision'],False)
        self.assertEqual(self.flows.get(closed['flow_id'])['flow']['state'],'cleaning')
        self.assertEqual(self.flows.resume(closed['flow_id'])['flow']['state'],'cleaned')
        self.assertFalse(workspace.exists())

    def test_concurrent_lock_and_invalid_arguments(self):
        with self.flows.locked():
            self.assert_code('setup_flow_busy',lambda:self.create())
        for kind,key,label in [('wrong','id',''),('cluster_setup','',''),('cluster_setup','id',None)]:
            with self.assertRaises(ValueError):self.flows.create(kind,key,label)
        with self.assertRaises(ValueError):self.flows.get('../escape')
        with self.assertRaises(ValueError):self.flows.list(True)
        self.assert_code('setup_flow_unknown',lambda:self.flows.get('f_'+'0'*32))

    def test_cli_without_config(self):
        command=[sys.executable,'-m','hpc_mcp','--state-dir',str(self.root/'cli-state'),'--config',str(self.root/'missing.toml')]
        def run(args):
            result=subprocess.run(command+args,text=True,capture_output=True)
            return result,json.loads(result.stdout)
        result,data=run(['setup-flow-create','application_adaptation','--request-key','cli'])
        self.assertEqual(result.returncode,0);record=data['flow']
        result,data=run(['setup-flow-cleanup',record['flow_id'],'--revision',str(record['revision']),'--apply'])
        self.assertEqual(result.returncode,1);self.assertEqual(data['error'],'setup_flow_in_use')
        result,data=run(['setup-flow-close',record['flow_id'],'--revision',str(record['revision']),'--outcome','abandoned'])
        self.assertEqual(result.returncode,0);closed=data['flow']
        result,data=run(['setup-flow-cleanup',closed['flow_id'],'--revision',str(closed['revision']),'--apply'])
        self.assertEqual(result.returncode,0);self.assertEqual(data['flow']['state'],'cleaned')
