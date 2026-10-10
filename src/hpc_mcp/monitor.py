"""Opt-in durable monitoring policies and detached coordinator lifecycle."""

from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import threading
import time
import uuid

from .config import validate_patterns
from .history import now
from .jobs import relative_path
from .operations import initialize as initialize_operations
from .sync_support import positive


@dataclass(frozen=True)
class MonitorSettings:
    poll_interval_seconds: int = 60
    retry_initial_seconds: int = 60
    retry_max_seconds: int = 3600
    sync_max_attempts: int = 5
    query_concurrency: int = 2
    max_jobs_per_cycle: int = 50
    max_status_commands_per_cycle: int = 20
    max_sync_operations: int = 2
    batch_queries: bool = True
    cleanup_interval_seconds: int = 3600
    notifications_limit: int = 1000
    input_cache_cleanup: dict | None = None

    def __post_init__(self):
        bounds = {'poll_interval_seconds': (5, 86400), 'retry_initial_seconds': (5, 86400),
                  'retry_max_seconds': (5, 604800), 'sync_max_attempts': (1, 100),
                  'query_concurrency': (1, 8), 'max_jobs_per_cycle': (1, 500),
                  'max_status_commands_per_cycle': (1, 500), 'max_sync_operations': (1, 16),
                  'cleanup_interval_seconds': (30, 604800), 'notifications_limit': (10, 10000)}
        for name, (low, high) in bounds.items():
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f'{name} must be {low}..{high}')
        if self.retry_max_seconds < self.retry_initial_seconds:
            raise ValueError('retry_max_seconds cannot be below retry_initial_seconds')
        if type(self.batch_queries) is not bool:
            raise ValueError('batch_queries must be boolean')
        if self.input_cache_cleanup is not None:
            policy = self.input_cache_cleanup
            if not isinstance(policy, dict) or set(policy) - {'older_than_seconds', 'max_cache_bytes'}:
                raise ValueError('input_cache_cleanup accepts older_than_seconds and max_cache_bytes')
            for value in policy.values():
                positive(value, 'input cache retention', True)


def settings_from(values):
    if values is not None and not isinstance(values, dict):
        raise ValueError('monitor settings must be an object')
    try:
        return MonitorSettings(**(values or {}))
    except TypeError as error:
        raise ValueError(str(error)) from error


@contextmanager
def state_lock(history, name, blocking=True):
    path = history.root / name
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('coordinator lock must be a regular file')
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as error:
            raise ValueError('coordinator is already running') from error
        yield


def process_alive(runtime):
    pid = runtime.get('pid')
    token = runtime.get('instance_id')
    if type(pid) is not int or not token:
        return False
    try:
        os.kill(pid, 0)
        arguments = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
        return token.encode() in arguments and b'hpc_mcp.coordinator' in arguments
    except (OSError, ValueError):
        return False


def cleanup_from(policy):
    defaults = {'sync_cache_age_seconds': None, 'storage_categories': [], 'storage_age_seconds': 604800}
    if policy is not None and (not isinstance(policy, dict) or set(policy) - set(defaults)):
        raise ValueError('unknown cleanup policy settings')
    result = {**defaults, **(policy or {})}
    if result['sync_cache_age_seconds'] is not None:
        positive(result['sync_cache_age_seconds'], 'sync_cache_age_seconds', True)
    positive(result['storage_age_seconds'], 'storage_age_seconds', True)
    categories = result['storage_categories']
    if not isinstance(categories, list) or any(not isinstance(c, str) for c in categories) or set(categories) - {'snapshot', 'sync_history', 'old_outputs'}:
        raise ValueError('storage_categories: snapshot, sync_history, old_outputs')
    result['storage_categories'] = sorted(set(categories))
    return result


