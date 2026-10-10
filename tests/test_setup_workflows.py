"""Managed business-step gates, frozen evidence, revision loops and crash recovery."""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_jobs
from hpc_mcp.applications import ApplicationService, ApplicationError
from hpc_mcp.config import ConfigManager
from hpc_mcp.service import ClusterService
from hpc_mcp.setup_flows import SetupFlowError
from hpc_mcp.setup_workflows import SetupWorkflowService

ROOT=Path(__file__).resolve().parents[1]


class SetupWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.fixture=test_jobs.JobLifecycleTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.jobs=self.fixture.jobs;self.service=ClusterService(self.jobs.clusters,self.fixture.transport)
        self.config=ConfigManager(self.fixture.root/'clusters.toml',self.jobs.clusters)
        for name,target in list(self.jobs.clusters.items()):
            settings=asdict(target);settings.pop('name');self.config.set(name,settings)
        (self.fixture.bin/'bqueues').write_text('#!/bin/bash\ncat <<\'EOF\'\n'+(Path(__file__).parent/'fixtures/bqueues.txt').read_text()+'\nEOF\n')
        (self.fixture.bin/'bqueues').chmod(0o755)
        for name in ('bhist','bacct'):
            command=self.fixture.bin/name;command.write_text('#!/bin/bash\ntrue\n');command.chmod(0o755)
        self.flows=SetupWorkflowService(self.jobs,self.service,self.config);self.apps=ApplicationService(self.jobs)

    def create(self,kind='application_adaptation'):
        return self.flows.create(kind,kind)['flow']

    def step(self,record,action,payload):
        return self.flows.step(record['flow_id'],record['revision'],action,payload)['flow']

    def sources(self,record):
        source=ROOT/'examples/applications/hello_lsf/original/job.sh'
        return self.step(record,'application_source',{'application':'hello_lsf','source_paths':[str(source)]})

    def install(self,record):
        bundle=Path(record['workspace'])/'bundle'
        if bundle.exists():shutil.rmtree(bundle)
        shutil.copytree(ROOT/'examples/applications/hello_lsf',bundle)
        manifest=json.loads((bundle/'manifest.json').read_text());manifest['clusters']=['lsf'];(bundle/'manifest.json').write_text(json.dumps(manifest))
        return self.step(record,'application_install',{'bundle_dir':str(bundle)})

    def review(self,record,verdict='pass'):
        record=self.step(record,'application_review_request',{'author_session':'author'})
        packet=record['business']['review_request']
        report={'reviewer_session':'fresh-reviewer-'+str(record['business']['version']),'fresh_context':True,'verdict':verdict,
            'checks':{k:'Synthetic evidence for workflow testing.' for k in packet['required_checks']},
            'differences':[] if verdict=='pass' else ['Restore original environment branch.'],'unresolved':[],'allowed_changes':[]}
        return self.step(record,'application_review_submit',{'report':report})

    def ready_cluster(self,settings=None):
        record=self.create('cluster_setup')
        values={'ssh_host':'alias','scheduler':'lsf','work_root':str(self.fixture.remote)}
        if settings:values.update(settings)
        record=self.step(record,'cluster_inputs',{'cluster':'new','settings':values,'probe_options':{'module_avail':False}})
        return self.step(record,'cluster_probe',{})

    def confirm(self,record):
        return self.flows.confirm(record['flow_id'],record['revision'],record['business']['confirmation_token'],'User confirmed exact plan.')['flow']

    def assert_code(self,code,call,error_type=SetupFlowError):
        with self.assertRaises(error_type) as error:call()
        self.assertEqual(error.exception.code,code)

    def test_cluster_probe_preview_confirmation_and_closed_cleanup(self):
        record=self.ready_cluster()
        self.assertEqual(record['business']['stage'],'awaiting_confirmation');self.assertNotIn('new',self.jobs.clusters)
        self.assert_code('setup_flow_confirmation_changed',lambda:self.flows.confirm(record['flow_id'],record['revision'],'wrong','confirmed'))
        self.assert_code('setup_flow_invalid_step',lambda:self.flows.close(record['flow_id'],record['revision'],'completed',[]))
        completed=self.confirm(record);self.assertEqual(completed['state'],'completed');self.assertIn('new',self.jobs.clusters)
        self.assertEqual(self.confirm(record),completed)
        self.flows.cleanup(completed['flow_id'],completed['revision'],False)
        self.assertIn('new',self.jobs.clusters)
        self.assertFalse(any('find ' in command or 'module load' in command for command in self.fixture.transport.commands))

    def test_cluster_missing_inputs_or_permissions_cannot_confirm(self):
        record=self.create('cluster_setup')
        record=self.step(record,'cluster_inputs',{'cluster':'new','settings':{'ssh_host':'alias'},'probe_options':{'module_avail':False}})
        record=self.step(record,'cluster_probe',{})
        self.assertEqual(record['business']['stage'],'awaiting_inputs');self.assertIn('work_root',record['business']['needs'])
        self.assertNotIn('confirmation_token',record['business']);self.assertNotIn('new',self.jobs.clusters)
        record=self.step(record,'cluster_inputs',{'cluster':'new','settings':{'ssh_host':'alias','scheduler':'lsf','work_root':'/missing-dir'},'probe_options':{'module_avail':False}})
        record=self.step(record,'cluster_probe',{});self.assertIn('work_root_permissions',record['business']['needs'])

    def test_changed_config_and_expired_probe_invalidate_confirmation(self):
        record=self.ready_cluster()
        self.config.set('new',{'ssh_host':'changed','scheduler':'lsf','work_root':str(self.fixture.remote)})
        self.assert_code('setup_flow_configuration_changed',lambda:self.confirm(record))
        record=self.step(record,'cluster_inputs',{'cluster':'new','settings':{'ssh_host':'alias','scheduler':'lsf','work_root':str(self.fixture.remote)},'probe_options':{'module_avail':False}})
        record=self.step(record,'cluster_probe',{})
        with patch('hpc_mcp.setup_workflows.datetime') as clock:
            clock.now.return_value=datetime.now(timezone.utc)+timedelta(hours=2);clock.fromisoformat.side_effect=datetime.fromisoformat
            self.assert_code('setup_flow_probe_expired',lambda:self.confirm(record))
        self.assertEqual(self.confirm(record)['state'],'completed')

    def test_application_full_flow_blocks_standalone_bypass(self):
        record=self.sources(self.create());bundle=Path(record['workspace'])/'bundle';shutil.copytree(ROOT/'examples/applications/hello_lsf',bundle)
        self.assert_code('application_setup_flow_required',lambda:self.apps.install(str(bundle)),ApplicationError)
        record=self.install(record)
        version=record['business']['version'];token=record['business']['review_token']
        self.assert_code('application_setup_flow_required',lambda:self.apps.review_request('hello_lsf',version,'author'),ApplicationError)
        self.assert_code('setup_flow_invalid_step',lambda:self.flows.confirm(record['flow_id'],record['revision'],'missing','confirmed'))
        self.assert_code('application_in_use',lambda:self.apps.remove('hello_lsf',False),ApplicationError)
        record=self.review(record)
        self.assert_code('application_setup_flow_required',lambda:self.apps.activate('hello_lsf',version,token,'user confirmed'),ApplicationError)
        completed=self.confirm(record);self.assertEqual(completed['state'],'completed')
        self.assertEqual(self.apps.get('hello_lsf')['application']['status'],'active')
        self.flows.cleanup(completed['flow_id'],completed['revision'],False)
        self.assertTrue(self.apps.get('hello_lsf')['ok'])

    def test_review_revise_requires_new_version_and_frozen_original(self):
        record=self.review(self.install(self.sources(self.create())),'revise')
        first=record['business']['version'];self.assertEqual(record['business']['stage'],'awaiting_revision')
        self.assertIn('Restore original',record['business']['revision_feedback']['differences'][0])
        self.assert_code('setup_flow_invalid_step',lambda:self.step(record,'application_review_request',{'author_session':'author'}))
        bundle=Path(record['workspace'])/'bundle';(bundle/'original/job.sh').write_text('changed original')
        with self.assertRaises(ValueError):self.step(record,'application_install',{'bundle_dir':str(bundle)})
        record=self.install(record);self.assertGreater(record['business']['version'],first)
        self.assertNotIn('confirmation_token',record['business'])
        record=self.review(record);completed=self.confirm(record)
        self.assertEqual(completed['business']['version'],first+1)
        self.assertEqual(self.apps.get('hello_lsf',first)['application']['status'],'review_rejected')

    def test_application_changed_cluster_requires_reinstall_and_new_review(self):
        record=self.review(self.install(self.sources(self.create())))
        self.config.set('lsf',{'connect_timeout':11})
        self.assert_code('setup_flow_configuration_changed',lambda:self.confirm(record))
        self.assertFalse(self.flows.get(record['flow_id'])['flow']['confirmation_status']['valid'])
        record=self.install(record);record=self.review(record)
        self.assertEqual(self.confirm(record)['state'],'completed')

    def test_invalid_report_can_reset_failed_step_without_faking_review(self):
        record=self.install(self.sources(self.create()));record=self.step(record,'application_review_request',{'author_session':'author'})
        with self.assertRaises(ValueError):self.step(record,'application_review_submit',{'report':{}})
        failed=self.flows.get(record['flow_id'])['flow'];self.assertIn('operation',failed['business']);self.assertTrue(failed['last_error'])
        reset=self.step(failed,'reset_failed_step',{});self.assertEqual(reset['business']['stage'],'awaiting_review_report')
        packet=reset['business']['review_request'];report={'reviewer_session':'reviewer','fresh_context':True,'verdict':'pass','checks':{k:'Evidence.' for k in packet['required_checks']},'differences':[],'unresolved':[],'allowed_changes':[]}
        reviewed=self.step(reset,'application_review_submit',{'report':report});self.assertEqual(self.confirm(reviewed)['state'],'completed')

    def test_interrupted_install_and_review_recover_without_duplicate_version(self):
        record=self.sources(self.create());original=self.flows._save
        def fail_save(value,kind,**changes):
            if kind=='step_completed':raise OSError('interrupted after side effect')
            return original(value,kind,**changes)
        with patch.object(self.flows,'_save',fail_save):
            with self.assertRaises(OSError):self.install(record)
        interrupted=self.flows.get(record['flow_id'])['flow']
        self.assert_code('setup_flow_invalid_step',lambda:self.step(interrupted,'reset_failed_step',{}))
        resumed=self.flows.resume(record['flow_id'])['flow'];self.assertEqual(resumed['business']['version'],1)
        self.assertEqual(self.apps.list()['applications'][0]['versions'],[1])
        requested=self.step(resumed,'application_review_request',{'author_session':'author'})
        packet=requested['business']['review_request'];report={'reviewer_session':'reviewer','fresh_context':True,'verdict':'pass','checks':{k:'Evidence.' for k in packet['required_checks']},'differences':[],'unresolved':[],'allowed_changes':[]}
        with patch.object(self.flows,'_save',fail_save):
            with self.assertRaises(OSError):self.step(requested,'application_review_submit',{'report':report})
        resumed=self.flows.resume(record['flow_id'])['flow'];self.assertEqual(resumed['business']['stage'],'awaiting_confirmation')
        self.assertEqual(len(self.apps.get('hello_lsf',1)['application']['reviews']),1)

    def test_confirmation_crash_recovers_after_configuration_write(self):
        record=self.ready_cluster();original=self.flows._save
        def fail_save(value,kind,**changes):
            if kind=='business_completed':raise OSError('crash after config write')
            return original(value,kind,**changes)
        with patch.object(self.flows,'_save',fail_save):
            with self.assertRaises(OSError):self.confirm(record)
        self.assertIn('new',self.jobs.clusters)
        resumed=SetupWorkflowService(self.jobs,self.service,self.config).resume(record['flow_id'])['flow']
        self.assertEqual(resumed['state'],'completed')

    def test_slurm_cluster_flow_uses_partition_query(self):
        command=self.fixture.bin/'sinfo'
        command.write_text('#!/bin/bash\ncat <<\'EOF\'\n'+(Path(__file__).parent/'fixtures/sinfo.txt').read_text()+'\nEOF\n');command.chmod(0o755)
        record=self.ready_cluster({'scheduler':'slurm'})
        self.assertEqual(record['business']['stage'],'awaiting_confirmation')
        self.assertEqual(self.confirm(record)['state'],'completed')
        self.assertTrue(any('sinfo ' in c for c in self.fixture.transport.commands))

    def test_activation_race_rejects_unreviewed_cluster_snapshot(self):
        record=self.review(self.install(self.sources(self.create())));original=self.flows.apps.activate
        def changed(*args,**kwargs):
            self.config.set('lsf',{'connect_timeout':15})
            return original(*args,**kwargs)
        with patch.object(self.flows.apps,'activate',changed):
            self.assert_code('application_cluster_changed',lambda:self.confirm(record),ApplicationError)
        self.assertEqual(self.apps.get('hello_lsf',1)['application']['status'],'review_passed')
        failed=self.flows.get(record['flow_id'])['flow']
        reset=self.step(failed,'reset_failed_step',{})
        self.assertFalse(self.flows.get(reset['flow_id'])['flow']['confirmation_status']['valid'])

    def test_confirmation_resume_cannot_apply_expired_unwritten_plan(self):
        record=self.ready_cluster();original=self.flows.config.set
        with patch.object(self.flows.config,'set',side_effect=OSError('crash before write')):
            with self.assertRaises(OSError):self.confirm(record)
        with patch('hpc_mcp.setup_workflows.datetime') as clock:
            clock.now.return_value=datetime.now(timezone.utc)+timedelta(hours=2);clock.fromisoformat.side_effect=datetime.fromisoformat
            self.assert_code('setup_flow_probe_expired',lambda:self.flows.resume(record['flow_id']))
        self.assertNotIn('new',self.jobs.clusters)

    def test_cli_step_contract_and_business_close_gate(self):
        command=[sys.executable,'-m','hpc_mcp','--state-dir',str(self.jobs.history.root),'--config',str(self.config.path)]
        def run(args):
            result=subprocess.run(command+args,text=True,capture_output=True)
            return result,json.loads(result.stdout)
        result,created=run(['setup-flow-create','application_adaptation','--request-key','cli-app'])
        self.assertEqual(result.returncode,0);record=created['flow']
        payload=Path(record['workspace'])/'source-request.json'
        payload.write_text(json.dumps({'application':'hello_lsf','source_paths':[str(ROOT/'examples/applications/hello_lsf/original/job.sh')]}))
        result,updated=run(['setup-flow-step',record['flow_id'],'--revision',str(record['revision']),'--action','application_source','--payload-file',str(payload)])
        self.assertEqual(result.returncode,0);record=updated['flow'];self.assertEqual(record['business']['stage'],'awaiting_adaptation')
        result,error=run(['setup-flow-close',record['flow_id'],'--revision',str(record['revision']),'--outcome','completed'])
        self.assertEqual(result.returncode,1);self.assertEqual(error['error'],'setup_flow_invalid_step')

    def test_atomic_config_guard_rejects_concurrent_baseline_change(self):
        with self.assertRaises(ValueError):self.config.set('lsf',{'command_timeout':40},expected_current_digest='wrong')
        self.assertNotEqual(self.config.get('lsf')['command_timeout'],40)

    def test_invalid_kind_action_payload_and_abandon_protect_results(self):
        record=self.create()
        self.assert_code('setup_flow_invalid_step',lambda:self.step(record,'application_install',{'bundle_dir':record['workspace']}))
        with self.assertRaises(ValueError):self.step(record,'cluster_inputs',{'cluster':'bad','settings':{}})
        with self.assertRaises(ValueError):self.step(record,'invented',{})
        record=self.sources(record);record=self.install(record)
        abandoned=self.flows.close(record['flow_id'],record['revision'],'abandoned')['flow']
        self.assertEqual(abandoned['state'],'abandoned');self.assertTrue(self.apps.get('hello_lsf',1)['ok'])
