"""Bounded workflow release; dependency files create a new immutable execution snapshot."""

from copy import deepcopy
import hashlib
from pathlib import Path
import shutil
import tempfile
import uuid

from .gaussian import GaussianService
from .jobs import TERMINAL, file_manifest
from .monitor import MonitorSettings
from .monitor_queries import query_runs


def checked_file(run, name):
    record = next((e for e in run.get('output_manifest', []) if e['path'] == name), None)
    if run.get('sync_state') != 'complete' or not run.get('output_dir') or record is None:
        return None
    root = Path(run['output_dir']).resolve()
    path = root / name
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
        return None
    if any(p.is_symlink() for p in path.parents if p != root and p.is_relative_to(root)):
        return None
    with path.open('rb') as stream:
        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
    return (path, record) if actual == record['sha256'] else None


def ensure_outputs(service, value, parent_id, names, download_names):
    run = service.jobs.history.get(parent_id)
    if all(checked_file(run, name) for name in names):
        return True
    transfers = value.setdefault('dependency_syncs', {})
    operation_id = transfers.get(parent_id)
    if operation_id:
        operation = service.jobs.job_sync_operation(operation_id)['operation']
        if operation['state'] in ('queued', 'running'):
            return False
        if operation['state'] != 'succeeded':
            raise ValueError('dependency sync failed or interrupted; inspect and retry explicitly')
        if not all(checked_file(service.jobs.history.get(parent_id), name) for name in names):
            raise ValueError('required dependency outputs absent or changed after sync')
        return True
    started = service.jobs.job_sync_start(parent_id, {'mode': 'filtered', 'includes': download_names,
        'stable_only': True, 'layout': 'direct' if run.get('project_root') else 'snapshot', 'destination': None})
    transfers[parent_id] = started['operation']['operation_id']
    service._save(value)
    return False


def materialize(service, value, task_id, handoffs, limit):
    task = value['tasks'][task_id]
    if task['run_id'] != task['source_run_id'] or not handoffs:
        return
    jobs = service.jobs
    original = jobs.history.get(task['source_run_id'])
    inputs = Path(original['snapshot_dir'])
    if file_manifest(inputs) != original['manifest']:
        raise ValueError('original prepared snapshot changed')
    if sum(record['size'] for _, _, record in handoffs) > limit:
        raise ValueError('dependency handoff exceeds max_handoff_bytes')
    total_bytes = sum(e['size'] for e in original['manifest']) + sum(r['size'] for _, _, r in handoffs)
    if total_bytes > original['max_input_bytes']:
        raise ValueError('materialized input exceeds the prepared max_input_bytes')
    if shutil.disk_usage(tempfile.gettempdir()).free < 2 * total_bytes + jobs._cluster(original).sync_reserve_bytes:
        raise ValueError('insufficient temporary disk space for dependency snapshot')
    materialization = {'workflow_id': value['workflow_id'], 'task_id': task_id,
        'sources': [{'parent_run_id': parent, 'source': record['path'], 'target': target,
                     'sha256': record['sha256'], 'size': record['size']}
                    for parent, target, record in handoffs]}
    # Recover a draft created just before a crash, before registration in workflow state.
    with jobs.history.connect() as db:
        row = db.execute("SELECT run_id FROM runs WHERE json_extract(data,'$.template.workflow_materialization.workflow_id')=? "
            "AND json_extract(data,'$.template.workflow_materialization.task_id')=? ORDER BY created_at LIMIT 1",
            (value['workflow_id'], task_id)).fetchone()
    if row:
        derived = jobs.history.get(row[0])
        if derived['template']['workflow_materialization'] != materialization:
            raise ValueError('dependency outputs changed since materialization; create a new workflow')
        derived = jobs.history.update(derived['run_id'], 'dependency_materialization_recovered',
            input_dir=original['input_dir'], project_root=original.get('project_root'),
            profile=original.get('profile'), dependency_materialization=materialization,
            source_run_id=original['run_id'])
    else:
        with tempfile.TemporaryDirectory(prefix='hpc-mcp-dependency-') as directory:
            local = Path(directory)
            shutil.copytree(inputs, local, dirs_exist_ok=True)
            if file_manifest(local) != original['manifest']:
                raise ValueError('snapshot changed during copy')
            for parent, target, record in handoffs:
                verified = checked_file(jobs.history.get(parent), record['path'])
                if not verified or verified[1]['sha256'] != record['sha256']:
                    raise ValueError('dependency output changed before handoff')
                destination = local / target
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(verified[0], destination)
                with destination.open('rb') as stream:
                    if hashlib.file_digest(stream, 'sha256').hexdigest() != record['sha256']:
                        raise ValueError('dependency changed during handoff')
            template = dict(original.get('template', {}), workflow_materialization=materialization)
            result = jobs.job_prepare(original['cluster'], directory, original['script'], original['outputs'],
                original.get('output_mode', 'filtered'), original.get('output_exclude'), [], original.get('max_input_bytes'),
                input_files=[e['path'] for e in original['manifest']] + [t for _, t, _ in handoffs],
                template_context=template, application_context=deepcopy(original.get('application')),
                generation_context=deepcopy(original.get('generation')))
            derived = jobs.history.update(result['run']['run_id'], 'dependency_snapshot_created',
                input_dir=original['input_dir'], project_root=original.get('project_root'),
                profile=original.get('profile'), dependency_materialization=materialization,
                source_run_id=original['run_id'])
    task['run_id'] = derived['run_id']
    with jobs.history.connect() as db:
        db.execute('INSERT OR IGNORE INTO workflow_claims VALUES (?,?,?)',
                   (task['run_id'], value['workflow_id'], task_id))
    service._save(value)


