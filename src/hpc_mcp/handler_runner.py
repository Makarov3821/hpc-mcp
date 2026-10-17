"""Linux generator sandbox. Original wrappers and remote jobs never run here."""
from dataclasses import asdict, dataclass
import ctypes
import ctypes.util
import errno
import fnmatch
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import sysconfig
import tempfile
import time


class RunnerError(ValueError):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.details = code, details


@dataclass(frozen=True)
class RunnerSettings:
    mode: str = 'isolated'
    memory_bytes: int = 256 * 1024 ** 2
    staging_bytes: int = 64 * 1024 ** 2
    input_bytes: int = 64 * 1024 ** 2
    dependency_bytes: int = 32 * 1024 ** 2
    timeout_seconds: int = 30
    max_output_bytes: int = 262144

    def __post_init__(self):
        if self.mode not in ('isolated', 'trusted'):
            raise ValueError('runner mode must be isolated or explicitly trusted')
        for key, low, high in [('memory_bytes', 32 * 1024 ** 2, 4 * 1024 ** 3),
                ('staging_bytes', 4096, 1024 ** 3), ('input_bytes', 1, 1024 ** 3),
                ('dependency_bytes', 1, 256 * 1024 ** 2), ('timeout_seconds', 1, 60),
                ('max_output_bytes', 1024, 1048576)]:
            value = getattr(self, key)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f'{key} must be an integer in {low}..{high}')


# Executed by the isolated interpreter, with hard limits before loading any handler.
_BOOTSTRAP = '''import json,resource,runpy,sys
limits=json.loads(sys.argv[1])
for key,value in limits.items():
    resource.setrlimit(getattr(resource,key),(value,value))
sys.path.insert(0,"/dependencies")
runpy.run_path(sys.argv[2],run_name="__main__")
'''


