"""Optional Ed25519 endorsements from separately controlled approval/reviewer keys."""

import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import uuid

from .onboarding_store import digest

ACTIONS = {'job_authorize', 'workflow_start', 'batch_start', 'setup_flow_confirm',
           'application_review_submit', 'application_activate', 'handler_runner_configure',
           'history_archive', 'local_cache_cleanup', 'cluster_work_cleanup', 'retention_release',
           'cluster_registry_reconcile'}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      allow_nan=False, separators=(',', ':')).encode()


class ApprovalError(ValueError):
    code = 'external_approval_required'

    def __init__(self, message, request=None):
        super().__init__(message)
        self.request = request


class ApprovalPolicy:
    def __init__(self, root, path=None):
        self.root = str(Path(root).resolve())
        self.path = path or os.environ.get('HPC_MCP_APPROVAL_POLICY')
        self.keys = {}
        self.max_lifetime = 86400
        if self.path:
            self.path = str(Path(self.path).expanduser().resolve())
            policy = json.loads(Path(self.path).read_text())
            if (not isinstance(policy, dict)
                    or set(policy) != {'version', 'keys', 'max_lifetime_seconds'}
                    or policy['version'] != 1 or not isinstance(policy['keys'], dict)
                    or not 1 <= len(policy['keys']) <= 32
                    or type(policy['max_lifetime_seconds']) is not int
                    or not 30 <= policy['max_lifetime_seconds'] <= 604800):
                raise ApprovalError('invalid external approval policy')
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            self.max_lifetime = policy['max_lifetime_seconds']
            public_keys = set()
            for actor, values in policy['keys'].items():
                if (not isinstance(actor, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', actor)
                        or not isinstance(values, dict) or set(values) != {'public_key', 'role'}
                        or values['role'] not in ('approver', 'reviewer')):
                    raise ApprovalError('invalid approval key or role')
                raw = base64.b64decode(values['public_key'], validate=True)
                if raw in public_keys:
                    raise ApprovalError('approval key identities must be distinct')
                public_keys.add(raw)
                self.keys[actor] = (Ed25519PublicKey.from_public_bytes(raw), values['role'])

    def describe(self):
        return {'mode': 'external_signed' if self.path else 'client_attested',
                'policy_path': self.path, 'actors': {k: v[1] for k, v in self.keys.items()},
                'max_lifetime_seconds': self.max_lifetime,
                'boundary': 'Protect signer keys, policy, service code and launch configuration '
                            'from the agent. Signatures authenticate endorsement, '
                            'not fresh-context execution.'}

    def request(self, action, subject, token):
        if (action not in ACTIONS or not isinstance(subject, str) or not 1 <= len(subject) <= 256
                or not isinstance(token, str) or not re.fullmatch(r'[a-f0-9]{64}', token)):
            raise ValueError('approval request needs a supported action, subject and SHA-256 token')
        return {'version': 1, 'domain': 'hpc-mcp.approval.v1', 'state_dir': self.root,
                'action': action,
                'subject': subject, 'token': token, 'nonce': uuid.uuid4().hex,
                'actor': '<configured-key-id>', 'issued_at': '<UTC ISO timestamp>',
                'expires_at': '<UTC ISO timestamp>'}

    def verify(self, envelope, action, subject, token, accepted_at=None):
        if not self.path:
            raise ApprovalError('external proof requires the original approval policy at startup')
        try:
            if not isinstance(envelope, dict) or set(envelope) != {'claims', 'signature'}:
                raise ValueError('envelope fields')
            if len(canonical(envelope)) > 8192:
                raise ValueError('envelope exceeds 8 KiB')
            claims = envelope['claims']
            if set(claims) != set(self.request(action, subject, token)):
                raise ValueError('claims fields')
            if (claims['version'] != 1 or claims['domain'] != 'hpc-mcp.approval.v1'
                    or claims['state_dir'] != self.root
                    or claims['action'] != action or claims['subject'] != subject
                    or claims['token'] != token
                    or not re.fullmatch('[a-f0-9]{32}', claims['nonce'])):
                raise ValueError('scope mismatch')
            key, role = self.keys[claims['actor']]
            if role != ('reviewer' if action == 'application_review_submit' else 'approver'):
                raise ValueError('signer role mismatch')
            issued = datetime.fromisoformat(claims['issued_at'])
            expires = datetime.fromisoformat(claims['expires_at'])
            at = datetime.fromisoformat(accepted_at) if accepted_at else datetime.now(timezone.utc)
            if issued.tzinfo is None or expires.tzinfo is None or at.tzinfo is None:
                raise ValueError('timestamps must include timezone')
            if (not 0 < (expires - issued).total_seconds() <= self.max_lifetime
                    or not issued <= at < expires):
                raise ValueError('approval expired or not yet valid')
            key.verify(base64.b64decode(envelope['signature'], validate=True), canonical(claims))
        except Exception as error:
            raise ApprovalError('invalid external endorsement: ' + str(error)) from error
        return envelope


def register(history, envelope):
    claims = envelope.get('claims', {}) if isinstance(envelope, dict) else {}
    policy = history.approvals
    policy.verify(envelope, claims.get('action'), claims.get('subject'), claims.get('token'))
    with history.connect() as db:
        existing = db.execute('SELECT data FROM external_approvals WHERE nonce=?',
                              (claims['nonce'],)).fetchone()
        if existing and json.loads(existing[0]) != envelope:
            raise ApprovalError('approval nonce has conflicting evidence')
        db.execute('INSERT OR IGNORE INTO external_approvals VALUES (?,?,?,?)',
                   (claims['nonce'], claims['action'], claims['subject'], json.dumps(envelope)))
    return {'ok': True, 'claims': claims}


def require(history, action, subject, token):
    if not history.approvals.path:
        return None
    with history.connect() as db:
        rows = db.execute('SELECT data FROM external_approvals WHERE action=? AND subject=? '
                          'ORDER BY rowid DESC LIMIT 1000', (action, subject)).fetchall()
    for row in rows:
        envelope = json.loads(row[0])
        try:
            history.approvals.verify(envelope, action, subject, token)
            with history.connect() as db:
                revoked = db.execute('SELECT 1 FROM approval_revocations '
                                     'WHERE nonce=? AND subject=?',
                    (envelope['claims']['nonce'], subject)).fetchone()
            if not revoked:
                return envelope
        except ApprovalError:
            pass
    request = history.approvals.request(action, subject, token)
    raise ApprovalError('external signed approval required for ' + action, request)


def stored(history, table, subject):
    # Table names are selected only by this module, never interpolated from clients.
    if table not in ('workflows', 'batches', 'setup_flows'):
        raise ValueError('invalid approval scope table')
    with history.connect() as db:
        row = db.execute(f'SELECT data FROM {table} WHERE id=?', (subject,)).fetchone()
    if not row:
        raise ApprovalError('signed aggregate scope is missing')
    return json.loads(row[0])


def workflow_proof(history, workflow, envelope=None):
    if envelope is None:
        return require(history, 'workflow_start', workflow['workflow_id'], workflow['review_token'])
    claims = envelope['claims']
    if claims['action'] != 'batch_start':
        raise ApprovalError('workflow delegation needs a signed batch')
    batch = stored(history, 'batches', claims['subject'])
    if (digest(batch['frozen_plan']) != claims['token']
            or batch['workflow_id'] != workflow['workflow_id']
            or batch['frozen_plan']['workflow_plan'] != workflow['plan']):
        raise ApprovalError('batch delegation scope changed')
    return history.approvals.verify(envelope, 'batch_start', batch['batch_id'], claims['token'])


def verify_run(history, run, approved):
    envelope = approved.get('external_approval')
    if not history.approvals.path and envelope is None:
        return
    if not envelope:
        raise ApprovalError('legacy client-attested authorization needs external confirmation')
    claims = envelope['claims']
    history.approvals.verify(envelope, claims['action'], claims['subject'], claims['token'],
                             accepted_at=approved['time'])
    with history.connect() as db:
        revoked = db.execute('SELECT 1 FROM approval_revocations WHERE nonce=? AND subject=?',
                             (claims['nonce'], run['run_id'])).fetchone()
    if revoked:
        raise ApprovalError('external approval was revoked; obtain a new signed endorsement')
    if claims['action'] == 'job_authorize':
        if claims['subject'] == run['run_id'] and claims['token'] == approved['plan_token']:
            return
    elif claims['action'] in ('workflow_start', 'batch_start'):
        if claims['action'] == 'batch_start':
            batch = stored(history, 'batches', claims['subject'])
            if digest(batch['frozen_plan']) != claims['token']:
                raise ApprovalError('signed batch changed')
            plan = batch['frozen_plan']['workflow_plan']
        else:
            value = stored(history, 'workflows', claims['subject'])
            plan = value['plan']
            if digest(plan) != claims['token']:
                raise ApprovalError('signed workflow changed')
        plans = list(plan['execution_plans'].values()) + list(plan['external_plans'].values())
        if any(p['run_id'] == run['run_id'] and digest(p) == approved['plan_token'] for p in plans):
            return
    raise ApprovalError('external signature does not authorize this exact run')


def setup_proof(history, flow_id, application, version, review_token, activation_plan):
    flow = stored(history, 'setup_flows', flow_id)
    business = flow['business']
    plan = business['confirmation_plan']
    proof = business['confirmation'].get('external_approval')
    if not history.approvals.path and proof is None:
        return None
    if (plan.get('application') != application or plan.get('version') != version
            or plan.get('review_token') != review_token
            or plan.get('activation_plan') != activation_plan
            or digest(plan) != business['confirmation_token']):
        raise ApprovalError('signed setup activation scope changed')
    return history.approvals.verify(proof, 'setup_flow_confirm', flow_id,
        business['confirmation_token'], accepted_at=business['confirmation']['time'])