def advance(service, value, limits, budget_override):
    jobs = service.jobs
    max_commands = limits.max_status_commands if budget_override is None else min(limits.max_status_commands, budget_override)
    if max_commands <= 0:
        return {'ok': True, 'workflow': value, 'deferred': True, 'status_commands': 0}
    ids = {t['run_id'] for t in value['tasks'].values()}
    ids |= {e['parent_run_id'] for edges in value['plan']['dependencies'].values() for e in edges}
    current = {rid: jobs.history.get(rid) for rid in ids}
    # Resolve internal parents to their materialized execution run.
    aliases = {rid: task['run_id'] for rid, task in value['tasks'].items()}
    ids |= set(aliases.values())
    queried = [current[rid] for rid in sorted(ids) if rid in current and current[rid]['phase'] == 'submitted']
    # Rotate the query window: skipped records stay unknown for this cycle and retain capacity.
    cursor = value.get('query_cursor', 0) % max(len(queried), 1)
    rotated = queried[cursor:] + queried[:cursor]
    selected = rotated[:limits.max_status_jobs]
    value['query_cursor'] = cursor + len(selected)
    settings = MonitorSettings(max_status_commands_per_cycle=max_commands, max_jobs_per_cycle=limits.max_status_jobs)
    observations, used = query_runs(jobs, selected, settings)
    value['submission_times'] = [t for t in value['submission_times'] if t > service.clock() - 60]
    fresh_terminal = {rid for rid, result in observations.items() if result.get('ok') and result.get('run', {}).get('state') in TERMINAL}
    for task in value['tasks'].values():
        observed = observations.get(task['run_id'])
        if task['stage'] == 'complete' and observed and observed.get('ok') and observed['run']['state'] not in TERMINAL:
            task['stage'] = 'submitted'
    in_flight = sum(1 for task in value['tasks'].values() if task['stage'] != 'complete' and
        current[task['run_id']]['phase'] in ('submitted', 'submitting', 'submission_unknown', 'uploading')
        and task['run_id'] not in fresh_terminal)
    submitted = 0
    needed = {}
    for task_id, edges in value['plan']['dependencies'].items():
        for edge in edges:
            parent_id = aliases.get(edge['parent_run_id'], edge['parent_run_id'])
            names = [m['source'] for m in edge['files']]
            if edge['condition'] == 'application_succeeded':
                names.append(jobs.history.get(parent_id).get('application', {}).get('log', 'stdout.log'))
            needed.setdefault(parent_id, set()).update(names)
    for task_id, task in value['tasks'].items():
        if service.stopping() or not service.get(value['workflow_id'])['workflow']['enabled']:
            break
        run_id = task['run_id']
        run = jobs.history.get(run_id)
        if run_id in fresh_terminal:
            task.update(stage='complete', scheduler_state=run['state'])
            continue
        if run['phase'] == 'submitted':
            if task['stage'] != 'complete':
                task.update(stage='submitted', scheduler_state=run['state'])
            continue
        if run['phase'] in ('submitting', 'submission_unknown'):
            recovered = jobs.job_recover(run_id)
            task.update(stage='submitted' if recovered['run']['phase'] == 'submitted' else 'needs_attention',
                        last_error=None if recovered.get('ok') else recovered.get('error'))
            continue
        if task['stage'] == 'needs_attention':
            continue
        if run['phase'] == 'rejected':
            task.update(stage='needs_attention', last_error='scheduler rejected submission; prepare a new run')
            continue
        ready, handoffs = True, []
        try:
            for edge in value['plan']['dependencies'][task_id]:
                parent_id = aliases.get(edge['parent_run_id'], edge['parent_run_id'])
                observed = observations.get(parent_id)
                if not observed or not observed.get('ok'):
                    task.update(stage='waiting', last_error='parent status not freshly verified')
                    ready = False
                    break
                parent = jobs.history.get(parent_id)
                if parent['state'] in ('failed', 'cancelled'):
                    task.update(stage='blocked', last_error='parent did not succeed')
                    ready = False
                    break
                if parent['state'] != 'succeeded':
                    task.update(stage='waiting', last_error='parent not complete')
                    ready = False
                    break
                names = [m['source'] for m in edge['files']]
                if edge['condition'] == 'application_succeeded':
                    app_log = parent.get('application', {}).get('log', 'stdout.log')
                    names = sorted(set(names + [app_log]))
                if names and not ensure_outputs(service, value, parent_id, names, sorted(needed[parent_id])):
                    task.update(stage='waiting', last_error='dependency files syncing')
                    ready = False
                    break
                if edge['condition'] == 'application_succeeded':
                    result = GaussianService(jobs).result(parent_id)['application_result']
                    if result['state'] != 'succeeded':
                        task.update(stage='blocked' if result['state'] == 'failed' else 'waiting', last_error='application success unproven')
                        ready = False
                        break
                for mapping in edge['files']:
                    item = checked_file(jobs.history.get(parent_id), mapping['source'])
                    if not item:
                        raise ValueError('required dependency file missing or changed')
                    handoffs.append((parent_id, mapping['target'], item[1]))
            if not ready:
                continue
            task.update(stage='ready', last_error=None)
            reserved_slot = int(run['phase'] == 'uploading' and task.get('reservation') is not None)
            if in_flight - reserved_slot >= limits.max_in_flight or submitted >= limits.max_submissions_per_tick or len(value['submission_times']) >= limits.submissions_per_minute:
                continue
            if task['attempts'] >= limits.max_submit_attempts:
                task.update(stage='needs_attention', last_error='submission attempts exhausted')
                continue
            materialize(service, value, task_id, handoffs, limits.max_handoff_bytes)
            task['reservation'] = uuid.uuid4().hex
            task['attempts'] += 1
            task['stage'] = 'submitting'
            value['submission_times'].append(service.clock())
            service._save(value)  # Reserve capacity/intention before network activity.
            result = jobs.job_submit(task['run_id'], _workflow_token=task['reservation'])
            submitted += 1
            run = result['run']
            if run['phase'] in ('submitted', 'submitting', 'submission_unknown', 'uploading'):
                in_flight += 1 - reserved_slot
            task.update(stage='submitted' if run['phase'] == 'submitted' else 'needs_attention',
                        last_error=None if result.get('ok') else result.get('error'))
        except (OSError, ValueError) as exc:
            task.update(stage='needs_attention', last_error=str(exc)[:4000])
        service._save(value)
    service._save(value)
    live = any(t['stage'] != 'complete' and jobs.history.get(t['run_id'])['phase'] in
               ('submitted', 'submitting', 'submission_unknown', 'uploading') for t in value['tasks'].values())
    if not live and all(task['stage'] in ('complete', 'blocked', 'needs_attention') for task in value['tasks'].values()):
        value['finished_at'] = service.clock()
        service._save(value)
        service._control(value['workflow_id'], enabled=False)
        value['enabled'] = False
    return {'ok': True, 'workflow': value, 'submitted': submitted, 'in_flight': in_flight, 'status_commands': used}
