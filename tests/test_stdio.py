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
                project = root / "project"
                source = project / "B"
                source.mkdir(parents=True)
                (source / "test.gjf").write_text("input card\n")
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
                    async with Client(parameters, mode=mode, read_timeout_seconds=10) as restarted:
                        saved = await restarted.call_tool("job_get", {"run_id": run["run_id"]})
                        self.assertFalse(saved.is_error)
                        self.assertEqual(saved.structured_content["run"]["manifest"], run["manifest"])
                        self.assertEqual(saved.structured_content["events"][0]["kind"], "prepared")
                self.assertFalse((cwd / ".hpc-mcp").exists())
                self.assertFalse((project / ".hpc-mcp-sync").exists())
