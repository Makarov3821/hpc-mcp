"""Bounded, coalesced status queries with existing per-job archive fallback."""

from concurrent.futures import ThreadPoolExecutor
import re
import threading

from .history import now
from .job_scheduler import cluster_scope, parse_status
from .jobs import JobService
from .ssh import CommandResult


class QueryBudget:
    def __init__(self, transport, maximum):
        self.transport = transport
        self.remaining = maximum
        self.used = 0
        self.lock = threading.Lock()

    def run(self, cluster, command):
        with self.lock:
            if self.remaining <= 0:
                return CommandResult(-1, error='query_budget_exhausted')
            self.remaining -= 1
            self.used += 1
        return self.transport.run(cluster, command)


def query_runs(jobs, runs, settings):
    budget = QueryBudget(jobs.transport, settings.max_status_commands_per_cycle)
    query_jobs = JobService(jobs.clusters, jobs.history.root, budget, jobs.transfer)
    groups = {}
    results = {}
    for run in runs:
        try:
            cluster = query_jobs._cluster(run)
            if not re.fullmatch(r'[0-9]+', run.get('job_id') or ''):
                raise ValueError('no valid confirmed scheduler job ID')
            groups.setdefault((cluster.name, run.get('scheduler_cluster')), []).append(run)
        except ValueError as error:
            results[run['run_id']] = {'ok': False, 'error': str(error)}

    def group_query(group):
        pending = list(group)
        observations = {}
        if settings.batch_queries and len(group) > 1:
            cluster = query_jobs._cluster(group[0])
            ids = sorted({run['job_id'] for run in group})
            if cluster.scheduler == 'slurm':
                scope = cluster_scope(group[0].get('scheduler_cluster'))
                command = f"squeue {scope} --noheader --jobs={','.join(ids)} --format='%i|%T|%r'"
                source = 'squeue_batch'
            else:
                command = ('LC_ALL=C bjobs -a -noheader -o '
                           '"jobid stat exit_code exit_reason delimiter=\'|\'" ' + ' '.join(ids))
                source = 'bjobs_batch'
            result = budget.run(cluster, command)
            if result.ok:
                for run in group:
                    try:
                        status = parse_status(cluster.scheduler, result.stdout, run['job_id'])
                    except ValueError:
                        status = None
                    if status and status['state'] != 'unknown' and not (
                            cluster.scheduler == 'lsf' and status['raw_state'] == 'EXIT'
                            and not status.get('termination_reason')):
                        value = jobs.history.update(run['run_id'], 'status_observed', **status,
                            status_source=source, last_status_query=now(), status_query_ok=True,
                            status_diagnostics=[{'command': command, **result.diagnostic()}])
                        observations[run['run_id']] = {'ok': True, 'run': value}
                        pending.remove(run)
        for run in pending:
            try:
                observations[run['run_id']] = query_jobs.job_status(run['run_id'])
            except (OSError, ValueError) as error:
                observations[run['run_id']] = {'ok': False, 'error': str(error)}
        return observations

    with ThreadPoolExecutor(max_workers=settings.query_concurrency) as executor:
        for observations in executor.map(group_query, groups.values()):
            results.update(observations)
    return results, budget.used
