"""Exact per-run permissions shared by direct submission and automatic workflows."""
from dataclasses import asdict
import json
import fnmatch
from pathlib import Path

from .history import now
from .job_scheduler import submit_command
from .onboarding_store import digest


class SubmissionError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def owner(history, run_id):
    with history.connect() as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_claims'").fetchone()
        row = db.execute('SELECT workflow_id,task_id FROM workflow_claims WHERE run_id=?', (run_id,)).fetchone() if exists else None
    return {'workflow_id': row[0], 'task_id': row[1]} if row else None


def sync_policy(jobs, run, values=None):
    """Canonical, local-only permissions including a stable destination template."""
    from .monitor import sync_options_for
    if values is not None and not isinstance(values, dict):
        raise ValueError('sync options must be an object')
    values = dict(values or {})
    stable = values.pop('stable_only', False)
    if type(stable) is not bool:
        raise ValueError('stable_only must be boolean')
    result = sync_options_for(jobs, run, values)
    result['stable_only'] = stable
    if run.get('project_root') and result['destination'] == run['input_dir']:
        result['layout'] = 'direct'
    if result['destination'] is None:
        root = jobs.history.root / run['run_id'] / 'outputs'
        result['destination_template'] = str(root) if result['layout'] == 'direct' else str(root / '<attempt_id>')
    else:
        target = Path(result['destination']); source = Path(run['input_dir'])
        inplace = run.get('project_root') and target == source
        from .sync_support import validate_destination
        validate_destination(jobs.history, run, target, inplace)
        result['destination_template'] = str(target)
    return result


def options_from(policy):
    return {key: value for key, value in policy.items() if key != 'destination_template'}


def automation(jobs, run, options=None):
    from .monitor import cleanup_from
    if options is not None and (not isinstance(options, dict) or set(options) - {
            'auto_submit', 'auto_sync', 'sync_options', 'cleanup_policy', 'remote_cleanup',
            'extra_sync', 'release_policy'}):
        raise ValueError('unknown automation settings; release rules belong in release_policy')
    values = options or {}
    for key in ('auto_submit', 'auto_sync', 'remote_cleanup'):
        if type(values.get(key, False)) is not bool:
            raise ValueError(f'{key} must be boolean')
    additional = values.get('extra_sync', [])
    if not isinstance(additional, list) or len(additional) > 16:
        raise ValueError('extra_sync must contain at most 16 explicit sync policies')
    sync = sync_policy(jobs, run, values.get('sync_options'))
    if values.get('auto_sync'):
        sync['stable_only'] = True
    result = {'auto_submit': values.get('auto_submit', False), 'auto_sync': values.get('auto_sync', False),
            'sync_options': options_from(sync), 'cleanup_policy': cleanup_from(values.get('cleanup_policy')),
            'remote_cleanup': values.get('remote_cleanup', False),
            'extra_sync': [options_from(sync_policy(jobs, run, p)) for p in additional]}
    # Omit the new field for old plans so existing signed tokens remain valid.
    if 'release_policy' in values:
        from .finalization import policy
        result['release_policy'] = policy(values['release_policy'], sync)
    return result


