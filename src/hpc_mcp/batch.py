"""Idempotent batch preparation and one confirmed start over existing execution gates."""

from dataclasses import asdict
import fnmatch
import json
from pathlib import Path
import shutil
import uuid

from .applications import ApplicationService
from .history import now
from .jobs import file_manifest
from .lifecycle import LifecycleService
from .monitor import MonitorService, settings_from, state_lock
from .onboarding_store import digest
from .submission import build, receipt
from .workflow import WorkflowLimits, WorkflowService

MAX_TASKS = 500
MAX_PLAN_BYTES = 4 * 1024 * 1024


class BatchError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def encoded(value):
    return json.dumps(value, allow_nan=False).encode()


class BatchService:
    def __init__(self, jobs):
        self.jobs, self.history = jobs, jobs.history
        self.apps = ApplicationService(jobs)
        self.workflows = WorkflowService(jobs)
        self.lifecycles = LifecycleService(jobs)
        self.monitor = MonitorService(jobs)
        with self.history.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS batches '
                '(id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL, data TEXT NOT NULL)')

    def get(self, batch_id):
        with self.history.connect() as db:
            row = db.execute('SELECT data FROM batches WHERE id=?', (batch_id,)).fetchone()
        if not row:
            raise ValueError('unknown batch')
        return json.loads(row[0])

    def save(self, value, **changes):
        value.update(changes, updated_at=now())
        with self.history.connect() as db:
            db.execute('UPDATE batches SET data=? WHERE id=?',
                (json.dumps(value), value['batch_id']))

    def lock(self, batch_id):
        return state_lock(self.history, 'batch-' + self.get(batch_id)['batch_id'] + '.lock')

    def normalize(self, name, cluster, project_root, tasks, automation, limits):
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ValueError('name must contain 1..100 characters')
        project = Path(project_root).expanduser().resolve()
        if not project.is_dir() or project.is_relative_to(self.history.root):
            raise ValueError('project_root must be an existing project outside state')
        if not isinstance(tasks, list) or not 1 <= len(tasks) <= MAX_TASKS:
            raise ValueError('tasks must contain 1..500 explicit tasks')
        if automation is not None and not isinstance(automation, dict):
            raise ValueError('automation must be an object')
        permissions = {'auto_submit': True, 'auto_sync': True, **(automation or {})}
        if limits is not None and not isinstance(limits, dict):
            raise ValueError('limits must be an object')
        result, ids, inputs = [], set(), set()
        for index, original in enumerate(tasks):
            if not isinstance(original, dict):
                raise ValueError('each task must be an object')
            task = dict(original)
            tid = task.setdefault('task_id', f't{index + 1:04d}')
            if not isinstance(tid, str) or not tid.strip() or len(tid) > 100 or tid in ids:
                raise ValueError('task_id must be unique and contain 1..100 characters')
            ids.add(tid)
            app = 'application' in task
            allowed = ({'application', 'input_path', 'parameters', 'version'} if app else
                {'input_dir', 'script', 'outputs', 'input_files', 'output_exclude',
                 'input_exclude', 'max_input_bytes'}) | {'task_id', 'automation'}
            if set(task) - allowed:
                raise ValueError('unknown task fields or mixed application/script task')
            if app:
                self.apps.name(task['application'])
                if task.get('parameters') is not None and not isinstance(task['parameters'], dict):
                    raise ValueError('application parameters must be an object')
                version = task.get('version')
                if version is not None and (type(version) is not int or version < 1):
                    raise ValueError('application version must be a positive integer')
            field = 'input_path' if app else 'input_dir'
            if not isinstance(task.get(field), str) or not task[field]:
                raise ValueError(f'{field} must be an explicit path')
            original_path = Path(task[field]).expanduser()
            if original_path.is_symlink():
                raise ValueError('task input cannot be a symlink')
            path = original_path.resolve()
            if not path.is_relative_to(project):
                raise ValueError('all task inputs must be within project_root')
            task[field] = str(path)
            identity = (str(path), task.get('script') if not app else None)
            if identity in inputs:
                raise ValueError('duplicate task input; use a separate batch for repeated calculations')
            inputs.add(identity)
            if not app and (not isinstance(task.get('script'), str) or
                            not isinstance(task.get('outputs'), list) or not task['outputs']):
                raise ValueError('script tasks require script and explicit nonempty outputs')
            overrides = task.get('automation', {})
            if not isinstance(overrides, dict):
                raise ValueError('task automation must be an object')
            effective = {**permissions, **overrides}
            if effective.get('auto_submit') is not True or effective.get('auto_sync') is not True:
                raise ValueError('full batches require auto_submit=true and auto_sync=true')
            result.append(task)
        value = {'name': name, 'cluster': cluster, 'project_root': str(project),
            'tasks': result, 'automation': permissions, 'limits': asdict(WorkflowLimits(**(limits or {})))}
        if len(encoded(value)) > 1024 * 1024:
            raise ValueError('batch request exceeds 1 MiB')
        return value

    def plan(self, request_key, name, cluster, project_root, tasks, automation=None,
             limits=None, start_monitor=True, monitor_settings=None, compact=True):
        if not isinstance(request_key, str) or not request_key.strip() or len(request_key) > 200:
            raise ValueError('request_key must contain 1..200 characters')
        if type(start_monitor) is not bool or type(compact) is not bool:
            raise ValueError('start_monitor/compact must be boolean')
        request = self.normalize(name, cluster, project_root, tasks, automation, limits)
        request.update(start_monitor=start_monitor, monitor_settings=monitor_settings)
        # An omitted coordinator configuration is pinned at first creation, not reselected on retry.
        with state_lock(self.history, 'batch-request-' + digest(request_key) + '.lock'):
            with self.history.connect() as db:
                row = db.execute('SELECT data FROM batches WHERE request_key=?', (request_key,)).fetchone()
            if row:
                value = json.loads(row[0])
                if value['request'] != request:
                    raise BatchError('batch_request_conflict', 'This request_key has different inputs or policies.')
            else:
                self.jobs.refresh_clusters()
                if cluster not in self.jobs.clusters:
                    raise ValueError('unknown cluster')
                policy = settings_from(monitor_settings if monitor_settings is not None else
                    self.monitor.runtime().get('settings'))
                value = {'batch_id': 'b_' + uuid.uuid4().hex, 'request_key': request_key,
                    'request': request, 'request_token': digest(request), 'created_at': now(),
                    'stage': 'planning', 'workflow_id': 'w_' + uuid.uuid4().hex,
                    'cluster_settings': asdict(self.jobs.clusters[cluster]),
                    'monitor_settings': asdict(policy), 'review_token': None,
                    'tasks': {t['task_id']: {'run_id': 'r_' + uuid.uuid4().hex,
                        'stage': 'waiting', 'version': t.get('version')} for t in request['tasks']}}
                with self.history.connect() as db:
                    db.execute('INSERT INTO batches VALUES (?,?,?)',
                        (value['batch_id'], request_key, json.dumps(value)))
        with self.lock(value['batch_id']):
            value = self.get(value['batch_id'])
            if value.get('frozen_plan'):
                return self.preview(value, compact)
            self.save(value, stage='planning', last_error=None)
            for task in value['request']['tasks']:
                item = value['tasks'][task['task_id']]
                try:
                    self.prepare(value, task, item)
                    item.update(stage='prepared', error=None)
                except (ValueError, OSError) as error:
                    item.update(stage='prepare_failed', error=str(error)[:4000],
                        error_code=getattr(error, 'code', 'batch_prepare_failed'))
                self.save(value)
            if any(t['stage'] != 'prepared' for t in value['tasks'].values()):
                self.save(value, stage='prepare_failed')
                return self.preview(value, compact)
            try:
                self.jobs.refresh_clusters()
                if digest(asdict(self.jobs.clusters[cluster])) != digest(value['cluster_settings']):
                    raise ValueError('cluster configuration changed while preparing; create a new batch')
                runs = [self.history.get(t['run_id']) for t in value['tasks'].values()]
                scopes = {t['run_id']: self.permissions(value, tid) for tid, t in value['tasks'].items()}
                plans = [build(self.jobs, run, scopes[run['run_id']]) for run in runs]
                self.check_outputs(plans)
                try:
                    workflow = self.workflows.get(value['workflow_id'])['workflow']
                    if workflow['plan']['run_ids'] != [r['run_id'] for r in runs]:
                        raise ValueError('reserved workflow does not match this batch')
                except ValueError as error:
                    if str(error) != 'unknown workflow':
                        raise
                    workflow = self.workflows.plan(value['request']['name'],
                        [r['run_id'] for r in runs], limits=value['request']['limits'],
                        _permissions_by_run=scopes, _workflow_id=value['workflow_id'])['workflow']
                self.freeze(value, workflow)
            except (ValueError, OSError) as error:
                self.save(value, stage='plan_failed', last_error=str(error)[:4000])
            return self.preview(value, compact)

    def permissions(self, value, tid):
        task = next(t for t in value['request']['tasks'] if t['task_id'] == tid)
        return {**value['request']['automation'], **task.get('automation', {})}

    def prepare(self, value, task, item):
        self.jobs.refresh_clusters()
        cluster = value['request']['cluster']
        if cluster not in self.jobs.clusters:
            raise ValueError('cluster was removed while preparing; restore it or create a new batch')
        if digest(asdict(self.jobs.clusters[cluster])) != digest(value['cluster_settings']):
            raise ValueError('cluster configuration changed while preparing; create a new batch')
        context = {'batch_id': value['batch_id'], 'task_id': task['task_id'],
            'request_token': value['request_token']}
        with self.history.connect() as db:
            exists = db.execute('SELECT 1 FROM runs WHERE run_id=?', (item['run_id'],)).fetchone()
        if exists:
            if self.history.get(item['run_id']).get('template', {}).get('batch') != context:
                raise ValueError('reserved run has unrelated provenance')
            return
        root = self.history.root / item['run_id']
        marker = root / 'batch-owner.json'
        if root.exists():
            if root.is_symlink() or marker.is_symlink() or not marker.is_file() or \
                    json.loads(marker.read_text()) != context:
                raise ValueError('unregistered snapshot ownership cannot be verified')
            if set(p.name for p in root.iterdir()) - {'input', 'batch-owner.json'}:
                raise ValueError('unregistered snapshot contains unexpected material')
            file_manifest(root)  # Reject links/special files before deleting only an owned orphan.
            shutil.rmtree(root)
        root.mkdir(mode=0o700)
        marker.write_text(json.dumps(context))
        if 'application' in task:
            if item['version'] is None:
                with self.apps.locked():
                    version = self.apps.read_index().get(task['application'], {}).get('bindings', {}).get(cluster)
                    record, _ = self.apps.record(task['application'], version)
                for candidate in value['request']['tasks']:
                    candidate_item = value['tasks'][candidate['task_id']]
                    if (candidate.get('application') == task['application'] and
                            candidate_item['version'] is None):
                        candidate_item['version'] = record['version']
                self.save(value)
            self.apps.prepare(task['application'], cluster, task['input_path'],
                value['request']['project_root'], task.get('parameters'), item['version'],
                _run_id=item['run_id'], _batch_context=context)
        else:
            options = {k: task[k] for k in ('input_files', 'output_exclude',
                'input_exclude', 'max_input_bytes') if k in task}
            self.jobs.job_prepare(cluster, task['input_dir'], task['script'], task['outputs'],
                project_root=value['request']['project_root'], template_context={'batch': context},
                prepared_run_id=item['run_id'], **options)

    @staticmethod
    def check_outputs(plans):
        def related(a, b):
            return a.is_relative_to(b) or b.is_relative_to(a)
        def wild(pattern):
            return any(c in pattern for c in '*?[]')
        sources = [Path(p['input_dir']) / f['path'] for p in plans for f in p['manifest']]
        for plan in plans:
            target = Path(plan['sync']['destination'])
            patterns = plan['sync']['includes']
            for source in sources:
                if source.is_relative_to(target):
                    name = source.relative_to(target).as_posix()
                    if any(fnmatch.fnmatchcase(name if '/' in p else source.name, p)
                           for p in patterns):
                        raise ValueError('batch outputs could overwrite a prepared input')
        for index, left in enumerate(plans):
            a = Path(left['sync']['destination'])
            for right in plans[index + 1:]:
                b = Path(right['sync']['destination'])
                if not related(a, b):
                    continue
                for p in left['sync']['includes']:
                    for q in right['sync']['includes']:
                        if wild(p) or wild(q) or a / p == b / q:
                            raise ValueError('batch output scopes overlap; use distinct names/directories')

    def freeze(self, value, workflow):
        frozen = {'workflow_plan': workflow['plan'], 'request': value['request'],
            'coordinator': {'start': value['request']['start_monitor'],
                'settings': value['monitor_settings']}}
        if len(encoded(frozen)) > MAX_PLAN_BYTES:
            raise ValueError('batch plan exceeds 4 MiB')
        self.save(value, frozen_plan=frozen, review_token=digest(frozen),
            stage='awaiting_confirmation', last_error=None)

    def preview(self, value, compact=True):
        plans = (value.get('frozen_plan') or {}).get('workflow_plan', {}).get('execution_plans', {})
        tasks, policies = [], {}
        for tid, item in value['tasks'].items():
            plan = plans.get(item['run_id'])
            task = next(t for t in value['request']['tasks'] if t['task_id'] == tid)
            summary = dict(task_id=tid, **item, input=task.get('input_path', task.get('input_dir')))
            if plan:
                policy = {'sync': {k: v for k, v in plan['sync'].items()
                    if k not in ('includes', 'destination', 'destination_template')},
                    'automation': {k: v for k, v in plan['automation'].items() if k != 'sync_options'}}
                policy_id = digest(policy)
                policies[policy_id] = policy
                summary.update(script=plan['script'],
                    script_sha256=next(f['sha256'] for f in plan['manifest'] if f['path'] == plan['script']),
                    inputs=[f for f in plan['manifest'] if f['path'] != plan['script']],
                    remote_dir=plan['remote_dir'], destination=plan['sync']['destination'],
                    outputs=plan['sync']['includes'], policy_id=policy_id,
                    resources=(plan.get('generation') or {}).get('spec', {}).get('resources'),
                    parameters=(plan.get('template') or {}).get('application_plugin', {}).get('parameters'))
            tasks.append(summary)
        result = {'ok': bool(value.get('frozen_plan')), 'batch_id': value['batch_id'],
            'stage': value['stage'], 'review_token': value['review_token'], 'tasks': tasks,
            'policies': policies,
            'approval': self.history.approvals.describe(),
            'last_error': value.get('last_error'), 'coordinator': {'start': value['request']['start_monitor'],
                'settings': value['monitor_settings']}, 'limits': value['request']['limits'],
            'notes': ['No upload or submission during planning. All tasks must prepare successfully.',
                'Confirm this exact batch token once using the configured approval mode.',
                'compact=false returns every frozen script and manifest without regeneration.']}
        if not compact:
            result['plan'] = value.get('frozen_plan')
        return result

    def checked(self, value, token):
        frozen = value.get('frozen_plan')
        if not frozen or token != value['review_token'] or digest(frozen) != token:
            raise BatchError('batch_token_invalid', 'Confirm the current exact batch plan token.')
        workflow = self.workflows.get(value['workflow_id'])['workflow']
        if workflow['plan'] != frozen['workflow_plan']:
            raise BatchError('batch_plan_changed', 'Workflow plan changed; batch_review and confirm again.')
        return workflow

    def start(self, batch_id, review_token, confirmation_note):
        if not isinstance(confirmation_note, str) or not confirmation_note.strip() or len(confirmation_note) > 4096:
            raise ValueError('explicit batch user confirmation note required')
        with self.lock(batch_id):
            value = self.get(batch_id)
            workflow = self.checked(value, review_token)
            if value['stage'] == 'running':
                for item in value['tasks'].values():
                    receipt(self.jobs, self.history.get(item['run_id']))
                return self.status(batch_id)
            from .approvals import require
            proof = require(self.history, 'batch_start', batch_id, review_token)
            if value['stage'] not in ('awaiting_confirmation', 'starting'):
                raise ValueError('use batch_resume for a paused/failed batch or batch_review for a new approval')
            self.save(value, stage='starting', last_error=None)
            try:
                self.workflows.start(value['workflow_id'], workflow['review_token'],
                    confirmation_note, _enable=False, _external_approval=proof)
                paused = [rid for rid, task in workflow['tasks'].items()
                    if task['stage'] == 'paused' and self.history.get(rid)['phase'] in
                    ('prepared', 'uploading', 'upload_failed')]
                if paused:
                    self.workflows.retry(value['workflow_id'], paused)
                for item in value['tasks'].values():
                    self.lifecycles.start(item['run_id'])
                if value['request']['start_monitor']:
                    self.monitor.start(value['monitor_settings'])
                self.workflows._control(value['workflow_id'], enabled=True)
                self.save(value, stage='running', confirmed_at=now())
            except (ValueError, OSError) as error:
                self.workflows.pause(value['workflow_id'])
                self.save(value, stage='starting', last_error=str(error)[:4000])
                raise
        return self.status(batch_id)

    def review(self, batch_id, compact=True):
        with self.lock(batch_id):
            value = self.get(batch_id)
            if not value.get('frozen_plan'):
                raise ValueError('finish batch_plan first')
            self.workflows.pause(value['workflow_id'])
            for item in value['tasks'].values():
                self.lifecycles.pause(item['run_id'])
            workflow = self.workflows.review(value['workflow_id'])['workflow']
            self.check_outputs(list(workflow['plan']['execution_plans'].values()))
            self.freeze(value, workflow)
        return self.preview(value, compact)

    def pause(self, batch_id):
        with self.lock(batch_id):
            value = self.get(batch_id)
            if value.get('frozen_plan'):
                self.workflows.pause(value['workflow_id'])
                with state_lock(self.history, 'workflow-dispatch.lock'):
                    workflow = self.workflows.get(value['workflow_id'])['workflow']
                    for rid, task in workflow['tasks'].items():
                        if self.history.get(rid)['phase'] in ('prepared', 'uploading', 'upload_failed'):
                            task['stage'] = 'paused'
                    self.workflows._save(workflow)
                for item in value['tasks'].values():
                    self.lifecycles.pause(item['run_id'])
            self.save(value, stage='paused')
        return self.status(batch_id)

    def resume(self, batch_id, task_ids=None):
        with self.lock(batch_id):
            value = self.get(batch_id)
            workflow = self.checked(value, value['review_token'])
            if task_ids is None:
                if value['stage'] != 'paused':
                    raise ValueError('provide explicit task_ids after inspecting failed steps')
                task_ids = list(value['tasks'])
            if (not isinstance(task_ids, list) or not 1 <= len(task_ids) <= MAX_TASKS or
                    any(not isinstance(t, str) for t in task_ids) or len(set(task_ids)) != len(task_ids) or
                    any(t not in value['tasks'] for t in task_ids)):
                raise ValueError('provide known task_ids to resume')
            for tid in task_ids:
                receipt(self.jobs, self.history.get(value['tasks'][tid]['run_id']))
            if (workflow.get('confirmation') or {}).get('plan_token') != workflow['review_token']:
                raise ValueError('batch needs new confirmation')
            retry = [value['tasks'][tid]['run_id'] for tid in task_ids
                if self.history.get(value['tasks'][tid]['run_id'])['phase'] in
                ('prepared', 'uploading', 'upload_failed')]
            if retry:
                self.workflows.retry(value['workflow_id'], retry)
            for tid in task_ids:
                self.lifecycles.start(value['tasks'][tid]['run_id'], resume=True)
            if value['request']['start_monitor']:
                self.monitor.start(value['monitor_settings'])
            self.workflows._control(value['workflow_id'], enabled=True)
            self.save(value, stage='running', last_error=None)
        return self.status(batch_id)

    def status(self, batch_id, limit=50, offset=0, include_completed=False):
        if (type(limit) is not int or not 1 <= limit <= 500 or type(offset) is not int or offset < 0
                or type(include_completed) is not bool):
            raise ValueError('limit 1..500, offset >=0 and include_completed boolean')
        value = self.get(batch_id)
        workflow = self.workflows.get(value['workflow_id'])['workflow'] if value.get('frozen_plan') else None
        counts, rows = {}, []
        for tid, item in value['tasks'].items():
            with self.history.connect() as db:
                exists = db.execute('SELECT 1 FROM runs WHERE run_id=?', (item['run_id'],)).fetchone()
            if exists:
                state = self.lifecycles.get(item['run_id'])
                stage = state['lifecycle']['stage']
                wf_task = workflow['tasks'][item['run_id']] if workflow else {}
                if wf_task.get('stage') in ('needs_attention', 'blocked'):
                    stage = wf_task['stage']
                row = {'task_id': tid, 'run_id': item['run_id'], 'stage': stage,
                    'scheduler_state': state['scheduler']['state'], 'job_id': state['scheduler']['job_id'],
                    'application_status': state['application_result']['status'],
                    'sync_state': state['sync']['sync_state'], 'output_dir': state['sync']['output_dir'],
                    'failed_step': state['lifecycle'].get('failed_step'),
                    'error': wf_task.get('last_error') or state['lifecycle'].get('last_error'),
                    'pending_action': state['pending_action']}
                if item.get('error'):
                    row.update(stage=item['stage'], error=item['error'])
                    stage = item['stage']
            else:
                stage = item['stage']
                row = dict(task_id=tid, **item)
            counts[stage] = counts.get(stage, 0) + 1
            if stage != 'complete' or include_completed:
                rows.append(row)
        rows.sort(key=lambda r: (r['stage'] not in ('needs_attention', 'blocked', 'prepare_failed'), r['task_id']))
        overall = 'complete' if counts.get('complete') == len(value['tasks']) else value['stage']
        failures = any(s in counts for s in ('needs_attention', 'blocked', 'prepare_failed'))
        if overall == 'running' and failures:
            overall = 'needs_attention'
        action = {
            'prepare_failed': 'fix_failed_inputs_then_repeat_identical_batch_plan',
            'plan_failed': 'resolve_output_or_policy_conflicts_then_create_new_batch',
            'awaiting_confirmation': 'batch_start_after_user_confirmation',
            'starting': 'inspect_start_error_then_repeat_batch_start',
            'paused': 'batch_resume',
            'needs_attention': 'inspect_failed_tasks_then_batch_resume',
        }.get(overall, 'wait_or_read_batch_status')
        return {'ok': True, 'batch_id': batch_id, 'stage': overall,
            'control_stage': value['stage'],
            'workflow_id': value['workflow_id'], 'review_token': value['review_token'],
            'task_count': len(value['tasks']), 'counts': counts, 'detail_count': len(rows),
            'tasks': rows[offset:offset + limit], 'next_offset': offset + limit if offset + limit < len(rows) else None,
            'last_error': value.get('last_error'),
            'pending_action': action}
