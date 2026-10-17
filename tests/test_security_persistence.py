"""Crash-window recovery and bounded history retention without external services."""

import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_jobs
from hpc_mcp.history import History, now
from hpc_mcp.jobs import JobService


class InstallationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_jobs.JobLifecycleTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.jobs = self.fixture.jobs
        self.run = self.fixture.prepare()
        self.rid = self.run['run_id']
        test_jobs.authorized_submit(self.jobs, self.rid)

    def restart(self):
        self.jobs = JobService(self.jobs.clusters, self.fixture.state,
                              self.fixture.transport, self.fixture.transfer)

    def interrupted_link(self):
        link = os.link
        def crash(src, dst, **kwargs):
            link(src, dst, **kwargs)
            raise SystemExit('crash after publication, before progress commit')
        with patch('hpc_mcp.result_install.os.link', side_effect=crash):
            with self.assertRaises(SystemExit):
                test_jobs.authorized_sync(self.jobs, self.rid)

    def test_crash_after_publication_recovers_exact_file_without_redownload(self):
        self.interrupted_link()
        before = self.jobs.history.get(self.rid)
        target = Path(before['sync_installation']['target']) / 'results/value.txt'
        inode = target.stat().st_ino
        self.restart()
        real_transfer = self.fixture.transfer.run
        with patch.object(self.fixture.transfer, 'run', wraps=real_transfer) as transfer:
            result = self.jobs.job_sync(self.rid)
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['resumed'])
        self.assertEqual(target.stat().st_ino, inode)
        self.assertEqual(result['run']['sync_installation']['phase'], 'complete')
        self.assertFalse(Path(before['sync_installation']['staging']).exists())
        self.assertFalse(any(c.kwargs.get('download') and not c.kwargs.get('dry_run')
                             for c in transfer.call_args_list))

    def test_killed_process_during_copy_recovers_recorded_partial_temporary(self):
        test_jobs.authorize(self.jobs, self.rid)
        code = f'''
import os, signal
from unittest.mock import patch
from hpc_mcp.jobs import JobService
from hpc_mcp.config import Cluster
from pathlib import Path
from test_jobs import LocalSSH, LocalTransfer
jobs = JobService({{'lsf': Cluster('lsf', 'alias', 'lsf', {str(self.fixture.remote)!r})}},
    {str(self.fixture.state)!r}, LocalSSH(Path({str(self.fixture.bin)!r})), LocalTransfer())
def interrupted(source, target):
    target.write(b'partial copy'); target.flush()
    os.kill(os.getpid(), signal.SIGKILL)
with patch('hpc_mcp.result_install.shutil.copyfileobj', side_effect=interrupted):
    jobs.job_sync({self.rid!r})
'''
        root = Path(__file__).resolve().parents[1]
        process = subprocess.run([sys.executable, '-c', code], timeout=15,
            env=dict(os.environ, PYTHONPATH=str(root / 'src') + ':' + str(root / 'tests')))
        self.assertEqual(process.returncode, -9)
        journal = self.jobs.history.get(self.rid)['sync_installation']
        first = journal['files']['results/value.txt']
        partial = Path(journal['target']) / 'results' / first['temporary']
        self.assertEqual(partial.read_bytes(), b'partial copy')
        self.restart()
        result = self.jobs.job_sync(self.rid)
        self.assertTrue(result['ok'], result)
        self.assertEqual((Path(result['run']['output_dir']) / 'results/value.txt').read_text(), '42\n')
        self.assertFalse(partial.exists())

    def test_failure_after_all_files_install_can_finalize_without_staging(self):
        update = self.jobs.history.update
        def crash(rid, kind, **changes):
            if kind == 'synced':
                raise SystemExit('crash before success record')
            return update(rid, kind, **changes)
        with patch.object(self.jobs.history, 'update', side_effect=crash):
            with self.assertRaises(SystemExit):
                test_jobs.authorized_sync(self.jobs, self.rid)
        journal = self.jobs.history.get(self.rid)['sync_installation']
        shutil.rmtree(journal['staging'])
        self.restart()
        result = self.jobs.job_sync(self.rid)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['run']['sync_state'], 'complete')

    def test_recovery_rejects_same_content_replaced_by_other_writer(self):
        self.interrupted_link()
        journal = self.jobs.history.get(self.rid)['sync_installation']
        target = Path(journal['target']) / 'results/value.txt'
        other = target.with_name('other.tmp')
        other.write_bytes(target.read_bytes())
        other.replace(target)
        self.restart()
        result = self.jobs.job_sync(self.rid)
        self.assertFalse(result['ok'])
        self.assertIn('installed output changed', result['error'])
        self.assertEqual(target.read_text(), '42\n')
        self.assertTrue(Path(journal['staging']).is_dir())

    def test_recovery_requires_original_scope_and_unchanged_remote_selection(self):
        self.interrupted_link()
        with self.assertRaisesRegex(ValueError, 'original sync options|Sync selection'):
            self.jobs.job_sync(self.rid, resume=False)
        remote = Path(self.run['remote_dir']) / 'results/value.txt'
        remote.write_text('different remote result')
        with self.assertRaisesRegex(ValueError, 'remote selection changed'):
            self.jobs.job_sync(self.rid)
        self.assertEqual(self.jobs.history.get(self.rid)['sync_installation']['phase'], 'pending')

    def test_recovery_rejects_tampered_destination_and_cache_paths(self):
        self.interrupted_link()
        run = self.jobs.history.get(self.rid)
        original = run['sync_installation']
        for field, value in (('target', str(self.fixture.root / 'other')),
                             ('staging', str(self.fixture.source))):
            changed = dict(original, **{field: value})
            self.jobs.history.update(self.rid, 'test_tamper', sync_installation=changed)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'paths or options changed'):
                self.jobs.job_sync(self.rid)
        self.jobs.history.update(self.rid, 'test_restore', sync_installation=original)
        self.assertTrue(self.jobs.job_sync(self.rid)['ok'])

    def test_crash_after_backup_move_recovers_without_rearchiving(self):
        first = test_jobs.authorized_sync(self.jobs, self.rid, layout='direct')['run']
        move = shutil.move
        moved = []
        def crash(src, dst, **kwargs):
            result = move(src, dst, **kwargs)
            moved.append(dst)
            raise SystemExit('crash after backup move')
        with patch('hpc_mcp.sync_installation.shutil.move', side_effect=crash):
            with self.assertRaises(SystemExit):
                test_jobs.authorized_sync(self.jobs, self.rid, layout='direct', overwrite='replace')
        self.restart()
        result = self.jobs.job_sync(self.rid, layout='direct', overwrite='replace')
        self.assertTrue(result['ok'], result)
        self.assertEqual(len(moved), 1)
        self.assertEqual(Path(result['run']['output_dir']), Path(first['output_dir']))
        self.assertTrue((Path(result['run']['sync_backup']) / 'results/value.txt').is_file())

    def test_success_is_committed_before_staging_cleanup(self):
        remove = shutil.rmtree
        def fail_staging(path, **kwargs):
            if Path(path).parent.name == 'sync-attempts':
                raise OSError('cleanup failed')
            return remove(path, **kwargs)
        with patch('hpc_mcp.sync_installation.shutil.rmtree', side_effect=fail_staging):
            result = test_jobs.authorized_sync(self.jobs, self.rid)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['run']['sync_state'], 'complete')
        self.assertEqual(result['run']['sync_staging_cleanup_error'], 'cleanup failed')


class HistoryRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.history = History(self.temp.name)
        self.history.add({'run_id': 'test', 'created_at': now(), 'phase': 'prepared'})

    def test_telemetry_is_compact_and_bounded_but_audit_events_are_retained(self):
        self.history.update('test', 'submission_authorized', evidence='keep forever')
        with patch('hpc_mcp.history.TELEMETRY_LIMIT', 3):
            for index in range(8):
                self.history.update('test', 'status_observed', state='running',
                    status_diagnostics=[{'stderr': 'x' * 10000, 'returncode': index}])
        events = self.history.events('test')
        telemetry = [e for e in events if e['kind'] == 'status_observed']
        self.assertEqual(len(telemetry), 3)
        self.assertEqual(telemetry[-1]['detail']['diagnostic'], {'returncode': 7})
        self.assertNotIn('status_diagnostics', telemetry[-1]['detail'])
        self.assertEqual(len(self.history.get('test')['status_diagnostics'][0]['stderr']), 10000)
        self.assertTrue(any(e['kind'] == 'submission_authorized' for e in events))
        with self.history.connect() as db:
            plan = db.execute('EXPLAIN QUERY PLAN SELECT id FROM events WHERE run_id=? '
                              'AND id>? ORDER BY id LIMIT 5', ('test', 0)).fetchall()
        self.assertIn('events_run_id', str(plan))

    def test_indexed_cursor_pages_are_bounded_and_do_not_repeat_rows(self):
        for i in range(8):
            self.history.update('test', 'audit', evidence=i)
        ids, cursor = [], 0
        while True:
            page = self.history.event_page('test', cursor, 2)
            self.assertLessEqual(len(page['events']), 2)
            ids.extend(e['id'] for e in page['events'])
            if page['next_after_id'] is None:
                break
            cursor = page['next_after_id']
        self.assertEqual(len(ids), 9)
        self.assertEqual(len(set(ids)), 9)
        for kwargs in ({'limit': 501}, {'limit': True}, {'after_id': -1}):
            with self.assertRaises(ValueError):
                self.history.event_page('test', **kwargs)

    def test_archive_preserves_complete_evidence_before_removing_rows(self):
        self.history.update('test', 'submission_authorized', evidence='原始审计')
        before = self.history.events('test')
        preview = self.history.archive_events(0, dry_run=True)
        self.assertEqual(preview['count'], 2)
        self.assertEqual(self.history.events('test'), before)
        self.assertFalse((self.history.root / 'history-archives').exists())
        result = self.history.archive_events(0, dry_run=False)
        path = Path(result['archive']['path'])
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), result['archive']['sha256'])
        with gzip.open(path, 'rt') as stream:
            archived = [json.loads(line) for line in stream]
        self.assertEqual([e['detail'] for e in archived], [e['detail'] for e in before])
        self.assertEqual(self.history.events('test'), [])
        self.assertEqual(self.history.get('test')['phase'], 'prepared')
        self.assertEqual(self.history.archives()['archives'], [result['archive']])

    def test_large_event_pages_are_byte_bounded_and_full_evidence_is_chunked(self):
        self.history.update('test', 'audit', evidence='原始材料' * 10000)
        page = self.history.event_page('test')
        event = page['events'][-1]
        self.assertTrue(event['detail_omitted'])
        chunks, offset = [], 0
        while True:
            part = self.history.event_detail('test', event['id'], offset, 8192)
            self.assertLessEqual(len(part['text']), 8192)
            chunks.append(part['text'])
            if part['next_offset'] is None:
                break
            offset = part['next_offset']
        raw = ''.join(chunks)
        self.assertEqual(hashlib.sha256(raw.encode()).hexdigest(), event['detail']['sha256'])
        self.assertEqual(json.loads(raw)['evidence'], '原始材料' * 10000)
        for i in range(30):
            self.history.update('test', 'audit', evidence='x' * 60000, index=i)
        page = self.history.event_page('test', limit=100)
        self.assertLessEqual(len(json.dumps(page['events']).encode()), 1024 ** 2 + 1000)
        self.assertIsNotNone(page['next_after_id'])

    def test_failed_archive_never_removes_database_evidence(self):
        before = self.history.events('test')
        with patch('hpc_mcp.history_archives.gzip.GzipFile.write', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.history.archive_events(0, dry_run=False)
        self.assertEqual(self.history.events('test'), before)
        self.assertEqual(self.history.archives()['archives'], [])
        self.assertEqual(list((self.history.root / 'history-archives').iterdir()), [])

    def test_archive_rejects_symlink_directory(self):
        outside = self.history.root / 'outside'; outside.mkdir()
        (self.history.root / 'history-archives').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            self.history.archive_events(0, dry_run=False)
        self.assertTrue(self.history.events('test'))
        self.assertEqual(list(outside.iterdir()), [])
