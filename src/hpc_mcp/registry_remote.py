"""Standalone stdlib-only program sent over SSH; never imports the local package."""

import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import time
import uuid


def safe_file(name, create=False):
    flags = os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT if create else 0)
    fd = os.open(name, flags, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
        os.close(fd)
        raise ValueError('unsafe registry file')
    return fd


def host_identity():
    machine = Path('/etc/machine-id')
    value = machine.read_text().strip() if machine.is_file() else ''
    host = socket.gethostname()
    return {'id': hashlib.sha256((value + ':' + host).encode()).hexdigest(), 'hostname': host}


def sync_directory():
    fd = os.open('.', os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def cleanup_command(command, timeout):
    """Bound remote diagnostic memory and terminate the entire child process group on failure."""
    buffers = {'stdout': bytearray(), 'stderr': bytearray()}
    process = subprocess.Popen(['bash', '-e', '-c', command], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True)
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            for stream, name in ((process.stdout, 'stdout'), (process.stderr, 'stderr')):
                selector.register(stream, selectors.EVENT_READ, name)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError('cleanup command timed out; deleting record retained')
                for key, _ in selector.select(min(remaining, 0.1)):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    buffers[key.data].extend(chunk)
                    if len(buffers[key.data]) > 65536:
                        raise ValueError('cleanup diagnostic limit exceeded; deleting record retained')
        code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        return {'returncode': code, **{k: v.decode('utf-8', errors='replace')
                                      for k, v in buffers.items()}}
    except Exception:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        raise
    finally:
        process.stdout.close()
        process.stderr.close()


def run(request):
    if sys.version_info < (3, 8):
        raise ValueError('remote registry requires Python 3.8 or newer')
    root = Path(request['root'])
    if not root.is_absolute() or root == Path('/'):
        raise ValueError('registry requires a non-root absolute work_root')
    # Pin the configured root and private metadata directory; never traverse metadata links.
    root_fd = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fchdir(root_fd)
        initialize = request['action'] in ('probe', 'register') and request.get('initialize', True)
        if initialize:
            try:
                os.mkdir('.hpc-mcp-registry', 0o700)
                os.fsync(root_fd)
            except FileExistsError:
                pass
        try:
            meta_fd = os.open('.hpc-mcp-registry', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            if request['action'] == 'probe':
                return {'ok': True, 'initialized': False, 'host': host_identity(),
                        'cleanup_allowed': False, 'warnings': ['Registry absent; existing tasks remain protected.']}
            raise ValueError('registry absent; task is protected')
        try:
            info = os.fstat(meta_fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('registry directory must be owned by this user and mode 0700')
            os.fchdir(meta_fd)
            return locked(request, initialize)
        finally:
            os.close(meta_fd)
    finally:
        os.close(root_fd)


def locked(request, initialize):
    lock = safe_file('registry.lock', create=initialize)
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError('registry busy; cleanup is protected')
                time.sleep(0.05)
        host = host_identity()
        marker = 'workspace.json'
        if not Path(marker).exists() and initialize:
            if Path('registry.sqlite3').exists():
                raise ValueError('workspace identity missing beside existing database; inspect recovery')
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump({'version': 1, 'workspace_id': uuid.uuid4().hex,
                           'owner_host': host}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            sync_directory()
        fd = safe_file(marker)
        with os.fdopen(fd) as stream:
            workspace = json.load(stream)
        if (workspace.get('version') != 1
                or not re.fullmatch('[a-f0-9]{32}', workspace.get('workspace_id', ''))):
            raise ValueError('invalid workspace identity')
        owner = workspace['owner_host']
        conflict = owner['id'] != host['id']
        # Record conflicting hosts before opening SQLite. Never open the DB from a second host.
        if conflict and request.get('initialize', True):
            name = 'conflict-' + host['id'] + '.json'
            fd = safe_file(name, create=True)
            with os.fdopen(fd, 'w') as stream:
                json.dump({'host': host, 'observed_at': time.time()}, stream)
                stream.flush()
                stream.truncate()
                os.fsync(stream.fileno())
            sync_directory()
        conflicts = sorted(Path('.').glob('conflict-*.json'))
        if len(conflicts) > 128:
            raise ValueError('too many host conflicts; registry requires manual inspection')
        observed = []
        for path in conflicts:
            fd = safe_file(str(path))
            with os.fdopen(fd) as stream:
                observed.append(json.load(stream))
        response = {'ok': True, 'initialized': True, 'workspace_id': workspace['workspace_id'],
                    'host': host, 'owner_host': owner, 'node_conflict': conflict or bool(conflicts),
                    'cleanup_allowed': not conflict and not conflicts, 'conflicts': observed, 'warnings': []}
        if request.get('workspace_id') is not None and request['workspace_id'] != workspace['workspace_id']:
            raise ValueError('workspace identity changed; data protected')
        if response['node_conflict']:
            response['warnings'].append('Shared work_root observed from different login hosts; '
                'use the owner_host. Destructive cleanup is blocked until explicitly reconciled.')
        if request['action'] == 'probe' and (conflict or not initialize):
            return response
        if conflict:
            raise ValueError('registry belongs to another login host; use ' + owner['hostname'])
        database = 'registry.sqlite3'
        existed = Path(database).exists()
        if not existed and not initialize:
            raise ValueError('registry database missing; task is protected')
        for name in (database, database + '-journal', database + '-wal', database + '-shm'):
            if os.path.lexists(name):
                os.close(safe_file(name))
        if not existed:
            os.close(safe_file(database, create=True))
        db = sqlite3.connect(database, timeout=5)
        try:
            db.execute('PRAGMA journal_mode=DELETE')
            db.execute('PRAGMA synchronous=FULL')
            if not existed:
                db.executescript('''
                    CREATE TABLE tasks (run_id TEXT PRIMARY KEY, data TEXT NOT NULL);
                    CREATE TABLE events (id INTEGER PRIMARY KEY, time REAL NOT NULL,
                                         run_id TEXT, action TEXT NOT NULL, detail TEXT NOT NULL);
                ''')
            if request['action'] == 'probe':
                return response
            if request['action'] == 'reconcile':
                if request['conflicts'] != observed:
                    raise ValueError('conflict observations changed; review again')
                with db:
                    db.execute('INSERT INTO events(time,run_id,action,detail) VALUES (?,?,?,?)',
                        (time.time(), None, 'reconcile', json.dumps({
                            'conflicts': observed, 'reason': request['reason']})))
                for path in conflicts:
                    os.unlink(str(path))
                sync_directory()
                response.update(node_conflict=False, cleanup_allowed=True, conflicts=[], warnings=[])
                return response
            return tasks(db, request, response)
        finally:
            db.close()
    finally:
        os.close(lock)


def tasks(db, request, response):
    action, results = request['action'], []
    ids = request.get('run_ids', [])
    if not isinstance(ids, list) or not 1 <= len(ids) <= 500:
        raise ValueError('1..500 explicit run IDs required')
    if len(set(ids)) != len(ids) or any(not re.fullmatch('r_[a-f0-9]{32}', r) for r in ids):
        raise ValueError('invalid run IDs')
    holder = request.get('holder')
    if action in ('register', 'hold', 'release', 'dependency_hold', 'auto_release',
                  'auto_dependency_release', 'invalidate') and (
            not isinstance(holder, str) or not re.fullmatch('[A-Za-z0-9_.:-]{1,180}', holder)):
        raise ValueError('invalid holder identity')
    db.execute('BEGIN IMMEDIATE')
    for rid in ids:
        row = db.execute('SELECT data FROM tasks WHERE run_id=?', (rid,)).fetchone()
        task = json.loads(row[0]) if row else None
        if action == 'register':
            facts = request['facts']
            if task and task['origin'] != facts:
                raise ValueError('task registration identity mismatch')
            if not task:
                task = {'run_id': rid, 'origin': facts, 'state': 'live', 'revision': 0, 'holds': {}}
        if not task:
            if action == 'status':
                results.append({'run_id': rid, 'registered': False, 'cleanup_allowed': False})
                continue
            raise ValueError('unregistered task protected: ' + rid)
        if action == 'status':
            ready = (response['cleanup_allowed']
                and task['state'] == 'live' and bool(task['holds'])
                and all(h['released'] for h in task['holds'].values()))
            results.append(dict(task, registered=True, cleanup_allowed=ready, cleanup_ready=ready))
            continue
        if task['state'] != 'live':
            claim = task['holds'].get(holder, {})
            if (action == 'release' and claim.get('released')
                    and (claim.get('release_token') == request['review_token']
                         or request['revisions'].get(rid) == task['revision'])):
                results.append(task)
                continue
            raise ValueError('task is deleting/deleted; cannot acquire, release or repeat deletion')
        changed = False
        if action in ('register', 'hold', 'dependency_hold'):
            if action == 'dependency_hold' and task['origin'] != request['facts']:
                raise ValueError('dependency registration identity mismatch')
            previous = task['holds'].get(holder)
            if previous is None or previous['released']:
                if previous is None and len(task['holds']) >= 1000:
                    raise ValueError('too many holders for one task')
                task['holds'][holder] = {'released': False, 'reason': request.get('reason', ''),
                                        'created_at': time.time(), 'manual_override': action == 'hold'}
                changed = True
            claim = task['holds'][holder]
            if action == 'hold' and not claim.get('manual_override'):
                claim['manual_override'] = True
                changed = True
            if action == 'dependency_hold' and claim.get('dependency') != request['binding']:
                claim['dependency'] = request['binding']
                changed = True
        elif action == 'release':
            claim = task['holds'].get(holder)
            if claim is None:
                raise ValueError('this holder has no claim; cannot release another holder')
            repeated = claim['released'] and claim.get('release_token') == request['review_token']
            if not repeated and request['revisions'].get(rid) != task['revision']:
                raise ValueError('retention changed after release preview')
            if not claim['released'] or claim.get('release_source') == 'automatic':
                claim.update(released=True, released_at=time.time(), reason=request['reason'],
                             release_token=request['review_token'], release_source='manual')
                changed = True
        elif action in ('auto_release', 'auto_dependency_release', 'invalidate'):
            if task['origin'] != request['facts']:
                raise ValueError('automatic retention identity mismatch')
            if request['revisions'].get(rid) != task['revision']:
                raise ValueError('retention changed during automatic completion; retry')
            claim = task['holds'].get(holder)
            if not claim:
                raise ValueError('automatic completion has no matching holder')
            evidence = request['evidence']
            if action == 'invalidate':
                if claim['released'] and claim.get('release_source') == 'automatic':
                    claim.update(released=False, reason=request['reason'])
                    changed = True
            else:
                if not response['cleanup_allowed'] or claim.get('manual_override'):
                    raise ValueError('host conflict or explicit hold blocks automatic release')
                if action == 'auto_dependency_release':
                    if (claim.get('dependency') != request['binding']
                            or evidence.get('run_id') != request['binding']['consumer_run_id']):
                        raise ValueError('dependency completion does not match confirmed consumer')
                elif evidence.get('run_id') != rid or claim.get('dependency'):
                    raise ValueError('completion evidence belongs to another stage')
                previous = claim.get('automatic_evidence', {})
                if not claim['released'] or (claim.get('release_source') == 'automatic' and any(
                        previous.get(k) != evidence.get(k) for k in ('authority', 'facts', 'plan_token'))):
                    claim.update(released=True, released_at=time.time(), release_source='automatic',
                        automatic_evidence=evidence, reason=request['reason'])
                    changed = True
        elif action == 'observe':
            task.setdefault('observations', {})[request['observer']] = request['observation']
            db.execute('UPDATE tasks SET data=? WHERE run_id=?', (json.dumps(task), rid))
            db.execute('INSERT INTO events(time,run_id,action,detail) VALUES (?,?,?,?)',
                (time.time(), rid, action, json.dumps(request['observation'])))
        elif action == 'cleanup':
            if not response['cleanup_allowed']:
                raise ValueError('login host conflict blocks cleanup')
            if task['origin'] != request['facts']:
                raise ValueError('cleanup registration identity mismatch')
            if request.get('revision') is not None and task['revision'] != request['revision']:
                raise ValueError('retention changed after cleanup preview')
            if not task['holds'] or any(not h['released'] for h in task['holds'].values()):
                raise ValueError('task has active retention claims; explicit release required')
            task['state'] = 'deleting'
            changed = True
        else:
            raise ValueError('unknown registry action')
        if changed:
            task['revision'] += 1
            db.execute('INSERT OR REPLACE INTO tasks VALUES (?,?)', (rid, json.dumps(task)))
            detail = {'holder': holder, 'reason': request.get('reason')}
            for key in ('evidence', 'binding'):
                if request.get(key):
                    detail[key] = request[key]
            db.execute('INSERT INTO events(time,run_id,action,detail) VALUES (?,?,?,?)',
                (time.time(), rid, action, json.dumps(detail)))
        results.append(task)
    db.commit()
    if action == 'cleanup':
        if len(ids) != 1:
            raise ValueError('cleanup must address exactly one task')
        # Registry lock spans validation and removal. A crash leaves deleting, never releasable.
        result = cleanup_command(request['command'], request['timeout'])
        task = results[0]
        task['state'] = 'deleted' if result['returncode'] == 0 else 'live'
        task['revision'] += 1
        with db:
            db.execute('UPDATE tasks SET data=? WHERE run_id=?', (json.dumps(task), ids[0]))
            db.execute('INSERT INTO events(time,run_id,action,detail) VALUES (?,?,?,?)',
                (time.time(), ids[0], 'cleanup_result', json.dumps({'returncode': result['returncode']})))
        return dict(response, tasks=results, command_result=result)
    return dict(response, tasks=results)


if __name__ == '__main__':
    os.umask(0o077)
    try:
        payload = json.loads(base64.b64decode(sys.argv[1]))
        print(json.dumps(run(payload)))
    except Exception as error:
        print(json.dumps({'ok': False, 'error': 'registry_protected', 'message': str(error)}))
        sys.exit(1)
