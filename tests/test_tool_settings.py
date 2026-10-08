"""Validate tool handler wiring without claiming a real SDK/protocol handshake."""

from pathlib import Path
import inspect
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

from xn02_mcps.config import ConfigManager
from xn02_mcps.jobs import JobService
from xn02_mcps.server import create_server
from xn02_mcps.service import ClusterService


class RecordingServer:
    def __init__(self, name):
        self.tools = {}

    def tool(self):
        def register(function):
            self.tools[function.__name__] = function
            return function
        return register


class ToolSettingsTests(unittest.TestCase):
    def test_discover_configure_and_prepare_through_tool_handlers(self):
        module = ModuleType("mcp.server")
        module.MCPServer = RecordingServer
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "sys.modules", {"mcp": ModuleType("mcp"), "mcp.server": module}
        ):
            root = Path(directory)
            clusters = {}
            service = ClusterService(clusters)
            jobs = JobService(clusters, root / "state")
            config = ConfigManager(root / "clusters.toml", clusters)
            server = create_server(service, jobs, config)
            template = server.tools["settings_get"]()["settings"]
            self.assertNotIn("name", template)
            server.tools["cluster_configure"]("lab", {
                "ssh_host": "alias", "scheduler": "lsf", "work_root": "/shared/jobs",
                "output_mode": "filtered", "output_include": ["*.chk"],
            })
            self.assertEqual(server.tools["cluster_list"]()[0]["name"], "lab")
            settings = server.tools["settings_get"]("lab")
            self.assertEqual(settings["settings"]["output_include"], ["*.chk"])
            source = root / "input"
            source.mkdir()
            (source / "job.sh").write_text("#!/bin/bash\ntrue\n")
            prepared = server.tools["job_prepare"]("lab", str(source), "job.sh")
            self.assertEqual(prepared["run"]["outputs"], ["*.chk"])
            self.assertEqual(prepared["run"]["output_mode"], "filtered")
            parameters = inspect.signature(server.tools["job_sync"]).parameters
            for name in ("mode", "includes", "excludes", "destination", "layout", "overwrite",
                         "checksum", "compress", "timeout"):
                self.assertIn(name, parameters)
