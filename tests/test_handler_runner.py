"""Real namespace/seccomp boundary tests; no credentials, SSH or network services."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from hpc_mcp.handler_runner import HandlerRunner, RunnerError


class HandlerRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.state = self.root / 'state'
        self.runner = HandlerRunner(self.state)
        self.plugin = self.root / 'plugin'; self.plugin.mkdir()
        self.project = self.root / 'project'; self.project.mkdir()
        self.card = self.project / 'water.gjf'; self.card.write_text('original card')
        self.secret = self.root / '.ssh' / 'id_ed25519'; self.secret.parent.mkdir(); self.secret.write_text('dummy secret')
        self.manifest = {'timeout_seconds': 2, 'max_output_bytes': 8192, 'dependencies': []}
        self.request = {'input_path': str(self.card), 'input_dir': str(self.project),
                        'input_name': self.card.name, 'parameters': {}, 'remote_dir': '/tmp/remote/water'}

    def run_code(self, code, request=None):
        (self.plugin / 'handler.py').write_text('import json,os,sys\nfrom pathlib import Path\nr=json.load(sys.stdin)\n' + code)
        raw, permissions = self.runner.run(self.manifest, self.plugin, request or self.request)
        return json.loads(raw), permissions

    def test_actual_capability_probe_and_minimal_environment(self):
        status = self.runner.describe(True)
        self.assertTrue(status['available'], status.get('diagnostic'))
        with patch.dict('os.environ', {'HPC_TEST_SECRET': 'never inherit', 'SSH_AUTH_SOCK': '/dummy/socket'}):
            result, permissions = self.run_code('print(json.dumps(dict(os.environ)))')
        self.assertNotIn('HPC_TEST_SECRET', result); self.assertNotIn('SSH_AUTH_SOCK', result)
        self.assertEqual(result['HOME'], '/nonexistent'); self.assertEqual(result['TMPDIR'], '/staging')
        self.assertFalse(permissions['permissions']['network'])

    def test_only_selected_input_is_readable_and_never_writable(self):
        (self.project / 'other.txt').write_text('another task')
        code = f'''assert Path(r['input_path']).read_text() == 'original card'
for name in [{str(self.secret)!r}, str(Path(r['input_dir'])/'other.txt'), '/etc/passwd']:
    try: Path(name).read_text()
    except OSError: pass
    else: raise AssertionError('sensitive read allowed: '+name)
for name in [r['input_path'], __file__, {str(self.plugin / 'handler.py')!r}, '/tmp/evil', '/evil']:
    try: Path(name).write_text('changed')
    except OSError: pass
    else: raise AssertionError('write allowed: '+name)
p=Path(r['staging_dir'])/'generated.lsf'; p.write_text('generated')
print(json.dumps({{'script':p.read_text(), 'remote':str(Path(r['remote_dir']).resolve())}}))
'''
        result, _ = self.run_code(code)
        self.assertEqual(result, {'script': 'generated', 'remote': '/tmp/remote/water'})
        self.assertEqual(self.card.read_text(), 'original card')
        self.assertFalse(list(self.state.rglob('.run-*')))

    def test_network_and_background_process_creation_are_denied(self):
        code = '''import socket
for operation in [lambda:socket.socket(), lambda:socket.socket(socket.AF_UNIX), os.fork]:
    try: operation()
    except PermissionError: pass
    else: raise AssertionError('forbidden syscall allowed')
import threading
try: threading.Thread(target=lambda:None).start()
except RuntimeError: pass
else: raise AssertionError('thread creation allowed')
print(json.dumps({'blocked':True}))
'''
        result, _ = self.run_code(code)
        self.assertTrue(result['blocked'])

    def test_hard_memory_and_staging_capacity_limits(self):
        self.runner.configure({'memory_bytes': 64*1024**2, 'staging_bytes': 1024**2}, 'user approved lower limits')
        code = '''try: data=bytearray(128*1024**2)
except MemoryError: pass
else: raise AssertionError('memory budget escaped')
try:
    for n in range(32): (Path(r['staging_dir'])/str(n)).write_bytes(b'x'*65536)
except OSError as exc:
    assert exc.errno == 28, exc
else: raise AssertionError('aggregate staging budget escaped')
print(json.dumps({'limited':True}))
'''
        result, _ = self.run_code(code); self.assertTrue(result['limited'])
        self.assertFalse(list(self.state.rglob('.run-*')))

    def test_timeout_and_output_budgets_reject_execution(self):
        self.manifest['timeout_seconds'] = 1
        with self.assertRaises(RunnerError) as error: self.run_code('import time;time.sleep(3)')
        self.assertEqual(error.exception.code, 'application_timeout')
        with self.assertRaises(RunnerError) as error: self.run_code('print("x"*16384)')
        self.assertEqual(error.exception.code, 'application_output_limit')
        self.assertFalse(list(self.state.rglob('.run-*')))

    def test_unavailable_backend_never_runs_handler_or_falls_back(self):
        with patch('hpc_mcp.handler_runner.shutil.which', return_value=None), \
                patch('hpc_mcp.handler_runner.subprocess.Popen') as launch:
            with self.assertRaises(RunnerError) as error: self.run_code('print("should not run")')
        self.assertEqual(error.exception.code, 'application_isolation_unavailable'); launch.assert_not_called()

    def test_trusted_mode_requires_explicit_configuration_and_reports_downgrade(self):
        with self.assertRaises(ValueError): self.runner.configure({'mode':'trusted'}, '')
        status = self.runner.configure({'mode':'trusted'}, 'user explicitly trusts this handler with host access')
        self.assertFalse(status['permissions']['isolated']); self.assertTrue(status['permissions']['network'])
        result, permissions = self.run_code(f'print(json.dumps(Path({str(self.secret)!r}).read_text()))')
        self.assertEqual(result, 'dummy secret'); self.assertEqual(permissions['backend'], 'trusted-subprocess')
        self.runner.configure({}, 'restore default isolation')
        self.assertEqual(self.runner.settings().mode, 'isolated')

    def test_directory_selection_exclusions_and_symlink_rejection(self):
        hidden = self.project / '.ssh'; hidden.mkdir(); (hidden / 'key').write_text('dummy')
        (self.project / 'other.txt').write_text('chosen task file')
        request = dict(self.request, input_path=str(self.project), input_name=None)
        (self.plugin / 'handler.py').write_text('import json,sys\nfrom pathlib import Path\nr=json.load(sys.stdin)\nassert not (Path(r["input_dir"])/".ssh").exists()\nprint(json.dumps(sorted(p.name for p in Path(r["input_dir"]).iterdir())))')
        raw, _ = self.runner.run(self.manifest, self.plugin, request, excludes=('.ssh',))
        self.assertEqual(json.loads(raw), ['other.txt','water.gjf'])
        (self.project / 'escape').symlink_to(self.secret)
        with self.assertRaises(OSError): self.runner.run(self.manifest, self.plugin, request)
        self.assertEqual(self.secret.read_text(), 'dummy secret')

    def test_snapshot_and_special_file_limits(self):
        self.runner.configure({'input_bytes': 4}, 'user sets tiny read budget')
        with self.assertRaises(RunnerError) as error: self.run_code('print("unused")')
        self.assertEqual(error.exception.code, 'application_input_limit')
        self.runner.configure({}, 'restore defaults')
        fifo = self.project / 'fifo'; __import__('os').mkfifo(fifo)
        with self.assertRaises(ValueError): self.run_code('print("unused")', dict(self.request,input_path=str(fifo)))

    def test_dependency_snapshot_and_pinning(self):
        package = self.root / 'sampledep'; package.mkdir(); (package/'__init__.py').write_text('VALUE=42')
        self.manifest['dependencies']=['sampledep']
        with patch('sys.path', [str(self.root), *sys.path]):
            pinned = self.runner.dependencies(self.manifest)
            result, permissions = self.run_code('import sampledep;print(json.dumps(sampledep.VALUE))')
            self.assertEqual(result,42); self.assertEqual(permissions['dependencies'],pinned)
            (package/'__init__.py').write_text('VALUE=43')
            with self.assertRaises(RunnerError) as error:
                self.runner.run(self.manifest, self.plugin, self.request, expected_dependencies=pinned)
            self.assertEqual(error.exception.code,'application_dependency_changed')

    def test_cli_exposes_configuration_without_cluster_config(self):
        command = [sys.executable, '-m', 'hpc_mcp', '--state-dir', str(self.state), 'handler-runner-status','--no-probe']
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)['settings']['mode'],'isolated')

    def test_snapshot_hash_check_detects_changed_plugin_before_execution(self):
        from hpc_mcp.jobs import file_manifest
        (self.plugin/'handler.py').write_text('print("original")')
        expected = file_manifest(self.plugin)
        (self.plugin/'handler.py').write_text('print("modified")')
        with self.assertRaises(RunnerError) as error:
            self.runner.run(self.manifest,self.plugin,self.request,expected_files=expected)
        self.assertEqual(error.exception.code,'application_changed')

    def test_execution_receipt_uses_frozen_settings_during_configuration_change(self):
        original = self.runner.dependencies
        def change_after_read(*args, **kwargs):
            result = original(*args, **kwargs)
            self.runner.configure({'mode':'trusted'}, 'concurrent explicit configuration')
            return result
        with patch.object(self.runner,'dependencies',side_effect=change_after_read):
            result, receipt = self.run_code('print(json.dumps("done"))')
        self.assertEqual(result,'done')
        self.assertEqual(receipt['settings']['mode'],'isolated')
        self.assertTrue(receipt['permissions']['isolated'])
        self.assertEqual(self.runner.settings().mode,'trusted')

    def test_task_directory_containing_state_cannot_hide_or_replace_handler(self):
        request=dict(self.request,input_path=str(self.root),input_dir=str(self.root),input_name=None)
        result, _ = self.run_code('assert not (Path(r["input_dir"])/"state").exists()\nprint(json.dumps("done"))',request)
        self.assertEqual(result,'done')
