"""Reviewed application profiles bind immutable templates to cluster evidence and probes."""

from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import shlex
import tempfile
import uuid

from .cluster_probe import ClusterProbe
from .history import now
from .onboarding_store import OnboardingStore, digest
from .script_inspection import ScriptInspector
from .templates import TemplateService, bind, validate_definition


class ProfileService:
    def __init__(self, service, jobs):
        self.service, self.jobs = service, jobs
        self.templates = TemplateService(jobs)
        self.store = OnboardingStore(jobs.history)
        self.probes = ClusterProbe(service, self.store)
        with jobs.history.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS application_profiles (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, version INTEGER NOT NULL,
                    cluster TEXT NOT NULL, application TEXT NOT NULL, data TEXT NOT NULL,
                    UNIQUE(name, version));
                CREATE TABLE IF NOT EXISTS profile_confirmations (
                    profile_id TEXT PRIMARY KEY, time TEXT NOT NULL, note TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS profile_defaults (
                    cluster TEXT NOT NULL, application TEXT NOT NULL, profile_id TEXT NOT NULL,
                    PRIMARY KEY(cluster, application));
            ''')

    def inspect(self, script_path, cluster=None, max_bytes=262144):
        report = ScriptInspector(self.service).inspect(script_path, cluster, max_bytes)
        return self.store.save('script', report)

    def draft(self, name, cluster, application, definition, report_ids=None):
        self.templates._name(name)
        self.templates._name(application)
        target = self.service._cluster(cluster)
        validate_definition(definition)
        if len(json.dumps(definition, sort_keys=True, ensure_ascii=False, separators=(',', ':'),
                          allow_nan=False).encode()) > 262144:
            raise ValueError('profile template definition exceeds 256 KiB')
        if definition['scheduler'] != target.scheduler:
            raise ValueError('profile scheduler differs from cluster')
        ids = [] if report_ids is None else report_ids
        if not isinstance(ids, list) or len(ids) > 32 or any(not isinstance(i, str) for i in ids):
            raise ValueError('report_ids must be a list of at most 32 report IDs')
        reports = [self.store.get(i) for i in ids]
        for report in reports:
            host = report.get('ssh_host') or report.get('source', {}).get('ssh_host')
            if host is not None and host != target.ssh_host:
                raise ValueError('evidence belongs to a different SSH destination')
        profile = {'profile_id': 'p_' + uuid.uuid4().hex, 'name': name, 'cluster': cluster,
                   'application': application, 'created_at': now(), 'definition': deepcopy(definition),
                   'cluster_settings': asdict(target), 'cluster_sha256': digest(asdict(target)),
                   'report_ids': ids, 'evidence_sha256': {i: digest(r) for i, r in zip(ids, reports)},
                   'unresolved': [{'report_id': r['report_id'], 'items': r.get('unresolved', []),
                                  'needs': r.get('needs', [])} for r in reports],
                   'validation': 'unverified'}
        # Check record size before starting a write transaction.
        if len(json.dumps(profile).encode()) > 512 * 1024:
            raise ValueError('profile draft exceeds 512 KiB')
        with self.jobs.history.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            profile['version'] = db.execute('SELECT COALESCE(MAX(version),0)+1 FROM application_profiles WHERE name=?',
                                            (name,)).fetchone()[0]
            profile['review_token'] = digest(profile)
            db.execute('INSERT INTO application_profiles VALUES (?,?,?,?,?,?)',
                       (profile['profile_id'], name, profile['version'], cluster, application, json.dumps(profile)))
        return self.get(profile['profile_id'])

    def _read(self, profile_id):
        with self.jobs.history.connect() as db:
            row = db.execute('SELECT data FROM application_profiles WHERE id=?', (profile_id,)).fetchone()
        if row is None:
            raise ValueError('unknown profile')
        profile = json.loads(row[0])
        token = profile.pop('review_token')
        if digest(profile) != token:
            raise ValueError('profile checksum mismatch')
        profile['review_token'] = token
        return profile

    def get(self, profile_id):
        profile = self._read(profile_id)
        with self.jobs.history.connect() as db:
            confirmed = db.execute('SELECT time,note FROM profile_confirmations WHERE profile_id=?', (profile_id,)).fetchone()
            default = db.execute('SELECT profile_id FROM profile_defaults WHERE cluster=? AND application=?',
                                 (profile['cluster'], profile['application'])).fetchone()
            validation_row = db.execute("SELECT id FROM onboarding_records WHERE kind='validation' "
                                       "AND json_extract(data,'$.profile_id')=? ORDER BY created_at DESC LIMIT 1",
                                       (profile_id,)).fetchone()
        target = self.service.clusters.get(profile['cluster'])
        changed = target is None or digest(asdict(target)) != profile['cluster_sha256']
        profile['confirmation'] = {'time': confirmed[0], 'note': confirmed[1]} if confirmed else None
        profile['is_default'] = bool(default and default[0] == profile_id)
        profile['requires_recheck'] = changed
        validation = self.store.get(validation_row[0]) if validation_row else None
        profile['validation_evidence'] = validation
        if changed:
            profile['validation'] = 'stale'
        elif validation:
            profile['validation'] = validation['status']
        profile['notes'] = ['Remote software changes cannot be detected by a local read; explicitly revalidate after site changes.',
                            'Validation covers only its recorded command, bindings and resource layout; application correctness is separate.']
        profile['template'] = {'name': 'profile.' + profile_id, 'version': 1} if confirmed else None
        return {'ok': True, 'profile': profile}

    def list(self, cluster=None, application=None, limit=50, offset=0):
        if type(limit) is not int or not 1 <= limit <= 500 or type(offset) is not int or offset < 0:
            raise ValueError('limit must be 1..500; offset must be nonnegative')
        with self.jobs.history.connect() as db:
            rows = db.execute('SELECT id FROM application_profiles WHERE (? IS NULL OR cluster=?) '
                'AND (? IS NULL OR application=?) ORDER BY rowid DESC LIMIT ? OFFSET ?',
                (cluster, cluster, application, application, limit, offset)).fetchall()
        return {'ok': True, 'profiles': [self.get(row[0])['profile'] for row in rows]}

    def confirm(self, profile_id, review_token, confirmation_note, make_default=True):
        profile = self._read(profile_id)
        if review_token != profile['review_token']:
            raise ValueError('review token differs from the displayed draft')
        if not isinstance(confirmation_note, str) or not confirmation_note.strip() or len(confirmation_note) > 4096:
            raise ValueError('record the user confirmation and acknowledgement of unresolved items')
        if type(make_default) is not bool:
            raise ValueError('make_default must be boolean')
        self._current(profile)
        for report_id, expected in profile['evidence_sha256'].items():
            if digest(self.store.get(report_id)) != expected:
                raise ValueError('source evidence changed')
        # Reuse a reserved immutable template name; one profile version has one definition.
        template_name = 'profile.' + profile_id
        with self.jobs.history.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT 1 FROM profile_confirmations WHERE profile_id=?', (profile_id,)).fetchone()
            if not existing:
                definition = json.dumps(profile['definition'], sort_keys=True, ensure_ascii=False,
                                        separators=(',', ':'), allow_nan=False)
                db.execute('INSERT INTO templates VALUES (?,?,?,?,?)',
                           (template_name, 1, now(), hashlib.sha256(definition.encode()).hexdigest(), definition))
                db.execute('INSERT INTO profile_confirmations VALUES (?,?,?)',
                           (profile_id, now(), confirmation_note))
            if make_default:
                db.execute('INSERT INTO profile_defaults VALUES (?,?,?) ON CONFLICT(cluster,application) '
                           'DO UPDATE SET profile_id=excluded.profile_id',
                           (profile['cluster'], profile['application'], profile_id))
        return self.get(profile_id)

    def _current(self, profile):
        target = self.service._cluster(profile['cluster'])
        if digest(asdict(target)) != profile['cluster_sha256']:
            raise ValueError('cluster settings changed; create and confirm a new profile draft')
        return target

    def _confirmed(self, profile_id):
        profile = self.get(profile_id)['profile']
        if not profile['confirmation']:
            raise ValueError('profile requires user confirmation before use')
        self._current(profile)
        return profile

    def plan(self, input_dir, profile_id=None, cluster=None, application=None,
             parameters=None, project_root=None):
        if profile_id is None:
            with self.jobs.history.connect() as db:
                row = db.execute('SELECT profile_id FROM profile_defaults WHERE cluster=? AND application=?',
                                 (cluster, application)).fetchone()
            if row is None:
                raise ValueError('no confirmed default profile for this cluster/application')
            profile_id = row[0]
        profile = self._confirmed(profile_id)
        if (cluster is not None and cluster != profile['cluster']) or (application is not None and application != profile['application']):
            raise ValueError('profile does not match requested cluster/application')
        result = self.templates.template_plan('profile.' + profile_id, profile['cluster'], input_dir,
                                              parameters, 1, project_root)
        validation = profile['validation']
        evidence = profile.get('validation_evidence')
        if evidence and evidence['probe']['parameters'] != result['run']['template']['parameters']:
            validation = 'unverified'
        run_id = result['run']['run_id']
        result['run'] = self.jobs.history.update(run_id, 'profile_selected',
            profile={'profile_id': profile_id, 'name': profile['name'], 'version': profile['version'],
                     'review_token': profile['review_token'], 'validation': validation,
                     'report_ids': profile['report_ids'],
                     'validation_report_id': evidence['report_id'] if evidence and validation == 'validated' else None})
        result['notes'] = ['Profile confirmation and recorded probe validation are separate.',
                           'Validation applies only to the recorded probe command and matching parameter bindings.']
        return result

    def validate(self, profile_id, command=None, parameters=None, run_id=None):
        profile = self._confirmed(profile_id)
        if run_id is not None:
            if command is not None or parameters is not None:
                raise ValueError('checking an existing probe takes run_id only')
            return self._check_validation(profile, run_id)
        if not isinstance(command, list) or not command or len(command) > 128:
            raise ValueError('provide an explicitly reviewed short validation command argv')
        effective, bindings = bind(profile['definition'], parameters)
        # Validate argv without executing anything; original application input is not uploaded.
        from .scripts import line_value
        for arg in command:
            line_value(arg, 'validation command argument')
        token = uuid.uuid4().hex
        temporary = tempfile.TemporaryDirectory(prefix='hpc-mcp-validation-')
        local = Path(temporary.name)
        marker = 'hpc-mcp-validation.input'
        (local / marker).write_text(token)
        inner = ('set -euo pipefail; '
                 f'test "$(cat {shlex.quote(marker)})" = {shlex.quote(token)}; '
                 'printf "hostname=%s\\n" "$(hostname)"; '
                 + shlex.join(command) + '; '
                 + f'printf "\\n%s\\n" {shlex.quote("HPC_MCP_VALIDATED_" + token)}')
        spec = deepcopy(effective['spec'])
        for key in ('stdin', 'stdout', 'stderr', 'output_directories'):
            spec.pop(key, None)
        spec.update(command=['bash', '-c', inner], stdout='validation.log', stderr='validation.err')
        try:
            result = self.templates.job_prepare_generated(profile['cluster'], str(local), spec,
                         ['validation.log', 'validation.err'], input_files=[marker],
                         script_name='hpc-mcp-validation.sh')
        finally:
            temporary.cleanup()
        result['run'] = self.jobs.history.update(result['run']['run_id'], 'validation_prepared',
            validation_probe={'profile_id': profile_id, 'review_token': profile['review_token'],
                'cluster_sha256': profile['cluster_sha256'], 'token': token,
                'command': command, 'parameters': bindings, 'resources': spec.get('resources', {})})
        result['notes'] = ['Prepare only. Review rendered probe and authorize job_submit separately.',
                           'The supplied command must be small and appropriate for this application/environment.',
                           'Validation evidence is recorded only by profile_validate(run_id=...) after successful stable sync.']
        return result

    def _check_validation(self, profile, run_id):
        run = self.jobs.history.get(run_id)
        probe = run.get('validation_probe')
        if not probe or probe['profile_id'] != profile['profile_id'] or probe['review_token'] != profile['review_token']:
            raise ValueError('run is not a probe for this exact profile version')
        status = self.jobs.job_status(run_id)
        run = status['run']
        if not run.get('status_query_ok') or run['state'] not in ('succeeded', 'failed', 'cancelled'):
            return {'ok': True, 'status': 'pending', 'run': run,
                    'notes': ['Fresh terminal scheduler evidence is required; validation remains unverified.']}
        report = dict(profile_id=profile['profile_id'], run_id=run_id, probe=probe,
                      scheduler_state=run['state'], cluster_sha256=profile['cluster_sha256'], status='failed')
        if run['state'] == 'succeeded':
            synced = self.jobs.job_sync(run_id, mode='filtered', includes=['validation.log', 'validation.err'],
                                       max_file_bytes=1048576, max_total_bytes=2097152, stable_only=True)
            if not synced.get('ok') or not synced.get('final'):
                return {'ok': False, 'status': 'pending', 'sync': synced}
            run = synced['run']
            if run['state'] != 'succeeded' or not run.get('status_query_ok'):
                return {'ok': True, 'status': 'pending', 'run': run,
                        'notes': ['Successful state changed during sync; validation remains unverified.']}
            entry = next((e for e in run.get('output_manifest', []) if e['path'] == 'validation.log'), None)
            if entry:
                log = Path(run['output_dir']) / 'validation.log'
                if log.is_symlink() or not log.is_file():
                    raise ValueError('validation log must remain a regular file')
                with log.open('rb') as stream:
                    data = stream.read(1048577)
                if len(data) > 1048576:
                    raise ValueError('validation log grew beyond 1 MiB')
                if hashlib.sha256(data).hexdigest() != entry['sha256']:
                    raise ValueError('validation output checksum changed')
                text = data.decode(errors='replace')
                report['output_manifest'] = run['output_manifest']
                report['log_excerpt'] = text[-8192:]
                ranks = probe['resources'].get('tasks', 1)
                if (text.splitlines().count('HPC_MCP_VALIDATED_' + probe['token']) >= ranks
                        and sum(line.startswith('hostname=') for line in text.splitlines()) >= ranks):
                    report['status'] = 'validated'
                    report['checks'] = {'queue_submission': 'passed', 'work_root_visible': 'passed',
                        'setup_and_explicit_command': 'passed', 'selected_output_sync': 'passed'}
                    report['scope'] = 'This run, command, parameters and layout only; no application correctness or other queue claim.'
        saved = self.store.save('validation', report)
        return {'ok': True, 'status': saved['status'], 'evidence': saved, 'profile': self.get(profile['profile_id'])['profile']}
