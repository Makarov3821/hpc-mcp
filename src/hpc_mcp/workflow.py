"""Explicitly authorized, durable release of independent prepared runs."""

from contextlib import ExitStack
from dataclasses import asdict, dataclass
import json
import time
import uuid

from .history import now
from .jobs import relative_path
from .monitor import state_lock
from .onboarding_store import digest


@dataclass(frozen=True)
class WorkflowLimits:
    max_in_flight: int = 2
    submissions_per_minute: int = 10
    max_submissions_per_tick: int = 1
    max_status_commands: int = 20
    max_status_jobs: int = 50
    poll_interval_seconds: int = 60
    max_submit_attempts: int = 3
    max_handoff_bytes: int = 1073741824

    def __post_init__(self):
        for key, value in asdict(self).items():
            upper = 86400 if key == 'poll_interval_seconds' else (1 << 40 if key == 'max_handoff_bytes' else 500)
            lower = 5 if key == 'poll_interval_seconds' else 1
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(f'{key} must be {lower}..{upper}')


def initialize(history):
    with history.connect() as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS workflows (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS workflow_claims (
                run_id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, task_id TEXT NOT NULL);
        ''')


def submission_guard(history, run_id, token):
    with history.connect() as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_claims'").fetchone()
        if not exists:
            return
        row = db.execute('SELECT workflow_id,task_id FROM workflow_claims WHERE run_id=?', (run_id,)).fetchone()
        if not row:
            return
        workflow = json.loads(db.execute('SELECT data FROM workflows WHERE id=?', (row[0],)).fetchone()[0])
    task = workflow['tasks'][row[1]]
    if not workflow['enabled'] or task.get('reservation') != token or not token or task['run_id'] != run_id:
        raise ValueError('run belongs to a workflow; release it through the confirmed workflow, not job_submit')


class WorkflowService:
    def __init__(self, jobs, clock=time.time, stopping=None):
        self.jobs, self.clock = jobs, clock
        self.stopping = stopping or (lambda: False)
        initialize(jobs.history)

    def _save(self, value):
        with self.jobs.history.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = json.loads(db.execute('SELECT data FROM workflows WHERE id=?', (value['workflow_id'],)).fetchone()[0])
            value['enabled'] = existing['enabled']
            value['confirmation'] = existing['confirmation']
            db.execute('UPDATE workflows SET data=? WHERE id=?', (json.dumps(value), value['workflow_id']))

    def _control(self, workflow_id, **changes):
        with self.jobs.history.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            value = json.loads(db.execute('SELECT data FROM workflows WHERE id=?', (workflow_id,)).fetchone()[0])
            value.update(changes)
            db.execute('UPDATE workflows SET data=? WHERE id=?', (json.dumps(value), workflow_id))

    def get(self, workflow_id):
        with self.jobs.history.connect() as db:
            row = db.execute('SELECT data FROM workflows WHERE id=?', (workflow_id,)).fetchone()
        if row is None:
            raise ValueError('unknown workflow')
        value = json.loads(row[0])
        if digest(value['plan']) != value['review_token']:
            raise ValueError('workflow plan checksum mismatch')
        return {'ok': True, 'workflow': value}

    def list(self, limit=50, offset=0):
        if type(limit) is not int or not 1 <= limit <= 500 or type(offset) is not int or offset < 0:
            raise ValueError('limit must be 1..500; offset nonnegative')
        with self.jobs.history.connect() as db:
            rows = db.execute('SELECT id FROM workflows ORDER BY rowid DESC LIMIT ? OFFSET ?', (limit, offset)).fetchall()
        values = [self.get(r[0])['workflow'] for r in rows]
        return {'ok': True, 'workflows': [{'workflow_id': v['workflow_id'], 'name': v['plan']['name'],
            'enabled': v['enabled'], 'created_at': v['created_at'], 'task_count': len(v['tasks']),
            'stages': {stage: sum(t['stage'] == stage for t in v['tasks'].values())
                       for stage in sorted({t['stage'] for t in v['tasks'].values()})}} for v in values]}

    def plan(self, name, run_ids, dependencies=None, limits=None):
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ValueError('workflow name must be nonempty and at most 100 characters')
        if (not isinstance(run_ids, list) or not 1 <= len(run_ids) <= 500
                or any(not isinstance(r, str) for r in run_ids) or len(set(run_ids)) != len(run_ids)):
            raise ValueError('run_ids must contain 1..500 unique prepared runs')
        try:
            policy = WorkflowLimits(**(limits or {}))
        except TypeError as exc:
            raise ValueError(str(exc)) from exc
        runs = {rid: self.jobs.history.get(rid) for rid in run_ids}
        for run in runs.values():
            self.jobs._cluster(run)
            if run['phase'] != 'prepared' or run.get('job_id'):
                raise ValueError('only unsubmitted prepared runs can enter a new workflow')
        edges = [] if dependencies is None else dependencies
        if not isinstance(edges, list) or len(edges) > 1000:
            raise ValueError('dependencies must contain at most 1000 edges')
        parents = {rid: [] for rid in run_ids}
        for edge in edges:
            if not isinstance(edge, dict) or set(edge) - {'run_id', 'parent_run_id', 'condition', 'files'}:
                raise ValueError('dependency accepts run_id,parent_run_id,condition,files')
            child, parent = edge.get('run_id'), edge.get('parent_run_id')
            condition = edge.get('condition', 'scheduler_succeeded')
            if child not in runs or child == parent or condition not in ('scheduler_succeeded', 'application_succeeded', 'files_ready'):
                raise ValueError('invalid child, self-dependency or dependency condition')
            parent_run = self.jobs.history.get(parent)
            self.jobs._cluster(parent_run)
            if parent not in runs and parent_run['phase'] != 'submitted':
                raise ValueError('external parents must already be confirmed submitted')
            if condition == 'application_succeeded' and parent_run.get('application', {}).get('kind') != 'gaussian':
                raise ValueError('application_succeeded currently requires a prepared Gaussian parent')
            files = edge.get('files', [])
            if not isinstance(files, list) or len(files) > 64 or (condition == 'files_ready' and not files):
                raise ValueError('files_ready requires explicit file mappings, at most 64')
            canonical = []
            for mapping in files:
                if not isinstance(mapping, dict) or set(mapping) != {'source', 'target'}:
                    raise ValueError('file mapping requires source and target relative paths')
                source = relative_path(mapping['source'])
                if any(c in source for c in '*?[]'):
                    raise ValueError('handoff source must be a literal path, not an rsync pattern')
                target = relative_path(mapping['target'])
                if target.startswith(('.hpc-mcp-', '.xn02-')):
                    raise ValueError('reserved handoff target')
                if any(e['path'] == target or e['path'].startswith(target + '/') or target.startswith(e['path'] + '/')
                       for e in runs[child]['manifest']):
                    raise ValueError('handoff cannot overwrite a prepared input or script')
                canonical.append({'source': source, 'target': target})
            parents[child].append({'parent_run_id': parent, 'condition': condition, 'files': canonical})
        # Kahn traversal avoids recursion limits on long chains.
        pending = set(run_ids)
        while pending:
            ready = {rid for rid in pending if not any(e['parent_run_id'] in pending for e in parents[rid])}
            if not ready:
                raise ValueError('dependency graph contains a cycle')
            pending -= ready
        for rid, requirements in parents.items():
            targets = [m['target'] for e in requirements for m in e['files']]
            if len(set(targets)) != len(targets) or any(a.startswith(b + '/') for a in targets for b in targets if a != b):
                raise ValueError('duplicate or overlapping handoff targets')
        plan = {'name': name, 'run_ids': run_ids, 'dependencies': parents, 'limits': asdict(policy),
                'runs': {rid: {'cluster_config': r['cluster_config'], 'manifest': r['manifest'],
                               'command': r['command'], 'outputs': r['outputs'],
                               'generation': r.get('generation'), 'profile': r.get('profile')} for rid, r in runs.items()}}
        workflow_id = 'w_' + uuid.uuid4().hex
        value = {'workflow_id': workflow_id, 'created_at': now(), 'plan': plan, 'review_token': digest(plan),
                 'enabled': False, 'next_tick_at': 0, 'submission_times': [], 'confirmation': None,
                 'tasks': {rid: {'source_run_id': rid, 'run_id': rid, 'stage': 'waiting', 'attempts': 0} for rid in run_ids}}
        if len(json.dumps(value).encode()) > 4 * 1024 * 1024:
            raise ValueError('workflow plan exceeds 4 MiB')
        with state_lock(self.jobs.history, 'workflow-dispatch.lock'):
            with ExitStack() as locks, self.jobs.history.connect() as db:
                for rid in sorted(run_ids):
                    locks.enter_context(self.jobs.history.lock(rid))
                db.execute('BEGIN IMMEDIATE')
                for rid in run_ids:
                    if self.jobs.history.get(rid)['phase'] != 'prepared':
                        raise ValueError('run changed during workflow planning')
                    if db.execute('SELECT 1 FROM workflow_claims WHERE run_id=?', (rid,)).fetchone():
                        raise ValueError('run is already owned by a workflow')
                    db.execute('INSERT INTO workflow_claims VALUES (?,?,?)', (rid, workflow_id, rid))
                db.execute('INSERT INTO workflows VALUES (?,?)', (workflow_id, json.dumps(value)))
        return self.get(workflow_id)

    def start(self, workflow_id, review_token, confirmation_note):
        if not isinstance(confirmation_note, str) or not confirmation_note.strip() or len(confirmation_note) > 4096:
            raise ValueError('record explicit user authorization for this automatic submission plan')
        value = self.get(workflow_id)['workflow']
        if value['review_token'] != review_token:
            raise ValueError('workflow review token differs')
        for task in value['tasks'].values():
            self.jobs._cluster(self.jobs.history.get(task['run_id']))
        self._control(workflow_id, enabled=True, confirmation={'time': now(), 'note': confirmation_note})
        return self.get(workflow_id)

    def pause(self, workflow_id):
        self.get(workflow_id)
        self._control(workflow_id, enabled=False)
        return self.get(workflow_id)

    def retry(self, workflow_id, task_ids):
        if not isinstance(task_ids, list) or not task_ids or len(task_ids) > 500:
            raise ValueError('provide explicit task_ids to retry')
        with state_lock(self.jobs.history, 'workflow-dispatch.lock'):
            value = self.get(workflow_id)['workflow']
            for task_id in task_ids:
                if task_id not in value['tasks']:
                    raise ValueError('unknown workflow task')
                task = value['tasks'][task_id]
                run = self.jobs.history.get(task['run_id'])
                if run['phase'] not in ('prepared', 'uploading', 'upload_failed'):
                    raise ValueError('retry only unsubmitted prepared/upload tasks; rejected/failed jobs need a new plan')
                task.update(stage='waiting', attempts=0, last_error=None)
            value['dependency_syncs'] = {}
            value['next_tick_at'] = 0
            self._save(value)
        return self.get(workflow_id)

    def tick(self, workflow_id, max_status_commands=None):
        from .workflow_engine import advance
        with state_lock(self.jobs.history, 'workflow-dispatch.lock', blocking=False):
            value = self.get(workflow_id)['workflow']
            if not value['enabled'] or self.clock() < value['next_tick_at']:
                return {'ok': True, 'workflow': value, 'deferred': True, 'status_commands': 0}
            limits = WorkflowLimits(**value['plan']['limits'])
            value['next_tick_at'] = self.clock() + limits.poll_interval_seconds
            self._save(value)
            return advance(self, value, limits, max_status_commands)

    def next_due(self):
        with self.jobs.history.connect() as db:
            row = db.execute("SELECT id FROM workflows WHERE json_extract(data,'$.enabled')=1 "
                "AND json_extract(data,'$.next_tick_at')<=? ORDER BY json_extract(data,'$.next_tick_at'),rowid LIMIT 1",
                (self.clock(),)).fetchone()
        return row[0] if row else None
