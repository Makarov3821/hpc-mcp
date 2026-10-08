"""History imports and annotations must work before and after Python 3.14."""

import os
from pathlib import Path
import subprocess
import sys
import unittest


class HistoryStartupTests(unittest.TestCase):
    def test_history_import_and_annotation_resolution_in_fresh_process(self):
        source = str(Path(__file__).resolve().parents[1] / "src")
        result = subprocess.run([sys.executable, "-c",
            "from typing import get_type_hints; from hpc_mcp.history import History; "
            "assert get_type_hints(History.events)['return'] == list[dict]; "
            "from hpc_mcp.jobs import JobService"],
            env=dict(os.environ, PYTHONPATH=source), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
