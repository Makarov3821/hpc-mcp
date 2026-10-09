"""Resource mapping and real Bash execution using local launcher/container stand-ins."""

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from hpc_mcp.scripts import script_generate
from hpc_mcp.templates import TemplateService
import test_jobs


class ExecutionTests(unittest.TestCase):
    def test_uniform_slurm_mpi_layout_and_native_gpu(self):
        spec = {'command': ['solver'], 'resources': {'nodes': 2, 'tasks': 4, 'tasks_per_node': 2,
                'cpus': 8, 'memory_mb': 4096, 'memory_scope': 'per_node',
                'slurm_gpus_per_task': 'a100:1', 'slurm_constraint': 'gpu&fast'},
                'environment': {'OMP_NUM_THREADS': '8'}, 'launcher': {'kind': 'srun'}}
        original = deepcopy(spec)
        rendered = script_generate('slurm', spec)
        for text in ('--nodes=2', '--ntasks=4', '--ntasks-per-node=2', '--cpus-per-task=8',
                     '--gpus-per-task=a100:1', '--mem=4096M', 'exec -- srun --ntasks=4 --cpus-per-task=8 solver'):
            self.assertIn(text, rendered['script'])
        self.assertEqual(spec, original)
        self.assertIn('--gres=gpu:a100:2', script_generate('slurm', {'command': ['solver'],
            'resources': {'slurm_gres': 'gpu:a100:2'}})['script'])

    def test_lsf_slots_and_gpu_scope_remain_site_native(self):
        rendered = script_generate('lsf', {'command': ['solver'], 'resources': {
            'tasks': 8, 'cpus': 2, 'tasks_per_node': 4,
            'lsf_gpu': 'num=1/task:mode=exclusive_process:gmodel=A100'},
            'launcher': {'kind': 'mpirun', 'arguments': ['--map-by', 'slot:PE=2']}})
        self.assertIn('#BSUB -n 16', rendered['script'])
        self.assertIn('span[ptile=8]', rendered['script'])
        self.assertIn('num=1/task', rendered['script'])
        self.assertIn('mpirun -np 8 --map-by slot:PE=2 solver', rendered['script'])
        self.assertTrue(rendered['warnings'])
        self.assertNotIn('span[hosts=1]', rendered['script'])

    def test_invalid_combinations_are_rejected(self):
        cases = [
            ('slurm', {'resources': {'tasks': 2}}),
            ('slurm', {'resources': {'nodes': 2, 'tasks': 3, 'tasks_per_node': 2}, 'launcher': {'kind': 'srun'}}),
            ('slurm', {'resources': {'nodes': 2, 'tasks': 2, 'memory_mb': 1024, 'memory_scope': 'job'}, 'launcher': {'kind': 'srun'}}),
            ('lsf', {'resources': {'nodes': 2, 'tasks': 2}, 'launcher': {'kind': 'mpirun'}}),
            ('lsf', {'launcher': {'kind': 'srun'}}),
            ('slurm', {'resources': {'lsf_gpu': 'num=1'}}),
            ('lsf', {'resources': {'slurm_gres': 'gpu:1'}}),
            ('slurm', {'resources': {'slurm_gres': 'gpu:1', 'slurm_gpus_per_task': '1'}}),
            ('slurm', {'resources': {'slurm_gpus_per_task': '1'}}),
            ('slurm', {'resources': {'slurm_gres': 'gpu:0'}}),
            ('lsf', {'resources': {'lsf_gpu': 'gmodel=A100'}}),
            ('lsf', {'resources': {'lsf_gpu': 'num=1', 'lsf_resource_requirement': 'select[ngpus>0]'}}),
            ('lsf', {'resources': {'lsf_resource_requirement': 'span[hosts=2]'}}),
            ('slurm', {'launcher': {'kind': 'srun', 'arguments': ['--ntasks=99']}}),
            ('slurm', {'launcher': {'kind': 'mpirun', 'arguments': ['-np99']}}),
            ('slurm', {'launcher': {'kind': 'srun', 'arguments': ['--nta=99']}}),
            ('slurm', {'launcher': {'kind': 'mpirun', 'arguments': ['--map-by', 'slot:PE=99']}}),
            ('slurm', {'environment': {'OMP_NUM_THREADS': '99'}}),
            ('slurm', {'container': {'image': '/trusted.sif', 'gpu': 'nv'}}),
            ('slurm', {'scratch': {'root': '/tmp', 'environment_variable': 'HOME'}}),
            ('slurm', {'scratch': {'root': '/tmp', 'environment_variable': 'TMPDIR'}, 'environment': {'TMPDIR': 'elsewhere'}}),
            ('slurm', {'resources': {'nodes': 2, 'tasks': 2}, 'launcher': {'kind': 'srun'}, 'scratch': {'root': '/tmp'}}),
            ('slurm', {'container': {'image': '/trusted.sif', 'binds': [{'source': '/a:b', 'destination': '/a'}]}}),
        ]
        for scheduler, extra in cases:
            with self.subTest(scheduler=scheduler, spec=extra), self.assertRaises(ValueError):
                script_generate(scheduler, {'command': ['true'], **extra})

    def test_scratch_cleanup_preserves_failure_and_outputs_and_can_be_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scratch = root / 'scratch'
            scratch.mkdir()
            for cleanup in (True, False):
                spec = {'command': ['bash', '-c', 'printf scratch > "$TMPDIR/intermediate"; printf output; exit 7'],
                        'stdout': 'results/result.log', 'scratch': {'root': str(scratch), 'cleanup': cleanup}}
                rendered = script_generate('slurm', spec)
                result = subprocess.run(['bash'], input=rendered['script'], cwd=root, text=True, capture_output=True)
                self.assertEqual(result.returncode, 7, result.stderr)
                self.assertEqual((root / 'results/result.log').read_text(), 'output')
                self.assertEqual(len(list(scratch.iterdir())), 0 if cleanup else 1)

    def test_launcher_container_quoting_binds_and_compute_dependency_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            image = root / 'trusted image.sif'
            image.write_text('image')
            scratch = root / 'scratch'
            scratch.mkdir()
            capture = root / 'argv.json'
            # Capture actual argv. Remove launcher options; container stand-in just exits 9.
            (bin_dir / 'srun').write_text('#!/bin/bash\nshift 2\nexec "$@"\n')
            (bin_dir / 'apptainer').write_text('#!/usr/bin/env python3\nimport json,os,sys\n'
                'assert os.environ["APPTAINERENV_TOKEN"] == os.environ["TOKEN"]\n'
                'assert os.environ["APPTAINERENV_TMPDIR"] == os.environ["TMPDIR"]\n'
                'open(os.environ["CAPTURE"],"w").write(json.dumps(sys.argv[1:]))\nsys.exit(9)\n')
            for path in bin_dir.iterdir():
                path.chmod(0o755)
            literal = '$(touch unwanted); quoted value'
            spec = {'command': ['solver', literal], 'resources': {'tasks': 2, 'slurm_gres': 'gpu:1'},
                    'environment': {'TOKEN': literal + ',with comma'},
                    'launcher': {'kind': 'srun'}, 'container': {'image': str(image), 'gpu': 'nv',
                    'binds': [{'source': str(root), 'destination': '/data', 'read_only': True}]},
                    'scratch': {'root': str(scratch)}, 'remote_dependencies': [{'path': str(image), 'kind': 'file'}]}
            rendered = script_generate('slurm', spec)
            env = {**os.environ, 'PATH': str(bin_dir) + ':' + os.environ['PATH'], 'CAPTURE': str(capture)}
            result = subprocess.run(['bash'], input=rendered['script'], cwd=root, env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 9, result.stderr)
            argv = json.loads(capture.read_text())
            self.assertEqual(argv[-3:], [str(image), 'solver', literal])
            self.assertIn(str(root) + ':/data:ro', argv)
            self.assertIn(str(root) + ':' + str(root) + ':rw', argv)
            self.assertIn('--nv', argv)
            self.assertIn('--no-eval', argv)
            self.assertFalse((root / 'unwanted').exists())
            self.assertEqual(list(scratch.iterdir()), [])
            spec['remote_dependencies'][0]['path'] = str(root / 'missing')
            capture.unlink()
            result = subprocess.run(['bash'], input=script_generate('slurm', spec)['script'], cwd=root,
                                    env=env, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(capture.exists())

    def test_checked_in_resource_specs_render_without_running_remote_software(self):
        root = Path(__file__).resolve().parents[1] / 'examples/resources'
        for path in root.glob('*.json'):
            scheduler = 'slurm' if 'slurm' in path.stem else 'lsf'
            result = script_generate(scheduler, json.loads(path.read_text()))
            self.assertTrue(result['ok'])
            syntax = subprocess.run(['bash', '-n'], input=result['script'], text=True, capture_output=True)
            self.assertEqual(syntax.returncode, 0, syntax.stderr)

    def test_template_plan_pins_extended_execution_settings(self):
        fixture = test_jobs.JobLifecycleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        templates = TemplateService(fixture.jobs)
        definition = {'scheduler': 'lsf', 'spec': {'command': ['true'],
            'resources': {'tasks': 2, 'cpus': 2, 'tasks_per_node': 2, 'lsf_gpu': 'num=1/host'},
            'launcher': {'kind': 'mpirun'}, 'container': {'image': '/remote/trusted.sif'},
            'scratch': {'root': '/remote/scratch'}}, 'outputs': ['result.log']}
        templates.template_import('extended', definition)
        plan = templates.template_plan('extended', 'lsf', str(fixture.source))
        self.assertEqual(plan['rendered']['spec']['resources']['tasks'], 2)
        self.assertEqual(plan['rendered']['spec']['scratch']['root'], '/remote/scratch')
        self.assertIn('mpirun -np 2 apptainer exec', plan['rendered']['script'])
        stored = fixture.jobs.history.get(plan['run']['run_id'])
        self.assertEqual(stored['generation']['spec']['resources']['tasks'], 2)
        self.assertEqual(stored['generation']['spec']['container']['image'], '/remote/trusted.sif')


if __name__ == '__main__':
    unittest.main()