def sync_options_for(jobs, run, options):
    if options is not None and not isinstance(options, dict):
        raise ValueError('sync_options must be an object')
    values = options or {}
    allowed = {'mode', 'includes', 'excludes', 'destination', 'layout', 'overwrite', 'checksum',
               'compress', 'timeout', 'resume', 'max_file_bytes', 'max_total_bytes', 'reserve_bytes', 'stable_only'}
    if set(values) - allowed:
        raise ValueError('unknown sync options')
    if values.get('stable_only') not in (None, True) or ('stable_only' in values and type(values['stable_only']) not in (bool, type(None))):
        raise ValueError('automatic sync always requires stable_only=true')
    cluster = jobs._cluster(run)
    defaults = {'mode': 'filtered' if values.get('includes') is not None else run.get('output_mode', 'filtered'),
        'includes': run['outputs'], 'excludes': run.get('output_exclude', []),
        'layout': cluster.sync_layout, 'overwrite': cluster.sync_overwrite,
        'checksum': cluster.transfer_checksum, 'compress': cluster.transfer_compress,
        'timeout': cluster.transfer_timeout, 'resume': cluster.sync_resume,
        'max_file_bytes': cluster.max_output_file_bytes, 'max_total_bytes': cluster.max_output_bytes,
        'reserve_bytes': cluster.sync_reserve_bytes, 'destination': run.get('input_dir') if run.get('project_root') else None}
    effective = {key: values.get(key) if values.get(key) is not None else value for key, value in defaults.items()}
    # Reuse the cluster's validators, without any network calls or directory creation.
    replace(cluster, output_mode=effective['mode'], output_include=effective['includes'],
        output_exclude=effective['excludes'], sync_layout=effective['layout'],
        sync_overwrite=effective['overwrite'], transfer_checksum=effective['checksum'],
        transfer_compress=effective['compress'], transfer_timeout=effective['timeout'],
        sync_resume=effective['resume'], max_output_file_bytes=effective['max_file_bytes'],
        max_output_bytes=effective['max_total_bytes'], sync_reserve_bytes=effective['reserve_bytes'])
    validate_patterns(effective['includes'])
    if effective['destination'] is not None:
        if not isinstance(effective['destination'], str) or not effective['destination'] or '\x00' in effective['destination']:
            raise ValueError('destination must be a local directory path')
        effective['destination'] = str(Path(effective['destination']).expanduser().resolve())
    if run.get('project_root') and effective['destination'] == run['input_dir'] and (
            effective['mode'] != 'filtered' or effective['overwrite'] == 'replace'):
        raise ValueError('project return requires filtered mode and error/merge overwrite')
    effective['stable_only'] = True
    return effective


