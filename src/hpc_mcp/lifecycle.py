"""Durable, explicitly enabled orchestration over existing job and monitor services."""

import time

from .history import now
from .monitor import MonitorService, state_lock, verified_outputs
from .submission import SubmissionError, owner, receipt


class LifecycleService:
    def __init__(self, jobs, clock=time.time, stopping=None):
        self.jobs = jobs
        self.history = jobs.history
        self.monitor = MonitorService(jobs)
        self.clock = clock
        self.stopping = stopping or (lambda: False)

    def save(self, run_id, **changes):
        with self.history.lock(run_id):
            current = dict(self.history.get(run_id).get('lifecycle') or {})
            current.update(changes, updated_at=now())
            self.history.update(run_id, 'lifecycle_updated', lifecycle=current)
        return current

    def get(self, run_id):
        run = self.history.get(run_id)
        record = run.get('lifecycle') or {
            'enabled': False, 'stage': 'not_started' if run.get('submission_authorization')
                else 'awaiting_authorization', 'next_check_at': None}
        stage = record['stage']
        action = {
            'awaiting_authorization': 'job_submission_plan_then_user_confirmation_and_job_authorize',
            'not_started': 'job_lifecycle_start_after_checking_current_authorization',
            'ready': 'job_lifecycle_tick_or_monitor_start',
            'awaiting_workflow': 'workflow_review_and_workflow_start_or_workflow_tick',
            'tracking': 'monitor_start_or_wait_for_next_cycle',
            'needs_attention': 'inspect_failed_step_then_job_lifecycle_resume',
            'complete': 'inspect_separate_scheduler_application_and_sync_results',
            'paused': 'job_lifecycle_resume',
            'awaiting_release': 'inspect_finalization_blockers_or_wait_for_dependent_stages',
        }.get(stage, 'wait_or_inspect_job_lifecycle_get')
        monitor = None
        try:
            monitor = self.monitor.entry(run_id)
        except ValueError:
            pass
        return {'ok': True, 'run_id': run_id, 'lifecycle': record,
            'scheduler': {k: run.get(k) for k in
                ('phase', 'job_id', 'state', 'raw_state', 'status_query_ok', 'last_status_query')},
            'application_result': (monitor or {}).get('application_result') or
                run.get('application_validation') or {'status': 'unverified'},
            'sync': {k: run.get(k) for k in
                ('sync_state', 'output_dir', 'output_manifest', 'sync_error', 'last_sync_operation')},
            'monitor_stage': (monitor or {}).get('stage'), 'pending_action': action,
            'finalization': run.get('finalization'),
            'cleanup': {'local': (monitor or {}).get('last_cleanup'),
                'deferred': (monitor or {}).get('cleanup_deferred') or record.get('cleanup_deferred'),
                'remote_removed': run.get('remote_removed', False)}}

    def list(self, limit=50, offset=0):
        if type(limit) is not int or not 1 <= limit <= 500 or type(offset) is not int or offset < 0:
            raise ValueError('limit 1..500 and offset >=0')
        with self.history.connect() as db:
            ids = [row[0] for row in db.execute(
                "SELECT run_id FROM runs WHERE json_extract(data,'$.lifecycle') IS NOT NULL "
                "ORDER BY rowid LIMIT ? OFFSET ?", (limit, offset))]
        return {'ok': True, 'lifecycles': [self.get(rid) for rid in ids]}

    def start(self, run_id, resume=False):
        with state_lock(self.history, 'lifecycle-' + self.history.get(run_id)['run_id'] + '.lock'):
            run = self.history.get(run_id)
            if run['phase'] in ('abandoned', 'rejected'):
                raise ValueError('prepare a new run; rejected/abandoned jobs are not retried')
            plan, _ = receipt(self.jobs, run)
            if not plan['automation']['auto_sync']:
                raise SubmissionError('lifecycle_authorization_required',
                    'Approve auto_sync=true before enabling the full lifecycle.')
            if run['phase'] != 'submitted' and not plan['automation']['auto_submit']:
                raise SubmissionError('lifecycle_authorization_required',
                    'Approve auto_submit=true for lifecycle submission, or submit manually first.')
            previous = run.get('lifecycle') or {}
            if previous.get('stage') == 'complete' and previous.get('authorization_token') == run['submission_plan_token'] and not resume:
                return self.get(run_id)
            if previous.get('enabled') and previous.get('authorization_token') == run['submission_plan_token'] and not resume:
                return self.get(run_id)
            if previous.get('stage') == 'needs_attention' and not resume:
                raise ValueError('inspect the failed step and explicitly call job_lifecycle_resume')
            if run['phase'] == 'submitted':
                reset_monitor = False
                if resume:
                    try:
                        reset_monitor = self.monitor.entry(run_id)['stage'] != 'complete'
                    except ValueError:
                        pass
                self.monitor.watch([run_id], True, plan['automation']['sync_options'],
                    plan['automation']['cleanup_policy'], reset=reset_monitor, _lifecycle=True)
            self.save(run_id, enabled=True, stage='ready', failed_step=None, last_error=None,
                authorization_token=run['submission_plan_token'], next_check_at=0,
                created_at=previous.get('created_at') or now())
        return self.get(run_id)

    def pause(self, run_id):
        with state_lock(self.history, 'lifecycle-' + self.history.get(run_id)['run_id'] + '.lock'):
            self.save(run_id, enabled=False, stage='paused', next_check_at=None)
            try:
                if self.monitor.entry(run_id)['policy'].get('lifecycle'):
                    self.monitor.unwatch(run_id)
            except ValueError:
                pass
        return self.get(run_id)

    def tick(self, run_id, poll_interval_seconds=60):
        with state_lock(self.history, 'lifecycle-' + self.history.get(run_id)['run_id'] + '.lock'):
            run = self.history.get(run_id)
            record = run.get('lifecycle') or {}
            if self.stopping() or not record.get('enabled') or record.get('next_check_at', 0) > self.clock():
                return self.get(run_id)
            step = 'authorization'
            try:
                # A lost response is resolved from receipts, never by repeating submission.
                if run['phase'] in ('submitting', 'submission_unknown'):
                    step = 'recovery'
                    recovered = self.jobs.job_recover(run_id)
                    if not recovered['ok']:
                        raise ValueError('Submission remains ambiguous; inspect job_recover diagnostics.')
                    run = recovered['run']
                plan, _ = receipt(self.jobs, run)
                if record['authorization_token'] != run['submission_plan_token']:
                    raise SubmissionError('submission_plan_changed', 'Lifecycle approval changed; resume explicitly.')
                if run['phase'] != 'submitted':
                    if owner(self.history, run_id):
                        self.save(run_id, stage='awaiting_workflow',
                            next_check_at=self.clock() + poll_interval_seconds)
                        return self.get(run_id)
                    if not plan['automation']['auto_submit']:
                        raise ValueError('Lifecycle automatic submission is not approved.')
                    step = 'submission'
                    self.save(run_id, stage='submitting', failed_step=step)
                    result = self.jobs.job_submit(run_id)
                    if not result['ok']:
                        raise ValueError(result.get('error') or 'Submission failed; inspect job_get.')
                    run = result['run']
                step = 'monitor_registration'
                try:
                    entry = self.monitor.entry(run_id)
                except ValueError:
                    self.monitor.watch([run_id], True, plan['automation']['sync_options'],
                        plan['automation']['cleanup_policy'], _lifecycle=True)
                    entry = self.monitor.entry(run_id)
                if (not entry['policy'].get('lifecycle') or
                        entry['policy'].get('authorization_token') != record['authorization_token']):
                    raise ValueError('Monitor policy changed; resume the lifecycle explicitly.')
                if entry['stage'] == 'needs_attention':
                    step = entry.get('failed_step') or 'sync_or_cleanup'
                    raise ValueError(entry.get('last_error') or 'Monitor requires attention.')
                if not entry['enabled']:
                    raise ValueError('Monitoring was disabled; resume explicitly.')
                if entry['stage'] != 'complete':
                    self.save(run_id, stage='tracking', failed_step=None,
                        next_check_at=self.clock() + poll_interval_seconds)
                    return self.get(run_id)
                run = self.history.get(run_id)
                step = 'retention'
                if not verified_outputs(run, plan['automation']['sync_options']):
                    raise ValueError('Retained outputs changed; inspect before retrying synchronization.')
                release = plan['automation'].get('release_policy', {})
                if release.get('strategy', 'manual') != 'manual':
                    from .finalization import FinalizationService
                    completion = FinalizationService(self.jobs).advance(run_id)
                    run = self.history.get(run_id)
                    if not completion.get('cleanup_ready'):
                        self.save(run_id, stage='awaiting_release', failed_step=None,
                            cleanup_deferred='; '.join(completion.get('blockers', [])),
                            next_check_at=self.clock() + poll_interval_seconds)
                        return self.get(run_id)
                    self.save(run_id, cleanup_deferred=None)
                step = 'remote_cleanup'
                if (plan['automation']['remote_cleanup'] and
                        entry.get('application_result', {}).get('status') == 'passed' and
                        run['state'] == 'succeeded' and not run.get('remote_removed')):
                    if not run.get('retention', {}).get('released'):
                        self.save(run_id, cleanup_deferred='Retention claims remain; release before cleanup.')
                    else:
                        self.save(run_id, stage='cleaning', failed_step=step)
                        result = self.jobs.job_remote_cleanup(run_id, dry_run=False)
                        if not result['ok']:
                            raise ValueError(result.get('error') or 'Remote cleanup failed.')
                self.save(run_id, stage='complete', enabled=False, failed_step=None,
                    next_check_at=None, last_error=None, completed_at=now())
            except (ValueError, OSError) as error:
                self.save(run_id, enabled=False, stage='needs_attention', failed_step=step,
                    next_check_at=None, last_error=str(error)[:4000])
        return self.get(run_id)

    def due(self, limit=50):
        with self.history.connect() as db:
            rows = db.execute("SELECT run_id FROM runs WHERE json_extract(data,'$.lifecycle.enabled')=1 "
                "AND json_extract(data,'$.lifecycle.next_check_at')<=? "
                "ORDER BY json_extract(data,'$.lifecycle.next_check_at'),rowid LIMIT ?",
                (self.clock(), limit)).fetchall()
        return [row[0] for row in rows]
