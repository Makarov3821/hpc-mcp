"""Explicit business steps for durable cluster setup and reviewed application adaptation."""

from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import uuid

from .applications import ApplicationService, ApplicationError, digest as application_digest
from .cluster_probe import ClusterProbe
from .config import Cluster, load_config
from .history import now
from .jobs import file_manifest
from .onboarding_store import OnboardingStore, digest
from .service import ClusterService
from .setup_flows import SetupFlowService, SetupFlowError
from .schedulers import COMMANDS


class SetupWorkflowService(SetupFlowService):
    def __init__(self, jobs, service=None, config=None):
        super().__init__(jobs)
        self.service = service or ClusterService(jobs.clusters, jobs.transport)
        self.config = config
        self.apps = ApplicationService(jobs)

    def create(self, kind, request_key, label=''):
        result = super().create(kind, request_key, label)
        with self.locked():
            record = self._get(result['flow']['flow_id'])
            if record['state'] == 'open' and 'business' not in record:
                stage = 'awaiting_inputs' if kind == 'cluster_setup' else 'awaiting_source'
                record = self._save(record, 'business_flow_initialized', business={'stage': stage}, pending_action=stage)
            return {'ok': True, 'flow': record}

    def get(self, flow_id):
        result = super().get(flow_id)
        record = result['flow']; business = record.get('business', {})
        if record['state']=='open' and business.get('stage')=='awaiting_confirmation' and not business.get('operation'):
            try:
                self._validate_confirmation(record,business['confirmation_token'])
                record['confirmation_status']={'valid':True}
            except (ValueError,OSError) as exc:
                record['confirmation_status']={'valid':False,'error':getattr(exc,'code','setup_flow_invalid_request'),'message':str(exc)}
                record['pending_action']='cluster_inputs' if record['kind']=='cluster_setup' else 'application_install'
        return result

    def _clusters(self):
        if self.config:
            current = load_config(self.config.path) if self.config.path.exists() else {}
            self.jobs.clusters.clear(); self.jobs.clusters.update(current)
        return self.jobs.clusters

    def _baseline(self, name):
        cluster = self._clusters().get(name)
        return digest(asdict(cluster)) if cluster else None

    @staticmethod
    def keys(payload, required, optional=()):
        if not isinstance(payload, dict) or not set(required) <= set(payload) or set(payload) - set(required) - set(optional):
            raise ValueError('invalid action payload; required: ' + ', '.join(required))

    def _stage(self, record, allowed):
        if record['state'] != 'open' or record.get('business', {}).get('stage') not in allowed:
            raise SetupFlowError('setup_flow_invalid_step', 'Action is not allowed at the current business stage.')
        if record.get('business', {}).get('operation'):
            raise SetupFlowError('setup_flow_invalid_step', 'Resume the interrupted action first.')
        self.owner(record)

    def step(self, flow_id, expected_revision, action, payload):
        with self.locked():
            record = self._get(flow_id); self.revision(record, expected_revision)
            business = dict(record.get('business', {}))
            if action == 'reset_failed_step':
                self.keys(payload, ())
                operation = business.get('operation')
                if record['state'] != 'open' or not record.get('last_error') or not operation:
                    raise SetupFlowError('setup_flow_invalid_step', 'No failed pending operation to reset.')
                prior = operation['action']
                if prior == 'application_source':
                    raise SetupFlowError('setup_flow_invalid_step', 'Frozen source allocation must resume or be abandoned.')
                if prior == 'application_install' and self._installed_operation(business, flow_id, operation['id']):
                    raise SetupFlowError('setup_flow_invalid_step', 'Installation already persisted; resume instead.')
                if prior == 'application_review_submit':
                    current, _ = self.apps.record(business['application'],business['version'])
                    if current.get('review',{}).get('setup_operation_id') == operation['id']:
                        raise SetupFlowError('setup_flow_invalid_step', 'Review already persisted; resume instead.')
                if prior == 'confirm':
                    plan=business['confirmation_plan']
                    if record['kind']=='cluster_setup':
                        applied=self._baseline(plan['cluster']) == digest({'name':plan['cluster'],**plan['settings']})
                    else:
                        current, _=self.apps.record(business['application'],business['version']);applied=current['status']=='active'
                    if applied: raise SetupFlowError('setup_flow_invalid_step','Confirmation already applied; resume instead.')
                business.pop('operation')
                record=self._save(record,'failed_step_reset',business=business,last_error=None,pending_action=business['stage'])
                return {'ok':True,'flow':record}
            if action == 'cluster_inputs':
                if record['kind'] != 'cluster_setup': raise ValueError('cluster_inputs requires cluster_setup')
                self._stage(record, ('awaiting_inputs', 'awaiting_probe', 'awaiting_confirmation'))
                self.keys(payload, ('cluster', 'settings'), ('probe_options',))
                name, settings = payload['cluster'], payload['settings']
                if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', name): raise ValueError('invalid cluster name')
                if not isinstance(settings, dict) or 'ssh_host' not in settings or 'name' in settings: raise ValueError('settings require ssh_host, without name')
                try:
                    Cluster(name, **(dict(scheduler='lsf', work_root='/') | settings))
                except TypeError as exc:
                    raise ValueError(str(exc)) from exc
                options = payload.get('probe_options', {})
                self.keys(options, (), ('paths', 'module_avail', 'max_module_bytes', 'timeout', 'queue_details', 'max_probe_age_seconds'))
                age = options.get('max_probe_age_seconds', 3600)
                if type(age) is not int or not 30 <= age <= 86400: raise ValueError('max_probe_age_seconds must be 30..86400')
                for field in ('module_avail', 'queue_details'):
                    if field in options and type(options[field]) is not bool: raise ValueError(field + ' must be boolean')
                business = {'stage': 'awaiting_probe', 'cluster': name, 'settings': settings,
                            'probe_options': options, 'baseline': self._baseline(name)}
                record = self._save(record, 'cluster_inputs_updated', business=business, pending_action='cluster_probe', last_error=None)
                return {'ok': True, 'flow': record}
            if action == 'application_source':
                if record['kind'] != 'application_adaptation': raise ValueError('application_source requires application_adaptation')
                self._stage(record, ('awaiting_source',))
                self.keys(payload, ('application', 'source_paths'))
                self.apps.name(payload['application'])
                paths = payload['source_paths']
                if not isinstance(paths, list) or not 1 <= len(paths) <= 32 or any(not isinstance(p,str) for p in paths): raise ValueError('source_paths requires 1..32 explicit files')
                names = set(); sources = []; total = 0
                for supplied in paths:
                    path = Path(supplied).expanduser()
                    if path.is_symlink() or not path.is_file(): raise ValueError('source must be a regular file')
                    if path.name in names: raise ValueError('source basenames must be unique')
                    names.add(path.name); total += path.stat().st_size
                    if total > 2097152: raise ValueError('sources exceed 2 MiB')
                    import hashlib
                    sources.append({'source': str(path.resolve()), 'path': path.name, 'size': path.stat().st_size,
                                    'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
                business = {'stage': 'saving_source', 'application': payload['application'], 'sources': sources}
            elif action == 'cluster_probe':
                if record['kind'] != 'cluster_setup': raise ValueError('cluster_probe requires cluster_setup')
                self._stage(record, ('awaiting_probe',)); self.keys(payload, ())
            elif action == 'application_install':
                if record['kind'] != 'application_adaptation': raise ValueError('application_install requires application_adaptation')
                self._stage(record, ('awaiting_adaptation', 'awaiting_revision', 'awaiting_review', 'awaiting_confirmation')); self.keys(payload, ('bundle_dir',))
                if not isinstance(payload['bundle_dir'], str): raise ValueError('bundle_dir must be a string')
                bundle = self.safe(Path(payload['bundle_dir']).expanduser())
                if not bundle.is_relative_to(Path(record['workspace'])) or not bundle.is_dir(): raise ValueError('bundle must be inside this flow workspace')
                files = file_manifest(bundle)
                manifest = json.loads((bundle/'manifest.json').read_text()); self.apps.validate_manifest(manifest)
                if manifest['application'] != business['application']: raise ValueError('bundle application differs from source registration')
                expected = [{k: v for k,v in item.items() if k != 'source'} for item in business['sources']]
                if file_manifest(bundle/'original') != sorted(expected, key=lambda x:x['path']): raise ValueError('original/ must match the frozen source files')
                clusters = self._clusters()
                if any(c not in clusters or clusters[c].scheduler != manifest['scheduler'] for c in manifest['clusters']): raise ValueError('configure declared clusters with matching scheduler before installation')
                business['cluster_settings'] = {c: application_digest(asdict(clusters[c])) for c in manifest['clusters']}
                payload = {'bundle_dir': str(bundle), 'files': files}
            elif action == 'application_review_request':
                self._stage(record, ('awaiting_review',)); self.keys(payload, ('author_session',))
            elif action == 'application_review_submit':
                self._stage(record, ('awaiting_review_report',)); self.keys(payload, ('report',))
            else:
                raise ValueError('unknown setup action')
            business['operation'] = {'id': uuid.uuid4().hex, 'action': action, 'payload': payload}
            record = self._save(record, 'step_started', business=business, pending_action='resume_if_interrupted', last_error=None)
            return {'ok': True, 'flow': self._execute(record)}

    def _installed_operation(self, business, flow_id, operation_id):
        origin = {'flow_id': flow_id, 'operation_id': operation_id}
        app = business['application']
        with self.apps.locked():
            versions = self.apps.safe(self.apps.root/app/'versions')
            if not versions.is_dir(): return None
            for path in sorted(versions.iterdir()):
                if not path.name.isdigit(): continue
                data = json.loads(self.apps.safe(path/'record.json').read_text())
                if data.get('setup_origin') != origin: continue
                index = self.apps.read_index(); entry = index.setdefault(app, {'active':None, 'versions':[]})
                if entry.get('removing'): raise ApplicationError('application_removing','Finish interrupted removal first.')
                version = int(path.name)
                if version not in entry['versions']:
                    entry['versions'].append(version); self.apps.atomic(self.apps.index, index)
                record, _ = self.apps.record(app,version)
                return record
        return None

    def _execute(self, record):
        business = dict(record['business']); operation = business['operation']; action = operation['action']; payload = operation['payload']
        try:
            if action == 'application_source':
                source_dir = self.safe(Path(record['workspace'])/'source'); source_dir.mkdir(exist_ok=True, mode=0o700)
                import hashlib
                for item in business['sources']:
                    target = self.safe(source_dir/item['path'])
                    if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == item['sha256']: continue
                    path = Path(item['source'])
                    if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']: raise ValueError('source changed while archiving; start a new flow')
                    shutil.copyfile(path,target); target.chmod(0o600)
                business['stage'] = 'awaiting_adaptation'
            elif action == 'cluster_probe':
                settings = business['settings']; options = dict(business['probe_options']); options.pop('max_probe_age_seconds',None)
                report = ClusterProbe(self.service, OnboardingStore(self.history)).probe(settings['ssh_host'],
                    scheduler=settings.get('scheduler'), work_root=settings.get('work_root'), **options)
                needs = list(report.get('needs', []))
                if not report['ok']: needs.append('ssh_or_environment')
                scheduler = report.get('scheduler')
                if scheduler:
                    for command in (*COMMANDS[scheduler], 'rsync', 'sha256sum'):
                        if not report.get('commands',{}).get(command): needs.append('command.'+command)
                if report.get('work_root_checks') and not all(report['work_root_checks'].values()): needs.append('work_root_permissions')
                for check in report.get('path_checks',[]):
                    if not check['exists'] or not check['readable']: needs.append('path:'+check['path'])
                if not report.get('queues',{}).get('ok'): needs.append('queue_query')
                business['probe'] = {'report_id': report['report_id'], 'sha256': digest(report), 'created_at': report['created_at']}
                business['needs'] = sorted(set(needs))
                if needs:
                    business['stage'] = 'awaiting_inputs'
                else:
                    target = Cluster(business['cluster'], **(settings | {'scheduler':scheduler}))
                    full = asdict(target); full.pop('name')
                    plan = {'kind':'cluster_setup','flow_id':record['flow_id'],'cluster':business['cluster'],
                            'settings':full,'baseline':business['baseline'],'probe':business['probe'],
                            'unverified': ['shared_storage', 'submission_permission', 'compute_environment']}
                    business.update(stage='awaiting_confirmation', confirmation_plan=plan, confirmation_token=digest(plan))
            elif action == 'application_install':
                installed = self._installed_operation(business,record['flow_id'],operation['id'])
                if installed is None:
                    bundle = self.safe(Path(payload['bundle_dir']))
                    if file_manifest(bundle) != payload['files']: raise ValueError('bundle changed after frozen installation intent')
                    installed = self.apps.install(str(bundle), _setup_origin={'flow_id':record['flow_id'],'operation_id':operation['id']})['application']
                business.update(stage='awaiting_review', version=installed['version'], review_token=installed['review_token'])
                business.pop('review_request',None);business.pop('confirmation_plan',None);business.pop('confirmation_token',None)
            elif action == 'application_review_request':
                packet = self.apps.review_request(business['application'],business['version'],payload['author_session'],_setup_flow_id=record['flow_id'])['review_request']
                business.update(stage='awaiting_review_report', review_request=packet)
            elif action == 'application_review_submit':
                current, _ = self.apps.record(business['application'],business['version'])
                if current.get('review',{}).get('setup_operation_id') == operation['id']:
                    reviewed = current
                else:
                    reviewed = self.apps.review_submit(business['application'],business['version'],business['review_token'],payload['report'],
                        _setup_flow_id=record['flow_id'],_setup_operation_id=operation['id'])['application']
                if reviewed['review']['report']['verdict'] == 'revise':
                    business.update(stage='awaiting_revision', revision_feedback=reviewed['review']['report'])
                else:
                    plan = {'kind':'application_adaptation','flow_id':record['flow_id'],'application':business['application'],
                            'version':business['version'],'review_token':business['review_token'],
                            'manifest':reviewed['manifest'], 'files':reviewed['files'],
                            'review_sha256':digest(reviewed['review']), 'cluster_settings':business['cluster_settings'],
                            'validation': 'unverified_static_review_only'}
                    business.update(stage='awaiting_confirmation',confirmation_plan=plan,confirmation_token=digest(plan))
            elif action == 'confirm':
                self._validate_confirmation(record, payload['confirmation_token'], recovery=True)
                plan = business['confirmation_plan']
                if record['kind'] == 'cluster_setup':
                    plan = business['confirmation_plan']
                    self.config.set(plan['cluster'],plan['settings'],expected_current_digest=plan['baseline'],desired_digest=digest({'name':plan['cluster'],**plan['settings']}))
                    business['result'] = {'cluster':plan['cluster'],'settings_sha256':digest({'name':plan['cluster'],**plan['settings']})}
                    refs = [{'kind':'onboarding_report','report_id':business['probe']['report_id']}]
                else:
                    activated = self.apps.activate(business['application'],business['version'],business['review_token'],payload['confirmation_note'],_setup_flow_id=record['flow_id'],_setup_expected_clusters=plan['cluster_settings'])['application']
                    business['result'] = {'application':business['application'],'version':business['version'],'review_token':activated['review_token'],'restart_required':True}
                    refs = [{'kind':'application_version','application':business['application'],'version':business['version']}]
                evidence = self.artifacts(refs);business['stage']='completed';business.pop('operation')
                return self._save(record,'business_completed',business=business,state='completed',close_outcome='completed',artifacts=evidence,pending_action=None,last_error=None)
            else:
                raise ValueError('unknown persisted action')
            business.pop('operation')
            return self._save(record,'step_completed',business=business,pending_action=business['stage'],last_error=None)
        except (ValueError, OSError) as exc:
            self._save(record,'step_failed',last_error=str(exc)[:4096],pending_action='resume_or_abandon')
            raise

    def _validate_confirmation(self, record, token, recovery=False):
        business=record['business'];plan=business.get('confirmation_plan')
        if not plan or token != business.get('confirmation_token') or digest(plan) != token:
            raise SetupFlowError('setup_flow_confirmation_changed','Confirmation token differs from frozen plan.')
        if record['kind']=='cluster_setup':
            if not self.config: raise ValueError('configuration persistence is unavailable')
            current=self._baseline(plan['cluster']);desired=digest({'name':plan['cluster'],**plan['settings']})
            if current != plan['baseline'] and not (recovery and current == desired):
                raise SetupFlowError('setup_flow_configuration_changed','Cluster changed; update inputs and probe again.')
            report=OnboardingStore(self.history).get(plan['probe']['report_id'],'probe')
            if digest(report)!=plan['probe']['sha256']:raise ValueError('probe evidence changed')
            age=(datetime.now(timezone.utc)-datetime.fromisoformat(report['created_at'])).total_seconds()
            if age>business['probe_options'].get('max_probe_age_seconds',3600) and not (recovery and current==desired):
                raise SetupFlowError('setup_flow_probe_expired','Probe expired; update inputs and probe again.')
        else:
            clusters=self._clusters()
            if any(c not in clusters or application_digest(asdict(clusters[c])) != value for c,value in plan['cluster_settings'].items()):
                raise SetupFlowError('setup_flow_configuration_changed','Application cluster changed; restart independent review with current configuration.')
            installed,_=self.apps.record(plan['application'],plan['version'])
            if installed['review_token'] != plan['review_token'] or digest(installed.get('review'))!=plan['review_sha256'] or installed['review']['report']['verdict']!='pass':
                raise SetupFlowError('setup_flow_review_changed','Independent review changed; request a new review.')

    def confirm(self, flow_id, expected_revision, confirmation_token, confirmation_note):
        if not isinstance(confirmation_note,str) or not confirmation_note.strip() or len(confirmation_note)>4096:raise ValueError('explicit bounded user confirmation note required')
        with self.locked():
            record=self._get(flow_id)
            if record['state'] in ('completed','cleaned') and record.get('business',{}).get('confirmation_token')==confirmation_token:
                return {'ok':True,'flow':record}
            self.revision(record,expected_revision);self._stage(record,('awaiting_confirmation',))
            self._validate_confirmation(record,confirmation_token)
            business=dict(record['business']);business['confirmation']={'time':now(),'note':confirmation_note,'token':confirmation_token}
            business['operation']={'id':uuid.uuid4().hex,'action':'confirm','payload':{'confirmation_token':confirmation_token,'confirmation_note':confirmation_note}}
            record=self._save(record,'confirmation_recorded',business=business,pending_action='resume_if_interrupted')
            return {'ok':True,'flow':self._execute(record)}

    def _resume(self, record):
        if record['state']=='open' and record.get('business',{}).get('operation'):
            self.owner(record)
            return self._execute(record)
        return super()._resume(record)

    def close(self, flow_id, expected_revision, outcome, artifacts=None):
        if outcome=='completed' and self._get(flow_id).get('business'):
            record=self._get(flow_id)
            if record['state'] not in ('completed','cleaned'):
                raise SetupFlowError('setup_flow_invalid_step','Managed business flows complete only through exact-plan confirmation.')
        return super().close(flow_id,expected_revision,outcome,artifacts)