def build(jobs, run, permissions=None, verify_files=True):
    from .jobs import file_manifest, relative_path
    cluster = jobs._cluster(run)
    rid = run['run_id']; script = relative_path(run['script'])
    snapshot = jobs.history.root / rid / 'input'
    if Path(run['snapshot_dir']) != snapshot or snapshot.is_symlink():
        raise SubmissionError('submission_plan_changed', 'Registered input snapshot path changed.')
    remote = cluster.work_root.rstrip('/') + '/' + rid
    command = submit_command(cluster.scheduler, rid, script, remote)
    if run['remote_dir'] != remote or run['command'] != command or run['scheduler'] != cluster.scheduler:
        raise SubmissionError('submission_plan_changed', 'Submission destination or command differs from the prepared run.')
    entry = next((f for f in run['manifest'] if f['path'] == script), None)
    if entry is None:
        raise ValueError('submission script absent from input manifest')
    if verify_files and run['phase'] not in ('submitted', 'submitting', 'submission_unknown', 'rejected'):
        if file_manifest(snapshot) != run['manifest']:
            raise SubmissionError('submission_snapshot_changed', 'Prepared snapshot changed; prepare a new run.')
    previous = run.get('submission_plan') or {}
    text = previous.get('script_text') if run['phase'] in ('submitted', 'submitting', 'submission_unknown', 'rejected') else None
    if text is None and (snapshot / script).is_file():
        if entry['size'] > 1024 ** 2:
            raise ValueError('submission script exceeds 1 MiB plan budget')
        text = (snapshot / script).read_text()
    permissions = automation(jobs, run, permissions)
    result = {'version': 1, 'run_id': rid, 'created_at': run['created_at'],
        'cluster': run['cluster'], 'cluster_settings': asdict(cluster), 'scheduler': run['scheduler'],
        'config_path': str(jobs.config_path) if jobs.config_path else None,
        'remote_dir': remote, 'command': command, 'script': script, 'script_text': text,
        'snapshot_dir': str(snapshot), 'manifest': run['manifest'], 'input_dir': run['input_dir'],
        'project_root': run.get('project_root'), 'input_files': run.get('input_files'),
        'input_exclude': run.get('input_exclude'), 'max_input_bytes': run.get('max_input_bytes'),
        'outputs': run['outputs'], 'output_mode': run.get('output_mode', 'filtered'),
        'output_exclude': run.get('output_exclude', []), 'internal_prefix': jobs._receipt_prefix(run),
        'generation': run.get('generation'), 'template': run.get('template'), 'application': run.get('application'),
        'dependency_materialization': run.get('dependency_materialization'),
        'sync': sync_policy(jobs, run, permissions['sync_options']), 'automation': permissions}
    if 'release_policy' in permissions:
        from .finalization import contract
        result['release_contract'] = contract(jobs, run, permissions['release_policy'])
    if len(json.dumps(result, allow_nan=False).encode()) > 4 * 1024 ** 2:
        raise ValueError('submission plan exceeds 4 MiB')
    return result


def receipt(jobs, run, verify_files=True):
    plan = run.get('submission_plan'); token = run.get('submission_plan_token')
    approved = run.get('submission_authorization')
    if not plan or not approved:
        raise SubmissionError('submission_authorization_required', 'Review job_submission_plan and confirm its exact token with job_authorize first.')
    if digest(plan) != token or approved.get('plan_token') != token or approved.get('run_id') != run['run_id']:
        raise SubmissionError('submission_authorization_invalid', 'Authorization is not bound to this exact run and plan.')
    if digest(build(jobs, run, plan['automation'], verify_files)) != token:
        raise SubmissionError('submission_plan_changed', 'Inputs, configuration, output policy or task changed; review and confirm a new plan.')
    from .approvals import verify_run
    verify_run(jobs.history, run, approved)
    return plan, approved


def plan_locked(jobs, run, permissions=None):
    if run['phase'] in ('abandoned', 'rejected'):
        raise ValueError('abandoned/rejected tasks require a new run')
    plan = build(jobs, run, permissions)
    token = digest(plan)
    changes = {'submission_plan': plan, 'submission_plan_token': token}
    if token != run.get('submission_plan_token'):
        changes['submission_authorization'] = None
    jobs.history.update(run['run_id'], 'submission_plan_frozen', **changes)
    return {'ok': True, 'run_id': run['run_id'], 'plan': plan, 'review_token': token,
            'approval': jobs.history.approvals.describe(),
            'authorized': bool(jobs.history.get(run['run_id']).get('submission_authorization')),
            'notes': ['Use the configured approval mode. client_attested is not identity authentication.',
                      'This plan does not authorize cancelling tasks or changing remote account permissions.']}


def authorize_locked(jobs, run, token, note, workflow_context=None):
    if not isinstance(note, str) or not note.strip() or len(note) > 4096:
        raise ValueError('explicit user confirmation note required')
    plan = run.get('submission_plan')
    if not plan or token != run.get('submission_plan_token') or digest(plan) != token:
        raise SubmissionError('submission_authorization_invalid', 'Confirm the current exact submission plan token.')
    if digest(build(jobs, run, plan['automation'])) != token:
        raise SubmissionError('submission_plan_changed', 'Prepared task or settings changed after plan preview.')
    owned = owner(jobs.history, run['run_id'])
    if owned and run['phase'] not in ('submitted', 'submitting', 'submission_unknown', 'rejected'):
        if not workflow_context or workflow_context.get('workflow_id') != owned['workflow_id']:
            raise SubmissionError('submission_workflow_owned', 'Confirm this task through workflow_review and workflow_start.')
    approved = {'run_id': run['run_id'], 'plan_token': token, 'time': now(), 'note': note,
                'workflow': ({k: v for k, v in workflow_context.items() if k != 'external_approval'}
                             if workflow_context else None)}
    from .approvals import require, verify_run
    proof = (workflow_context or {}).get('external_approval')
    if proof is None:
        proof = require(jobs.history, 'job_authorize', run['run_id'], token)
    if proof:
        approved['external_approval'] = proof
        verify_run(jobs.history, run, approved)
    jobs.history.update(run['run_id'], 'submission_authorized', submission_authorization=approved)
    return {'ok': True, 'run_id': run['run_id'], 'authorization': approved}


