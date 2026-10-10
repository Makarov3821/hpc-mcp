"""Read legacy application profiles and persist cluster and script inspection evidence."""

from dataclasses import asdict
import json

from .cluster_probe import ClusterProbe
from .onboarding_store import OnboardingStore, digest
from .profile_files import configuration_file
from .script_inspection import ScriptInspector


class ProfileService:
    def __init__(self, service, jobs):
        self.service, self.jobs = service, jobs
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
        path, _ = configuration_file(self.jobs.history, profile)
        profile['configuration_file'] = str(path)
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

    def list(self, cluster=None, application=None, limit=50, offset=0, compact=False):
        if type(compact) is not bool:
            raise ValueError('compact must be boolean')
        if type(limit) is not int or not 1 <= limit <= 500 or type(offset) is not int or offset < 0:
            raise ValueError('limit must be 1..500; offset must be nonnegative')
        with self.jobs.history.connect() as db:
            rows = db.execute('SELECT id FROM application_profiles WHERE (? IS NULL OR cluster=?) '
                'AND (? IS NULL OR application=?) ORDER BY rowid DESC LIMIT ? OFFSET ?',
                (cluster, cluster, application, application, limit, offset)).fetchall()
        profiles = [self.get(row[0])['profile'] for row in rows]
        if compact:
            profiles = [{key: p[key] for key in ('profile_id', 'name', 'version', 'cluster',
                'application', 'is_default', 'validation', 'requires_recheck')} |
                {'confirmed': bool(p['confirmation']), 'parameters': p['definition'].get('parameters', {}),
                 'configuration_file': p['configuration_file']}
                for p in profiles]
        return {'ok': True, 'profiles': profiles}
