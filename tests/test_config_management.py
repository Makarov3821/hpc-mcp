from dataclasses import asdict
from pathlib import Path
import tempfile
import unittest

from xn02_mcps.config import Cluster, ConfigManager, load_config


class ConfigManagementTests(unittest.TestCase):
    def test_create_update_and_shared_live_configuration(self):
        with tempfile.TemporaryDirectory() as root:
            file = Path(root) / "clusters.toml"
            shared = {}
            manager = ConfigManager(file, shared)
            created = manager.set("lab", {"ssh_host": "alias", "scheduler": "lsf",
                "work_root": "/shared/jobs", "output_mode": "filtered", "output_include": ["*.chk"]})
            self.assertEqual(created["output_include"], ["*.chk"])
            manager.set("lab", {"transfer_timeout": 600, "sync_layout": "direct"})
            self.assertEqual(shared["lab"].transfer_timeout, 600)
            self.assertEqual(shared["lab"].output_include, ["*.chk"])
            self.assertEqual(asdict(load_config(file)["lab"]), asdict(shared["lab"]))
            before = file.read_bytes()
            with self.assertRaises(ValueError):
                manager.set("lab", {"transfer_timeout": -1})
            self.assertEqual(file.read_bytes(), before)

    def test_pattern_and_type_validation(self):
        for settings in [{"output_include": "*.txt"}, {"sync_layout": "bad"},
                         {"input_exclude": ["../bad"]}, {"ssh_output_limit": 0}]:
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                Cluster("lab", "alias", "lsf", "/shared/jobs", **settings)
