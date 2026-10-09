"""Exercise the installed official SDK against the real CLI subprocess, without SSH."""

import asyncio
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


@unittest.skipIf(Client is None, "install the mcp extra for real stdio integration tests")
class StdioIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_tools_and_restart_in_modern_and_legacy_modes(self):
        for mode in ("auto", "legacy"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = root / "agent-data" / "clusters.toml"
                config.parent.mkdir()
                # First connection must work before a configuration file exists.
                state = root / "agent-data" / "state"
                from hpc_mcp.updates import REPOSITORY, installation
                import json
                import time
                local = installation()
                state.mkdir()
                (state / "update-check.json").write_text(json.dumps({
                    "ok": True, "repository": REPOSITORY, "checked_at": time.time(),
                    "local_commit": local.get("commit"), "local_version": local["version"],
                    "update_available": False, "comparison": "identical"}))
                project = root / "project"
                source = project / "B"
                source.mkdir(parents=True)
                (source / "test.gjf").write_text("%chk=test.chk\n# hf/sto-3g\n\ntitle\n\n0 1\nH 0 0 0\n\n")
                (source / "job.sh").write_text("#!/bin/bash\ntrue\n")
                cwd = root / "unrelated-working-directory"
                cwd.mkdir()
                parameters = StdioServerParameters(command=sys.executable,
                    args=["-m", "hpc_mcp", "--config", str(config), "--state-dir", str(state), "serve"],
                    cwd=str(cwd), env=dict(os.environ,
                        PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src")))
                async with asyncio.timeout(30):
                    async with Client(parameters, mode=mode, read_timeout_seconds=10) as client:
                        self.assertEqual(client.server_info.name, "hpc-mcp")
                        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                        self.assertEqual(set(tools), {
                            "settings_get", "cluster_configure", "cluster_list", "cluster_check",
                            "cluster_info", "job_prepare", "job_submit", "job_list", "job_get",
                            "job_status", "job_recover", "job_logs", "job_cancel", "job_sync",
                            "job_cache_cleanup",
                            "script_inspect", "script_generate", "job_prepare_generated", "template_import",
                            "template_list", "template_get", "template_plan", "template_run",
                            "update_check", "update_plan", "gaussian_inspect", "gaussian_prepare", "gaussian_result",
                            "job_sync_preview", "job_sync_start", "job_sync_operation",
                            "job_storage_cleanup", "input_cache_cleanup", "job_remote_cleanup",
                            "monitor_watch", "monitor_unwatch", "monitor_start", "monitor_stop",
                            "monitor_status", "monitor_notifications",
                            "cluster_probe", "onboarding_report", "profile_draft", "profile_confirm",
                            "profile_get", "profile_list", "profile_plan", "profile_validate",
                            "workflow_plan", "workflow_start", "workflow_pause", "workflow_retry",
                            "workflow_status", "workflow_tick", "job_usage", "usage_report",
                        })
                        properties = tools["job_prepare"].input_schema["properties"]
                        self.assertIn("project_root", properties)
                        self.assertIn("input_files", properties)
                        configured = await client.call_tool("cluster_configure", {"cluster": "lab",
                            "settings": {"ssh_host": "offline-alias", "scheduler": "lsf",
                                         "work_root": "/shared/jobs"}})
                        self.assertFalse(configured.is_error)
                        settings = await client.call_tool("settings_get", {"cluster": "lab"})
                        self.assertFalse(settings.is_error)
                        self.assertEqual(settings.structured_content["config_path"], str(config))
                        self.assertEqual(settings.structured_content["state_dir"], str(state))
                        update = await client.call_tool("update_check", {})
                        self.assertFalse(update.is_error)
                        self.assertTrue(update.structured_content["cached"])
                        self.assertFalse(update.structured_content["update_available"])
                        plan = await client.call_tool("update_plan", {})
                        self.assertFalse(plan.is_error)
                        self.assertEqual(plan.structured_content["commands"], [])
                        prepared = await client.call_tool("job_prepare", {"cluster": "lab",
                            "project_root": str(project), "input_dir": str(source), "script": "job.sh",
                            "input_files": ["test.gjf"], "outputs": ["test.log", "test.chk"]})
                        self.assertFalse(prepared.is_error, prepared.content)
                        run = prepared.structured_content["run"]
                        self.assertEqual(run["phase"], "prepared")
                        self.assertEqual(run["project_root"], str(project))
                        self.assertTrue(Path(run["snapshot_dir"]).is_relative_to(state))
                        cleanup = await client.call_tool("job_cache_cleanup", {"run_id": run["run_id"]})
                        self.assertFalse(cleanup.is_error)
                        self.assertTrue(cleanup.structured_content["dry_run"])
                        inspected_script = await client.call_tool("script_inspect", {"script_path": str(source / "job.sh")})
                        self.assertFalse(inspected_script.is_error)
                        self.assertTrue(inspected_script.structured_content["requires_review"])
                        self.assertIn("max_bytes", tools["script_inspect"].input_schema["properties"])
                        report_id = inspected_script.structured_content['report_id']
                        self.assertEqual(tools['cluster_probe'].input_schema['properties']['module_avail']['default'], True)
                        draft = await client.call_tool('profile_draft', {'name': 'protocol-profile', 'cluster': 'lab',
                            'application': 'test', 'definition': {'scheduler': 'lsf',
                                'spec': {'command': ['true']}, 'input_files': ['test.gjf'], 'outputs': ['test.log']},
                            'report_ids': [report_id]})
                        self.assertFalse(draft.is_error, draft.content)
                        profile = draft.structured_content['profile']
                        confirmed = await client.call_tool('profile_confirm', {'profile_id': profile['profile_id'],
                            'review_token': profile['review_token'], 'confirmation_note': 'Offline test user confirmed all displayed settings'})
                        self.assertFalse(confirmed.is_error, confirmed.content)
                        profile_plan = await client.call_tool('profile_plan', {'input_dir': str(source),
                            'cluster': 'lab', 'application': 'test', 'project_root': str(project)})
                        self.assertFalse(profile_plan.is_error, profile_plan.content)
                        self.assertEqual(profile_plan.structured_content['run']['profile']['validation'], 'unverified')
                        validation = await client.call_tool('profile_validate', {'profile_id': profile['profile_id'], 'command': ['true']})
                        self.assertFalse(validation.is_error, validation.content)
                        self.assertEqual(validation.structured_content['run']['phase'], 'prepared')
                        self.assertIn("tasks", settings.structured_content["script_resources"])
                        monitor_status = await client.call_tool("monitor_status", {})
                        self.assertFalse(monitor_status.is_error)
                        self.assertFalse(monitor_status.structured_content["runtime"]["alive"])
                        self.assertEqual(monitor_status.structured_content["total"], 0)
                        self.assertIn("sync_options", tools["monitor_watch"].input_schema["properties"])
                        preview = await client.call_tool("script_generate", {
                            "scheduler": "lsf", "spec": {"command": ["true"]}})
                        self.assertFalse(preview.is_error)
                        self.assertIn("exec -- true", preview.structured_content["script"])
                        inspected = await client.call_tool("gaussian_inspect", {"input_file": str(source / "test.gjf")})
                        self.assertFalse(inspected.is_error)
                        self.assertEqual(inspected.structured_content["analysis"]["candidate_outputs"], ["test.log", "test.chk"])
                        gaussian = await client.call_tool("gaussian_prepare", {
                            "cluster": "lab", "input_file": str(source / "test.gjf"), "project_root": str(project),
                            "spec": {"command": ["g16"], "resources": {"cpus": 2}},
                            "outputs": ["test.log", "test.chk"], "changes": {"cpus": 2}})
                        self.assertFalse(gaussian.is_error, gaussian.content)
                        self.assertTrue(gaussian.structured_content["gaussian"]["diff"])
                        self.assertNotIn('spec', tools['gaussian_prepare'].input_schema.get('required', []))
                        self.assertTrue(tools['gaussian_prepare'].input_schema['properties']['compact']['default'])
                        learned = await client.call_tool('profile_draft', {'name': 'learned-qg16', 'cluster': 'lab',
                            'application': 'gaussian', 'definition': {'scheduler': 'lsf',
                                'spec': {'command': ['g16'], 'stdin': '{{input}}', 'stdout': '{{stem}}.log',
                                    'resources': {'cpus': 2}},
                                'parameters': {'input': {'type': 'string'}, 'stem': {'type': 'string'}},
                                'input_files': ['{{input}}'], 'outputs': ['{{stem}}.log', '{{stem}}.chk']}})
                        self.assertFalse(learned.is_error, learned.content)
                        learned_profile = learned.structured_content['profile']
                        self.assertTrue(Path(learned_profile['configuration_file']).is_file())
                        activated = await client.call_tool('profile_confirm', {'profile_id': learned_profile['profile_id'],
                            'review_token': learned_profile['review_token'], 'confirmation_note': 'User confirms Gaussian defaults'})
                        self.assertFalse(activated.is_error, activated.content)
                        reused = await client.call_tool('gaussian_prepare', {'cluster': 'lab',
                            'input_file': str(source / 'test.gjf'), 'project_root': str(project)})
                        self.assertFalse(reused.is_error, reused.content)
                        self.assertTrue(reused.structured_content['compact'])
                        self.assertEqual(reused.structured_content['run']['profile']['profile_id'], learned_profile['profile_id'])
                        self.assertEqual(reused.structured_content['gaussian']['diff'], '')
                        discovery = await client.call_tool('profile_list', {'cluster': 'lab', 'application': 'gaussian'})
                        self.assertFalse(discovery.is_error)
                        self.assertNotIn('definition', discovery.structured_content['profiles'][0])
                        cache = await client.call_tool("input_cache_cleanup", {})
                        self.assertFalse(cache.is_error)
                        self.assertTrue(cache.structured_content["dry_run"])
                        self.assertEqual(tools["job_sync"].input_schema["properties"]["resume"]["default"], None)
                        imported = await client.call_tool("template_import", {"name": "protocol-test",
                            "definition": {"scheduler": "lsf", "spec": {"command": ["true"]},
                                           "input_files": ["test.gjf"], "outputs": ["test.log"]}})
                        self.assertFalse(imported.is_error, imported.content)
                        planned = await client.call_tool("template_plan", {"name": "protocol-test",
                            "cluster": "lab", "input_dir": str(source), "project_root": str(project)})
                        self.assertFalse(planned.is_error, planned.content)
                        self.assertEqual(planned.structured_content["run"]["template"]["version"], 1)
                        template_plan_id = planned.structured_content["plan_id"]
                        workflow = await client.call_tool('workflow_plan', {'name': 'offline-workflow', 'run_ids': [template_plan_id]})
                        self.assertFalse(workflow.is_error, workflow.content)
                        workflow_value = workflow.structured_content['workflow']
                        self.assertFalse(workflow_value['enabled'])
                        started = await client.call_tool('workflow_start', {'workflow_id': workflow_value['workflow_id'],
                            'review_token': workflow_value['review_token'], 'confirmation_note': 'Offline test explicitly authorizes this plan'})
                        self.assertFalse(started.is_error)
                        paused = await client.call_tool('workflow_pause', {'workflow_id': workflow_value['workflow_id']})
                        self.assertFalse(paused.structured_content['workflow']['enabled'])
                        tick = await client.call_tool('workflow_tick', {'workflow_id': workflow_value['workflow_id']})
                        self.assertTrue(tick.structured_content['deferred'])
                        usage = await client.call_tool('job_usage', {'run_id': run['run_id'], 'refresh': False})
                        self.assertIsNone(usage.structured_content['usage'])
                        summary = await client.call_tool('usage_report', {})
                        self.assertIsNone(summary.structured_content['totals']['actual_cpu_seconds']['sum'])
                    async with Client(parameters, mode=mode, read_timeout_seconds=10) as restarted:
                        saved = await restarted.call_tool("job_get", {"run_id": run["run_id"]})
                        self.assertFalse(saved.is_error)
                        self.assertEqual(saved.structured_content["run"]["manifest"], run["manifest"])
                        self.assertEqual(saved.structured_content["events"][0]["kind"], "prepared")
                        template = await restarted.call_tool("template_get", {"name": "protocol-test", "version": 1})
                        self.assertFalse(template.is_error)
                        self.assertEqual(template.structured_content["template"]["version"], 1)
                        template_run = await restarted.call_tool("job_get", {"run_id": template_plan_id})
                        self.assertEqual(template_run.structured_content["run"]["template"]["version"], 1)
                        saved_profile = await restarted.call_tool('profile_get', {'profile_id': profile['profile_id']})
                        self.assertTrue(saved_profile.structured_content['profile']['is_default'])
                        saved_report = await restarted.call_tool('onboarding_report', {'report_id': report_id})
                        self.assertEqual(saved_report.structured_content['report']['source'], inspected_script.structured_content['source'])
                        saved_workflow = await restarted.call_tool('workflow_status', {'workflow_id': workflow_value['workflow_id']})
                        self.assertEqual(saved_workflow.structured_content['workflow']['plan']['run_ids'], [template_plan_id])
                self.assertFalse((cwd / ".hpc-mcp").exists())
                self.assertFalse((project / ".hpc-mcp-sync").exists())