class MonitorService:
    def __init__(self, jobs):
        self.jobs = jobs
        self.history = jobs.history
        initialize_operations(self.history)
        with self.history.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS monitor_runs (run_id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS monitor_runtime (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS monitor_notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT UNIQUE NOT NULL,
                    created_at TEXT NOT NULL, run_id TEXT, kind TEXT NOT NULL, detail TEXT NOT NULL);
            ''')

    def runtime(self):
        with self.history.connect() as db:
            row = db.execute('SELECT data FROM monitor_runtime WHERE id=1').fetchone()
        return json.loads(row[0]) if row else {'state': 'stopped', 'settings': asdict(MonitorSettings())}

    def runtime_update(self, instance_id, **changes):
        with self.history.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM monitor_runtime WHERE id=1').fetchone()
            current = json.loads(row[0]) if row else {}
            if current.get('instance_id') != instance_id:
                return current
            current.update(changes, updated_at=now())
            db.execute('UPDATE monitor_runtime SET data=? WHERE id=1', (json.dumps(current),))
        return current

    def entry(self, run_id):
        with self.history.connect() as db:
            row = db.execute('SELECT data FROM monitor_runs WHERE run_id=?', (run_id,)).fetchone()
        if row is None:
            raise ValueError('run is not registered for monitoring')
        return json.loads(row[0])

    def entries(self):
        with self.history.connect() as db:
            return [json.loads(row[0]) for row in db.execute('SELECT data FROM monitor_runs')]

    def update(self, entry, notification=None, **changes):
        with self.history.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM monitor_runs WHERE run_id=?', (entry['run_id'],)).fetchone()
            current = json.loads(row[0]) if row else {}
            if current.get('generation') != entry['generation'] or not current.get('enabled'):
                return None
            current.update(changes, updated_at=now())
            db.execute('UPDATE monitor_runs SET data=? WHERE run_id=?', (json.dumps(current), entry['run_id']))
            if notification:
                kind, key, detail = notification
                db.execute('INSERT OR IGNORE INTO monitor_notifications(event_key,created_at,run_id,kind,detail) VALUES (?,?,?,?,?)',
                    (key, now(), entry['run_id'], kind, json.dumps(detail)))
        return current

    def watch(self, run_ids, auto_sync=True, sync_options=None, cleanup_policy=None, reset=False):
        if not isinstance(run_ids, list) or not run_ids or len(run_ids) > 500 or any(not isinstance(r, str) for r in run_ids):
            raise ValueError('run_ids must contain 1..500 registered run IDs')
        if type(auto_sync) is not bool or type(reset) is not bool:
            raise ValueError('auto_sync/reset must be boolean')
        cleanup = cleanup_from(cleanup_policy)
        plans = []
        for run_id in dict.fromkeys(run_ids):
            run = self.history.get(run_id)
            if run['phase'] != 'submitted' or not run.get('job_id'):
                raise ValueError('monitoring requires confirmed submitted runs; recover uncertain submissions first')
            plans.append((run_id, {'auto_sync': auto_sync,
                'sync_options': sync_options_for(self.jobs, run, sync_options), 'cleanup_policy': cleanup}))
        result = []
        with state_lock(self.history, 'monitor-control.lock'), self.history.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for run_id, policy in plans:
                row = db.execute('SELECT data FROM monitor_runs WHERE run_id=?', (run_id,)).fetchone()
                previous = json.loads(row[0]) if row else {}
                if previous.get('enabled') and previous.get('policy') == policy and not reset:
                    result.append(previous)
                    continue
                value = {'run_id': run_id, 'generation': uuid.uuid4().hex, 'enabled': True,
                         'policy': policy, 'created_at': now(), 'stage': 'watching', 'next_check_at': 0,
                         'query_failures': 0, 'sync_failures': 0, 'last_error': None}
                db.execute('INSERT OR REPLACE INTO monitor_runs VALUES (?,?)', (run_id, json.dumps(value)))
                result.append(value)
        return {'ok': True, 'monitors': result, 'notes': ['Explicit opt-in only; start the coordinator separately. No submission or remote deletion.',
            'Sync selection, destination, overwrite and limits are fixed here. reset=true re-arms retries; existing sync workers continue.',
            'Cleanup is disabled by default; selected policies may delete snapshots/archives after verified result retention.']}

    def unwatch(self, run_id):
        with state_lock(self.history, 'monitor-control.lock'), self.history.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM monitor_runs WHERE run_id=?', (run_id,)).fetchone()
            if row is None:
                raise ValueError('run is not registered for monitoring')
            value = json.loads(row[0])
            value.update(enabled=False, stage='disabled', updated_at=now())
            db.execute('UPDATE monitor_runs SET data=? WHERE run_id=?', (json.dumps(value), run_id))
        return {'ok': True, 'monitor': value, 'notes': ['Existing sync workers continue; this does not cancel the scheduler job.']}

    def start(self, settings=None, foreground=False):
        requested = settings_from(settings) if settings is not None else None
        with state_lock(self.history, 'monitor-control.lock'):
            current = self.runtime()
            if process_alive(current) or (current.get('state') == 'starting' and not current.get('pid')
                    and time.time() - current.get('started_epoch', 0) < 30):
                if foreground:
                    raise ValueError('coordinator already running; stop it before foreground service startup')
                if requested and asdict(requested) != current['settings']:
                    raise ValueError('stop the coordinator before changing settings')
                return {'ok': True, 'already_running': True, 'runtime': current}
            effective = requested or settings_from(current.get('settings'))
            instance_id = 'mon_' + uuid.uuid4().hex
            value = {'instance_id': instance_id, 'state': 'starting', 'settings': asdict(effective),
                     'clusters': {name: asdict(c) for name, c in self.jobs.clusters.items()},
                     'stop_requested': False, 'started_at': now(), 'started_epoch': time.time()}
            if not value['clusters'] and any(e['enabled'] for e in self.entries()):
                raise ValueError('load the cluster configuration before starting registered monitors')
            with self.history.connect() as db:
                db.execute('INSERT OR REPLACE INTO monitor_runtime VALUES (1,?)', (json.dumps(value),))
            try:
                argv = [sys.executable, '-m', 'hpc_mcp.coordinator', str(self.history.root), instance_id]
                environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
                if foreground:
                    self.runtime_update(instance_id, pid=os.getpid())
                    os.execve(sys.executable, argv, environment)
                process = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                    cwd=str(self.history.root), env=environment)
                self.runtime_update(instance_id, pid=process.pid)
                threading.Thread(target=process.wait, daemon=True).start()
            except OSError as error:
                self.runtime_update(instance_id, state='failed', error=str(error))
                raise
        return {'ok': True, 'runtime': self.runtime()}

    def stop(self):
        with state_lock(self.history, 'monitor-control.lock'):
            runtime = self.runtime()
            if runtime.get('instance_id'):
                self.runtime_update(runtime['instance_id'], stop_requested=True,
                                    state='stopping' if process_alive(runtime) else 'stopped')
        return {'ok': True, 'runtime': self.runtime(),
                'notes': ['Graceful stop waits for bounded status queries; existing sync workers finish independently. Jobs are not cancelled.']}

    def status(self, limit=50, offset=0):
        if type(limit) is not int or not 1 <= limit <= 500 or type(offset) is not int or offset < 0:
            raise ValueError('limit 1..500 and offset >=0')
        runtime = self.runtime()
        alive = process_alive(runtime)
        if not alive and runtime.get('state') in ('running', 'stopping', 'starting'):
            starting = runtime.get('state') == 'starting' and not runtime.get('pid') and time.time() - runtime.get('started_epoch', 0) < 30
            if not starting:
                runtime = {**runtime, 'state': 'interrupted', 'error': 'coordinator is not alive; restart to resume policies'}
        entries = sorted(self.entries(), key=lambda e: (e['created_at'], e['run_id']))
        runtime.pop('clusters', None)
        selected = entries[offset:offset + limit]
        for entry in selected:
            run = self.history.get(entry['run_id'])
            entry['scheduler_observation'] = {key: run.get(key) for key in
                ('cluster', 'job_id', 'state', 'raw_state', 'last_status_query', 'status_query_ok')}
        return {'ok': True, 'runtime': {**runtime, 'alive': alive}, 'total': len(entries),
                'monitors': selected, 'log_path': str(self.history.root / 'monitor.log')}

    def notifications(self, after_id=0, limit=50):
        if type(after_id) is not int or after_id < 0 or type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError('after_id >=0 and limit 1..500')
        with self.history.connect() as db:
            rows = db.execute('SELECT id,created_at,run_id,kind,detail FROM monitor_notifications WHERE id>? ORDER BY id LIMIT ?', (after_id, limit)).fetchall()
            minimum = db.execute('SELECT MIN(id) FROM monitor_notifications').fetchone()[0]
        return {'ok': True, 'notifications': [{'id': r[0], 'created_at': r[1], 'run_id': r[2], 'kind': r[3], 'detail': json.loads(r[4])} for r in rows],
                'next_after_id': rows[-1][0] if rows else after_id,
                'oldest_available_id': minimum, 'cursor_gap': bool(after_id and minimum and after_id < minimum - 1)}


def verified_outputs(run, options):
    if run.get('sync_state') != 'complete' or not run.get('output_dir') or not run.get('output_manifest'):
        return False
    previous = run.get('sync_options', {})
    for key, value in options.items():
        if key not in ('stable_only', 'resume') and value is not None and previous.get(key) != value:
            # Project runs always use direct layout regardless of the inherited layout.
            if key == 'layout' and run.get('project_root') and previous.get('destination') == run['input_dir']:
                continue
            return False
    root = Path(run['output_dir'])
    if not root.is_dir() or root.is_symlink() or root.resolve() != root:
        return False
    for item in run.get('output_manifest', []):
        path = root / relative_path(item['path'])
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            return False
        try:
            with path.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != item['sha256']:
                    return False
        except OSError:
            return False
    return True
