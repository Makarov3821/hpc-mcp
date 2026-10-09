"""Read-only, provenance-preserving accounting. Missing metrics remain null."""

from datetime import datetime, timedelta
import math
import re
import shlex

from .history import now
from .job_scheduler import cluster_scope, parse_lsf_long


FIELDS = ['JobIDRaw', 'JobName', 'State', 'Submit', 'Start', 'End', 'ElapsedRaw',
          'AllocCPUS', 'ReqCPUS', 'ReqMem', 'CPUTimeRAW', 'TotalCPU', 'MaxRSS', 'Partition', 'Account']


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (TypeError, ValueError):
        return None


def duration(value):
    if not value or value in ('Unknown', 'N/A', '-'):
        return None
    match = re.fullmatch(r'(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)', value)
    if not match:
        return number(value)
    days, hours, minutes, seconds = match.groups()
    if int(minutes) >= 60 or float(seconds) >= 60:
        return None
    return int(days or 0) * 86400 + int(hours or 0) * 3600 + int(minutes) * 60 + float(seconds)


def memory(value, default_unit=None):
    match = re.fullmatch(r'(\d+(?:\.\d+)?)\s*([KMGTPE]?)(?:bytes|byte|B)?', value or '', re.I)
    if not match:
        return None
    unit = match[2].upper() or default_unit
    if unit is None:
        return None
    return int(float(match[1]) * 1024 ** 'BKMGTPE'.index(unit))


def elapsed_between(start, end):
    try:
        seconds = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
        return seconds if seconds >= 0 else None
    except (ValueError, TypeError):
        return None


def parse_slurm(text, job_id, run_name):
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        cells = line.strip().split('|')
        if len(cells) != len(FIELDS):
            raise ValueError('unsupported sacct accounting columns')
        row = dict(zip(FIELDS, (c.strip() for c in cells)))
        if row['JobIDRaw'] == job_id or re.fullmatch(re.escape(job_id) + r'\.[A-Za-z0-9_-]+', row['JobIDRaw']):
            rows.append(row)
    primary = [row for row in rows if row['JobIDRaw'] == job_id]
    if not primary:
        return None
    if len(primary) != 1 or primary[0]['JobName'] != run_name:
        raise ValueError('ambiguous or mismatched Slurm allocation identity')
    row = primary[0]
    steps = [r for r in rows if r is not row]
    rss = [memory(r['MaxRSS'], 'K') for r in rows]
    peak = max((v for v in rss if v is not None), default=None)
    request = re.fullmatch(r'(.+?)([cn])?', row['ReqMem'])
    scope = {'c': 'per_cpu', 'n': 'per_node'}.get(request[2]) if request else None
    return {'scheduler_state': row['State'], 'submit_time': row['Submit'], 'start_time': row['Start'],
            'end_time': row['End'], 'wait_seconds': elapsed_between(row['Submit'], row['Start']),
            'elapsed_seconds': number(row['ElapsedRaw']), 'allocated_cpus': number(row['AllocCPUS']),
            'requested_cpus': number(row['ReqCPUS']),
            'requested_memory_bytes': memory(request[1], 'K') if request else None,
            'requested_memory_scope': scope, 'allocated_cpu_seconds': number(row['CPUTimeRAW']),
            'actual_cpu_seconds': duration(row['TotalCPU']), 'peak_rss_bytes': peak,
            'peak_rss_scope': 'maximum task RSS across reported steps; not aggregate job memory',
            'queue': row['Partition'] or None, 'account': row['Account'] or None,
            'allocation': row, 'steps': steps,
            'warnings': ['Site accounting can report zero for unavailable usage; zero is reported, not proof of no consumption.',
                         'Allocation CPU time is distinct from actual CPU usage; step CPU values are not added to the allocation.',
                         'Memory collection and accounting retention depend on site configuration.']}


