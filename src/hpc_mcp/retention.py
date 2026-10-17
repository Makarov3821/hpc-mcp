"""Shared remote retention claims, with explicit release and host-conflict protection."""

import base64
import json
from pathlib import Path
import re
import shlex
import time
import uuid

from .approvals import require
from .onboarding_store import digest
from .ssh import CommandResult


def registration_facts(run):
    return {**{k: run.get(k) for k in ('run_id', 'created_at', 'remote_dir', 'project_root')},
            'input_digest': digest(run.get('manifest'))}


class RemoteRegistry:
    def __init__(self, transport):
        self.transport = transport

    def call(self, cluster, action, **values):
        source = Path(__file__).with_name('registry_remote.py').read_text()
        request = {'root': cluster.work_root, 'action': action, **values}
        encoded = base64.b64encode(json.dumps(request).encode()).decode()
        result = self.transport.run(cluster,
            'python3 -c ' + shlex.quote(source) + ' ' + shlex.quote(encoded))
        try:
            response = json.loads(result.stdout)
        except (ValueError, TypeError):
            raise ValueError('remote registry unavailable; data protected: ' + str(result.diagnostic()))
        if not result.ok or not response.get('ok'):
            raise ValueError(response.get('message', 'remote registry request failed'))
        return response

    def cleanup(self, cluster, run, command, revision=None):
        try:
            value = self.call(cluster, 'cleanup', run_ids=[run['run_id']],
                facts=registration_facts(run), revision=revision, command=command,
                workspace_id=run.get('retention', {}).get('workspace_id'),
                timeout=max(1, cluster.command_timeout - 2))
            return CommandResult(**value['command_result'])
        except ValueError as error:
            return CommandResult(-1, stderr=str(error), error='retention_protected')


