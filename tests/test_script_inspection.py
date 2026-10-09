"""Read-only inspection, conservative conversion and scheduler boundary regression tests."""

import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from hpc_mcp.config import Cluster
from hpc_mcp.script_inspection import ScriptInspector
from hpc_mcp.service import ClusterService
from hpc_mcp.ssh import CommandResult
from hpc_mcp.templates import validate_definition


class InspectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'job.sh'

    def inspect(self, text):
        self.path.write_text(text)
        return ScriptInspector().inspect(str(self.path))

    def test_simple_slurm_draft_is_valid_and_has_line_provenance(self):
        report = self.inspect('#!/bin/bash\n#SBATCH -c 4 --mem=2G\n#SBATCH -o old.log\n'
                              'source /opt/site/init.sh\nexport OMP_NUM_THREADS=4\n'
                              'g16 < "input card.gjf" > result.log 2> error.log\n')
        self.assertEqual(report['unresolved'], [])
        draft = report['template_draft']
        self.assertEqual(draft['spec']['resources']['memory_mb'], 2048)
        self.assertEqual(draft['input_files'], ['input card.gjf'])
        validate_definition(draft)
        self.assertTrue(report['requires_review'])
        self.assertEqual(report['commands'][0]['line'], 6)
        self.assertEqual(report['paths'][0]['role'], 'scheduler_output_overridden')
        self.assertEqual(len(report['source']['sha256']), 64)
        self.assertEqual(report['directives'][1]['options'][0]['submission_override'], True)

    def test_slurm_late_directives_ignored_lsf_late_directives_active(self):
        slurm = self.inspect('#SBATCH -c 2\napp > result.log\n#SBATCH -c 8\n')
        self.assertFalse(slurm['directives'][1]['active'])
        self.assertEqual(slurm['template_draft']['spec']['resources']['cpus'], 2)
        lsf = self.inspect('#BSUB -n 2\napp > result.log\n#BSUB -n 8 -q normal\n#BSUB -R "span[hosts=1]"\n')
        self.assertTrue(lsf['directives'][1]['active'])
        self.assertEqual(lsf['template_draft']['spec']['resources']['cpus'], 8)
        validate_definition(lsf['template_draft'])

    def test_static_scheduler_time_is_preserved(self):
        slurm = self.inspect('#SBATCH --time=1-02:03:00\napp > result.log\n')
        lsf = self.inspect('#BSUB -W 26:03\napp > result.log\n')
        for report in (slurm, lsf):
            self.assertEqual(report['extracted_spec']['resources']['time_minutes'], 1563)
            self.assertTrue(report['template_draft'])
        self.assertEqual(self.inspect('#SBATCH --time=10:00\napp > result.log\n')['extracted_spec']['resources']['time_minutes'], 10)
        self.assertIsNone(self.inspect('#SBATCH --time=10:30\napp > result.log\n')['template_draft'])

    def test_unknown_directives_and_variables_are_not_guessed(self):
        for directive in ('#SBATCH --array=1-3', '#SBATCH --cpus-per-task=$N', '#BSUB -M 1024'):
            report = self.inspect(directive + '\napp > result.log\n')
            self.assertIsNone(report['template_draft'])
            self.assertTrue(report['unresolved'])
            self.assertTrue(report['directives'][0]['options'][0]['literal'])

    def test_shell_side_effects_never_execute_and_no_dynamic_draft(self):
        marker = self.root / 'executed'
        for body in (f'app $(touch {marker}) > result.log',
                     f'if true; then touch {marker}; fi',
                     f'cat <<EOF\n#BSUB -n 8\ntouch {marker}\nEOF',
                     'app \\\n --argument > result.log',
                     'cd child\napp > result.log',
                     'app > result.log\nexport X=value',
                     'export X=value\nsource /opt/init.sh\napp > result.log'):
            report = self.inspect('#BSUB -n 1\n' + body + '\n')
            self.assertIsNone(report['template_draft'], body)
            self.assertTrue(report['unresolved'])
            self.assertFalse(marker.exists())

    def test_no_fabricated_outputs_or_interpreter_conversion(self):
        for text in ('#SBATCH -c 1\napp input.gjf\n',
                     '#!/usr/bin/python3\n#BSUB -n 1\napp > result.log\n'):
            self.assertIsNone(self.inspect(text)['template_draft'])

    def test_lsf_long_short_aliases_are_not_split_as_attached_options(self):
        report = self.inspect('#BSUB -n 1 -cwd /old -oo old.log -eo old.err\napp > result.log\n')
        self.assertTrue(report['template_draft'])
        options = report['directives'][0]['options']
        self.assertEqual([o['option'] for o in options], ['-n', '-cwd', '-oo', '-eo'])
        self.assertTrue(all(o['submission_override'] for o in options[1:]))

    def test_numeric_argument_not_mistaken_for_stderr(self):
        report = self.inspect('#BSUB -n 1\necho 2 > result.log\n')
        command = report['commands'][0]
        self.assertEqual(command['argv'], ['echo', '2'])
        self.assertEqual(command['redirections'], {'stdout': 'result.log'})
        self.assertIsNone(self.inspect('#BSUB -n 1\necho ">" > result.log\n')['template_draft'])

    def test_bounds_symlink_encoding_and_cli(self):
        self.path.write_text('#BSUB -n 1\napp > result.log\n')
        with self.assertRaises(ValueError):
            ScriptInspector().inspect(str(self.path), max_bytes=8)
        link = self.root / 'link'
        link.symlink_to(self.path)
        with self.assertRaises(ValueError):
            ScriptInspector().inspect(str(link))
        result = subprocess.run([sys.executable, '-m', 'hpc_mcp', 'script-inspect', str(self.path)],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['template_draft'])
        self.path.write_bytes(b'\xff')
        with self.assertRaises(ValueError):
            ScriptInspector().inspect(str(self.path))

    def test_remote_bounded_read_quotes_path_and_records_source(self):
        class Transport:
            def run(self, cluster, command):
                self.command = command
                return CommandResult(0, base64.b64encode(b'#BSUB -n 2\napp > result.log\n').decode())
        transport = Transport()
        service = ClusterService({'test': Cluster('test', 'ssh-alias', 'lsf', '/work')}, transport)
        report = ScriptInspector(service).inspect('/scripts/a $(touch no).sh', 'test')
        self.assertEqual(report['source']['ssh_host'], 'ssh-alias')
        self.assertIn("'/scripts/a $(touch no).sh'", transport.command)
        self.assertIn('head -c 262145', transport.command)
        self.assertNotIn('source ', transport.command)
        with self.assertRaises(ValueError):
            ScriptInspector(service).inspect('/scripts/job.sh', 'test', 1048576)


if __name__ == '__main__':
    unittest.main()
