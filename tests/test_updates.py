"""Update checks must distinguish unavailable, divergent and safe fast-forward installs."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from hpc_mcp.updates import REPOSITORY, UpdateService, installation


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.updates = UpdateService(self.root / "state")
        self.local = {"version": "0.1.0", "python": "/venv/bin/python", "kind": "source",
                      "checkout": str(self.root / "checkout"), "commit": "a" * 40,
                      "branch": "main", "dirty": False, "origin": REPOSITORY + ".git"}
        self.install_patch = patch("hpc_mcp.updates.installation", return_value=self.local)
        self.install_patch.start()
        self.addCleanup(self.install_patch.stop)

    def remote(self, status="ahead"):
        return patch("hpc_mcp.updates._request", side_effect=[
            {"sha": "b" * 40}, {"status": status}])

    def test_cache_and_force_and_changed_commit(self):
        with self.remote() as request:
            result = self.updates.check()
            self.assertTrue(result["update_available"])
            self.assertEqual(request.call_count, 2)
        with patch("hpc_mcp.updates._request") as request:
            self.local["dirty"] = True
            cached = self.updates.check()
            self.assertTrue(cached["cached"])
            self.assertTrue(cached["installation"]["dirty"])
            request.assert_not_called()
        with self.remote() as request:
            self.updates.check(force=True)
            self.assertEqual(request.call_count, 2)
        self.local["commit"] = "c" * 40
        with self.remote() as request:
            self.assertFalse(self.updates.check()["cached"])
            self.assertEqual(request.call_count, 2)

    def test_offline_is_unknown_and_does_not_replace_good_cache(self):
        with self.remote():
            self.updates.check()
        original = self.updates.cache.read_bytes()
        with patch("hpc_mcp.updates._request", side_effect=OSError("offline")):
            result = self.updates.check(force=True)
        self.assertFalse(result["ok"])
        self.assertIsNone(result["update_available"])
        self.assertEqual(original, self.updates.cache.read_bytes())

    def test_pinned_plan_does_not_execute_commands_or_touch_job_data(self):
        self.updates.root.mkdir()
        history = self.updates.root / "history.sqlite3"
        history.write_bytes(b"untouched")
        with self.remote(), patch("hpc_mcp.updates.subprocess.run") as process:
            plan = self.updates.plan()
        process.assert_not_called()
        self.assertTrue(plan["ok"])
        self.assertTrue(plan["restart_required"])
        self.assertEqual(plan["commands"][0]["argv"], ["git", "fetch", "origin", "b" * 40])
        self.assertEqual(plan["commands"][1]["argv"], ["git", "merge", "--ff-only", "b" * 40])
        self.assertEqual(plan["commands"][2]["argv"][0], "/venv/bin/python")
        self.assertEqual(history.read_bytes(), b"untouched")

    def test_block_dirty_fork_branch_and_divergence(self):
        for change in ({"dirty": True}, {"origin": "https://example.com/fork"},
                       {"branch": "feature"}, {"kind": "package"}):
            with self.subTest(change=change):
                original = self.local.copy()
                self.local.update(change)
                with self.remote():
                    plan = self.updates.plan(force=True)
                self.assertFalse(plan["ok"])
                self.assertEqual(plan["commands"], [])
                self.local.clear()
                self.local.update(original)
        for status in ("behind", "diverged"):
            with self.remote(status):
                plan = self.updates.plan(force=True)
            self.assertFalse(plan["ok"])
            self.assertEqual(plan["commands"], [])

    def test_identical_and_invalid_network_response(self):
        with patch("hpc_mcp.updates._request", return_value={"sha": "a" * 40}):
            result = self.updates.plan()
        self.assertTrue(result["ok"])
        self.assertFalse(result["check"]["update_available"])
        self.assertEqual(result["commands"], [])
        with patch("hpc_mcp.updates._request", return_value={"sha": "main;bad"}):
            self.assertFalse(self.updates.check(force=True)["ok"])

    def test_controls_and_corrupt_cache(self):
        self.updates.root.mkdir()
        self.updates.cache.write_text("not json")
        self.assertIsNone(self.updates.cached())
        for settings in ({"timeout": 0}, {"timeout": True}, {"max_age_seconds": -1},
                         {"force": "yes"}):
            with self.assertRaises(ValueError):
                self.updates.check(**settings)

    def test_git_installed_package_retains_commit_for_notifications(self):
        self.install_patch.stop()
        distribution = Mock(version="0.1.0")
        distribution.read_text.return_value = json.dumps({
            "url": REPOSITORY + ".git", "vcs_info": {"vcs": "git", "commit_id": "a" * 40}})
        with patch("hpc_mcp.updates.importlib.metadata.distribution", return_value=distribution), \
             patch("hpc_mcp.updates.__file__", str(self.root / "site-packages/hpc_mcp/updates.py")):
            local = installation()
        self.assertEqual(local["kind"], "package")
        self.assertEqual(local["commit"], "a" * 40)
        with patch("hpc_mcp.updates.installation", return_value=local), self.remote():
            plan = self.updates.plan()
        self.assertTrue(plan["check"]["update_available"])
        self.assertEqual(plan["commands"], [])

    def test_cli_offline_check_needs_no_cluster_configuration(self):
        cache = {"ok": True, "repository": REPOSITORY, "checked_at": __import__("time").time(),
                 "local_commit": self.local["commit"], "local_version": "0.1.0"}
        # CLI uses a fresh process; compare its actual installation fingerprint.
        self.install_patch.stop()
        local = installation()
        cache.update(local_commit=local.get("commit"), local_version=local["version"])
        self.updates.root.mkdir()
        self.updates.cache.write_text(json.dumps(cache))
        result = subprocess.run([sys.executable, "-m", "hpc_mcp", "--config",
                                 str(self.root / "missing.toml"), "--state-dir",
                                 str(self.updates.root), "update-check"],
                                capture_output=True, text=True, check=True)
        self.assertTrue(json.loads(result.stdout)["cached"])