class RetentionService:
    def __init__(self, jobs):
        self.jobs, self.history = jobs, jobs.history
        self.remote = RemoteRegistry(jobs.transport)
        with self.history.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS retention_client (id INTEGER PRIMARY KEY, value TEXT NOT NULL)')
            db.execute('INSERT OR IGNORE INTO retention_client VALUES (1,?)', (uuid.uuid4().hex,))
            self.client_id = db.execute('SELECT value FROM retention_client WHERE id=1').fetchone()[0]
            db.execute('CREATE TABLE IF NOT EXISTS retention_plans (token TEXT PRIMARY KEY, data TEXT NOT NULL)')

    def cluster(self, name):
        self.jobs.refresh_clusters()
        if name not in self.jobs.clusters:
            raise ValueError('unknown cluster')
        return self.jobs.clusters[name]

    def holder(self, label='submission'):
        if not isinstance(label, str) or not re.fullmatch('[A-Za-z0-9_.-]{1,100}', label):
            raise ValueError('holder must be a stable 1..100 character label: letters/digits/_.-')
        return self.client_id + ':' + label

    def ids(self, run_ids):
        if (not isinstance(run_ids, list) or not 1 <= len(run_ids) <= 500
                or any(not isinstance(r, str) or not re.fullmatch('r_[a-f0-9]{32}', r) for r in run_ids)
                or len(set(run_ids)) != len(run_ids)):
            raise ValueError('1..500 unique explicit run IDs required')
        return run_ids

    def probe(self, cluster, initialize=True):
        config = self.cluster(cluster)
        if type(initialize) is not bool:
            raise ValueError('initialize must be boolean')
        response = self.remote.call(config, 'probe', initialize=initialize)
        response['configured_aliases'] = [c.name for c in self.jobs.clusters.values()
            if c.name != cluster and c.work_root.rstrip('/') == config.work_root.rstrip('/')]
        if response['configured_aliases']:
            response['warnings'].append('Other configured clusters use the same work_root path; '
                'compare workspace_id to confirm whether storage is shared.')
        response['warnings'].append('Host detection does not certify network-filesystem locking reliability.')
        return response

    def register(self, run):
        value = self.remote.call(self.jobs._cluster(run), 'register', run_ids=[run['run_id']],
            facts=registration_facts(run), holder=self.holder(), reason='Submission inputs and remote results retained')
        self.history.update(run['run_id'], 'retention_registered', retention={
            'workspace_id': value['workspace_id'], 'holder': self.holder(), 'released': False})
        return value

    def status(self, cluster, run_ids):
        return self.remote.call(self.cluster(cluster), 'status', run_ids=self.ids(run_ids), initialize=False)

    def observe(self, run):
        try:
            self.remote.call(self.jobs._cluster(run), 'observe', run_ids=[run['run_id']],
                observer=self.client_id, observation={
                    'job_id': run.get('job_id'), 'phase': run['phase'],
                    'sync_state': run['sync_state'], 'observed_at': time.time(),
                    'output_digest': digest(run.get('output_manifest'))})
        except ValueError as error:
            self.history.update(run['run_id'], 'retention_observation_deferred',
                                retention_observation_error=str(error))

    def hold(self, cluster, run_ids, holder, reason):
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise ValueError('nonempty retention reason required (max 2000 characters)')
        self.ids(run_ids)
        # Explicitly holding an old local task adopts it as protected, never as released.
        self.probe(cluster)
        status = self.status(cluster, run_ids)
        for task in status['tasks']:
            if not task.get('registered'):
                run = self.history.get(task['run_id'])
                if run['cluster'] != cluster or run['phase'] != 'submitted' or run.get('remote_removed'):
                    raise ValueError('only known submitted legacy tasks can be adopted')
                self.register(run)
        value = self.remote.call(self.cluster(cluster), 'hold', run_ids=run_ids,
            holder=self.holder(holder), reason=reason)
        self.mirror(value)
        return value

    def mirror(self, value):
        for task in value['tasks']:
            try:
                with self.history.lock(task['run_id']):
                    run = self.history.get(task['run_id'])
                    active = any(not h['released'] for h in task['holds'].values())
                    self.history.update(run['run_id'], 'retention_observed', retention={
                        'workspace_id': value['workspace_id'], 'released': not active,
                        'revision': task['revision']})
            except ValueError:
                # Foreign tasks can be held without importing their local history.
                continue

    def release(self, cluster=None, run_ids=None, holder='submission', reason=None,
                dry_run=True, review_token=None, confirmation_note=None):
        if type(dry_run) is not bool:
            raise ValueError('dry_run must be boolean')
        if dry_run:
            if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
                raise ValueError('nonempty release reason required (max 2000 characters)')
            status = self.status(cluster, run_ids)
            identity = self.holder(holder)
            for task in status['tasks']:
                if not task.get('registered') or identity not in task.get('holds', {}):
                    raise ValueError('holder has no claim on every selected task')
            plan = {'action': 'retention_release', 'cluster': cluster,
                'cluster_config': self.cluster(cluster).__dict__, 'workspace_id': status['workspace_id'],
                'holder': identity, 'run_ids': run_ids, 'reason': reason,
                'revisions': {t['run_id']: t['revision'] for t in status['tasks']},
                'expires_at': time.time() + 3600}
            token = digest(plan)
            with self.history.connect() as db:
                db.execute('INSERT OR REPLACE INTO retention_plans VALUES (?,?)', (token, json.dumps(plan)))
            return {'ok': True, 'dry_run': True, 'review_token': token, 'plan': plan,
                'tasks': status['tasks'], 'notes': ['Release permits future cleanup; does not delete files.',
                    'Other holders remain protected. No automatic expiry or release after sync.']}
        if not isinstance(confirmation_note, str) or not confirmation_note.strip() or len(confirmation_note) > 4096:
            raise ValueError('explicit user confirmation_note required')
        with self.history.connect() as db:
            row = db.execute('SELECT data FROM retention_plans WHERE token=?', (review_token,)).fetchone()
        if not row:
            raise ValueError('unknown exact retention release plan')
        plan = json.loads(row[0])
        if (plan.get('action') != 'retention_release' or digest(plan) != review_token
                or time.time() >= plan['expires_at']):
            raise ValueError('release plan changed or expired; preview again')
        config = self.cluster(plan['cluster'])
        if digest(config.__dict__) != digest(plan['cluster_config']):
            raise ValueError('cluster configuration changed; preview release again')
        proof = require(self.history, 'retention_release', plan['cluster'], review_token)
        value = self.remote.call(config, 'release', run_ids=plan['run_ids'], holder=plan['holder'],
            revisions=plan['revisions'], reason=plan['reason'], workspace_id=plan['workspace_id'],
            review_token=review_token)
        self.mirror(value)
        with self.history.connect() as db:
            db.execute('INSERT INTO events(run_id,time,kind,detail) VALUES (?,?,?,?)',
                ('maintenance', str(time.time()), 'retention_released', json.dumps({
                    'review_token': review_token, 'confirmation_note': confirmation_note,
                    'external_approval': proof})))
        return dict(value, dry_run=False, review_token=review_token)

    def reconcile(self, cluster=None, dry_run=True, review_token=None, confirmation_note=None):
        if type(dry_run) is not bool:
            raise ValueError('dry_run must be boolean')
        if dry_run:
            status = self.probe(cluster, initialize=False)
            if not status.get('initialized') or status['host']['id'] != status['owner_host']['id']:
                raise ValueError('reconcile must run on the original owner host')
            plan = {'action': 'cluster_registry_reconcile', 'cluster': cluster,
                'cluster_config': self.cluster(cluster).__dict__, 'workspace_id': status['workspace_id'],
                'conflicts': status['conflicts'], 'expires_at': time.time() + 3600}
            token = digest(plan)
            with self.history.connect() as db:
                db.execute('INSERT OR REPLACE INTO retention_plans VALUES (?,?)', (token, json.dumps(plan)))
            return {'ok': True, 'dry_run': True, 'review_token': token, 'plan': plan,
                'notes': ['Confirm all clients now use the owner host and other accesses have stopped.',
                    'This only clears reviewed conflict observations; no holds or task files are released.']}
        if not isinstance(confirmation_note, str) or not confirmation_note.strip() or len(confirmation_note) > 4096:
            raise ValueError('explicit user confirmation_note required')
        with self.history.connect() as db:
            row = db.execute('SELECT data FROM retention_plans WHERE token=?', (review_token,)).fetchone()
        if not row:
            raise ValueError('unknown exact conflict reconciliation plan')
        plan = json.loads(row[0])
        if (plan.get('action') != 'cluster_registry_reconcile' or digest(plan) != review_token
                or time.time() >= plan['expires_at']):
            raise ValueError('wrong or expired reconciliation plan')
        config = self.cluster(plan['cluster'])
        if digest(config.__dict__) != digest(plan['cluster_config']):
            raise ValueError('cluster configuration changed; review again')
        proof = require(self.history, 'cluster_registry_reconcile', plan['cluster'], review_token)
        value = self.remote.call(config, 'reconcile', initialize=False,
            workspace_id=plan['workspace_id'], conflicts=plan['conflicts'], reason=confirmation_note)
        with self.history.connect() as db:
            db.execute('INSERT INTO events(run_id,time,kind,detail) VALUES (?,?,?,?)',
                ('maintenance', str(time.time()), 'registry_reconciled', json.dumps({
                    'review_token': review_token, 'confirmation_note': confirmation_note,
                    'external_approval': proof})))
        return dict(value, dry_run=False, review_token=review_token)
