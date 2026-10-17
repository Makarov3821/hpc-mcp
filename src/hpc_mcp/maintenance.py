"""Explicit, bounded local/cluster retention plans without changing job authorizations."""

from contextlib import contextmanager
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time

from .approvals import require
from .history import now
from .jobs import TERMINAL
from .monitor import state_lock, verified_outputs
from .onboarding_store import digest
from .storage import InputCache

TTL_SECONDS = 3600
MAX_PLAN_BYTES = 4 * 1024 ** 2
LOCAL_CATEGORIES = ('input_cache', 'sync_cache', 'snapshot', 'sync_history', 'old_outputs')
DEFAULT_LOCAL_CATEGORIES = ('input_cache', 'sync_cache', 'sync_history', 'old_outputs')


@contextmanager
def directory(path):
    """Pin existing ancestors only; never create directories during cleanup."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open('/', flags)
    try:
        for name in Path(path).absolute().parts[1:]:
            child = os.open(name, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def tree(path):
    """Fingerprint names/inodes/metadata without reading cache contents or following links."""
    path = Path(path)
    entries, size = [], 0
    with directory(path.parent) as parent:
        info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        def add(name, meta):
            nonlocal size
            if not (stat.S_ISREG(meta.st_mode) or stat.S_ISDIR(meta.st_mode)):
                raise ValueError('cleanup refuses links or special files')
            if meta.st_dev != info.st_dev:
                raise ValueError('cleanup refuses filesystem mount boundaries')
            entries.append([name, meta.st_dev, meta.st_ino, meta.st_mode,
                            meta.st_size, meta.st_mtime_ns, meta.st_ctime_ns])
            if len(entries) > 50000:
                raise ValueError('cleanup tree exceeds 50000 entries')
            if stat.S_ISREG(meta.st_mode):
                size += meta.st_size
        add('.', info)
        if stat.S_ISDIR(info.st_mode):
            fd = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                if os.fstat(fd).st_ino != info.st_ino:
                    raise ValueError('cleanup directory changed')
                for root, dirs, files, current in os.fwalk('.', follow_symlinks=False, dir_fd=fd):
                    for name in dirs + files:
                        add(str(Path(root) / name), os.stat(name, dir_fd=current, follow_symlinks=False))
            finally:
                os.close(fd)
    return {'fingerprint': digest(sorted(entries)), 'bytes': size,
            'newest_mtime': max(e[5] for e in entries) / 10 ** 9,
            'is_directory': stat.S_ISDIR(info.st_mode)}


def run_facts(run):
    return digest({k: run.get(k) for k in (
        'run_id', 'cluster', 'cluster_config', 'phase', 'state', 'job_id', 'remote_dir',
        'input_dir', 'project_root', 'snapshot_dir', 'manifest', 'sync_state', 'sync_options',
        'output_dir', 'output_manifest', 'sync_installation', 'submission_plan_token',
        'submission_authorization', 'retention')})


def eligible(jobs, run, include_failed):
    if run['phase'] != 'submitted' or run['state'] not in TERMINAL:
        raise ValueError('active, unsubmitted or unknown task')
    if run['state'] != 'succeeded' and not include_failed:
        raise ValueError('failed/cancelled task retained; include_failed requires explicit approval')
    journal = run.get('sync_installation') or {}
    if journal and journal.get('phase') != 'complete':
        raise ValueError('pending result installation still needs staging/backup files')
    with jobs.history.connect() as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='operations'").fetchone()
        if exists and db.execute("SELECT 1 FROM operations WHERE run_id=? AND "
                "json_extract(data,'$.state') IN ('queued','running')", (run['run_id'],)).fetchone():
            raise ValueError('sync worker is queued/running; inspect it before cleanup')
    if not verified_outputs(run, {}):
        raise ValueError('nonempty final downloaded outputs must still pass SHA-256 verification')
    if not run.get('retention', {}).get('released'):
        raise ValueError('task retained; explicitly release retention claims before cleanup')
    from .finalization import assert_cleanup
    assert_cleanup(jobs, run)


def remote_grant(jobs, run, token):
    """Called inside the original remote-cleanup run lock; never authorizes computation."""
    service = MaintenanceService(jobs)
    value = service.load(token)
    if value['plan']['kind'] != 'cluster_work_cleanup' or value['state'] != 'applying':
        raise ValueError('remote cleanup needs the active exact maintenance plan')
    plan = value['plan']
    if time.time() >= plan['expires_at']:
        raise ValueError('cleanup plan expired; preview again')
    candidate = next((c for c in plan['candidates'] if c.get('run_id') == run['run_id']), None)
    if not candidate or candidate['run_facts'] != run_facts(run):
        raise ValueError('task changed after maintenance preview')
    eligible(jobs, run, plan['request']['include_failed'])
    cluster = jobs._cluster(run)
    if digest(asdict(cluster)) != digest(plan['cluster_settings']):
        raise ValueError('cluster settings changed after maintenance preview')
    require(jobs.history, 'cluster_work_cleanup', plan['subject'], token)
    return plan['cutoff']


class MaintenanceService:
    def __init__(self, jobs):
        self.jobs, self.history = jobs, jobs.history
        with self.history.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS maintenance_plans '
                       '(token TEXT PRIMARY KEY, data TEXT NOT NULL)')

    def load(self, token):
        if not isinstance(token, str) or not re.fullmatch(r'[a-f0-9]{64}', token):
            raise ValueError('exact cleanup review_token required')
        with self.history.connect() as db:
            row = db.execute('SELECT data FROM maintenance_plans WHERE token=?', (token,)).fetchone()
        if not row:
            raise ValueError('unknown cleanup plan; preview first')
        value = json.loads(row[0])
        if digest(value['plan']) != token:
            raise ValueError('cleanup plan checksum changed')
        return value

    def save(self, token, value):
        with self.history.connect() as db:
            db.execute('INSERT OR REPLACE INTO maintenance_plans VALUES (?,?)',
                       (token, json.dumps(value)))

    def request(self, kind, cluster, project_root, run_ids, older_than_seconds,
                include_failed, limit, offset, categories=None, max_input_cache_bytes=None):
        if type(older_than_seconds) is not int or not 0 <= older_than_seconds <= 10 ** 9:
            raise ValueError('older_than_seconds must be 0..1000000000')
        if type(include_failed) is not bool or type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError('include_failed boolean and limit 1..500 required')
        if type(offset) is not int or offset < 0:
            raise ValueError('offset must be nonnegative')
        if run_ids is not None and (not isinstance(run_ids, list) or not 1 <= len(run_ids) <= 500
                or any(not isinstance(r, str) or not re.fullmatch(r'r_[a-f0-9]{32}', r) for r in run_ids)
                or len(set(run_ids)) != len(run_ids)):
            raise ValueError('run_ids must contain 1..500 unique registered run IDs')
        if cluster is not None and (not isinstance(cluster, str) or not cluster):
            raise ValueError('cluster must be a configured name')
        if kind == 'cluster_work_cleanup' and cluster is None:
            raise ValueError('cluster is required')
        project = str(Path(project_root).expanduser().resolve()) if project_root else None
        request = {'cluster': cluster, 'project_root': project, 'run_ids': run_ids,
            'older_than_seconds': older_than_seconds, 'include_failed': include_failed,
            'limit': limit, 'offset': offset}
        if kind == 'local_cache_cleanup':
            if categories is not None and not isinstance(categories, list):
                raise ValueError('categories must be an array')
            values = list(DEFAULT_LOCAL_CATEGORIES if categories is None else categories)
            if (not values or any(not isinstance(c, str) for c in values)
                    or set(values) - set(LOCAL_CATEGORIES)):
                raise ValueError('unknown local cache category')
            if 'input_cache' in values and (cluster or project or run_ids):
                if categories is not None:
                    raise ValueError('shared input_cache is global; select it without task filters')
                values.remove('input_cache')
            if max_input_cache_bytes is not None and (type(max_input_cache_bytes) is not int
                    or not 0 <= max_input_cache_bytes <= 2 ** 60):
                raise ValueError('max_input_cache_bytes must be 0..2**60')
            request.update(categories=sorted(set(values)), max_input_cache_bytes=max_input_cache_bytes)
        return request

    def runs(self, request):
        sql, params = 'SELECT data FROM runs WHERE 1=1', []
        for field in ('cluster', 'project_root'):
            if request[field] is not None:
                sql += f" AND json_extract(data,'$.{field}')=?"
                params.append(request[field])
        if request['run_ids'] is not None:
            sql += ' AND run_id IN (' + ','.join('?' for _ in request['run_ids']) + ')'
            params += request['run_ids']
        sql += ' ORDER BY created_at,run_id LIMIT ? OFFSET ?'
        params += [request['limit'] + 1, request['offset']]
        with self.history.connect() as db:
            rows = [json.loads(row[0]) for row in db.execute(sql, params)]
        return rows[:request['limit']], request['offset'] + request['limit'] if len(rows) > request['limit'] else None

    def preview(self, kind, request):
        started = time.time()
        plan = {'version': 1, 'kind': kind, 'subject': request['cluster'] or 'local-cache',
            'state_dir': str(self.history.root), 'config_path': str(self.jobs.config_path) if self.jobs.config_path else None,
            'request': request, 'cutoff': int(started - request['older_than_seconds']),
            'expires_at': started + TTL_SECONDS, 'candidates': []}
        skipped, omitted_blobs = [], 0
        if kind == 'cluster_work_cleanup':
            self.jobs.refresh_clusters()
            if request['cluster'] not in self.jobs.clusters:
                raise ValueError('unknown cluster')
            cluster = self.jobs.clusters[request['cluster']]
            if cluster.work_root == '/':
                raise ValueError('cluster work_root cannot be a filesystem root for bulk cleanup')
            plan['cluster_settings'] = asdict(cluster)
            plan['work_root'] = cluster.work_root
        elif 'input_cache' in request['categories']:
            cache = InputCache(self.history)
            preview = cache.cleanup(request['older_than_seconds'],
                request['max_input_cache_bytes'] if request['max_input_cache_bytes'] is not None else 2 ** 60)
            omitted_blobs = max(0, len(preview['candidates']) - request['limit'])
            for item in preview['candidates'][:request['limit']]:
                plan['candidates'].append({'category': 'input_cache', 'path': item['path'], **tree(item['path'])})
        runs, next_offset = self.runs(request)
        for original in runs:
            rid = original['run_id']
            try:
                with self.history.lock(rid):
                    run = self.history.get(rid)
                    if kind == 'cluster_work_cleanup' and run.get('remote_removed'):
                        continue
                    eligible(self.jobs, run, request['include_failed'])
                    if kind == 'cluster_work_cleanup':
                        # Remote directory age is checked by find in the same validated SSH command.
                        result = self.jobs.job_remote_cleanup(rid, _lock_held=True,
                            _cleanup_cutoff=plan['cutoff'])
                        if not result['ok']:
                            raise ValueError(result.get('diagnostic'))
                        if result.get('already_removed'):
                            skipped.append({'run_id': rid, 'reason': 'remote registry already records deletion'})
                            continue
                        run = self.history.get(rid)
                        output = result['diagnostic'].get('stdout', '').split()
                        plan['candidates'].append({'run_id': rid, 'path': run['remote_dir'],
                            'category': 'remote_work', 'bytes': int(output[0]), 'run_facts': run_facts(run),
                            'retention_revision': result['retention_revision'],
                            'workspace_id': result['workspace_id']})
                    else:
                        # Helpers below acquire their own run lock; obtain local paths without nesting it.
                        pass
                if kind == 'local_cache_cleanup':
                    previews = []
                    if 'sync_cache' in request['categories']:
                        result = self.jobs.job_cache_cleanup(rid, request['older_than_seconds'])
                        previews += [dict(c, category='sync_cache') for c in result['candidates']]
                    storage = [c for c in request['categories'] if c in ('snapshot', 'sync_history', 'old_outputs')]
                    if storage:
                        previews += self.jobs.job_storage_cleanup(rid, storage,
                            request['older_than_seconds'])['candidates']
                    with self.history.lock(rid):
                        run = self.history.get(rid)
                        eligible(self.jobs, run, request['include_failed'])
                        for candidate in previews:
                            info = tree(candidate['path'])
                            if info['newest_mtime'] <= started - request['older_than_seconds']:
                                plan['candidates'].append({**candidate, **info,
                                    'run_id': rid, 'run_facts': run_facts(run)})
            except (ValueError, OSError) as error:
                skipped.append({'run_id': rid, 'reason': str(error)[:2000]})
        if len(json.dumps(plan).encode()) > MAX_PLAN_BYTES:
            raise ValueError('cleanup plan exceeds 4 MiB; reduce limit')
        token = digest(plan)
        self.save(token, {'plan': plan, 'state': 'preview', 'results': []})
        return {'ok': True, 'dry_run': True, 'review_token': token, 'plan': plan,
            'candidate_count': len(plan['candidates']), 'bytes': sum(c['bytes'] for c in plan['candidates']),
            'skipped': skipped, 'scanned_runs': len(runs), 'next_offset': next_offset,
            'omitted_shared_cache_entries': omitted_blobs,
            'approval': self.history.approvals.describe(),
            'notes': ['Only registered task data and independently copied shared cache blobs are eligible.',
                'Pending installation/active workers, latest outputs, project inputs and unknown directories are protected.',
                'Remote cleanup deletes unselected remote files too; retain every required result first.',
                'No timer is installed; repeat preview/approval for subsequent maintenance.']}

    def execute(self, kind, token, confirmation_note):
        if not isinstance(confirmation_note, str) or not confirmation_note.strip() or len(confirmation_note) > 4096:
            raise ValueError('explicit user confirmation_note required')
        with state_lock(self.history, 'maintenance-' + kind + '.lock'):
            value = self.load(token)
            plan = value['plan']
            if plan['kind'] != kind:
                raise ValueError('cleanup token belongs to a different tool')
            if value['state'] == 'complete':
                return self.result(token, value)
            if time.time() >= plan['expires_at']:
                raise ValueError('cleanup plan expired; preview again')
            if plan['state_dir'] != str(self.history.root):
                raise ValueError('cleanup state directory changed')
            proof = require(self.history, kind, plan['subject'], token)
            value.update(state='applying', confirmation_note=confirmation_note,
                         external_approval=proof, accepted_at=now())
            self.save(token, value)
            completed = {r['path'] for r in value['results'] if r['ok']}
            for candidate in plan['candidates']:
                if candidate['path'] in completed:
                    continue
                try:
                    if time.time() >= plan['expires_at']:
                        raise ValueError('cleanup plan expired; preview remaining data again')
                    if kind == 'cluster_work_cleanup':
                        result = self.jobs.job_remote_cleanup(candidate['run_id'], False,
                            _maintenance_token=token, _cleanup_cutoff=plan['cutoff'],
                            _registry_revision=candidate['retention_revision'])
                        if not result['ok']:
                            raise ValueError(result.get('diagnostic'))
                    else:
                        self.remove_local(plan, candidate)
                    result = {'ok': True, 'path': candidate['path'],
                              'bytes': candidate['bytes'], 'category': candidate['category']}
                except (ValueError, OSError) as error:
                    result = {'ok': False, 'path': candidate['path'], 'error': str(error)[:2000]}
                value['results'] = [r for r in value['results'] if r['path'] != candidate['path']] + [result]
                self.save(token, value)
            value['state'] = 'complete' if all(r['ok'] for r in value['results']) else 'needs_attention'
            self.save(token, value)
            with self.history.connect() as db:
                db.execute('INSERT INTO events(run_id,time,kind,detail) VALUES (?,?,?,?)',
                    ('maintenance', now(), kind, json.dumps({'review_token': token,
                     'state': value['state'], 'external_approval': proof})))
            return self.result(token, value)

    def remove_local(self, plan, candidate):
        rid = candidate.get('run_id')
        lock = self.history.lock(rid) if rid else InputCache(self.history).lock()
        with lock:
            if rid:
                run = self.history.get(rid)
                eligible(self.jobs, run, plan['request']['include_failed'])
                if run_facts(run) != candidate['run_facts']:
                    raise ValueError('task or retained results changed after cleanup preview')
            require(self.history, 'local_cache_cleanup', plan['subject'], digest(plan))
            path = Path(candidate['path'])
            try:
                current = tree(path)
            except FileNotFoundError:
                current = None
            if current is not None:
                if current['fingerprint'] != candidate['fingerprint']:
                    raise ValueError('cache contents/identity changed after preview; preview again')
                with directory(path.parent) as fd:
                    if candidate['is_directory']:
                        if not shutil.rmtree.avoids_symlink_attacks:
                            raise ValueError('platform lacks safe directory cleanup')
                        shutil.rmtree(path.name, dir_fd=fd)
                    else:
                        os.unlink(path.name, dir_fd=fd)
            if rid:
                changes = {'last_maintenance_cleanup': {'path': str(path), 'token': digest(plan)}}
                if candidate['category'] == 'snapshot':
                    changes['snapshot_removed'] = True
                self.history.update(rid, 'maintenance_cache_cleaned', **changes)

    @staticmethod
    def result(token, value):
        return {'ok': value['state'] == 'complete', 'dry_run': False, 'review_token': token,
            'state': value['state'], 'results': value['results'],
            'deleted_bytes': sum(r.get('bytes', 0) for r in value['results'] if r['ok'])}

    def local_cache_cleanup(self, cluster=None, project_root=None, run_ids=None,
            older_than_seconds=604800, categories=None, max_input_cache_bytes=None,
            include_failed=False, limit=100, offset=0, dry_run=True,
            review_token=None, confirmation_note=None):
        if type(dry_run) is not bool:
            raise ValueError('dry_run must be boolean')
        if not dry_run:
            return self.execute('local_cache_cleanup', review_token, confirmation_note)
        request = self.request('local_cache_cleanup', cluster, project_root, run_ids,
            older_than_seconds, include_failed, limit, offset, categories, max_input_cache_bytes)
        return self.preview('local_cache_cleanup', request)

    def cluster_work_cleanup(self, cluster=None, project_root=None, run_ids=None,
            older_than_seconds=604800, include_failed=False, limit=100, offset=0,
            dry_run=True, review_token=None, confirmation_note=None):
        if type(dry_run) is not bool:
            raise ValueError('dry_run must be boolean')
        if not dry_run:
            return self.execute('cluster_work_cleanup', review_token, confirmation_note)
        request = self.request('cluster_work_cleanup', cluster, project_root, run_ids,
            older_than_seconds, include_failed, limit, offset)
        return self.preview('cluster_work_cleanup', request)
