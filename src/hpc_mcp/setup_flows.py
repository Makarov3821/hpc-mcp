"""Durable setup-flow records and narrowly owned workspaces; no automatic execution."""

from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import re
import shutil
import uuid

from .history import now
from .jobs import file_manifest
from .onboarding_store import OnboardingStore, digest


class SetupFlowError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class SetupFlowService:
    def __init__(self, jobs):
        self.jobs, self.history = jobs, jobs.history
        self.root = self.history.root / 'onboarding'
        self.safe(self.root)
        self.root.mkdir(mode=0o700, exist_ok=True)
        with self.history.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS setup_flows (
                    id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL,
                    created_at TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS setup_flow_events (
                    id INTEGER PRIMARY KEY, flow_id TEXT NOT NULL, time TEXT NOT NULL,
                    kind TEXT NOT NULL, detail TEXT NOT NULL);
            ''')

    def safe(self, path):
        path = Path(path)
        if not path.is_relative_to(self.history.root):
            raise SetupFlowError('setup_flow_path_unsafe', 'Path escapes managed state.')
        for part in (path, *path.parents):
            if part.is_symlink():
                raise SetupFlowError('setup_flow_path_unsafe', 'Managed path cannot contain symlinks.')
        return path

    @contextmanager
    def locked(self):
        lock = self.safe(self.history.root / 'onboarding.lock')
        with lock.open('a') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SetupFlowError('setup_flow_busy', 'Another setup operation is running.') from exc
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def _get(self, flow_id):
        if not isinstance(flow_id, str) or not re.fullmatch(r'f_[a-f0-9]{32}', flow_id):
            raise ValueError('invalid setup flow ID')
        with self.history.connect() as db:
            row = db.execute('SELECT data FROM setup_flows WHERE id=?', (flow_id,)).fetchone()
        if not row:
            raise SetupFlowError('setup_flow_unknown', 'Unknown setup flow.')
        record = json.loads(row[0])
        expected = str(self.root / flow_id / 'workspace')
        if record['flow_id'] != flow_id or record['workspace'] != expected:
            raise SetupFlowError('setup_flow_path_unsafe', 'Workspace differs from registered path.')
        return record

    def _save(self, record, kind, **changes):
        revised = dict(record, **changes, revision=record['revision'] + 1, updated_at=now())
        with self.history.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = json.loads(db.execute('SELECT data FROM setup_flows WHERE id=?', (record['flow_id'],)).fetchone()[0])
            if current['revision'] != record['revision']:
                raise SetupFlowError('setup_flow_conflict', 'Flow revision changed; fetch current state.')
            db.execute('UPDATE setup_flows SET data=? WHERE id=?', (json.dumps(revised), record['flow_id']))
            db.execute('INSERT INTO setup_flow_events(flow_id,time,kind,detail) VALUES (?,?,?,?)',
                       (record['flow_id'], now(), kind, json.dumps(changes)))
        return revised

    @staticmethod
    def revision(record, expected):
        if type(expected) is not int or expected != record['revision']:
            raise SetupFlowError('setup_flow_conflict', 'Supply the current revision from setup_flow_get.')

    def owner(self, record, allocate=False):
        directory = self.safe(self.root / record['flow_id'])
        marker = self.safe(directory / 'owner.json')
        expected = {'flow_id': record['flow_id'], 'ownership_token': record['ownership_token']}
        if allocate:
            directory.mkdir(mode=0o700, exist_ok=True)
            if not marker.exists():
                if any(directory.iterdir()):
                    raise SetupFlowError('setup_flow_path_unsafe', 'Unowned nonempty allocation directory.')
                with marker.open('x') as stream:
                    json.dump(expected, stream)
        try:
            actual = json.loads(marker.read_text()) if marker.is_file() else None
        except ValueError as exc:
            raise SetupFlowError('setup_flow_path_unsafe', 'Workspace ownership marker is invalid.') from exc
        if actual != expected:
            raise SetupFlowError('setup_flow_path_unsafe', 'Workspace ownership cannot be verified.')
        return self.safe(Path(record['workspace']))

    def _resume(self, record):
        try:
            if record['state'] == 'allocating':
                workspace = self.owner(record, allocate=True)
                workspace.mkdir(mode=0o700, exist_ok=True)
                record = self._save(record, 'workspace_allocated', state='open', last_error=None)
            elif record['state'] == 'closing':
                record = self._save(record, 'flow_closed', state=record['close_outcome'], pending_action=None, last_error=None)
            elif record['state'] == 'cleaning':
                workspace = self.owner(record)
                if workspace.exists():
                    file_manifest(workspace, hashes=False)
                    shutil.rmtree(workspace)
                if workspace.exists():
                    raise OSError('Workspace deletion did not complete')
                record = self._save(record, 'workspace_cleaned', state='cleaned', last_error=None)
            elif record['state'] == 'open':
                if not self.owner(record).is_dir():
                    raise SetupFlowError('setup_flow_path_unsafe', 'Open workspace is missing or not a directory.')
        except (OSError, ValueError) as exc:
            self._save(record, 'operation_failed', last_error=str(exc)[:4096])
            raise
        return record

    def create(self, kind, request_key, label=''):
        if kind not in ('cluster_setup', 'application_adaptation'):
            raise ValueError('kind must be cluster_setup or application_adaptation')
        if not isinstance(request_key, str) or not request_key.strip() or len(request_key) > 128:
            raise ValueError('request_key must be a nonempty idempotency key, at most 128 characters')
        if not isinstance(label, str) or len(label) > 256:
            raise ValueError('label must be at most 256 characters')
        with self.locked():
            with self.history.connect() as db:
                row = db.execute('SELECT id FROM setup_flows WHERE request_key=?', (request_key,)).fetchone()
                if row:
                    record = self._get(row[0])
                    if (record['kind'], record['label']) != (kind, label):
                        raise SetupFlowError('setup_flow_conflict', 'Idempotency key already has different arguments.')
                else:
                    flow_id = 'f_' + uuid.uuid4().hex
                    record = {'flow_id': flow_id, 'kind': kind, 'label': label, 'request_key': request_key,
                        'created_at': now(), 'updated_at': now(), 'revision': 0, 'state': 'allocating',
                        'workspace': str(self.root / flow_id / 'workspace'), 'ownership_token': uuid.uuid4().hex,
                        'pending_action': 'collect_cluster_inputs' if kind == 'cluster_setup' else 'prepare_application_bundle',
                        'artifacts': [], 'last_error': None}
                    db.execute('INSERT INTO setup_flows VALUES (?,?,?,?)',
                               (flow_id, request_key, record['created_at'], json.dumps(record)))
                    db.execute('INSERT INTO setup_flow_events(flow_id,time,kind,detail) VALUES (?,?,?,?)',
                               (flow_id, now(), 'flow_created', json.dumps({'kind': kind, 'label': label})))
            return {'ok': True, 'flow': self._resume(record)}

    def get(self, flow_id):
        record = self._get(flow_id)
        if record['state'] != 'allocating':
            workspace = self.owner(record)
            if record['state'] == 'open' and not workspace.is_dir():
                raise SetupFlowError('setup_flow_path_unsafe', 'Open workspace is missing or not a directory.')
        return {'ok': True, 'flow': record}

    def list(self, limit=50, offset=0):
        if type(limit) is not int or not 1 <= limit <= 500 or type(offset) is not int or offset < 0:
            raise ValueError('limit must be 1..500; offset nonnegative')
        with self.history.connect() as db:
            rows = db.execute('SELECT data FROM setup_flows ORDER BY created_at DESC,id LIMIT ? OFFSET ?', (limit, offset)).fetchall()
        return {'ok': True, 'flows': [json.loads(row[0]) for row in rows]}

    def events(self, flow_id, limit=100, after_id=0):
        self._get(flow_id)
        if type(limit) is not int or not 1 <= limit <= 500 or type(after_id) is not int or after_id < 0:
            raise ValueError('limit must be 1..500; after_id nonnegative')
        with self.history.connect() as db:
            rows = db.execute('SELECT id,time,kind,detail FROM setup_flow_events WHERE flow_id=? AND id>? ORDER BY id LIMIT ?',
                              (flow_id, after_id, limit)).fetchall()
        return {'ok': True, 'events': [{'id': i, 'time': t, 'kind': k, 'detail': json.loads(d)} for i,t,k,d in rows]}

    def resume(self, flow_id):
        with self.locked():
            return {'ok': True, 'flow': self._resume(self._get(flow_id))}

    def artifacts(self, values):
        if not isinstance(values, list) or not 1 <= len(values) <= 32:
            raise ValueError('completed flows require 1..32 persisted artifact references')
        evidence = []
        for item in values:
            if not isinstance(item, dict): raise ValueError('artifact must be an object')
            if set(item) == {'kind', 'report_id'} and item['kind'] == 'onboarding_report':
                data = OnboardingStore(self.history).get(item['report_id'])
                evidence.append(dict(item, sha256=digest(data)))
            elif set(item) == {'kind', 'application', 'version'} and item['kind'] == 'application_version':
                from .applications import ApplicationService
                data, _ = ApplicationService(self.jobs).record(item['application'], item['version'])
                evidence.append(dict(item, review_token=data['review_token'], status=data['status']))
            else:
                raise ValueError('artifact must reference onboarding_report or application_version')
        return evidence

    def close(self, flow_id, expected_revision, outcome, artifacts=None):
        if outcome not in ('completed', 'abandoned'): raise ValueError('outcome must be completed or abandoned')
        with self.locked():
            record = self._get(flow_id)
            if record['state'] in ('completed', 'abandoned', 'cleaned'):
                if record.get('close_outcome') != outcome:
                    raise SetupFlowError('setup_flow_conflict', 'Flow already has a different closing outcome.')
                if artifacts is not None:
                    recorded = [{k: v for k, v in item.items() if k not in ('sha256', 'review_token', 'status')}
                                for item in record['artifacts']]
                    if artifacts != recorded:
                        raise SetupFlowError('setup_flow_conflict', 'Closed flow artifact references cannot change.')
                return {'ok': True, 'flow': record}
            self.revision(record, expected_revision)
            if record['state'] != 'open':
                raise SetupFlowError('setup_flow_invalid_state', 'Resume interrupted operation before closing.')
            self.owner(record)
            evidence = self.artifacts(artifacts) if outcome == 'completed' else []
            if outcome == 'abandoned' and artifacts:
                raise ValueError('abandon does not attach artifacts or remove installed applications')
            record = self._save(record, 'close_started', state='closing', close_outcome=outcome, artifacts=evidence)
            return {'ok': True, 'flow': self._resume(record)}

    def cleanup(self, flow_id, expected_revision, dry_run=True):
        if type(dry_run) is not bool: raise ValueError('dry_run must be boolean')
        with self.locked():
            record = self._get(flow_id)
            if record['state'] == 'cleaned':
                return {'ok': True, 'flow': record, 'dry_run': dry_run, 'status': 'already_cleaned', 'paths': []}
            self.revision(record, expected_revision)
            if record['state'] not in ('completed', 'abandoned', 'cleaning'):
                raise SetupFlowError('setup_flow_in_use', 'Open setup flows cannot be cleaned; close or abandon explicitly.')
            workspace = self.owner(record)
            files = file_manifest(workspace, hashes=False) if workspace.exists() else []
            receipt = {'paths': [str(workspace)], 'files': files, 'bytes': sum(f['size'] for f in files),
                'preserved': ['flow_history', 'artifact_references', 'installed_applications', 'inputs', 'outputs']}
            if dry_run: return {'ok': True, 'flow': record, 'dry_run': True, **receipt}
            if record['state'] != 'cleaning': record = self._save(record, 'cleanup_started', state='cleaning', pending_action=None)
            return {'ok': True, 'flow': self._resume(record), 'dry_run': False, **receipt}
