"""Real official SDK stdio lifecycle and dynamic handler discovery, without SSH."""
import asyncio
import json
import os
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
    async def test_discovery_tools_and_restart_in_modern_and_legacy_modes(self):
        for mode in ('auto', 'legacy'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root=Path(directory); config=root/'clusters.toml'; state=root/'state'; project=root/'project'
                project.mkdir(); (project/'water.gjf').write_text('# hf/sto-3g\n\nwater\n\n0 1\nH 0 0 0\n\n')
                params=StdioServerParameters(command=sys.executable,args=['-m','hpc_mcp','--config',str(config),'--state-dir',str(state),'serve'],
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
                        self.assertIn('application_prepare',tools)
                        self.assertIn('job_sync',tools)
                        self.assertIn('workflow_plan',tools)
                        self.assertEqual((await call(client,'application_list'))['applications'],[])
                        missing=await call(client,'application_prepare',{'application':'gaussian','cluster':'lab','input_path':str(project/'water.gjf'),'project_root':str(project)})
                        self.assertEqual(missing['error'],'application_not_registered')
                        await call(client,'cluster_configure',{'cluster':'lab','settings':{'ssh_host':'lab','scheduler':'lsf','work_root':'/tmp/hpc-mcp-example'}})
                        installed=await call(client,'application_install',{'bundle_dir':str(ROOT/'examples/applications/gaussian')})
                        record=installed['application']; token=record['review_token']
                        await call(client,'application_check',{'application':'gaussian','version':1,'review_token':token,'review_note':'Reviewed handler; original submission code is archived only.'})
                        await call(client,'application_activate',{'application':'gaussian','version':1,'review_token':token,'confirmation_note':'User confirmed exact generator and supported scope.'})
                        prepared=await call(client,'application_prepare',{'application':'gaussian','cluster':'lab','input_path':str(project/'water.gjf'),'project_root':str(project)})
                        run_id=prepared['run']['run_id']
                        self.assertEqual(prepared['run']['phase'],'prepared')
                        self.assertEqual(prepared['plugin']['version'],1)
                        details=await call(client,'job_get',{'run_id':run_id})
                        self.assertIn('#BSUB -n 28',details['run']['generated_script'])
                    async with Client(params,mode=mode) as client:
                        tools={t.name:t for t in (await client.list_tools()).tools}
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
