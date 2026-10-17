"""Explicit, bounded archival: fsync full evidence before deleting database events."""

from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
import os
import uuid

from .history import now
from .result_install import directory

MAX_ARCHIVE_BYTES = 16 * 1024 ** 2


def archive(history, older_than_seconds=86400, limit=1000, dry_run=True):
    if type(older_than_seconds) is not int or not 0 <= older_than_seconds <= 10 ** 9:
        raise ValueError('older_than_seconds must be 0..1000000000')
    if type(limit) is not int or not 1 <= limit <= 1000 or type(dry_run) is not bool:
        raise ValueError('limit must be 1..1000 and dry_run must be boolean')
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)).isoformat()
    from .approvals import require
    from .onboarding_store import digest
    with history.connect() as db:
        if not dry_run:
            db.execute('BEGIN IMMEDIATE')
        cursor = db.execute('SELECT id,run_id,time,kind,detail FROM events WHERE time<? '
                            'ORDER BY time,id LIMIT ?', (cutoff, limit))
        payloads, ids, size = [], [], 0
        for i, rid, time, kind, detail in cursor:
            raw = (json.dumps({'id': i, 'run_id': rid, 'time': time, 'kind': kind,
                               'detail': json.loads(detail)}, ensure_ascii=False) + '\n').encode()
            if size + len(raw) > MAX_ARCHIVE_BYTES:
                break
            ids.append(i); payloads.append(raw); size += len(raw)
        result = {'ok': True, 'dry_run': dry_run, 'count': len(ids),
                  'uncompressed_bytes': size, 'cutoff': cutoff, 'archive': None}
        plan = {'older_than_seconds': older_than_seconds, 'limit': limit, 'event_ids': ids,
                'payload_sha256': hashlib.sha256(b''.join(payloads)).hexdigest(),
                'uncompressed_bytes': size}
        token = digest(plan)
        result.update(plan=plan, approval_token=token)
        if dry_run or not ids:
            return result
        proof = require(history, 'history_archive', 'events', token)
        accepted_at = now()
        archive_id = uuid.uuid4().hex
        name = archive_id + '.jsonl.gz'
        with directory(history.root / 'history-archives') as fd:
            descriptor = os.open(name, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
                                 0o600, dir_fd=fd)
            try:
                with os.fdopen(descriptor, 'w+b') as out:
                    with gzip.GzipFile(fileobj=out, mode='wb', mtime=0) as compressed:
                        for payload in payloads:
                            compressed.write(payload)
                    out.flush(); os.fsync(out.fileno()); out.seek(0)
                    digest = hashlib.file_digest(out, 'sha256').hexdigest()
                os.fsync(fd)
            except BaseException:
                os.unlink(name, dir_fd=fd)
                raise
        record = {'id': archive_id, 'path': str(history.root / 'history-archives' / name),
                  'sha256': digest, 'count': len(ids), 'first_id': min(ids), 'last_id': max(ids),
                  'uncompressed_bytes': size, 'cutoff': cutoff,
                  'approval_token': token, 'accepted_at': accepted_at,
                  'external_approval': proof}
        db.execute('INSERT INTO history_archives VALUES (?,?,?)',
                   (archive_id, now(), json.dumps(record)))
        db.executemany('DELETE FROM events WHERE id=?', [(i,) for i in ids])
        return dict(result, archive=record)
