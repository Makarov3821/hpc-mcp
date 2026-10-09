"""Pre-configuration SSH probe; software discovery is limited to module avail."""

from dataclasses import replace
import shlex

from .config import Cluster
from .execution import absolute
from .service import ClusterService
from .schedulers import COMMANDS


class ClusterProbe:
    def __init__(self, service, store):
        self.service, self.store = service, store

    def probe(self, ssh_host, scheduler=None, work_root=None, paths=None,
              module_avail=True, max_module_bytes=32768, timeout=30, cluster=None,
              queue_details=False):
        if scheduler not in (None, 'lsf', 'slurm'):
            raise ValueError('scheduler must be lsf, slurm or null')
        if type(module_avail) is not bool or type(queue_details) is not bool:
            raise ValueError('module_avail and queue_details must be boolean')
        if type(max_module_bytes) is not int or not 1024 <= max_module_bytes <= 262144:
            raise ValueError('max_module_bytes must be 1024..262144')
        if cluster is not None:
            configured = self.service._cluster(cluster)
            if configured.ssh_host != ssh_host:
                raise ValueError('ssh_host differs from configured cluster')
        else:
            configured = None
        if configured:
            scheduler = scheduler or configured.scheduler
            work_root = work_root or configured.work_root
        target = (replace(configured, command_timeout=timeout) if configured else
                  Cluster('probe', ssh_host, scheduler or 'lsf', work_root or '/', command_timeout=timeout))
        if work_root is not None:
            absolute(work_root, 'work_root')
        paths = [] if paths is None else paths
        if not isinstance(paths, list) or len(paths) > 64:
            raise ValueError('paths must contain at most 64 explicit remote paths')
        for path in paths:
            absolute(path, 'explicit path')
        commands = sorted(set(COMMANDS['lsf'] + COMMANDS['slurm'] + ('rsync', 'sha256sum')))
        lines = ["printf 'hostname\\t%s\\n' \"$(hostname)\"",
                 "printf 'user\\t%s\\n' \"$(id -un)\"", "printf 'home\\t%s\\n' \"$HOME\""]
        for command in commands:
            lines.append(f"printf 'command.{command}\\t%s\\n' \"$(command -v {command} || true)\"")
        if work_root:
            for label, flag in [('exists', 'd'), ('readable', 'r'), ('writable', 'w'), ('traversable', 'x')]:
                lines.append(f"if test -{flag} {shlex.quote(work_root)}; then printf 'work_root.{label}\\ttrue\\n'; "
                             f"else printf 'work_root.{label}\\tfalse\\n'; fi")
        for index, path in enumerate(paths):
            for label, flag in [('exists', 'e'), ('readable', 'r'), ('executable', 'x')]:
                lines.append(f"if test -{flag} {shlex.quote(path)}; then printf 'path.{index}.{label}\\ttrue\\n'; "
                             f"else printf 'path.{index}.{label}\\tfalse\\n'; fi")
        response = {'ok': False, 'ssh_host': ssh_host, 'cluster': cluster,
                    'work_root': work_root, 'requested_paths': paths, 'scheduler': scheduler,
                    'shared_storage': 'unverified', 'submission_permission': 'unverified',
                    'warnings': ['Software discovery only queries module avail; no disk search.',
                                 'Login visibility does not establish compute-node usability.']}
        result = self.service.transport.run(target, '\n'.join(lines))
        response['ssh'] = result.diagnostic()
        values = {}
        expected = {'hostname', 'user', 'home'} | {'command.' + c for c in commands}
        if work_root:
            expected |= {'work_root.' + k for k in ('exists', 'readable', 'writable', 'traversable')}
        expected |= {f'path.{i}.{k}' for i in range(len(paths)) for k in ('exists', 'readable', 'executable')}
        if result.ok:
            for line in result.stdout.splitlines():
                key, sep, value = line.partition('\t')
                if not sep or key in values:
                    break
                values[key] = value
            else:
                response['ok'] = set(values) == expected and bool(values.get('hostname')) and bool(values.get('user'))
        if not response['ok']:
            response['error'] = 'ssh_failed_or_invalid_response'
            return self.store.save('probe', response)
        response['environment'] = {k: values[k] for k in ('hostname', 'user', 'home')}
        response['commands'] = {c: values['command.' + c] or None for c in commands}
        candidates = [s for s, c in [('lsf', 'bsub'), ('slurm', 'sbatch')] if response['commands'][c]]
        response['scheduler_candidates'] = candidates
        selected = scheduler or (candidates[0] if len(candidates) == 1 else None)
        response['scheduler'] = selected
        response['needs'] = []
        response['next_steps'] = ['Provide an application script or explicit settings.',
            'Confirm queue/resources/environment/output rules, save a profile, then authorize a short compute probe.']
        if selected is None:
            response['needs'].append('scheduler_selection')
        if not work_root:
            response['needs'].append('work_root')
        response['work_root_checks'] = {k: values['work_root.' + k] == 'true'
                                        for k in ('exists', 'readable', 'writable', 'traversable')} if work_root else None
        response['path_checks'] = [{'path': path, **{k: values[f'path.{i}.{k}'] == 'true'
                                       for k in ('exists', 'readable', 'executable')}} for i, path in enumerate(paths)]
        if selected:
            temporary = replace(target, scheduler=selected, work_root=work_root or '/')
            response['queues'] = ClusterService({'probe': temporary}, self.service.transport).cluster_info('probe')
            if queue_details:
                # Fixed read-only commands; raw site-specific limits remain evidence, not permission.
                query = 'bqueues -l' if selected == 'lsf' else 'scontrol show partition'
                response['queue_details'] = {'command': query,
                    **self.service.transport.run(temporary, query).diagnostic(),
                    'scope': 'raw site limits; account/QOS and user authorization may remain unknown'}
        if module_avail:
            module_target = replace(target, ssh_output_limit=max_module_bytes + 4096)
            module = self.service.transport.run(module_target,
                f"if type module >/dev/null 2>&1; then module avail 2>&1; else exit 127; fi")
            text = module.stdout.encode()
            response['modules'] = {'status': 'available' if module.ok else 'unknown',
                'text': text[:max_module_bytes].decode(errors='replace'),
                'truncated': len(text) > max_module_bytes or module.error == 'output_limit',
                'diagnostic': module.diagnostic() if not module.ok else None,
                'scope': 'login module candidates only; no load or compute validation'}
        else:
            response['modules'] = {'status': 'not_requested'}
        return self.store.save('probe', response)