def parse_lsf(text, job_id, run_name):
    state = parse_lsf_long(text, job_id, expected_name=run_name)
    if state is None:
        return None
    # Require an exact named header, not just a reused numeric job ID.
    headers = list(re.finditer(r'^\s*Job <([^>]+)>,', text, re.M))
    blocks = [text[h.start():headers[i + 1].start() if i + 1 < len(headers) else len(text)]
              for i, h in enumerate(headers) if h[1] == job_id]
    block = blocks[0]
    first_event = re.search(r'^\s*(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+', block, re.M)
    header = block[:first_event.start()] if first_event else block
    header = header.split('Command <', 1)[0]
    if not re.search(r'Job Name <' + re.escape(run_name) + r'>', header):
        raise ValueError('LSF accounting lacks matching run name')
    result = {'scheduler_state': state['raw_state'], 'wait_seconds': None, 'elapsed_seconds': None,
              'allocated_cpus': None, 'requested_cpus': None, 'requested_memory_bytes': None,
              'requested_memory_scope': None, 'allocated_cpu_seconds': None, 'actual_cpu_seconds': None,
              'peak_rss_bytes': None, 'peak_rss_scope': 'LSF job MEM metric; site collection semantics apply',
              'warnings': ['Unlabelled LSF memory units remain unknown; reservation scope requires site confirmation.']}
    cpu = re.search(r'CPU time used is\s+([\d.]+)\s+seconds', block, re.I)
    if cpu:
        result['actual_cpu_seconds'] = number(cpu[1])
    peak = re.search(r'MAX MEM:\s*([\d.]+)\s*([KMGT])(?:bytes|B)', block, re.I)
    if peak:
        result['peak_rss_bytes'] = memory(peak[1] + peak[2])
    lines = block.splitlines()
    tables = []
    for i, line in enumerate(lines):
        columns = line.split()
        if {'CPU_T', 'WAIT', 'TURNAROUND', 'STATUS'}.issubset(columns):
            for following in lines[i + 1:]:
                if not following.strip() or set(following.strip()) <= {'-'}:
                    continue
                cells = following.split()
                if len(cells) == len(columns) and cells[columns.index('STATUS')] in ('done', 'exit', 'DONE', 'EXIT'):
                    tables.append(dict(zip(columns, cells)))
                break
    if len(tables) > 1:
        raise ValueError('multiple LSF accounting usage rows; aggregation scope ambiguous')
    if tables:
        row = tables[0]
        cpu, wait, total = number(row.get('CPU_T')), number(row.get('WAIT')), number(row.get('TURNAROUND'))
        result.update(actual_cpu_seconds=cpu, wait_seconds=wait,
                      elapsed_seconds=total - wait if total is not None and wait is not None and total >= wait else None,
                      peak_rss_bytes=result['peak_rss_bytes'] if result['peak_rss_bytes'] is not None else memory(row.get('MEM')),
                      raw_usage=row)
    if not any(result.get(k) is not None for k in ('actual_cpu_seconds', 'elapsed_seconds', 'peak_rss_bytes')):
        result['warnings'].append('Identified record has no supported usage metrics.')
    return result


