"""Append-only onboarding evidence in the existing agent history database."""

import hashlib
import json
import uuid

from .history import now


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False, separators=(',', ':')).encode()).hexdigest()


class OnboardingStore:
    def __init__(self, history):
        self.history = history
        with history.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS onboarding_records (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, created_at TEXT NOT NULL,
                sha256 TEXT NOT NULL, data TEXT NOT NULL)''')

    def save(self, kind, data):
        record_id = 'o_' + uuid.uuid4().hex
        record = dict(data, report_id=record_id, created_at=now())
        encoded = json.dumps(record, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode()) > 4 * 1024 * 1024:
            raise ValueError('onboarding report exceeds 4 MiB')
        with self.history.connect() as db:
            db.execute('INSERT INTO onboarding_records VALUES (?,?,?,?,?)',
                       (record_id, kind, record['created_at'], digest(record), encoded))
        return record

    def get(self, record_id, kind=None):
        with self.history.connect() as db:
            row = db.execute('SELECT kind,sha256,data FROM onboarding_records WHERE id=?', (record_id,)).fetchone()
        if row is None or (kind is not None and row[0] != kind):
            raise ValueError('unknown onboarding report or unexpected report kind')
        data = json.loads(row[2])
        if digest(data) != row[1]:
            raise ValueError('onboarding report checksum mismatch')
        return data
