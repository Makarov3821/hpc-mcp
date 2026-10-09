"""Accounting identities, units, missing data and local scoped summaries."""

from pathlib import Path
import unittest
from unittest.mock import patch

import test_jobs
from hpc_mcp.accounting import AccountingService, FIELDS, duration, parse_lsf, parse_slurm
from hpc_mcp.ssh import CommandResult


def slurm_row(job_id='42', name='r_example', **values):
    row = dict(JobIDRaw=job_id, JobName=name, State='COMPLETED', Submit='2026-10-09T10:00:00',
        Start='2026-10-09T10:00:10', End='2026-10-09T10:02:10', ElapsedRaw='120',
        AllocCPUS='4', ReqCPUS='4', ReqMem='8Gn', CPUTimeRAW='480', TotalCPU='00:02:12',
        MaxRSS='', Partition='compute', Account='lab')
    row.update(values)
    return '|'.join(row.get(field, '') for field in FIELDS) + '\n'


def lsf_record(job_id='42', name='r_example', memory='4G'):
    return (f'Job <{job_id}>, Job Name <{name}>, Status <DONE>, User <user>\n'
        'CPU_T WAIT TURNAROUND STATUS HOG_FACTOR MEM SWAP\n'
        f'12.5 5 25 done 0.5 {memory} 0M\n')


class AccountingParsingTests(unittest.TestCase):
    def test_slurm_allocation_and_step_metrics_never_double_count(self):
        text = slurm_row() + slurm_row('42.batch', 'batch', TotalCPU='00:01:00', MaxRSS='2048K') \
            + slurm_row('42.0', 'step', TotalCPU='00:01:12', MaxRSS='16384K')
        metrics = parse_slurm(text, '42', 'r_example')
        self.assertEqual(metrics['actual_cpu_seconds'], 132)
        self.assertEqual(metrics['allocated_cpu_seconds'], 480)
        self.assertEqual(metrics['peak_rss_bytes'], 16 * 1024 ** 2)
        self.assertIn('not aggregate', metrics['peak_rss_scope'])
        self.assertEqual(metrics['requested_memory_bytes'], 8 * 1024 ** 3)
        self.assertEqual(metrics['requested_memory_scope'], 'per_node')
        self.assertEqual(metrics['wait_seconds'], 10)
        self.assertEqual(len(metrics['steps']), 2)

    def test_slurm_missing_zero_and_duplicate_identity(self):
        row = slurm_row(TotalCPU='', MaxRSS='', ReqMem='', ElapsedRaw='', Start='Unknown')
        metrics = parse_slurm(row, '42', 'r_example')
        self.assertIsNone(metrics['actual_cpu_seconds'])
        self.assertIsNone(metrics['wait_seconds'])
        self.assertIsNone(metrics['requested_memory_bytes'])
        self.assertIsNone(metrics['peak_rss_bytes'])
        for text in (slurm_row(name='wrong-run'), slurm_row() + slurm_row(), '42|broken\n'):
            with self.assertRaises(ValueError):
                parse_slurm(text, '42', 'r_example')
        metrics = parse_slurm(slurm_row(TotalCPU='00:00:00'), '42', 'r_example')
        self.assertEqual(metrics['actual_cpu_seconds'], 0)
        self.assertTrue(any('zero' in warning for warning in metrics['warnings']))
        self.assertEqual(duration('1-02:03:04.5'), 93784.5)

    def test_lsf_table_and_explicit_memory_units(self):
        metrics = parse_lsf(lsf_record(), '42', 'r_example')
        self.assertEqual(metrics['actual_cpu_seconds'], 12.5)
        self.assertEqual(metrics['wait_seconds'], 5)
        self.assertEqual(metrics['elapsed_seconds'], 20)
        self.assertEqual(metrics['peak_rss_bytes'], 4 * 1024 ** 3)
        self.assertIsNone(metrics['allocated_cpu_seconds'])
        self.assertIsNone(parse_lsf(lsf_record(memory='4096'), '42', 'r_example')['peak_rss_bytes'])
        with self.assertRaises(ValueError):
            parse_lsf(lsf_record(name='wrong'), '42', 'r_example')
        with self.assertRaises(ValueError):
            parse_lsf(lsf_record() + lsf_record(), '42', 'r_example')


class AccountingLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_jobs.JobLifecycleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.jobs = self.fixture.jobs
        self.accounting = AccountingService(self.jobs)

    def submitted(self, scheduler='slurm'):
        run = self.fixture.prepare(scheduler)
        self.jobs.job_submit(run['run_id'])
        return self.jobs.history.get(run['run_id'])

    def test_refresh_persists_and_failed_refresh_retains_stale_evidence(self):
        run = self.submitted()
        with patch.object(self.jobs.transport, 'run', return_value=CommandResult(0, slurm_row('4242', run['run_id']))) as remote:
            result = self.accounting.usage(run['run_id'])
        self.assertTrue(result['ok'])
        self.assertIn('--duplicates', remote.call_args.args[1])
        self.assertNotIn(' -X ', remote.call_args.args[1])  # Steps required for RSS.
        self.assertEqual(result['usage']['metrics']['actual_cpu_seconds'], 132)
        with patch.object(self.jobs.transport, 'run', return_value=CommandResult(1, stderr='permission denied')):
            unavailable = self.accounting.usage(run['run_id'])
        self.assertFalse(unavailable['ok'])
        self.assertTrue(unavailable['stale'])
        self.assertEqual(unavailable['usage'], result['usage'])
        with patch.object(self.jobs.transport, 'run') as remote:
            cached = AccountingService(self.jobs).usage(run['run_id'], refresh=False)
            remote.assert_not_called()
        self.assertEqual(cached['usage'], result['usage'])
        report = self.accounting.report()
        self.assertEqual(report['stale_runs'], 1)
        self.assertTrue(report['runs'][0]['stale'])

    def test_lsf_query_and_missing_accounting_are_not_zero(self):
        run = self.submitted('lsf')
        with patch.object(self.jobs.transport, 'run', return_value=CommandResult(0, lsf_record('4242', run['run_id']))) as remote:
            result = self.accounting.usage(run['run_id'])
        self.assertTrue(result['ok'])
        self.assertIn('bacct -l -S', remote.call_args.args[1])
        with patch.object(self.jobs.transport, 'run', return_value=CommandResult(0, '')):
            empty = self.accounting.usage(run['run_id'])
        self.assertFalse(empty['ok'])
        self.assertTrue(empty['stale'])
        self.assertEqual(empty['usage']['metrics']['actual_cpu_seconds'], 12.5)

    def test_summary_scopes_pagination_and_missing_coverage(self):
        run = self.submitted()
        with patch.object(self.jobs.transport, 'run', return_value=CommandResult(0, slurm_row('4242', run['run_id']))):
            self.accounting.usage(run['run_id'])
        missing = self.fixture.prepare('slurm')
        self.jobs.history.update(run['run_id'], 'project', project_root=str(self.fixture.root))
        report = self.accounting.report(cluster='slurm')
        self.assertEqual(report['selected_runs'], 2)
        total = report['totals']['actual_cpu_seconds']
        self.assertEqual(total, {'sum': 132, 'known_runs': 1, 'missing_runs': 1})
        scoped = self.accounting.report(project_root=str(self.fixture.root))
        self.assertEqual(scoped['selected_runs'], 1)
        page = self.accounting.report(limit=1)
        self.assertTrue(page['truncated'])
        self.assertEqual(page['selected_runs'], 1)
        self.assertEqual(page['total_matching_runs'], 2)
        empty = self.accounting.report(since='2030-01-01T00:00:00+00:00')
        self.assertIsNone(empty['totals']['actual_cpu_seconds']['sum'])
        with self.assertRaises(ValueError):
            self.accounting.report(since='2026-10-09')
        with patch.object(self.jobs.transport, 'run') as remote:
            self.accounting.report()
            remote.assert_not_called()
