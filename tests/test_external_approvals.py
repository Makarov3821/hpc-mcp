"""External signing keys stay in the test signer; only public policy and proofs enter state."""

import base64
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
except ImportError:
    Ed25519PrivateKey = None

import test_jobs
import test_applications
from hpc_mcp.approvals import ApprovalError, ApprovalPolicy, canonical, register
from hpc_mcp.batch import BatchService
from hpc_mcp.jobs import JobService
from hpc_mcp.onboarding_store import digest
from hpc_mcp.workflow import WorkflowService


@unittest.skipUnless(Ed25519PrivateKey, 'cryptography (MCP extra) required for signature tests')
class ExternalApprovalTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_jobs.JobLifecycleTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.keys = {actor: Ed25519PrivateKey.generate() for actor in ('user', 'reviewer')}
        self.policy_path = self.fixture.root / 'public-policy.json'
        policy = {'version': 1, 'max_lifetime_seconds': 3600, 'keys': {
            actor: {'public_key': base64.b64encode(key.public_key().public_bytes_raw()).decode(),
                    'role': 'approver' if actor == 'user' else 'reviewer'}
            for actor, key in self.keys.items()}}
        self.policy_path.write_text(json.dumps(policy))
        env = patch.dict(os.environ, HPC_MCP_APPROVAL_POLICY=str(self.policy_path))
        env.start(); self.addCleanup(env.stop)
        self.jobs = JobService(self.fixture.config, self.fixture.state,
                              self.fixture.transport, self.fixture.transfer)

    def signed(self, action, subject, token, actor='user', **changes):
        claims = self.jobs.history.approvals.request(action, subject, token)
        at = datetime.now(timezone.utc)
        claims.update(actor=actor, issued_at=(at - timedelta(seconds=1)).isoformat(),
                      expires_at=(at + timedelta(seconds=300)).isoformat(), **changes)
        return {'claims': claims,
                'signature': base64.b64encode(self.keys[actor].sign(canonical(claims))).decode()}

    def endorse(self, action, subject, token, actor='user'):
        proof = self.signed(action, subject, token, actor)
        register(self.jobs.history, proof)
        return proof

    def test_default_mode_remains_explicitly_client_attested(self):
        with patch.dict(os.environ, {}, clear=True):
            policy = ApprovalPolicy(self.fixture.state)
        self.assertEqual(policy.describe()['mode'], 'client_attested')

    def test_exact_job_signature_and_restart_receipt_are_required(self):
        run = self.fixture.prepare()
        plan = self.jobs.job_submission_plan(run['run_id'])
        with self.assertRaises(ApprovalError):
            self.jobs.job_authorize(run['run_id'], plan['review_token'], 'fabricated confirmation')
        self.assertEqual(self.fixture.transport.commands, [])
        proof = self.endorse('job_authorize', run['run_id'], plan['review_token'])
        self.jobs.job_authorize(run['run_id'], plan['review_token'], 'signed user approval')
        restarted = JobService(self.jobs.clusters, self.fixture.state,
                              self.fixture.transport, self.fixture.transfer)
        self.assertTrue(restarted.job_submit(run['run_id'])['ok'])
        record = restarted.history.get(run['run_id'])
        self.assertEqual(record['submission_authorization']['external_approval'], proof)
        record['submission_authorization']['external_approval']['signature'] = 'tampered'
        restarted.history.update(run['run_id'], 'test_tamper',
                                 submission_authorization=record['submission_authorization'])
        with self.assertRaises(ApprovalError):
            restarted.job_sync(run['run_id'])

    def test_invalid_signature_role_state_token_and_expiry_are_rejected(self):
        run = self.fixture.prepare(); plan = self.jobs.job_submission_plan(run['run_id'])
        proof = self.signed('job_authorize', run['run_id'], plan['review_token'])
        bad = dict(proof, signature='bad')
        with self.assertRaises(ApprovalError):
            register(self.jobs.history, bad)
        for changes in ({'state_dir': '/other/state'}, {'token': '0' * 64},
                        {'expires_at': (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()}):
            claims = dict(proof['claims'], **changes)
            envelope = {'claims': claims,
                'signature': base64.b64encode(self.keys['user'].sign(canonical(claims))).decode()}
            with self.subTest(changes=changes), self.assertRaises(ApprovalError):
                self.jobs.history.approvals.verify(envelope, 'job_authorize', run['run_id'],
                                                   plan['review_token'])
        reviewer = self.signed('job_authorize', run['run_id'], plan['review_token'], 'reviewer')
        with self.assertRaises(ApprovalError):
            register(self.jobs.history, reviewer)

    def test_revocation_blocks_reusing_the_same_signed_endorsement(self):
        run = self.fixture.prepare(); plan = self.jobs.job_submission_plan(run['run_id'])
        proof = self.endorse('job_authorize', run['run_id'], plan['review_token'])
        self.jobs.job_authorize(run['run_id'], plan['review_token'], 'signed')
        self.jobs.job_revoke_authorization(run['run_id'])
        register(self.jobs.history, proof)
        with self.assertRaises(ApprovalError):
            self.jobs.job_authorize(run['run_id'], plan['review_token'], 'try replay')
        self.endorse('job_authorize', run['run_id'], plan['review_token'])
        self.jobs.job_authorize(run['run_id'], plan['review_token'], 'fresh signed approval')
        self.assertTrue(self.jobs.job_submit(run['run_id'])['ok'])

    def test_signed_workflow_delegates_only_exact_frozen_run_plans(self):
        run = self.fixture.prepare(); workflows = WorkflowService(self.jobs)
        flow = workflows.plan('signed flow', [run['run_id']])['workflow']
        with self.assertRaises(ApprovalError):
            workflows.start(flow['workflow_id'], flow['review_token'], 'invented')
        self.endorse('workflow_start', flow['workflow_id'], flow['review_token'])
        workflows.start(flow['workflow_id'], flow['review_token'], 'external user approval')
        self.assertEqual(workflows.tick(flow['workflow_id'])['submitted'], 1)

    def test_signed_batch_delegates_without_extra_per_task_endorsements(self):
        batches = BatchService(self.jobs)
        planned = batches.plan('signed-batch', 'signed batch', 'lsf', str(self.fixture.root),
            [{'input_dir': str(self.fixture.source), 'script': 'job.sh',
              'outputs': ['results/***']}], start_monitor=False)
        with self.assertRaises(ApprovalError):
            batches.start(planned['batch_id'], planned['review_token'], 'invented')
        proof = self.endorse('batch_start', planned['batch_id'], planned['review_token'])
        batches.start(planned['batch_id'], planned['review_token'], 'signed batch')
        workflow = batches.get(planned['batch_id'])['workflow_id']
        self.assertEqual(batches.workflows.tick(workflow)['submitted'], 1)
        rid = planned['tasks'][0]['run_id']
        self.assertEqual(self.jobs.history.get(rid)['submission_authorization']['external_approval'], proof)

    def test_reviewer_signature_binds_packet_report_and_activation_plan(self):
        app_fixture = test_applications.ApplicationTests(); app_fixture.setUp()
        self.addCleanup(app_fixture.doCleanups)
        # Reuse its standard bundle but keep this test's protected service/state.
        from hpc_mcp.applications import ApplicationService
        apps = ApplicationService(self.jobs)
        record = apps.install(str(app_fixture.bundle_for('hello_lsf')))['application']
        packet = apps.review_request('hello_lsf', record['version'], 'author')['review_request']
        report = {'reviewer_session': 'external-reviewer', 'fresh_context': True, 'verdict': 'pass',
                  'checks': {k: 'Independent source evidence.' for k in packet['required_checks']},
                  'differences': [], 'unresolved': [], 'allowed_changes': []}
        with self.assertRaises(ApprovalError):
            apps.review_submit('hello_lsf', record['version'], record['review_token'], report)
        token = digest({'packet_sha256': digest(packet), 'report': report})
        self.endorse('application_review_submit', f'hello_lsf/{record["version"]}', token, 'reviewer')
        apps.review_submit('hello_lsf', record['version'], record['review_token'], report)
        plan = apps.activation_plan('hello_lsf', record['version'])
        with self.assertRaises(ApprovalError):
            apps.activate('hello_lsf', record['version'], record['review_token'], 'invented')
        self.endorse('application_activate', f'hello_lsf/{record["version"]}', plan['approval_token'])
        active = apps.activate('hello_lsf', record['version'], record['review_token'], 'signed activation')
        self.assertEqual(active['application']['activation_plan'], plan['plan'])
        altered = dict(report, allowed_changes=['unapproved change'])
        with self.assertRaises(ApprovalError):
            apps.review_submit('hello_lsf', record['version'], record['review_token'], altered)

    def test_cli_policy_and_signed_import_do_not_execute_remote_actions(self):
        run = self.fixture.prepare(); plan = self.jobs.job_submission_plan(run['run_id'])
        path = self.fixture.root / 'proof.json'
        path.write_text(json.dumps(self.signed('job_authorize', run['run_id'], plan['review_token'])))
        command = [sys.executable, '-m', 'hpc_mcp', '--state-dir', str(self.fixture.state),
                   '--approval-policy', str(self.policy_path)]
        imported = subprocess.run(command + ['approval-import', str(path)],
                                  text=True, capture_output=True, check=True)
        self.assertTrue(json.loads(imported.stdout)['ok'])
        status = subprocess.run(command + ['approval-status'], text=True,
                                capture_output=True, check=True)
        self.assertEqual(json.loads(status.stdout)['approval']['mode'], 'external_signed')
        self.assertFalse((self.fixture.root / 'submissions').exists())

    def test_strict_runner_changes_and_archival_need_matching_endorsements(self):
        from hpc_mcp.handler_runner import HandlerRunner, RunnerSettings
        runner = HandlerRunner(self.fixture.state)
        with self.assertRaises(ApprovalError):
            runner.configure({'mode': 'trusted'}, 'invented approval')
        self.endorse('handler_runner_configure', 'handler-runner',
                     digest(asdict(RunnerSettings(mode='trusted'))))
        self.assertFalse(runner.configure({'mode': 'trusted'}, 'signed permissions')['permissions']['isolated'])
        self.fixture.prepare()
        with self.assertRaises(ApprovalError):
            self.jobs.history.archive_events(0, 1, False)
        preview = self.jobs.history.archive_events(0, 1, True)
        self.endorse('history_archive', 'events', preview['approval_token'])
        self.assertEqual(self.jobs.history.archive_events(0, 1, False)['count'], 1)

    def test_signed_archive_cannot_be_reused_for_different_evidence(self):
        self.fixture.prepare()
        preview = self.jobs.history.archive_events(0, 1, True)
        self.endorse('history_archive', 'events', preview['approval_token'])
        self.jobs.history.archive_events(0, 1, False)
        self.fixture.prepare()
        with self.assertRaises(ApprovalError):
            self.jobs.history.archive_events(0, 1, False)

    def test_duplicate_public_key_roles_are_rejected(self):
        policy = json.loads(self.policy_path.read_text())
        policy['keys']['reviewer']['public_key'] = policy['keys']['user']['public_key']
        self.policy_path.write_text(json.dumps(policy))
        with self.assertRaisesRegex(ApprovalError, 'identities must be distinct'):
            ApprovalPolicy(self.fixture.state, self.policy_path)

    def test_signed_setup_delegates_exact_reviewed_application_activation(self):
        import test_setup_workflows
        f = test_setup_workflows.SetupWorkflowTests(); f.setUp()
        self.addCleanup(f.doCleanups)
        self.jobs = f.jobs
        record = f.install(f.sources(f.create()))
        record = f.step(record, 'application_review_request', {'author_session': 'author'})
        packet = record['business']['review_request']
        report = {'reviewer_session': 'external-reviewer', 'fresh_context': True, 'verdict': 'pass',
                  'checks': {k: 'Independent source evidence.' for k in packet['required_checks']},
                  'differences': [], 'unresolved': [], 'allowed_changes': []}
        version = record['business']['version']
        self.endorse('application_review_submit', f'hello_lsf/{version}',
                     digest({'packet_sha256': digest(packet), 'report': report}), 'reviewer')
        record = f.step(record, 'application_review_submit', {'report': report})
        with self.assertRaises(ApprovalError):
            f.confirm(record)
        proof = self.endorse('setup_flow_confirm', record['flow_id'],
                             record['business']['confirmation_token'])
        completed = f.confirm(record)
        self.assertEqual(completed['state'], 'completed')
        app = f.apps.get('hello_lsf')['application']
        self.assertEqual(app['activation_approval'], proof)
        self.assertEqual(app['activation_plan'], record['business']['confirmation_plan']['activation_plan'])
