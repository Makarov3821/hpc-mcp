"""Approved normal-completion contracts; no age-based inference or implicit deletion."""

import fnmatch
from functools import lru_cache
import json
from pathlib import Path

from .history import now
from .onboarding_store import digest

STRATEGIES = ('manual', 'after_verified_sync', 'after_consumers_complete')


def matches(name, pattern):
    """Root-relative POSIX glob; **/*** may span directories, * never spans '/'."""
    names, rules = name.split('/'), pattern.lstrip('/').rstrip('/').split('/')
    @lru_cache(None)
    def visit(i, j):
        if j == len(rules):
            return i == len(names)
        if rules[j] in ('**', '***'):
            return visit(i, j + 1) or (i < len(names) and visit(i + 1, j))
        return i < len(names) and fnmatch.fnmatchcase(names[i], rules[j]) and visit(i + 1, j + 1)
    return visit(0, 0)


def selected(name, rules):
    """Conservative rsync-rule coverage for literal output paths, including ancestors."""
    for rule in rules:
        rule = rule.rstrip('/')
        if '/' not in rule.strip('/') and not rule.startswith('/'):
            if fnmatch.fnmatchcase(Path(name).name, rule):
                return True
        elif matches(name, rule):
            return True
    return False


def covered(pattern, sync):
    wildcard = any(c in pattern for c in '*?[')
    exclusions = sync['excludes']
    if wildcard and exclusions:
        raise ValueError('wildcard output contract with excludes is ambiguous; use literal paths')
    if not wildcard:
        prefixes = ['/'.join(pattern.split('/')[:n]) for n in range(1, len(pattern.split('/')) + 1)]
        if any(selected(p, exclusions) for p in prefixes):
            return False
    if sync['mode'] == 'all':
        return True
    if not wildcard:
        return selected(pattern, sync['includes'])
    for rule in sync['includes']:
        if rule in ('**', '***', pattern):
            return True
        if rule.endswith('/***') and not any(c in rule[:-4] for c in '*?['):
            if pattern.startswith(rule[:-3]):
                return True
    return False


def policy(values, sync):
    from .jobs import relative_path
    if not isinstance(values, dict) or set(values) - {
            'strategy', 'required_outputs', 'optional_outputs', 'allow_unverified_application'}:
        raise ValueError('release_policy accepts strategy,required_outputs,optional_outputs,allow_unverified_application')
    strategy = values.get('strategy', 'manual')
    if strategy not in STRATEGIES:
        raise ValueError('unknown release strategy')
    allow = values.get('allow_unverified_application', False)
    if type(allow) is not bool:
        raise ValueError('allow_unverified_application must be boolean')
    required, optional = values.get('required_outputs', []), values.get('optional_outputs', [])
    if (not isinstance(required, list) or not isinstance(optional, list)
            or len(required) > 256 or len(optional) > 256):
        raise ValueError('required/optional outputs must be arrays of at most 256 entries')
    normalized = []
    for entry in required:
        if isinstance(entry, str):
            entry = {'pattern': entry, 'min_matches': 1}
        if not isinstance(entry, dict) or set(entry) - {'pattern', 'min_matches'}:
            raise ValueError('required output needs pattern and optional min_matches')
        raw = entry.get('pattern')
        if isinstance(raw, str) and raw.endswith('/'):
            raise ValueError('necessary outputs must be file patterns, not directory selectors')
        pattern = relative_path(raw)
        minimum = entry.get('min_matches', 1)
        if type(minimum) is not int or not 1 <= minimum <= 50000:
            raise ValueError('min_matches must be 1..50000')
        if not any(c in pattern for c in '*?[') and minimum != 1:
            raise ValueError('literal output paths can match exactly one file')
        normalized.append({'pattern': pattern, 'min_matches': minimum})
    optional = [relative_path(p) for p in optional]
    patterns = [r['pattern'] for r in normalized] + optional
    if len(set(patterns)) != len(patterns):
        raise ValueError('duplicate required/optional output contract')
    for pattern in patterns:
        if pattern.endswith('/') or pattern.startswith(('.hpc-mcp-', '.xn02-')):
            raise ValueError('output contracts must select nonreserved files')
        if not covered(pattern, sync):
            raise ValueError('output contract outside approved sync selection: ' + pattern)
    if strategy != 'manual' and not normalized:
        raise ValueError('automatic release requires explicit necessary outputs')
    return {'strategy': strategy, 'required_outputs': normalized, 'optional_outputs': optional,
            'allow_unverified_application': allow}


def application_contract(jobs, run):
    plugin = run.get('template', {}).get('application_plugin')
    if not plugin:
        return None
    from .applications import ApplicationService
    apps = ApplicationService(jobs)
    with apps.locked():
        record, _ = apps.record(plugin['application'], plugin['version'])
    return {'application': plugin['application'], 'version': plugin['version'],
            'manifest_sha256': record['manifest_sha256'],
            'rules': record['manifest'].get('validation')}


