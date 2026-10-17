"""Real official SDK stdio lifecycle and dynamic handler discovery, without SSH."""
import asyncio
import json
import os
import shutil
from pathlib import Path
import sys
import tempfile
import unittest
try:
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters
except ImportError:
    Client = None

ROOT = Path(__file__).resolve().parents[1]

@unittest.skipIf(Client is None, 'install mcp extra for real stdio integration')
class StdioIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_batch_tools_annotations_and_durable_start_in_both_modes(self):
        for mode in ('auto', 'legacy'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                project = root / 'project'
                tasks = []
                for name in ('B', 'D'):
                    task_dir = project / name
                    task_dir.mkdir(parents=True)
                    (task_dir / 'job.sh').write_text("#!/bin/bash\nprintf 'result' > result.txt\n")
                    tasks.append({'task_id': name, 'input_dir': str(task_dir),
                        'script': 'job.sh', 'outputs': ['result.txt'], 'input_files': ['job.sh']})
                params = StdioServerParameters(command=sys.executable,
                    args=['-m', 'hpc_mcp', '--config', str(root / 'clusters.toml'),
                          '--state-dir', str(root / 'state'), 'serve'],
                    env=dict(os.environ, PYTHONPATH=str(ROOT / 'src')), cwd=str(root))
                async def call(client, name, args):
                    response = await client.call_tool(name, args)
                    self.assertFalse(response.is_error, response.content)
                    return response.structured_content
                async with Client(params, mode=mode) as client:
                    tools = {t.name: t for t in (await client.list_tools()).tools}
                    for name in ('plan', 'start', 'status', 'pause', 'resume', 'review'):
                        self.assertIn('batch_' + name, tools)
                    from hpc_mcp.tool_catalog import ADVANCED_TOOLS
                    self.assertTrue(ADVANCED_TOOLS.isdisjoint(tools))
                    for name in ('setup_flow_create', 'application_get', 'cluster_probe',
                                 'job_get', 'job_cancel', 'job_logs', 'monitor_stop',
                                 'local_cache_cleanup', 'cluster_work_cleanup', 'retention_hold',
                                 'retention_release', 'retention_status', 'cluster_registry_probe',
                                 'cluster_registry_reconcile'):
                        self.assertIn(name, tools)
                    catalog = (await call(client, 'settings_get', {}))['tool_catalog']
                    self.assertEqual(catalog['profile'], 'batch')
                    self.assertFalse(catalog['advanced_enabled'])
                    cleanup = await call(client, 'local_cache_cleanup', {})
                    self.assertTrue(cleanup['dry_run'])
                    self.assertEqual(cleanup['candidate_count'], 0)
                    rejected = await call(client, 'local_cache_cleanup', {
                        'dry_run': False, 'review_token': cleanup['review_token']})
                    self.assertFalse(rejected['ok'])
                    applied = await call(client, 'local_cache_cleanup', {
                        'dry_run': False, 'review_token': cleanup['review_token'],
                        'confirmation_note': 'Synthetic approval of exact empty cache preview'})
                    self.assertTrue(applied['ok'])
                    self.assertTrue(tools['local_cache_cleanup'].annotations.destructive_hint)
                    remote = await call(client, 'cluster_work_cleanup', {})
                    self.assertFalse(remote['ok'])  # Explicit configured cluster required.
                    unavailable = await client.call_tool('job_prepare', {})
                    self.assertTrue(unavailable.is_error)
                    self.assertTrue(tools['batch_status'].annotations.read_only_hint)
                    self.assertFalse(tools['batch_plan'].annotations.read_only_hint)
                    self.assertTrue(tools['batch_start'].annotations.open_world_hint)
                    await call(client, 'cluster_configure', {'cluster': 'lab', 'settings': {
                        'ssh_host': 'unused', 'scheduler': 'lsf', 'work_root': '/tmp/batch-example'}})
                    value = await call(client, 'batch_plan', {'request_key': 'stdio-batch',
                        'name': 'Two tasks', 'cluster': 'lab', 'project_root': str(project),
                        'tasks': tasks, 'start_monitor': False, 'compact': False})
                    self.assertTrue(value['ok'], value)
                    self.assertEqual(len(value['plan']['workflow_plan']['runs']), 2)
                    rejected = await call(client, 'batch_start', {'batch_id': value['batch_id'],
                        'review_token': 'wrong', 'confirmation_note': 'Synthetic approval'})
                    self.assertEqual(rejected['error'], 'batch_token_invalid')
                    started = await call(client, 'batch_start', {'batch_id': value['batch_id'],
                        'review_token': value['review_token'], 'confirmation_note': 'Synthetic complete approval'})
                    self.assertEqual(started['stage'], 'running')
                async with Client(params, mode=mode) as client:
                    status = await call(client, 'batch_status', {'batch_id': value['batch_id']})
                    self.assertEqual(status['counts'], {'ready': 2})
                    for task in status['tasks']:
                        run = await call(client, 'job_get', {'run_id': task['run_id']})
                        self.assertEqual(run['run']['phase'], 'prepared')
                    await call(client, 'batch_pause', {'batch_id': value['batch_id']})
                    paused = await call(client, 'batch_status', {'batch_id': value['batch_id']})
                    self.assertEqual(paused['counts'], {'paused': 2})

    async def test_shared_retention_release_and_cleanup_over_real_stdio(self):
        import test_jobs
        from hpc_mcp.config import ConfigManager
        for mode in ('auto', 'legacy'):
            fixture = test_jobs.JobLifecycleTests()
            fixture.setUp()
            try:
                run = fixture.prepare()
                rid = run['run_id']
                self.assertTrue(test_jobs.authorized_submit(fixture.jobs, rid)['ok'])
                self.assertTrue(test_jobs.authorized_sync(fixture.jobs, rid)['ok'])
                for path in Path(run['remote_dir']).rglob('*'):
                    os.utime(path, (1, 1))
                os.utime(run['remote_dir'], (1, 1))
                ssh = fixture.bin / 'ssh'
                ssh.write_text('#!/usr/bin/env python3\nimport os,sys\n'
                    "os.execvp('bash',['bash','-c',' '.join(sys.argv[sys.argv.index('alias')+1:])])\n")
                ssh.chmod(0o755)
                config = fixture.root / 'clusters.toml'
                ConfigManager(config, {}).set('lsf', {'ssh_host': 'alias', 'scheduler': 'lsf',
                                                       'work_root': str(fixture.remote)})
                params = StdioServerParameters(command=sys.executable,
                    args=['-m', 'hpc_mcp', '--config', str(config), '--state-dir', str(fixture.state), 'serve'],
                    env=dict(os.environ, PYTHONPATH=str(ROOT / 'src'),
                             PATH=str(fixture.bin) + ':' + os.environ['PATH']), cwd=str(fixture.root))
                async def call(client, name, args):
                    response = await client.call_tool(name, args)
                    self.assertFalse(response.is_error, response.content)
                    return response.structured_content
                async with Client(params, mode=mode) as client:
                    status = await call(client, 'cluster_registry_probe', {'cluster': 'lsf'})
                    self.assertFalse(status['node_conflict'])
                    await call(client, 'retention_hold', {'cluster': 'lsf', 'run_ids': [rid],
                        'holder': 'another-agent', 'reason': 'Synthetic second stage'})
                    for holder in ('submission', 'another-agent'):
                        plan = await call(client, 'retention_release', {'cluster': 'lsf',
                            'run_ids': [rid], 'holder': holder, 'reason': 'Synthetic stage finished'})
                        result = await call(client, 'retention_release', {'dry_run': False,
                            'review_token': plan['review_token'], 'confirmation_note': 'Synthetic exact approval'})
                        self.assertTrue(result['ok'], result)
                        if holder == 'submission':
                            status = await call(client, 'retention_status', {'cluster': 'lsf', 'run_ids': [rid]})
                            self.assertFalse(status['tasks'][0]['cleanup_allowed'])
                    plan = await call(client, 'cluster_work_cleanup', {'cluster': 'lsf', 'older_than_seconds': 0})
                    self.assertEqual(plan['candidate_count'], 1, plan)
                    result = await call(client, 'cluster_work_cleanup', {'dry_run': False,
                        'review_token': plan['review_token'], 'confirmation_note': 'Synthetic exact cleanup approval'})
                    self.assertTrue(result['ok'], result)
                self.assertFalse(Path(run['remote_dir']).exists())
                async with Client(params, mode=mode) as client:
                    status = await call(client, 'retention_status', {'cluster': 'lsf', 'run_ids': [rid]})
                    self.assertEqual(status['tasks'][0]['state'], 'deleted')
            finally:
                fixture.doCleanups()

    async def test_registered_single_input_uses_batch_and_advanced_tools_require_explicit_profile(self):
        import test_applications
        from hpc_mcp.config import ConfigManager
        from hpc_mcp.history import History
        for mode in ('auto', 'legacy'):
            fixture = test_applications.ApplicationTests()
            fixture.setUp()
            try:
                fixture.register()
                root = fixture.fixture.root
                config = root / 'clusters.toml'
                ConfigManager(config, {}).set('lsf', {'ssh_host': 'alias', 'scheduler': 'lsf',
                    'work_root': str(fixture.fixture.remote)})
                base_args = ['-m', 'hpc_mcp', '--config', str(config),
                    '--state-dir', str(fixture.jobs.history.root), 'serve']
                params = StdioServerParameters(command=sys.executable, args=base_args,
                    env=dict(os.environ, PYTHONPATH=str(ROOT / 'src')), cwd=str(root))
                async def call(client, name, args):
                    response = await client.call_tool(name, args)
                    self.assertFalse(response.is_error, response.content)
                    return response.structured_content
                async with Client(params, mode=mode) as client:
                    tools = {t.name for t in (await client.list_tools()).tools}
                    self.assertNotIn('gaussian_prepare', tools)
                    self.assertNotIn('application_prepare', tools)
                    details = await call(client, 'application_get', {'application': 'gaussian'})
                    self.assertIn('cpus', details['application']['manifest']['parameters']['properties'])
                    value = await call(client, 'batch_plan', {'request_key': 'one-card',
                        'name': 'Single input', 'cluster': 'lsf', 'project_root': str(root),
                        'tasks': [{'application': 'gaussian', 'input_path': str(fixture.card)}],
                        'start_monitor': False})
                    self.assertTrue(value['ok'], value)
                    self.assertEqual(len(value['tasks']), 1)
                    started = await call(client, 'batch_start', {'batch_id': value['batch_id'],
                        'review_token': value['review_token'], 'confirmation_note': 'Synthetic one-input approval'})
                    self.assertEqual(started['task_count'], 1)
                    forbidden = await client.call_tool('job_submit', {'run_id': value['tasks'][0]['run_id']})
                    self.assertTrue(forbidden.is_error)
                advanced = StdioServerParameters(command=sys.executable,
                    args=base_args + ['--tool-profile', 'advanced'],
                    env=dict(os.environ, PYTHONPATH=str(ROOT / 'src')), cwd=str(root))
                async with Client(advanced, mode=mode) as client:
                    tools = {t.name for t in (await client.list_tools()).tools}
                    self.assertIn('gaussian_prepare', tools)
                    self.assertIn('application_prepare', tools)
                    self.assertIn('job_submit', tools)
                    self.assertIn('batch_start', tools)
                    catalog = (await call(client, 'settings_get', {}))['tool_catalog']
                    self.assertEqual(catalog['profile'], 'advanced')
                    denied = await client.call_tool('job_submit', {'run_id': value['tasks'][0]['run_id']})
                    self.assertTrue(denied.is_error)  # Workflow ownership still guards submission.
                history = History(fixture.jobs.history.root)
                self.assertEqual(history.get(value['tasks'][0]['run_id'])['phase'], 'prepared')
                self.assertFalse((root / 'submissions').exists())
            finally:
                fixture.doCleanups()

    async def test_discovery_tools_and_restart_in_modern_and_legacy_modes(self):
        for mode in ('auto', 'legacy'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root=Path(directory); config=root/'clusters.toml'; state=root/'state'; project=root/'project'
                project.mkdir(); (project/'water.gjf').write_text('# hf/sto-3g\n\nwater\n\n0 1\nH 0 0 0\n\n')
                params=StdioServerParameters(command=sys.executable,args=['-m','hpc_mcp','--config',str(config),'--state-dir',str(state),'serve','--tool-profile','advanced'],
                    env=dict(os.environ,PYTHONPATH=str(ROOT/'src')),cwd=str(root))
                async def call(client,name,args={}):
                    result=await client.call_tool(name,args)
                    self.assertFalse(result.is_error, result.content)
                    return result.structured_content
                async with asyncio.timeout(45):
                    async with Client(params,mode=mode) as client:
                        self.assertEqual(client.server_info.name,'hpc-mcp')
                        tools={t.name:t for t in (await client.list_tools()).tools}
                        self.assertNotIn('gaussian_prepare',tools)
                        self.assertNotIn('profile_plan',tools)
                        self.assertNotIn('application_check',tools)
                        self.assertIn('application_review_request',tools)
                        self.assertIn('application_prepare',tools)
                        self.assertIn('job_sync',tools)
                        self.assertIn('job_submission_plan',tools)
                        self.assertIn('job_authorize',tools)
                        for name in ('start', 'get', 'list', 'pause', 'resume', 'tick'):
                            self.assertIn('job_lifecycle_' + name, tools)
                        self.assertIn('workflow_review',tools)
                        self.assertIn('workflow_plan',tools)
                        self.assertIn('setup_flow_create',tools)
                        self.assertIn('setup_flow_cleanup',tools)
                        self.assertIn('setup_flow_step',tools)
                        self.assertIn('setup_flow_confirm',tools)
                        self.assertIn('handler_runner_status',tools)
                        self.assertIn('handler_runner_configure',tools)
                        runner=await call(client,'handler_runner_status',{'probe':True})
                        self.assertTrue(runner['available'],runner.get('diagnostic'))
                        self.assertEqual(runner['settings']['mode'],'isolated')
                        denied=await call(client,'handler_runner_configure',{'settings':{'mode':'trusted'},'confirmation_note':''})
                        self.assertFalse(denied['ok'])
                        self.assertEqual((await call(client,'application_list'))['applications'],[])
                        missing=await call(client,'application_prepare',{'application':'gaussian','cluster':'lab','input_path':str(project/'water.gjf'),'project_root':str(project)})
                        self.assertEqual(missing['error'],'application_not_registered')
                        await call(client,'cluster_configure',{'cluster':'lab','settings':{'ssh_host':'lab','scheduler':'lsf','work_root':'/tmp/hpc-mcp-example'}})
                        flow=(await call(client,'setup_flow_create',{'kind':'application_adaptation','request_key':'gaussian-first','label':'gaussian'}))['flow']
                        workspace=Path(flow['workspace']);self.assertTrue(workspace.is_relative_to(state/'onboarding'))
                        blocked_cleanup=await call(client,'setup_flow_cleanup',{'flow_id':flow['flow_id'],'expected_revision':flow['revision'],'dry_run':False})
                        self.assertEqual(blocked_cleanup['error'],'setup_flow_in_use')
                        flow=(await call(client,'setup_flow_step',{'flow_id':flow['flow_id'],'expected_revision':flow['revision'],'action':'application_source',
                            'payload':{'application':'gaussian','source_paths':[str(ROOT/'examples/applications/gaussian/original/qg16')]}}))['flow']
                        shutil.copytree(ROOT/'examples/applications/gaussian',workspace/'bundle')
                        blocked_install=await call(client,'application_install',{'bundle_dir':str(workspace/'bundle')})
                        self.assertEqual(blocked_install['error'],'application_setup_flow_required')
                        flow=(await call(client,'setup_flow_step',{'flow_id':flow['flow_id'],'expected_revision':flow['revision'],'action':'application_install','payload':{'bundle_dir':str(workspace/'bundle')}}))['flow']
                        token=flow['business']['review_token']
                        blocked=await call(client,'application_activate',{'application':'gaussian','version':1,'review_token':token,'confirmation_note':'Confirmed but not reviewed.'})
                        self.assertFalse(blocked['ok'])
                        flow=(await call(client,'setup_flow_step',{'flow_id':flow['flow_id'],'expected_revision':flow['revision'],'action':'application_review_request','payload':{'author_session':'test-author'}}))['flow']
                        packet=flow['business']['review_request']
                        report={'reviewer_session':'test-independent-reviewer','fresh_context':True,'verdict':'pass',
                                'checks':{k:'Synthetic test evidence.' for k in packet['required_checks']},
                                'differences':[],'unresolved':[],'allowed_changes':[]}
                        flow=(await call(client,'setup_flow_step',{'flow_id':flow['flow_id'],'expected_revision':flow['revision'],'action':'application_review_submit','payload':{'report':report}}))['flow']
                        bypass=await call(client,'application_activate',{'application':'gaussian','version':1,'review_token':token,'confirmation_note':'confirmed'})
                        self.assertEqual(bypass['error'],'application_setup_flow_required')
                        closed=(await call(client,'setup_flow_confirm',{'flow_id':flow['flow_id'],'expected_revision':flow['revision'],
                            'confirmation_token':flow['business']['confirmation_token'],'confirmation_note':'User confirmed exact generator and supported scope.'}))['flow']
                        self.assertEqual(closed['state'],'completed')
                        preview=await call(client,'setup_flow_cleanup',{'flow_id':flow['flow_id'],'expected_revision':closed['revision']})
                        self.assertTrue(preview['dry_run']);self.assertTrue(workspace.exists())
                        cleaned=await call(client,'setup_flow_cleanup',{'flow_id':flow['flow_id'],'expected_revision':closed['revision'],'dry_run':False})
                        self.assertEqual(cleaned['flow']['state'],'cleaned');self.assertFalse(workspace.exists())
                        prepared=await call(client,'application_prepare',{'application':'gaussian','cluster':'lab','input_path':str(project/'water.gjf'),'project_root':str(project)})
                        run_id=prepared['run']['run_id']
                        lifecycle=await call(client,'job_lifecycle_get',{'run_id':run_id})
                        self.assertEqual(lifecycle['lifecycle']['stage'],'awaiting_authorization')
                        self.assertEqual(lifecycle['application_result']['status'],'unverified')
                        denied=await call(client,'job_lifecycle_start',{'run_id':run_id})
                        self.assertEqual(denied['error'],'submission_authorization_required')
                        self.assertEqual(prepared['run']['phase'],'prepared')
                        self.assertEqual(prepared['plugin']['version'],1)
                        details=await call(client,'job_get',{'run_id':run_id})
                        self.assertIn('#BSUB -n 28',details['run']['generated_script'])
                        denied=await call(client,'job_submit',{'run_id':run_id})
                        self.assertEqual(denied['error'],'submission_authorization_required')
                        plan=await call(client,'job_submission_plan',{'run_id':run_id})
                        self.assertEqual(plan['plan']['manifest'],details['run']['manifest'])
                        bad=await call(client,'job_authorize',{'run_id':run_id,'review_token':'wrong','confirmation_note':'Synthetic user approval'})
                        self.assertEqual(bad['error'],'submission_authorization_invalid')
                        approved=await call(client,'job_authorize',{'run_id':run_id,'review_token':plan['review_token'],'confirmation_note':'Synthetic exact-plan approval; do not submit'})
                        self.assertTrue(approved['ok'])
                        self.assertEqual((await call(client,'job_get',{'run_id':run_id}))['run']['phase'],'prepared')

                    async with Client(params,mode=mode) as client:
                        tools={t.name:t for t in (await client.list_tools()).tools}
                        restored=await call(client,'setup_flow_get',{'flow_id':flow['flow_id']})
                        self.assertEqual(restored['flow']['state'],'cleaned')
                        self.assertEqual(restored['flow']['artifacts'][0]['application'],'gaussian')
                        self.assertIn('gaussian_prepare',tools)
                        self.assertIn('parameters',tools['gaussian_prepare'].input_schema['properties'])
                        model_schema=json.dumps(tools['gaussian_prepare'].input_schema)
                        self.assertIn('local_scratch',model_schema)
                        self.assertIn('cpus',model_schema)
                        prepared=await call(client,'gaussian_prepare',{'cluster':'lab','input_path':str(project/'water.gjf'),'project_root':str(project),'parameters':{'cpus':24}})
                        self.assertEqual(prepared['resources']['cpus'],24)
                        old=await call(client,'job_get',{'run_id':run_id})
                        self.assertEqual(old['run']['template']['application_plugin']['version'],1)
                        blocked=await call(client,'application_remove',{'application':'gaussian','dry_run':False})
                        self.assertEqual(blocked['error'],'application_in_use')
                        await call(client,'job_cancel',{'run_id':run_id})
                        await call(client,'job_cancel',{'run_id':prepared['run']['run_id']})
                        removed=await call(client,'application_remove',{'application':'gaussian','dry_run':False})
                        self.assertEqual(removed['residual_paths'],[])
                    async with Client(params,mode=mode) as client:
                        tools={t.name:t for t in (await client.list_tools()).tools}
                        self.assertNotIn('gaussian_prepare',tools)
                        self.assertEqual((await call(client,'application_list'))['applications'],[])
