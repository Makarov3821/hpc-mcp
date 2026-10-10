"""Cluster onboarding and static script evidence using local scheduler stand-ins."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from hpc_mcp.profiles import ProfileService
from hpc_mcp.service import ClusterService
from hpc_mcp.script_inspection import ScriptInspector
from hpc_mcp.scripts import script_generate
from hpc_mcp.ssh import CommandResult
import test_jobs


@unittest.skipUnless(shutil.which('rsync'), 'local rsync required')
class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_jobs.JobLifecycleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.jobs = self.fixture.jobs
        self.service = ClusterService(self.jobs.clusters, self.fixture.transport)
        self.profiles = ProfileService(self.service, self.jobs)
        (self.fixture.bin / 'bqueues').write_text('#!/bin/bash\ncat <<\'EOF\'\n' +
            (Path(__file__).parent / 'fixtures/bqueues.txt').read_text() + '\nEOF\n')
        (self.fixture.bin / 'bqueues').chmod(0o755)

    def test_alias_probe_is_bounded_no_disk_search_and_survives_restart(self):
        report = self.profiles.probes.probe('alias', scheduler='lsf', work_root=str(self.fixture.remote),
            paths=[str(self.fixture.source / 'data.txt')], module_avail=True, queue_details=True)
        self.assertTrue(report['ok'], report)
        self.assertEqual(report['scheduler_candidates'], ['lsf', 'slurm'])
        self.assertTrue(report['queues']['ok'])
        self.assertEqual(report['shared_storage'], 'unverified')
        self.assertEqual(report['submission_permission'], 'unverified')
        self.assertTrue(report['path_checks'][0]['readable'])
        self.assertEqual(report['modules']['status'], 'unknown')
        commands = self.fixture.transport.commands
        self.assertEqual(sum('module avail' in c for c in commands), 1)
        self.assertFalse(any('find ' in c or 'ls -' in c or 'source ' in c or 'module load ' in c for c in commands))
        restarted = ProfileService(self.service, self.jobs)
        self.assertEqual(restarted.store.get(report['report_id']), report)

    def test_ambiguous_scheduler_and_missing_work_root_remain_explicit(self):
        report = self.profiles.probes.probe('alias', module_avail=False)
        self.assertIsNone(report['scheduler'])
        self.assertEqual(report['needs'], ['scheduler_selection', 'work_root'])
        self.assertEqual(report['modules']['status'], 'not_requested')
        self.assertEqual(len(self.fixture.transport.commands), 1)

    def test_path_quoting_and_invalid_probes(self):
        marker = self.fixture.root / 'must-not-exist'
        malicious = f'/missing/$(touch {marker})'
        report = self.profiles.probes.probe('alias', paths=[malicious], module_avail=False)
        self.assertFalse(report['path_checks'][0]['exists'])
        self.assertFalse(marker.exists())
        for kwargs in ({'paths': ['relative']}, {'paths': ['/x'] * 65}, {'max_module_bytes': 0},
                       {'scheduler': 'torque'}, {'timeout': 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.profiles.probes.probe('alias', **kwargs)

    def test_failed_or_malformed_ssh_evidence_is_persisted_without_followups(self):
        class Transport:
            def __init__(self, result):
                self.result, self.calls = result, 0
            def run(self, cluster, command):
                self.calls += 1
                return self.result
        for result in (CommandResult(255, error='ssh_connection_failed'),
                       CommandResult(0, 'hostname\tx\nhostname\tx\n')):
            transport = Transport(result)
            service = ClusterService({}, transport)
            profiles = ProfileService(service, self.jobs)
            report = profiles.probes.probe('alias')
            self.assertFalse(report['ok'])
            self.assertEqual(transport.calls, 1)
            self.assertEqual(profiles.store.get(report['report_id']), report)

    def test_module_text_is_bounded_and_not_loaded(self):
        original = self.fixture.transport.run
        calls = []
        def run(cluster, command):
            if 'module avail' in command:
                calls.append(command)
                return CommandResult(0, 'openmpi/4.1\n' + 'x' * 3000)
            return original(cluster, command)
        self.fixture.transport.run = run
        report = self.profiles.probes.probe('alias', max_module_bytes=1024)
        self.assertEqual(len(report['modules']['text'].encode()), 1024)
        self.assertTrue(report['modules']['truncated'])
        self.assertEqual(len(calls), 1)
        self.assertNotIn('module load', calls[0])

class StaticEnvironmentTests(unittest.TestCase):
    def test_python_wrapper_constants_parameters_and_side_effects_are_only_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / 'executed'
            source = root / 'wrapper.py'
            source.write_text(f"import os\nos.system('touch {marker}')\nDEFAULT_CPU=28\n"
                "VERSIONS={'AVX2':'/opt/g16'}\nparser.add_argument('--cpu', default=DEFAULT_CPU, type=int)\n"
                "script=f'''#BSUB -n {args.cpu}\nsource /opt/init.sh\nmpirun app'''\n")
            report = ScriptInspector().inspect(str(source))
            self.assertFalse(marker.exists())
            self.assertEqual(report['constants'][0]['value'], 28)
            self.assertEqual(report['parameters'][0]['options'], ['--cpu'])
            self.assertTrue(report['parameters'][0]['dynamic'])
            self.assertTrue(any(e.get('operation') == 'system' for e in report['evidence']))
            self.assertTrue(report['unresolved'])
            self.assertIsNone(report['template_draft'])
            for app, name in (('gaussian', 'qg16'), ('vasp', 'qvasp')):
                report = ScriptInspector().inspect(str(Path(__file__).parents[1] / 'examples/applications' / app / 'original' / name))
                self.assertTrue(report['constants'], name)
                self.assertTrue(report['parameters'], name)

    def test_ordered_setup_executes_only_when_generated_job_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init = root / 'init.sh'
            init.write_text('export X="${X}-source"\nmodule() { printf "%s\\n" "$*"; }\n')
            spec = {'command': ['bash', '-c', 'printf "%s" "$X"'], 'stdout': 'result.log',
                    'setup_steps': [{'kind': 'export', 'name': 'X', 'value': 'before'},
                                    {'kind': 'source', 'path': str(init)},
                                    {'kind': 'module_load', 'modules': ['openmpi/4.1']}]}
            generated = script_generate('lsf', spec)
            self.assertFalse((root / 'result.log').exists())
            result = subprocess.run(['bash'], input=generated['script'], text=True, cwd=root, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / 'result.log').read_text(), 'before-source')
            self.assertIn('load openmpi/4.1', result.stdout)
            with self.assertRaises(ValueError):
                script_generate('lsf', dict(spec, environment={'X': 'competing'}))
            spec['setup_steps'] = [{'kind': 'module_load', 'modules': ['openmpi;touch /tmp/no']}]
            with self.assertRaises(ValueError):
                script_generate('lsf', spec)

    def test_cli_profiles_and_reports_work_before_configuration_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            argv = [sys.executable, '-m', 'hpc_mcp', '--config', str(root / 'absent.toml'),
                    '--state-dir', str(root / 'state')]
            result = subprocess.run(argv + ['profile-list'], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['profiles'], [])
            source = root / 'wrapper.py'
            source.write_text('DEFAULT_QUEUE="normal"\n')
            result = subprocess.run(argv + ['script-inspect', str(source)], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            result = subprocess.run(argv + ['onboarding-report', report['report_id']], text=True, capture_output=True)
            self.assertEqual(json.loads(result.stdout)['report']['source']['sha256'], report['source']['sha256'])
            self.assertFalse((root / 'absent.toml').exists())
