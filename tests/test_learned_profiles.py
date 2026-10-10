"""Historical profiles remain readable without authorizing application execution."""

from dataclasses import asdict, replace
import json
from pathlib import Path
import subprocess
import sys
import unittest

import test_jobs
from hpc_mcp.config import ConfigManager
from hpc_mcp.history import now
from hpc_mcp.onboarding_store import digest
from hpc_mcp.profiles import ProfileService
from hpc_mcp.responses import preparation_receipt
from hpc_mcp.service import ClusterService


class LegacyProfileTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_jobs.JobLifecycleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.jobs = self.fixture.jobs
        self.service = ClusterService(self.jobs.clusters, self.fixture.transport)
        self.profiles = ProfileService(self.service, self.jobs)
        definition = json.loads((Path(__file__).parents[1] /
                                 'examples/templates/gaussian-lsf.json').read_text())
        self.profile = {'profile_id': 'p_' + 'a' * 32, 'name': 'gaussian', 'version': 1,
            'cluster': 'lsf', 'application': 'gaussian', 'created_at': now(),
            'definition': definition, 'cluster_settings': asdict(self.jobs.clusters['lsf']),
            'cluster_sha256': digest(asdict(self.jobs.clusters['lsf'])),
            'report_ids': [], 'evidence_sha256': {}, 'unresolved': [], 'validation': 'unverified'}
        self.profile['review_token'] = digest(self.profile)
        with self.jobs.history.connect() as db:
            db.execute('INSERT INTO application_profiles VALUES (?,?,?,?,?,?)',
                (self.profile['profile_id'], 'gaussian', 1, 'lsf', 'gaussian', json.dumps(self.profile)))
            db.execute('INSERT INTO profile_confirmations VALUES (?,?,?)',
                (self.profile['profile_id'], now(), 'Historical confirmation'))
            db.execute('INSERT INTO profile_defaults VALUES (?,?,?)',
                ('lsf', 'gaussian', self.profile['profile_id']))

    def test_historical_definition_confirmation_and_validation_survive_restart(self):
        evidence = self.profiles.store.save('validation',
            {'profile_id': self.profile['profile_id'], 'status': 'validated'})
        restarted = ProfileService(self.service, self.jobs)
        profile = restarted.get(self.profile['profile_id'])['profile']
        self.assertEqual(profile['definition'], self.profile['definition'])
        self.assertEqual(json.loads(Path(profile['configuration_file']).read_text()), profile['definition'])
        self.assertTrue(profile['is_default'])
        self.assertEqual(profile['confirmation']['note'], 'Historical confirmation')
        self.assertEqual(profile['validation_evidence'], evidence)
        summary = restarted.list(cluster='lsf', application='gaussian', compact=True)['profiles'][0]
        self.assertNotIn('definition', summary)
        self.assertEqual(summary['configuration_file'], profile['configuration_file'])
        self.assertEqual(restarted.list(cluster='absent')['profiles'], [])
        self.jobs.clusters['lsf'] = replace(self.jobs.clusters['lsf'], ssh_host='changed')
        stale = restarted.get(self.profile['profile_id'])['profile']
        self.assertTrue(stale['requires_recheck'])
        self.assertEqual(stale['validation'], 'stale')

    def test_historical_checksums_and_configuration_are_still_protected(self):
        profile_id = self.profile['profile_id']
        path = Path(self.profiles.get(profile_id)['profile']['configuration_file'])
        path.write_text('invalid JSON')
        with self.assertRaisesRegex(ValueError, 'not valid JSON'):
            self.profiles.get(profile_id)
        path.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'configuration changed'):
            self.profiles.get(profile_id)
        path.unlink()
        path.symlink_to(self.fixture.source / 'data.txt')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.profiles.get(profile_id)
        with self.jobs.history.connect() as db:
            db.execute('UPDATE application_profiles SET data=? WHERE id=?',
                       (json.dumps(dict(self.profile, name='tampered')), profile_id))
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            self.profiles.get(profile_id)

    def test_compact_receipts_keep_details_in_history(self):
        full = self.jobs.job_prepare('lsf', str(self.fixture.source), 'job.sh')
        small = preparation_receipt(full)
        self.assertEqual(small['run']['run_id'], full['run']['run_id'])
        self.assertNotIn('manifest', small['run'])
        self.assertEqual(self.jobs.job_get(small['run']['run_id'])['run']['manifest'], full['run']['manifest'])
        self.assertIs(preparation_receipt(full, False), full)

    def test_cli_legacy_profile_does_not_authorize_unregistered_application(self):
        card = self.fixture.source / 'test.gjf'
        card.write_text('# hf\n\ntest\n\n0 1\nH 0 0 0\n\n')
        config = self.fixture.root / 'clusters.toml'
        values = asdict(self.jobs.clusters['lsf'])
        values.pop('name')
        ConfigManager(config, dict(self.jobs.clusters)).set('lsf', values)
        argv = [sys.executable, '-m', 'hpc_mcp', '--config', str(config),
                '--state-dir', str(self.fixture.state)]
        result = subprocess.run(argv + ['gaussian-prepare', 'lsf', str(card),
            '--project-root', str(self.fixture.root)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)['error'], 'application_not_registered')
        for action, arguments in [('profile-draft', ['name', 'lsf', 'gaussian', 'absent.json']),
                                  ('profile-confirm', [self.profile['profile_id'], 'token',
                                                       '--confirmation-note', 'historical']),
                                  ('profile-plan', [str(self.fixture.source)]),
                                  ('profile-validate', [self.profile['profile_id']])]:
            for legacy_arguments in (arguments, [], ['--unknown-legacy-option', 'value']):
                with self.subTest(action=action, arguments=legacy_arguments):
                    result = subprocess.run(argv + [action, *legacy_arguments],
                                            text=True, capture_output=True)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn('requires migration', json.loads(result.stdout)['error'])
        result = subprocess.run(argv + ['profile-list', '--unknown-option'],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('unrecognized arguments', result.stderr)
        self.assertEqual(self.jobs.job_list()['runs'], [])
        self.assertFalse((self.fixture.root / 'submissions').exists())
