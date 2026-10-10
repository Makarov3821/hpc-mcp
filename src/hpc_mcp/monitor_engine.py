"""One bounded coordinator cycle; only status, registered sync and local retention."""

import time

from .jobs import TERMINAL
from .monitor import state_lock, verified_outputs
from .monitor_queries import query_runs


class MonitorEngine:
    def __init__(self, service, settings, clock=time.time, stopping=None):
        self.service = service
        self.jobs = service.jobs
        self.settings = settings
        self.clock = clock
        self.stopping = stopping or (lambda: False)
        self.next_global_cleanup = 0
        self.next_status_cycle_at = 0

    def delay(self, failures):
        return min(self.settings.retry_max_seconds,
                   self.settings.retry_initial_seconds * 2 ** min(failures - 1, 20))

    def permitted(self, entry):
        current = self.service.entry(entry['run_id'])
        return (not self.stopping() and current['enabled'] and current['generation'] == entry['generation']
                and not self.service.runtime().get('stop_requested'))

    def notification(self, entry, kind, detail, suffix=''):
        return kind, entry['generation'] + ':' + kind + ':' + suffix, detail

    def failure(self, entry, error, sync=False, operation_id=None):
        key = 'sync_failures' if sync else 'query_failures'
        failures = entry.get(key, 0) + 1
        exhausted = sync and failures >= self.settings.sync_max_attempts
        changes = {key: failures, 'last_error': str(error)[:4000],
                   'stage': 'needs_attention' if exhausted else 'retry_wait',
                   'next_check_at': self.clock() + self.delay(failures)}
        if operation_id:
            changes.update(operation_id=None, last_processed_operation=operation_id)
        kind = 'needs_attention' if exhausted else ('sync_retry' if sync else 'query_unavailable')
        notice = self.notification(entry, kind, {'error': changes['last_error'], 'failures': failures},
                                   operation_id or str(failures) + ':' + str(self.clock())) if sync or failures == 1 else None
        return self.service.update(entry, notice, **changes)

    def operations(self):
        with self.jobs.history.connect() as db:
            rows = db.execute("SELECT id FROM operations WHERE json_extract(data,'$.state') IN ('queued','running')").fetchall()
        active = {}
        for row in rows:
            value = self.jobs.job_sync_operation(row[0])['operation']
            if value['state'] in ('queued', 'running'):
                active[value['run_id']] = value
        return active

    def reconcile(self, entry, active):
        if not entry['policy']['auto_sync']:
            return entry
        operation = active.get(entry['run_id'])
        if operation is None and entry.get('operation_id'):
            operation = self.jobs.job_sync_operation(entry['operation_id'])['operation']
        if operation is None:
            run = self.jobs.history.get(entry['run_id'])
            latest = run.get('last_sync_operation')
            if latest and latest != entry.get('last_processed_operation'):
                candidate = self.jobs.job_sync_operation(latest)['operation']
                if candidate['created_at'] >= entry['created_at']:
                    operation = candidate
        if operation is None:
            return entry
        operation_id = operation['operation_id']
        if operation_id == entry.get('last_processed_operation'):
            return entry
        if operation['state'] in ('queued', 'running'):
            return self.service.update(entry, stage='syncing', operation_id=operation_id,
                next_check_at=self.clock() + min(self.settings.poll_interval_seconds, 5),
                sync_progress=operation.get('progress', {}))
        if operation['state'] == 'succeeded' and operation.get('result', {}).get('run', {}).get('sync_state') == 'complete' and operation.get('result', {}).get('run', {}).get('output_manifest'):
            return self.service.update(entry, stage='watching', operation_id=None,
                last_processed_operation=operation_id, next_check_at=0,
                sync_progress=operation.get('progress', {}), last_error=None)
        error = operation.get('error') or operation.get('result', {}).get('error') or 'sync did not produce confirmed final outputs'
        return self.failure(entry, error, sync=True, operation_id=operation_id)

    def cleanup(self, entry):
        policy = entry['policy']['cleanup_policy']
        if not policy['storage_categories'] and policy['sync_cache_age_seconds'] is None:
            return {}
        run = self.jobs.history.get(entry['run_id'])
        if not verified_outputs(run, entry['policy']['sync_options']):
            raise ValueError('downloaded outputs changed or unavailable; automatic cleanup refused')
        # Reserve the action while holding only the short control lock. An unwatch/stop
        # may return while a cleanup already reserved here finishes under the run lock.
        with state_lock(self.jobs.history, 'monitor-control.lock'):
            if not self.permitted(entry):
                return {}
            self.service.update(entry, cleanup_started_at=self.clock())
        results = {}
        if policy['sync_cache_age_seconds'] is not None:
            results['sync_cache'] = self.jobs.job_cache_cleanup(entry['run_id'], policy['sync_cache_age_seconds'], False)
        if policy['storage_categories']:
            results['storage'] = self.jobs.job_storage_cleanup(entry['run_id'], policy['storage_categories'],
                                                              policy['storage_age_seconds'], False)
        return results

    def handle(self, entry, observed, active):
        if not self.permitted(entry):
            return
        if not observed['ok'] or observed.get('run', {}).get('state') not in TERMINAL:
            if not observed['ok']:
                diagnostics = observed.get('run', {}).get('status_diagnostics', [])
                if diagnostics and all(d.get('error') == 'query_budget_exhausted' for d in diagnostics):
                    self.service.update(entry, next_check_at=self.clock() + self.settings.poll_interval_seconds,
                                        deferred='status command budget exhausted')
                else:
                    self.service.update(entry, last_query_attempt_at=self.clock())
                    self.failure(entry, observed.get('error', 'scheduler status unavailable'))
            else:
                self.service.update(entry, self.notification(entry, 'query_recovered', {'scheduler_state': observed['run']['state']}, str(self.clock())) if entry.get('query_failures') else None,
                                    stage='watching', query_failures=0, last_error=None,
                                    last_scheduler_state=observed['run']['state'], last_query_attempt_at=self.clock(),
                                    next_check_at=self.clock() + self.settings.poll_interval_seconds)
            return
        run = observed['run']
        entry = self.service.update(entry, self.notification(entry, 'query_recovered', {'scheduler_state': run['state']}, str(self.clock())) if entry.get('query_failures') else None,
                                    query_failures=0, last_scheduler_state=run['state'],
                                    terminal_observed_at=self.clock(), last_query_attempt_at=self.clock(), last_error=None)
        if entry is None:
            return
        auto_sync = entry['policy']['auto_sync']
        if auto_sync and not verified_outputs(run, entry['policy']['sync_options']):
            if entry.get('completed_at') or run.get('remote_removed'):
                self.service.update(entry, self.notification(entry, 'needs_attention', {'error': 'retained outputs changed or remote run removed'}),
                                    stage='needs_attention', last_error='retained outputs changed or remote run removed')
                return
            if len(active) >= self.settings.max_sync_operations:
                self.service.update(entry, next_check_at=self.clock() + self.settings.poll_interval_seconds,
                                    deferred='sync operation capacity')
                return
            try:
                with state_lock(self.jobs.history, 'monitor-control.lock'):
                    if not self.permitted(entry):
                        return
                    result = self.jobs.job_sync_start(entry['run_id'], entry['policy']['sync_options'])
                    operation = result['operation']
                    active[entry['run_id']] = operation
                    self.service.update(entry, self.notification(entry, 'sync_started', {'operation_id': operation['operation_id']}, operation['operation_id']),
                        stage='syncing', operation_id=operation['operation_id'],
                        next_check_at=self.clock() + min(self.settings.poll_interval_seconds, 5))
            except (OSError, ValueError) as error:
                if 'already registered' in str(error) or 'another submit/recover/sync' in str(error):
                    self.service.update(entry, next_check_at=self.clock() + self.settings.poll_interval_seconds,
                                        deferred='another operation is active')
                else:
                    self.failure(entry, error, sync=True)
            return
        # A terminal scheduler state is not application-specific success. Download
        # failures/cancellations as well and report their distinct scheduler outcome.
        notice = self.notification(entry, 'complete', {'scheduler_state': run['state'],
            'output_dir': run.get('output_dir') if auto_sync else None, 'synced': auto_sync}) if entry['stage'] != 'complete' else None
        policy = entry['policy']['cleanup_policy']
        needs_cleanup = policy['storage_categories'] or policy['sync_cache_age_seconds'] is not None
        try:
            cleanup = self.cleanup(entry) if needs_cleanup and run.get('sync_state') == 'complete' else {}
            self.service.update(entry, notice, stage='complete', completed_at=entry.get('completed_at') or self.clock(), last_error=None, last_cleanup=cleanup,
                next_check_at=self.clock() + self.settings.cleanup_interval_seconds if needs_cleanup else None)
        except (OSError, ValueError) as error:
            self.service.update(entry, self.notification(entry, 'cleanup_blocked', {'error': str(error)[:4000]}),
                stage='needs_attention', last_error=str(error)[:4000])

    def tick(self):
        if self.stopping() or self.service.runtime().get('stop_requested'):
            return {'checked': 0, 'status_commands': 0}
        if self.next_status_cycle_at > self.clock() + self.settings.poll_interval_seconds:
            self.next_status_cycle_at = self.clock() + self.settings.poll_interval_seconds
        active = self.operations()
        due = []
        for entry in self.service.entries():
            if not entry['enabled'] or entry['stage'] == 'needs_attention' or entry.get('next_check_at') is None:
                continue
            if entry['next_check_at'] > self.clock() + max(self.settings.retry_max_seconds, self.settings.cleanup_interval_seconds):
                self.service.update(entry, next_check_at=self.clock() + self.settings.poll_interval_seconds)
                continue
            if entry['next_check_at'] > self.clock():
                continue
            entry = self.reconcile(entry, active)
            if entry and entry['stage'] != 'syncing' and entry.get('next_check_at') is not None and entry['next_check_at'] <= self.clock() and entry['stage'] != 'needs_attention':
                due.append(entry)
        if self.clock() < self.next_status_cycle_at:
            due = []
        elif due:
            self.next_status_cycle_at = self.clock() + self.settings.poll_interval_seconds
        due.sort(key=lambda e: (e['next_check_at'], e.get('last_query_attempt_at', 0), e['run_id']))
        due = due[:self.settings.max_jobs_per_cycle]
        runs = [self.jobs.history.get(entry['run_id']) for entry in due]
        observations, used = query_runs(self.jobs, runs, self.settings)
        for entry in due:
            self.handle(entry, observations[entry['run_id']], active)
        if self.settings.input_cache_cleanup is not None and self.clock() >= self.next_global_cleanup and not self.stopping() and not self.service.runtime().get('stop_requested'):
            # Shared blobs are independent of snapshots; this explicitly opted-in
            # local policy does not depend on unavailable remote scheduler evidence.
            self.jobs.input_cache_cleanup(dry_run=False, **self.settings.input_cache_cleanup)
            self.next_global_cleanup = self.clock() + self.settings.cleanup_interval_seconds
        with self.jobs.history.connect() as db:
            db.execute('DELETE FROM monitor_notifications WHERE id NOT IN (SELECT id FROM monitor_notifications ORDER BY id DESC LIMIT ?)',
                       (self.settings.notifications_limit,))
        return {'checked': len(due), 'status_commands': used, 'active_sync_operations': len(active),
                'next_status_cycle_at': self.next_status_cycle_at}
