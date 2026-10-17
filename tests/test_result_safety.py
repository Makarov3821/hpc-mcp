"""Regression tests for destination overlap, installation races and remote retention."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_jobs


class ResultSafetyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_jobs.JobLifecycleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.jobs = self.fixture.jobs

    def project_run(self):
        return self.jobs.job_prepare('lsf', str(self.fixture.source), 'job.sh',
            ['results/***'], project_root=str(self.fixture.root))['run']

    def submit(self, run):
        self.assertTrue(test_jobs.authorized_submit(self.jobs, run['run_id'])['ok'])

    def test_state_ancestor_is_rejected_by_plan_and_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / 'job.sh').write_text('#!/bin/bash\ntrue\n')
            run = self.jobs.job_prepare('lsf', directory, 'job.sh', ['state/history.sqlite3'])['run']
            self.submit(run)
            before = self.jobs.history.database.read_bytes()
            for overwrite in ('merge', 'replace'):
                options = dict(destination=str(self.fixture.root), overwrite=overwrite)
                with self.subTest(overwrite=overwrite):
                    with self.assertRaisesRegex(ValueError, 'overlaps input/state'):
                        self.jobs.job_submission_plan(run['run_id'], {'sync_options': options})
                    # Also enforce at execution, even if a stale/legacy scope passed its gate.
                    with patch('hpc_mcp.submission.sync_guard'), \
                            patch.object(self.jobs.transfer, 'run') as transfer:
                        with self.assertRaisesRegex(ValueError, 'overlaps input/state'):
                            self.jobs.job_sync(run['run_id'], **options)
                        transfer.assert_not_called()
            self.assertEqual(self.jobs.history.database.read_bytes(), before)

    def test_inplace_return_cannot_contain_state(self):
        source = self.fixture.root
        (source / 'job.sh').write_text('#!/bin/bash\ntrue\n')
        run = self.jobs.job_prepare('lsf', str(source), 'job.sh', ['result.log'],
            project_root=str(source), input_files=['job.sh'])['run']
        with self.assertRaisesRegex(ValueError, 'overlaps input/state'):
            self.jobs.job_submission_plan(run['run_id'])

    def test_error_policy_preserves_file_created_at_atomic_install(self):
        run = self.project_run()
        self.submit(run)
        target = self.fixture.source / 'results/value.txt'
        link = os.link

        def race(src, dst, **kwargs):
            target.write_text('concurrent result')
            return link(src, dst, **kwargs)

        with patch('hpc_mcp.result_install.os.link', side_effect=race):
            result = test_jobs.authorized_sync(self.jobs, run['run_id'], overwrite='error')
        self.assertFalse(result['ok'])
        self.assertEqual(target.read_text(), 'concurrent result')
        self.assertIn('output exists', result['error'])
        self.assertEqual(result['run']['sync_state'], 'failed')
        self.assertTrue(Path(result['run']['output_dir']).is_dir())
        self.assertFalse(list(self.fixture.source.rglob('.hpc-mcp-install-*')))

    def test_error_policy_preserves_symlink_created_at_atomic_install(self):
        run = self.project_run()
        self.submit(run)
        outside = self.fixture.root / 'outside.txt'
        outside.write_text('keep')
        target = self.fixture.source / 'results/value.txt'
        link = os.link

        def race(src, dst, **kwargs):
            target.symlink_to(outside)
            return link(src, dst, **kwargs)

        with patch('hpc_mcp.result_install.os.link', side_effect=race):
            result = test_jobs.authorized_sync(self.jobs, run['run_id'])
        self.assertFalse(result['ok'])
        self.assertTrue(target.is_symlink())
        self.assertEqual(outside.read_text(), 'keep')

    def test_error_policy_preserves_directory_created_before_install(self):
        run = self.fixture.prepare()
        self.submit(run)
        target = self.fixture.root / 'chosen'
        from hpc_mcp.result_install import install

        def race(*args, **kwargs):
            target.mkdir()
            (target / 'keep.txt').write_text('concurrent directory')
            return install(*args, **kwargs)

        with patch('hpc_mcp.result_install.install', side_effect=race):
            result = test_jobs.authorized_sync(self.jobs, run['run_id'], destination=str(target))
        self.assertFalse(result['ok'])
        self.assertEqual(list(target.iterdir()), [target / 'keep.txt'])
        self.assertEqual((target / 'keep.txt').read_text(), 'concurrent directory')

    def test_merge_replaces_racing_symlink_without_writing_its_referent(self):
        run = self.project_run()
        self.submit(run)
        outside = self.fixture.root / 'outside.txt'
        outside.write_text('keep')
        target = self.fixture.source / 'results/value.txt'
        replace = os.replace

        def race(src, dst, **kwargs):
            target.symlink_to(outside)
            return replace(src, dst, **kwargs)

        with patch('hpc_mcp.result_install.os.replace', side_effect=race):
            result = test_jobs.authorized_sync(self.jobs, run['run_id'], overwrite='merge')
        self.assertTrue(result['ok'], result)
        self.assertFalse(target.is_symlink())
        self.assertEqual(target.read_text(), '42\n')
        self.assertEqual(outside.read_text(), 'keep')

    def test_parent_symlink_swap_cannot_redirect_installed_file(self):
        run = self.project_run()
        self.submit(run)
        parent = self.fixture.source / 'results'
        parent.mkdir()
        outside = self.fixture.root / 'outside'
        outside.mkdir()
        open_file = os.open
        swapped = False

        def race(path, flags, *args, **kwargs):
            nonlocal swapped
            if path == 'results' and not swapped and flags & os.O_DIRECTORY:
                swapped = True
                parent.rmdir()
                parent.symlink_to(outside, target_is_directory=True)
            return open_file(path, flags, *args, **kwargs)

        with patch('hpc_mcp.result_install.os.open', side_effect=race):
            result = test_jobs.authorized_sync(self.jobs, run['run_id'], overwrite='merge')
        self.assertTrue(swapped)
        self.assertFalse(result['ok'])
        self.assertEqual(list(outside.iterdir()), [])

    def test_remote_cleanup_rejects_changed_missing_and_symlinked_outputs(self):
        run = self.fixture.prepare()
        self.submit(run)
        self.assertTrue(test_jobs.authorized_sync(self.jobs, run['run_id'])['ok'])
        test_jobs.release_retention(self.jobs, run['run_id'])
        test_jobs.authorize(self.jobs, run['run_id'], remote_cleanup=True)
        remote = Path(run['remote_dir'])
        file = remote / 'results/value.txt'
        content = file.read_bytes()
        metadata = file.stat()
        outside = self.fixture.root / 'outside.txt'
        outside.write_bytes(content)
        for mutation in ('same_size_same_mtime', 'missing', 'symlink', 'parent_symlink'):
            with self.subTest(mutation=mutation):
                if mutation == 'same_size_same_mtime':
                    file.write_bytes(b'99\n')
                    os.utime(file, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
                elif mutation == 'missing':
                    file.unlink()
                elif mutation == 'symlink':
                    file.unlink()
                    file.symlink_to(outside)
                else:
                    file.unlink()
                    file.parent.rmdir()
                    file.parent.symlink_to(outside.parent, target_is_directory=True)
                for dry_run in (True, False):
                    result = self.jobs.job_remote_cleanup(run['run_id'], dry_run=dry_run)
                    self.assertFalse(result['ok'], result)
                    self.assertIn('remote outputs changed or unsafe', result['diagnostic']['stderr'])
                    self.assertTrue(remote.is_dir())
                    self.assertFalse(self.jobs.history.get(run['run_id']).get('remote_removed'))
                if file.parent.is_symlink():
                    file.parent.unlink()
                    file.parent.mkdir()
                if file.is_symlink():
                    file.unlink()
                file.write_bytes(content)
        self.assertTrue(self.jobs.job_remote_cleanup(run['run_id'], dry_run=False)['ok'])
        self.assertFalse(remote.exists())
        self.assertEqual(outside.read_bytes(), content)

    def test_remote_hash_failure_never_deletes_directory(self):
        run = self.fixture.prepare()
        self.submit(run)
        self.assertTrue(test_jobs.authorized_sync(self.jobs, run['run_id'])['ok'])
        test_jobs.release_retention(self.jobs, run['run_id'])
        test_jobs.authorize(self.jobs, run['run_id'], remote_cleanup=True)
        sha = self.fixture.bin / 'sha256sum'
        for script in ('#!/bin/bash\nexit 1\n',
                       '#!/bin/bash\n/usr/bin/sha256sum "$@"\n'
                       'case "${@: -1}" in ./results/value.txt) exit 1;; esac\n'):
            with self.subTest(script=script):
                sha.write_text(script)
                sha.chmod(0o755)
                self.assertFalse(self.jobs.job_remote_cleanup(run['run_id'], dry_run=False)['ok'])
                self.assertTrue(Path(run['remote_dir']).exists())

    def test_remote_cleanup_quotes_output_names_and_sync_preserves_metadata(self):
        run = self.fixture.prepare()
        self.submit(run)
        remote = Path(run['remote_dir'])
        unusual = remote / "results/odd|'$(touch injected)'.txt"
        unusual.write_text('retained')
        unusual.chmod(0o750)
        os.utime(unusual, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
        synced = test_jobs.authorized_sync(self.jobs, run['run_id'])
        self.assertTrue(synced['ok'], synced)
        local = Path(synced['run']['output_dir']) / unusual.relative_to(remote)
        self.assertEqual(local.stat().st_mode & 0o777, 0o750)
        self.assertEqual(local.stat().st_mtime_ns, unusual.stat().st_mtime_ns)
        test_jobs.release_retention(self.jobs, run['run_id'])
        test_jobs.authorize(self.jobs, run['run_id'], remote_cleanup=True)
        self.assertTrue(self.jobs.job_remote_cleanup(run['run_id'])['ok'])
        self.assertFalse((remote / 'injected').exists())
        self.assertTrue(self.jobs.job_remote_cleanup(run['run_id'], dry_run=False)['ok'])

    def test_remote_cleanup_rejects_newly_selected_outputs_but_allows_unselected_files(self):
        run = self.fixture.prepare()
        self.submit(run)
        self.assertTrue(test_jobs.authorized_sync(self.jobs, run['run_id'])['ok'])
        test_jobs.release_retention(self.jobs, run['run_id'])
        test_jobs.authorize(self.jobs, run['run_id'], remote_cleanup=True)
        remote = Path(run['remote_dir'])
        new = remote / 'results/new.txt'
        new.write_text('not downloaded')
        for dry_run in (True, False):
            self.assertFalse(self.jobs.job_remote_cleanup(run['run_id'], dry_run=dry_run)['ok'])
            self.assertTrue(new.exists())
        new.unlink()
        (remote / 'unselected.tmp').write_text('excluded by the approved output rules')
        self.assertTrue(self.jobs.job_remote_cleanup(run['run_id'], dry_run=False)['ok'])


if __name__ == '__main__':
    unittest.main()