def sync_guard(jobs, run, options=None, expected_token=None):
    plan, _ = receipt(jobs, run, verify_files=False)
    if expected_token and expected_token != run['submission_plan_token']:
        raise SubmissionError('submission_plan_changed', 'Queued sync authorization is stale.')
    requested = sync_policy(jobs, run, options)
    policies = [plan['sync']] + [sync_policy(jobs, run, p) for p in plan['automation']['extra_sync']]
    def literal_allowed(name, patterns):
        if any(c in name for c in '*?[]'):
            return name in patterns
        for pattern in patterns:
            if pattern in ('**', '***') or ('/' not in pattern and fnmatch.fnmatchcase(Path(name).name, pattern)):
                return True
            if pattern.endswith('/***') and name.startswith(pattern[:-3]):
                return True
            parts, rules = name.split('/'), pattern.split('/')
            if len(parts) == len(rules) and all(fnmatch.fnmatchcase(p, r) for p, r in zip(parts, rules)):
                return True
        return False
    for policy in policies:
        # Waiting for a terminal state narrows a permitted live-sync scope.
        candidate = dict(requested, stable_only=policy['stable_only'])
        for key in ('max_file_bytes', 'max_total_bytes', 'timeout'):
            if requested[key] <= policy[key]: candidate[key] = policy[key]
        if requested['reserve_bytes'] >= policy['reserve_bytes']:
            candidate['reserve_bytes'] = policy['reserve_bytes']
        if requested['mode'] == policy['mode'] == 'filtered' and all(literal_allowed(n, policy['includes']) for n in requested['includes']):
            candidate['includes'] = policy['includes']
        if candidate == policy and (not policy['stable_only'] or requested['stable_only']):
            return run['submission_plan_token']
    raise SubmissionError('sync_authorization_required', 'Sync selection, destination, overwrite or budgets differ; confirm a new job plan.')


def monitor_guard(jobs, run, policy):
    plan, _ = receipt(jobs, run, verify_files=False)
    permitted = plan['automation']
    if policy['auto_sync']:
        if not permitted['auto_sync'] or sync_policy(jobs, run, policy['sync_options']) != plan['sync']:
            raise SubmissionError('monitor_authorization_required', 'Automatic sync needs an exact approved automation policy.')
    from .monitor import cleanup_from
    if policy['cleanup_policy'] != cleanup_from(None):
        sync_guard(jobs, run, policy['sync_options'])
    if policy['cleanup_policy'] != permitted['cleanup_policy']:
        raise SubmissionError('cleanup_authorization_required', 'Automatic cleanup policy changed; confirm its exact scope.')
    return run['submission_plan_token']


def cleanup_guard(jobs, run, category, age=None, categories=None):
    plan, _ = receipt(jobs, run, verify_files=False)
    permissions = plan['automation']; policy = permissions['cleanup_policy']
    allowed = ((category == 'remote' and permissions['remote_cleanup'])
        or (category == 'cache' and policy['sync_cache_age_seconds'] is not None
            and age >= policy['sync_cache_age_seconds'])
        or (category == 'storage' and set(categories) <= set(policy['storage_categories'])
            and age >= policy['storage_age_seconds']))
    if not allowed:
        raise SubmissionError('cleanup_authorization_required', 'Deletion scope exceeds the confirmed job plan; preview and authorize first.')
    if category != 'remote' and not run.get('retention', {}).get('released'):
        raise SubmissionError('retention_required', 'Task data retained; explicitly release claims before local cleanup.')
    from .finalization import assert_cleanup
    assert_cleanup(jobs, run)