class AccountingService:
    def __init__(self, jobs):
        self.jobs = jobs

    def usage(self, run_id, refresh=True):
        run = self.jobs.history.get(run_id)
        if type(refresh) is not bool:
            raise ValueError('refresh must be boolean')
        if not refresh:
            return {'ok': True, 'run_id': run_id, 'usage': run.get('accounting'), 'cached': True}
        if run['phase'] != 'submitted' or not re.fullmatch(r'[0-9]+', run.get('job_id') or ''):
            raise ValueError('usage requires a confirmed submitted run')
        cluster = self.jobs._cluster(run)
        start = (datetime.fromisoformat(run['created_at']) - timedelta(days=1)).strftime('%Y-%m-%d')
        if cluster.scheduler == 'slurm':
            fields = ','.join(f + ('%128' if f == 'JobName' else '%64' if f in ('JobIDRaw', 'State') else '') for f in FIELDS)
            command = (f"sacct {cluster_scope(run.get('scheduler_cluster'))} --duplicates --noheader --parsable2 "
                       f"--units=K --jobs={run['job_id']} --starttime={shlex.quote(start)} --format={fields}")
        else:
            interval = start.replace('-', '/') + '/00:00,'
            command = f"LC_ALL=C bacct -l -S {shlex.quote(interval)} {run['job_id']}"
        response = self.jobs.transport.run(cluster, command)
        previous = run.get('accounting')
        attempt = {'checked_at': now(), 'command': command, **response.diagnostic()}
        if not response.ok:
            self.jobs.history.update(run_id, 'accounting_unavailable', accounting_last_attempt=attempt)
            return {'ok': False, 'run_id': run_id, 'usage': previous, 'stale': previous is not None, 'error': attempt}
        try:
            metrics = (parse_slurm if cluster.scheduler == 'slurm' else parse_lsf)(response.stdout, run['job_id'], run_id)
        except ValueError as exc:
            attempt['parse_error'] = str(exc)
            metrics = None
        if metrics is None:
            self.jobs.history.update(run_id, 'accounting_unavailable', accounting_last_attempt=attempt)
            return {'ok': False, 'run_id': run_id, 'usage': previous, 'stale': previous is not None,
                    'error': attempt, 'notes': ['No identified record; retention, delay and permissions may be responsible.']}
        declared = run.get('generation', {}).get('spec', {}).get('resources')
        usage = {'run_id': run_id, 'scheduler': cluster.scheduler, 'checked_at': now(), 'source': attempt,
                 'metrics': metrics, 'declared_resources': declared,
                 'missing_metrics': [k for k in ('wait_seconds', 'elapsed_seconds', 'actual_cpu_seconds', 'peak_rss_bytes') if metrics.get(k) is None]}
        self.jobs.history.update(run_id, 'accounting_recorded', accounting=usage, accounting_last_attempt=attempt)
        return {'ok': True, 'run_id': run_id, 'usage': usage, 'stale': False}

    def report(self, cluster=None, project_root=None, since=None, until=None, limit=500, offset=0):
        if type(limit) is not int or not 1 <= limit <= 5000 or type(offset) is not int or offset < 0:
            raise ValueError('limit must be 1..5000; offset nonnegative')
        from pathlib import Path
        project = str(Path(project_root).expanduser().resolve()) if project_root else None
        # Filter by local task creation time. Accounting timestamps remain raw site-local evidence.
        bounds = []
        for value in (since, until):
            if value is not None:
                parsed = datetime.fromisoformat(value)
                if parsed.tzinfo is None:
                    raise ValueError('since/until require timezone-aware ISO timestamps')
                bounds.append(parsed.timestamp())
            else:
                bounds.append(None)
        if bounds[0] is not None and bounds[1] is not None and bounds[0] > bounds[1]:
            raise ValueError('since must not exceed until')
        where = ("WHERE (? IS NULL OR json_extract(data,'$.cluster')=?) "
                 "AND (? IS NULL OR json_extract(data,'$.project_root')=?) "
                 "AND (? IS NULL OR julianday(created_at)>=julianday(?)) "
                 "AND (? IS NULL OR julianday(created_at)<=julianday(?))")
        args = (cluster, cluster, project, project, since, since, until, until)
        with self.jobs.history.connect() as db:
            total = db.execute('SELECT COUNT(*) FROM runs ' + where, args).fetchone()[0]
            rows = db.execute('SELECT data FROM runs ' + where + ' ORDER BY created_at,run_id LIMIT ? OFFSET ?',
                              (*args, limit, offset)).fetchall()
        import json
        selected = [json.loads(r[0]) for r in rows]
        fields = ('wait_seconds', 'elapsed_seconds', 'allocated_cpu_seconds', 'actual_cpu_seconds')
        totals = {}
        for field in fields:
            values = [r.get('accounting', {}).get('metrics', {}).get(field) for r in selected]
            known = [v for v in values if v is not None]
            totals[field] = {'sum': sum(known) if known else None, 'known_runs': len(known),
                             'missing_runs': len(selected) - len(known)}
        return {'ok': True, 'scope': 'cached local registered runs, filtered by task creation time; this page only',
                'selected_runs': len(selected), 'total_matching_runs': total, 'limit': limit, 'offset': offset,
                'truncated': offset + len(selected) < total, 'totals': totals,
                'stale_runs': sum(bool(r.get('accounting') and r.get('accounting_last_attempt', {}).get('checked_at', '')
                                      > r['accounting']['checked_at']) for r in selected),
                'runs': [{'run_id': r['run_id'], 'cluster': r['cluster'], 'project_root': r.get('project_root'),
                    'created_at': r['created_at'], 'usage': r.get('accounting'),
                    'last_attempt': r.get('accounting_last_attempt'),
                    'stale': bool(r.get('accounting') and r.get('accounting_last_attempt', {}).get('checked_at', '')
                                  > r['accounting']['checked_at'])} for r in selected],
                'notes': ['No remote bulk query. Refresh selected runs with job_usage.',
                          'Missing metrics are not zero; reported zeros may still reflect site collection gaps.',
                          'Peak memory is per-run evidence and is not summed; no profile defaults are modified.']}