def contract(jobs, run, release):
    application = application_contract(jobs, run)
    if (release['strategy'] != 'manual' and not (application or {}).get('rules')
            and not release['allow_unverified_application']):
        raise ValueError('no application criterion; explicitly approve allow_unverified_application or use manual')
    return {'version': 1, 'application': application,
            'limitation': 'Scheduler exit and registered termination checks do not prove scientific correctness.'}


def facts(run):
    return digest({k: run.get(k) for k in ('submission_plan_token', 'submission_authorization',
        'phase', 'job_id', 'state', 'exit_code', 'sync_state', 'sync_options', 'output_dir',
        'output_manifest', 'sync_installation')})


def operations_active(history, rid):
    with history.connect() as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='operations'").fetchone()
        return bool(exists and db.execute("SELECT 1 FROM operations WHERE run_id=? AND "
            "json_extract(data,'$.state') IN ('queued','running')", (rid,)).fetchone())


def normal(jobs, run, plan):
    from .monitor import verified_outputs
    from .submission import options_from
    release = plan['automation']['release_policy']
    blockers = []
    if (run['phase'] != 'submitted' or run['state'] != 'succeeded'
            or str(run.get('exit_code')) != '0' or not run.get('status_query_ok')):
        blockers.append('scheduler_success_and_exit_zero_required')
    if (run.get('sync_installation') and run['sync_installation'].get('phase') != 'complete'):
        blockers.append('pending_result_installation')
    if operations_active(jobs.history, run['run_id']):
        blockers.append('active_sync_operation')
    if not verified_outputs(run, options_from(plan['sync'])):
        blockers.append('verified_final_outputs_required')
    root = Path(run.get('output_dir') or '.')
    for item in run.get('output_manifest', []):
        relative = Path(item['path'])
        if any((root / p).is_symlink() for p in relative.parents if str(p) != '.'):
            blockers.append('unsafe_output_parent')
            break
    names = [f['path'] for f in run.get('output_manifest', [])]
    required = [{**r, 'match_count': sum(matches(n, r['pattern']) for n in names)}
                for r in release['required_outputs']]
    if not required:
        blockers.append('necessary_output_contract_required')
    if any(r['match_count'] < r['min_matches'] for r in required):
        blockers.append('necessary_outputs_missing')
    app = {'status': 'unverified', 'reason': 'No application criterion registered.'}
    if (plan.get('release_contract', {}).get('application') or {}).get('rules'):
        from .applications import ApplicationService
        app = ApplicationService(jobs).validate(run['run_id'], local_only=True)
        if app['status'] != 'passed':
            blockers.append('application_check_not_passed')
    elif not release['allow_unverified_application']:
        blockers.append('unverified_application_not_approved')
    return {'normal_ready': not blockers, 'blockers': blockers, 'application': app,
            'required_outputs': required,
            'optional_outputs': [{'pattern': p, 'match_count': count}
                for p in release['optional_outputs'] if (count := sum(matches(n, p) for n in names))],
            'evidence': {'version': 1, 'plan_token': run['submission_plan_token'],
                'run_id': run['run_id'],
                'authority': digest(run['submission_authorization']), 'facts': facts(run),
                'scheduler_state': run['state'], 'exit_code': run.get('exit_code'),
                'output_digest': digest(run.get('output_manifest')),
                'application_status': app['status'], 'checked_at': now()}}