class HandlerRunner:
    def __init__(self, state_dir):
        self.state = Path(state_dir).resolve()
        self.config = self.state / 'handler-runner.json'

    def settings(self):
        if self.config.is_symlink():
            raise ValueError('runner configuration cannot be a symlink')
        values = json.loads(self.config.read_text()) if self.config.exists() else {}
        if not isinstance(values, dict) or set(values) - set(RunnerSettings.__dataclass_fields__):
            raise ValueError('unknown runner configuration key')
        return RunnerSettings(**values)

    def configure(self, settings, confirmation_note):
        """Full replacement; a permission downgrade must carry explicit user confirmation."""
        if not isinstance(settings, dict):
            raise ValueError('runner settings must be an object')
        if set(settings) - set(RunnerSettings.__dataclass_fields__):
            raise ValueError('unknown runner configuration key')
        configured = RunnerSettings(**settings)
        if not isinstance(confirmation_note, str) or not confirmation_note.strip() or len(confirmation_note) > 4096:
            raise ValueError('user confirmation note required for runner settings')
        self.settings()  # reject existing symlink/invalid configuration
        from .history import History
        from .approvals import require
        from .onboarding_store import digest
        require(History(self.state), 'handler_runner_configure', 'handler-runner',
                digest(asdict(configured)))
        self.state.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', dir=self.state, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(asdict(configured), stream); stream.flush(); os.fsync(stream.fileno())
        try:
            os.replace(temporary, self.config)
        finally:
            temporary.unlink(missing_ok=True)
        return self.describe()

    @staticmethod
    def _filter(stream):
        """libseccomp compiles architecture-aware BPF; no handwritten syscall numbers."""
        library = ctypes.util.find_library('seccomp')
        if not library:
            raise RunnerError('application_isolation_unavailable', 'libseccomp is required; no trusted fallback.')
        lib = ctypes.CDLL(library, use_errno=True)
        lib.seccomp_init.argtypes = [ctypes.c_uint32]; lib.seccomp_init.restype = ctypes.c_void_p
        lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
        lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
        lib.seccomp_export_bpf.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.seccomp_release.argtypes = [ctypes.c_void_p]
        context = lib.seccomp_init(0x7fff0000)  # SCMP_ACT_ALLOW
        if not context:
            raise RunnerError('application_isolation_unavailable', 'Cannot initialize seccomp.')
        try:
            # Exactly one handler process, no threads, networking or namespace/mount escape.
            for name in ('fork', 'vfork', 'clone', 'clone3', 'socket', 'socketpair',
                         'unshare', 'setns', 'mount', 'umount2', 'pivot_root', 'ptrace',
                         'process_vm_readv', 'process_vm_writev', 'bpf', 'io_uring_setup'):
                number = lib.seccomp_syscall_resolve_name(name.encode())
                if number >= 0 and lib.seccomp_rule_add(context, 0x50000 | errno.EPERM, number, 0) != 0:
                    raise RunnerError('application_isolation_unavailable', f'Cannot restrict syscall {name}.')
            if lib.seccomp_export_bpf(context, stream.fileno()) != 0:
                raise RunnerError('application_isolation_unavailable', 'Cannot export seccomp filter.')
            stream.flush(); stream.seek(0)
        finally:
            lib.seccomp_release(context)

    @staticmethod
    def _runtime():
        interpreter = Path(sys.executable).resolve()
        stdlib = Path(sysconfig.get_path('stdlib')).resolve()
        # Do not expose arbitrary virtualenv/project trees as interpreter runtime.
        if not stdlib.is_dir() or not interpreter.is_file():
            raise RunnerError('application_isolation_unavailable', 'Cannot locate Python runtime.')
        command = ['--ro-bind', str(interpreter), str(interpreter), '--ro-bind', str(stdlib), str(stdlib)]
        for path in ('/lib', '/lib64', '/usr/lib', '/usr/lib64'):
            if Path(path).exists():
                command += ['--ro-bind', str(Path(path).resolve()), path]
        # Mask third-party packages even when the distro places them inside /usr/lib.
        for base in {stdlib, Path('/usr/lib/python3/dist-packages'), Path(sysconfig.get_path('purelib'))}:
            if base == stdlib:
                bases = [base / 'site-packages', base / 'dist-packages']
            else:
                bases = [base]
            for path in bases:
                if path.exists():
                    command += ['--tmpfs', str(path), '--remount-ro', str(path)]
        return interpreter, command

    def _command(self, filter_fd, settings, mounts=()):
        bwrap = shutil.which('bwrap')
        if sys.platform != 'linux' or not bwrap:
            raise RunnerError('application_isolation_unavailable', 'Linux and bubblewrap are required; no trusted fallback.')
        interpreter, runtime = self._runtime()
        command = [bwrap, '--unshare-all', '--unshare-user', '--disable-userns', '--assert-userns-disabled',
                   '--die-with-parent', '--new-session', '--cap-drop', 'ALL', '--clearenv',
                   '--setenv', 'LANG', 'C.UTF-8', '--setenv', 'HOME', '/nonexistent',
                   '--setenv', 'TMPDIR', '/staging', '--setenv', 'PATH', '/nonexistent',
                   *runtime, '--dev', '/dev', '--remount-ro', '/dev', '--proc', '/proc',
                   '--remount-ro', '/proc', '--size', str(settings.staging_bytes), '--tmpfs', '/staging',
                   '--dir', '/tmp']
        for source, destination in mounts:
            command += ['--ro-bind', str(source), str(destination)]
        command += ['--remount-ro', '/', '--chdir', '/staging', '--seccomp', str(filter_fd),
                    '--', str(interpreter), '-I', '-S', '-B']
        return command

    def describe(self, probe=False, _settings=None):
        settings = _settings or self.settings()
        result = {'ok': True, 'settings': asdict(settings), 'config_path': str(self.config),
                  'backend': 'bubblewrap+seccomp' if settings.mode == 'isolated' else 'trusted-subprocess',
                  'permissions': {'isolated': settings.mode == 'isolated', 'max_processes': 1 if settings.mode == 'isolated' else None,
                    'input': 'selected read-only snapshots' if settings.mode == 'isolated' else 'host user permissions',
                    'network': False if settings.mode == 'isolated' else True,
                    'writable': '/staging only (plus protocol fds)' if settings.mode == 'isolated' else 'host user permissions',
                    'enforced_limits': ['wall_time', 'protocol_output', 'address_space', 'cpu_time', 'file_size', 'staging_space', 'single_process']
                        if settings.mode == 'isolated' else ['wall_time', 'protocol_output']},
                  'available': None}
        if probe and settings.mode == 'isolated':
            try:
                with tempfile.TemporaryFile() as rules:
                    command = self._command(rules.fileno(), settings)
                    self._filter(rules)
                    process = subprocess.run(command + ['-c', 'print("isolated")'], pass_fds=(rules.fileno(),),
                                             capture_output=True, timeout=5, env={})
                result['available'] = process.returncode == 0 and process.stdout == b'isolated\n'
                if not result['available']:
                    result['diagnostic'] = process.stderr.decode(errors='replace')[:4096]
            except (RunnerError, OSError, subprocess.TimeoutExpired) as exc:
                result.update(available=False, diagnostic=str(exc))
        return result

    @staticmethod
    def _copy(source, target, limit, excludes=(), hidden=()):
        """Snapshot using directory fds so source symlink swaps cannot escape selection."""
        total = 0; entries = 0
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        def visit(fd, dst, relative=''):
            nonlocal total, entries
            info = os.fstat(fd); entries += 1
            if entries > 8192:
                raise RunnerError('application_input_limit', 'Snapshot exceeds 8192 entries.')
            if stat.S_ISDIR(info.st_mode):
                dst.mkdir(parents=True)
                for name in sorted(os.listdir(fd)):
                    child = relative + '/' + name if relative else name
                    if any(fnmatch.fnmatch(child, p) or fnmatch.fnmatch(name, p) for p in excludes):
                        continue
                    if any(source / child == p for p in hidden):
                        continue
                    child_fd = os.open(name, flags, dir_fd=fd)
                    try: visit(child_fd, dst / name, child)
                    finally: os.close(child_fd)
            elif stat.S_ISREG(info.st_mode):
                total += info.st_size
                if total > limit:
                    raise RunnerError('application_input_limit', 'Read-only snapshot exceeds byte budget.')
                dst.parent.mkdir(parents=True, exist_ok=True)
                with dst.open('wb') as out:
                    count = 0
                    while chunk := os.read(fd, min(65536, info.st_size - count + 1)):
                        count += len(chunk)
                        if count > info.st_size:
                            raise ValueError('source changed during snapshot')
                        out.write(chunk)
                    if count != info.st_size or os.fstat(fd).st_mtime_ns != info.st_mtime_ns:
                        raise ValueError('source changed during snapshot')
            else:
                raise ValueError('handler inputs/dependencies must be regular files or directories')
        # Open every path component without following symlinks, including ancestors.
        source = Path(source).absolute()
        descriptor = os.open('/', flags | os.O_DIRECTORY)
        try:
            for component in source.parts[1:]:
                child_fd = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor); descriptor = child_fd
            visit(descriptor, target)
        finally:
            os.close(descriptor)

    def dependencies(self, manifest, destination=None, _settings=None):
        settings = _settings or self.settings()
        resolved = {}; copied_bytes = 0
        stdlib = Path(sysconfig.get_path('stdlib')).resolve()
        for name in manifest.get('dependencies', []):
            spec = importlib.util.find_spec(name)
            if spec is None:
                raise RunnerError('application_dependency_missing', 'Install declared dependencies explicitly.', modules=[name])
            if spec.origin in ('built-in', 'frozen'):
                resolved[name] = {'origin': spec.origin, 'python_version': sys.version}; continue
            if not spec.origin or (spec.submodule_search_locations and len(spec.submodule_search_locations) != 1):
                raise ValueError('namespace dependencies are unsupported; declare concrete packages')
            source = Path(spec.origin).resolve()
            if source.is_relative_to(stdlib) and 'site-packages' not in source.parts and 'dist-packages' not in source.parts:
                resolved[name] = {'origin': str(source), 'stdlib': True, 'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}; continue
            source = source.parent if spec.submodule_search_locations else source
            with tempfile.TemporaryDirectory(prefix='hpc-dependency-') as tmp:
                target = Path(tmp) / (name if source.is_dir() else source.name)
                self._copy(source, target, settings.dependency_bytes - copied_bytes, ('__pycache__', '*.pyc'))
                paths = sorted(target.rglob('*')) if target.is_dir() else [target]
                hashed = hashlib.sha256()
                for item in paths:
                    if item.is_file():
                        content = item.read_bytes(); copied_bytes += len(content)
                        entry = [str(item.relative_to(target) if target.is_dir() else item.name),
                                 len(content), hashlib.sha256(content).hexdigest()]
                        hashed.update(json.dumps(entry, separators=(',', ':')).encode())
                resolved[name] = {'origin': str(source), 'sha256': hashed.hexdigest()}
                if destination is not None:
                    if target.is_dir(): shutil.copytree(target, destination / target.name)
                    else: shutil.copy2(target, destination / target.name)
        return resolved

    def run(self, manifest, path, request, expected_dependencies=None, excludes=(), expected_files=None):
        settings = self.settings()
        timeout = min(settings.timeout_seconds, manifest.get('timeout_seconds', 30))
        limit = min(settings.max_output_bytes, manifest.get('max_output_bytes', 262144))
        self.state.mkdir(parents=True, exist_ok=True)
        run_root = self.state / 'applications'
        if run_root.is_symlink():
            raise ValueError('runner workspace root cannot be a symlink')
        run_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.run-', dir=run_root) as temp:
            workspace = Path(temp); dependencies = workspace / 'dependencies'; dependencies.mkdir()
            pinned = self.dependencies(manifest, dependencies, settings)
            if expected_dependencies is not None and pinned != expected_dependencies:
                raise RunnerError('application_dependency_changed', 'Declared dependency changed; review and reactivate before execution.')
            prepared = dict(request, interface_version=1, staging_dir='/staging')
            with tempfile.TemporaryFile() as rules, tempfile.TemporaryFile() as inp, tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
                mounts = [(dependencies, '/dependencies')]
                if settings.mode == 'isolated':
                    available = self.describe(probe=True, _settings=settings)
                    if not available['available']:
                        raise RunnerError('application_isolation_unavailable', 'Sandbox capability probe failed; no trusted fallback.', diagnostic=available.get('diagnostic'))
                    self._filter(rules)
                    plugin = workspace / 'plugin'
                    self._copy(path, plugin, 2 * 1024 ** 2, ('record.json',))
                    if expected_files is not None:
                        from .jobs import file_manifest
                        actual = [f for f in file_manifest(plugin) if f['path'] != 'record.json']
                        if actual != expected_files:
                            raise RunnerError('application_changed', 'Plugin changed during read-only snapshot.')
                    mounts.append((plugin, '/plugin'))
                    if request.get('input_path'):
                        source = Path(request['input_path'])
                        if not source.is_absolute() or any(p.is_symlink() for p in (source, *source.parents)):
                            raise ValueError('handler input path must be absolute without symlink components')
                        if not source.is_relative_to(self.state) and any(p in {'.ssh', '.aws', '.codex', '.agents'} for p in source.parts):
                            raise ValueError('agent configuration and credential directories cannot be selected as handler inputs')
                        if source == self.state:
                            raise ValueError('the MCP state directory cannot be selected as a handler task')
                        interpreter = Path(sys.executable).resolve()
                        protected = [Path(p) for p in ('/plugin', '/dependencies', '/staging', '/proc', '/dev', '/lib', '/lib64', '/usr/lib', '/usr/lib64')]
                        protected += [interpreter, Path(sysconfig.get_path('stdlib')).resolve()]
                        if any(source.is_relative_to(p) or p.is_relative_to(source) for p in protected):
                            raise ValueError('handler input cannot overlap sandbox or Python runtime paths')
                        target = workspace / 'input'
                        self._copy(source, target, settings.input_bytes, tuple(excludes) +
                            ('.ssh', '.aws', '.codex', '.agents', '.git', 'clusters.toml', 'handler-runner.json'),
                            (self.state, workspace))
                        mounts.append((target, str(source)))
                    limits = {'RLIMIT_AS': settings.memory_bytes, 'RLIMIT_CPU': timeout,
                              'RLIMIT_FSIZE': max(limit, settings.staging_bytes), 'RLIMIT_NOFILE': 64,
                              'RLIMIT_CORE': 0}
                    command = self._command(rules.fileno(), settings, mounts) + ['-c', _BOOTSTRAP, json.dumps(limits), '/plugin/handler.py']
                    environment = {}; fds = (rules.fileno(),)
                else:
                    prepared['staging_dir'] = str(workspace)
                    command = [sys.executable, '-I', '-B', str(path / 'handler.py')]
                    environment = {'LANG': 'C.UTF-8', 'PATH': os.defpath, 'HOME': str(workspace), 'TMPDIR': str(workspace)}
                    fds = ()
                inp.write(json.dumps(prepared, allow_nan=False).encode()); inp.seek(0)
                process = subprocess.Popen(command, stdin=inp, stdout=out, stderr=err, cwd=workspace,
                                           env=environment, pass_fds=fds, start_new_session=True)
                started = time.monotonic(); reason = None
                while process.poll() is None:
                    if time.monotonic() - started > timeout:
                        reason = 'application_timeout'; break
                    if os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size > limit:
                        reason = 'application_output_limit'; break
                    time.sleep(.02)
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                process.wait(); out.seek(0); err.seek(0)
                raw, diagnostic = out.read(limit + 1), err.read(limit + 1)
                if reason or len(raw) + len(diagnostic) > limit:
                    raise RunnerError(reason or 'application_output_limit', 'Handler exceeded execution budget.')
                if process.returncode:
                    raise RunnerError('application_handler_failed', diagnostic.decode(errors='replace')[:4096])
            permissions = self.describe(_settings=settings)
            permissions.update(dependencies=pinned, effective_timeout_seconds=timeout, effective_max_output_bytes=limit)
            return raw, permissions
