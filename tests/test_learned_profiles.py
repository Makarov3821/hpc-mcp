"""Learned JSON execution defaults survive restarts without repeated agent configuration."""

from dataclasses import asdict, replace
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_jobs
from hpc_mcp.config import ConfigManager
from hpc_mcp.profiles import ProfileService
from hpc_mcp.responses import preparation_receipt
from hpc_mcp.service import ClusterService


class LearnedProfileTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_jobs.JobLifecycleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.jobs = self.fixture.jobs
        self.service = ClusterService(self.jobs.clusters, self.fixture.transport)
        self.profiles = ProfileService(self.service, self.jobs)
        self.card = self.fixture.source / 'test.gjf'
        self.original = '%chk=test.chk\n%nprocshared=2\n%mem=1GB\n# hf/sto-3g\n\ntest\n\n0 1\nH 0 0 0\n\n'
        self.card.write_text(self.original)
        self.definition = json.loads((Path(__file__).parents[1] / 'examples/profiles/gaussian-lsf.json').read_text())
        self.definition['parameters']['cpus']['default'] = 28

    def draft(self, definition=None):
        return self.profiles.draft('qg16', 'lsf', 'gaussian', definition or self.definition)['profile']

    def confirm(self, profile):
        return self.profiles.confirm(profile['profile_id'], profile['review_token'],
            'User reviewed the executable JSON configuration')['profile']

    def prepare(self, profiles=None, **options):
        return (profiles or self.profiles).gaussian_plan('lsf', str(self.card), str(self.fixture.root), **options)

    def test_default_json_is_loaded_after_restart_and_preserves_card(self):
        profile = self.confirm(self.draft())
        configuration = Path(profile['configuration_file'])
        self.assertEqual(json.loads(configuration.read_text()), self.definition)
        self.assertTrue(configuration.is_relative_to(self.fixture.state))
        restarted = ProfileService(self.service, self.jobs)
        with patch.object(self.jobs.transport, 'run') as remote:
            result = self.prepare(restarted)
            remote.assert_not_called()
        run = result['run']
        self.assertEqual(run['generation']['spec']['resources']['cpus'], 28)
        self.assertEqual(run['generation']['spec']['resources']['queue'], 'Gaussian')
        self.assertEqual(run['generation']['spec']['setup_steps'], self.definition['spec']['setup_steps'])
        self.assertEqual(run['outputs'], ['test.log', 'test.chk'])
        self.assertEqual(run['application']['changes'], {})
        self.assertEqual(run['application']['diff'], '')
        self.assertEqual((Path(run['snapshot_dir']) / 'test.gjf').read_text(), self.original)
        self.assertEqual(self.card.read_text(), self.original)
        self.assertEqual(run['profile']['configuration_file'], str(configuration))
        self.assertEqual(run['template']['parameters']['input'], 'test.gjf')
        self.assertEqual(run['template']['parameters']['stem'], 'test')
        self.assertEqual(list(self.fixture.source.glob('*.md')), [])
        self.assertFalse((self.fixture.root / 'submissions').exists())

    def test_confirmation_cluster_binding_and_no_silent_fallback(self):
        draft = self.draft()
        with self.assertRaisesRegex(ValueError, 'no confirmed default'):
            self.prepare()
        with self.assertRaisesRegex(ValueError, 'requires user confirmation'):
            self.prepare(profile_id=draft['profile_id'])
        self.confirm(draft)
        with self.assertRaisesRegex(ValueError, 'fewer than Gaussian'):
            self.prepare(parameters={'cpus': 1})
        self.service.clusters['lsf'] = replace(self.service.clusters['lsf'], ssh_host='changed')
        with self.assertRaisesRegex(ValueError, 'cluster settings changed'):
            self.prepare()

    def test_editing_json_requires_new_reviewed_version(self):
        first = self.confirm(self.draft())
        path = Path(first['configuration_file'])
        edited = json.loads(path.read_text())
        edited['parameters']['cpus']['default'] = 16
        path.write_text(json.dumps(edited))
        with self.assertRaisesRegex(ValueError, 'configuration changed'):
            self.prepare()
        second = self.confirm(self.draft(json.loads(path.read_text())))
        self.assertEqual(second['version'], 2)
        path.write_text(json.dumps(self.definition))
        self.assertEqual(self.prepare()['run']['generation']['spec']['resources']['cpus'], 16)
        self.assertEqual(self.prepare(profile_id=first['profile_id'])['run']['generation']['spec']['resources']['cpus'], 28)

    def test_configuration_symlinks_and_invalid_json_are_rejected(self):
        profile = self.confirm(self.draft())
        path = Path(profile['configuration_file'])
        path.write_text('invalid JSON')
        with self.assertRaisesRegex(ValueError, 'not valid JSON'):
            self.prepare()
        path.unlink()
        path.symlink_to(self.card)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.prepare()

    def test_compact_discovery_and_receipts_keep_details_in_history(self):
        profile = self.confirm(self.draft())
        summary = self.profiles.list(compact=True)['profiles'][0]
        self.assertTrue(summary['is_default'])
        self.assertNotIn('definition', summary)
        self.assertIn('parameters', summary)
        self.assertEqual(summary['configuration_file'], profile['configuration_file'])
        full = self.prepare()
        small = preparation_receipt(full)
        self.assertEqual(small['run']['run_id'], full['run']['run_id'])
        self.assertNotIn('rendered', small)
        self.assertNotIn('manifest', small['run'])
        self.assertLess(len(json.dumps(small)), len(json.dumps(full)) // 2)
        self.assertEqual(self.jobs.job_get(small['run']['run_id'])['run']['manifest'], full['run']['manifest'])
        self.assertIs(preparation_receipt(full, False), full)

    def test_cli_reuses_default_without_spec_or_outputs(self):
        self.confirm(self.draft())
        config = self.fixture.root / 'clusters.toml'
        values = asdict(self.jobs.clusters['lsf'])
        values.pop('name')
        ConfigManager(config, dict(self.jobs.clusters)).set('lsf', values)
        command = [sys.executable, '-m', 'hpc_mcp', '--config', str(config),
                   '--state-dir', str(self.fixture.state), 'gaussian-prepare', 'lsf', str(self.card),
                   '--project-root', str(self.fixture.root)]
        result = subprocess.run(command, text=True, capture_output=True, check=True)
        receipt = json.loads(result.stdout)
        self.assertTrue(receipt['compact'])
        self.assertEqual(receipt['resources']['cpus'], 28)
        self.assertEqual(receipt['run']['profile']['name'], 'qg16')
        self.assertEqual(receipt['gaussian']['diff'], '')