class FinalizationService:
    def __init__(self, jobs):
        self.jobs, self.history = jobs, jobs.history
        from .retention import RetentionService
        self.retention = RetentionService(jobs)

    def save(self, rid, value):
        value = dict(value, checked_at=now())
        def summary(item):
            return {**{k: item.get(k) for k in ('strategy', 'normal_ready', 'cleanup_ready',
                'blockers', 'retention_revision', 'dependency_release_error')},
                'evidence': {k: item.get('evidence', {}).get(k) for k in ('facts', 'authority', 'plan_token')}}
        # Polling keeps one full current record; audit only compact semantic transitions.
        with self.history.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            run = json.loads(db.execute('SELECT data FROM runs WHERE run_id=?', (rid,)).fetchone()[0])
            changed = summary(run.get('finalization') or {}) != summary(value)
            run.update(finalization=value, updated_at=now())
            db.execute('UPDATE runs SET data=? WHERE run_id=?', (json.dumps(run), rid))
            if changed:
                db.execute('INSERT INTO events(run_id,time,kind,detail) VALUES (?,?,?,?)',
                           (rid, now(), 'finalization_checked', json.dumps(summary(value))))
        return value

    def many(self, run_ids):
        ids = self.retention.ids(run_ids)
        for rid in ids:
            self.history.get(rid)
        values = []
        for rid in ids:
            try:
                values.append({'run_id': rid, **self.advance(rid)})
            except (ValueError, OSError) as error:
                values.append({'run_id': rid, 'normal_ready': False, 'cleanup_ready': False,
                               'blockers': [str(error)]})
        return {'ok': True, 'tasks': values}

    def advance(self, run_id, _observed=None):
        from .submission import receipt
        result, observed = None, None
        with self.history.lock(run_id):
            run = self.history.get(run_id)
            release = (run.get('submission_plan') or {}).get('automation', {}).get('release_policy')
            if release is None:
                return {'strategy': 'manual', 'normal_ready': False, 'cleanup_ready': False,
                        'blockers': ['no_approved_release_rule']}
            try:
                plan, _ = receipt(self.jobs, run, verify_files=False)
                if run.get('remote_removed'):
                    return run.get('finalization') or {'strategy': release['strategy'], 'deleted': True}
                query = _observed if _observed is not None else self.jobs.job_status(run_id)
                run = self.history.get(run_id)
                result = normal(self.jobs, run, plan)
                result['strategy'] = release['strategy']
                if not query.get('ok'):
                    result['normal_ready'] = False
                    result['blockers'].append('fresh_scheduler_query_required')
                self.retention.reconcile_dependencies(run_id)
                shared = self.retention.status(run['cluster'], [run_id])
                task = shared['tasks'][0]
                if not task.get('registered'):
                    raise ValueError('remote registration missing')
                holder = self.retention.holder()
                claim = task['holds'].get(holder, {})
                dependencies = [h for h in task['holds'].values()
                                if h.get('dependency') and not h['released']]
                current = self.history.get(run_id)
                receipt(self.jobs, current, verify_files=False)
                if facts(current) != result['evidence']['facts']:
                    raise ValueError('task facts changed during completion checks; re-evaluate')
                if not result['normal_ready']:
                    observed = self.retention.automatic(run, task, 'invalidate', result['evidence'])
                elif release['strategy'] == 'manual':
                    if not claim.get('released'):
                        result['blockers'].append('manual_release_required')
                elif claim.get('released') and claim.get('release_source') == 'manual':
                    pass
                elif claim.get('manual_override'):
                    result['blockers'].append('explicit_hold_requires_manual_release')
                elif release['strategy'] == 'after_consumers_complete' and dependencies:
                    result['blockers'].append('dependent_stages_still_need_remote_files')
                else:
                    observed = self.retention.automatic(run, task, 'auto_release', result['evidence'])
                if observed:
                    task = observed['tasks'][0]
                result['submission_released'] = bool(task['holds'].get(holder, {}).get('released'))
                result['cleanup_ready'] = bool(result['normal_ready'] and shared['cleanup_allowed']
                    and task['state'] == 'live' and task['holds']
                    and all(h['released'] for h in task['holds'].values()))
                result['retention_revision'] = task['revision']
                result['active_holders'] = [k for k, h in task['holds'].items() if not h['released']]
                if result['active_holders'] and not result['blockers']:
                    result['blockers'].append('other_retention_claims_active')
                if shared['node_conflict']:
                    result['cleanup_ready'] = False
                    result['blockers'].append('login_host_conflict')
                result = self.save(run_id, result)
            except (ValueError, OSError) as error:
                try:
                    task = self.retention.status(run['cluster'], [run_id])['tasks'][0]
                    observed = self.retention.automatic(run, task, 'invalidate', {})
                except (ValueError, OSError):
                    pass
                result = self.save(run_id, {'strategy': release['strategy'], 'normal_ready': False,
                    'cleanup_ready': False, 'blockers': [str(error)]})
        if observed:
            self.retention.mirror(observed)
        # Each dependent stage ends its own workflow hold; never wait for its unrelated holders.
        if result.get('normal_ready'):
            try:
                self.retention.complete_dependencies(run_id, result['evidence'])
            except (ValueError, OSError) as error:
                result = self.save(run_id, dict(result, dependency_release_error=str(error)))
        return result


def assert_cleanup(jobs, run, automatic=False):
    """Recheck automatically released evidence; a ready flag is never deletion authority."""
    if not automatic and not run.get('retention', {}).get('automatic_release'):
        return
    from .submission import receipt
    plan, _ = receipt(jobs, run, verify_files=False)
    if ('release_policy' not in plan['automation'] or not normal(jobs, run, plan)['normal_ready']
            or (run.get('finalization') or {}).get('evidence', {}).get('facts') != facts(run)):
        raise ValueError('automatic completion evidence changed; re-evaluate retention_finalize before cleanup')
