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
                config.write_text("[clusters]\n")
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
                            "script_generate", "job_prepare_generated", "template_import",
                            "template_list", "template_get", "template_plan", "template_run",
                            "update_check", "update_plan", "gaussian_inspect", "gaussian_prepare", "gaussian_result",
                            "job_sync_preview", "job_sync_start", "job_sync_operation",
                            "job_storage_cleanup", "input_cache_cleanup", "job_remote_cleanup",
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
                self.assertFalse((cwd / ".hpc-mcp").exists())
                self.assertFalse((project / ".hpc-mcp-sync").exists())
