"""Bounded, conservative Shell inspection. Source text is data, never executed."""

import base64
import hashlib
import re
import shlex
from pathlib import Path

from .execution import absolute
from .scripts import script_generate
from .jobs import relative_path


class ScriptInspector:
    def __init__(self, service=None):
        self.service = service

    def inspect(self, script_path, cluster=None, max_bytes=262144):
        if type(max_bytes) is not int or not 1 <= max_bytes <= 1048576:
            raise ValueError('max_bytes must be 1..1048576')
        if cluster is None:
            path = Path(script_path).expanduser()
            if path.is_symlink() or not path.is_file():
                raise ValueError('script must be a regular file, not a symlink')
            with path.open('rb') as stream:
                data = stream.read(max_bytes + 1)
            source = {'kind': 'local', 'path': str(path.absolute())}
        else:
            if self.service is None:
                raise ValueError('remote inspection requires a cluster service')
            target = self.service._cluster(cluster)
            path = absolute(script_path, 'remote script')
            if ((max_bytes + 3) // 3 * 4 + 4096) > target.ssh_output_limit:
                raise ValueError('max_bytes exceeds the cluster SSH output budget')
            quoted = shlex.quote(path)
            result = self.service.transport.run(target,
                f'set -o pipefail\ntest ! -L {quoted}\ntest -f {quoted}\nhead -c {max_bytes + 1} -- {quoted} | base64')
            if not result.ok:
                raise ValueError(f'remote read failed: {result.error or result.returncode}')
            try:
                data = base64.b64decode(''.join(result.stdout.split()), validate=True)
            except ValueError as exc:
                raise ValueError('invalid remote file response') from exc
            source = {'kind': 'remote', 'cluster': cluster, 'ssh_host': target.ssh_host, 'path': path}
        if len(data) > max_bytes:
            raise ValueError('script exceeds max_bytes')
        try:
            text = data.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise ValueError('script must be UTF-8 text') from exc
        if '\x00' in text:
            raise ValueError('script contains NUL')
        return analyze(text, {**source, 'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)})


def analyze(text, source):
    directives, evidence, unresolved, paths, commands = [], [], [], [], []
    spec = {'resources': {}, 'environment': {}, 'init_scripts': []}
    schedulers = set()
    executable_seen = False
    lines = text.splitlines()
    shell_supported = (not lines or not lines[0].startswith('#!')
                       or bool(re.fullmatch(r'#!\s*/(?:bin/(?:ba)?sh|usr/bin/(?:ba)?sh|usr/bin/env (?:ba)?sh)\s*', lines[0])))
    if not shell_supported:
        unresolved.append({'source': source['path'], 'line': 1, 'certainty': 'unresolved', 'reason': 'unsupported interpreter; Bash/sh analysis only'})
    # Context-sensitive Shell constructs make even directive-looking text ambiguous.
    complex_shell = any(re.search(r'<<|\\$', line) or re.match(r'\s*(?:if|for|while|case|function)\b', line)
                        for line in lines if not line.lstrip().startswith('#'))

    def item(number, raw, **values):
        return {'source': source['path'], 'line': number, 'raw': raw, 'certainty': 'certain', **values}

    def problem(number, reason):
        unresolved.append({'source': source['path'], 'line': number, 'certainty': 'unresolved', 'reason': reason})

    def path_hint(number, raw, path, role, certainty='certain'):
        paths.append(item(number, raw, path=path, role=role, certainty=certainty))

    mapping = {
        'slurm': {'--cpus-per-task': 'cpus', '-c': 'cpus', '--nodes': 'nodes', '-N': 'nodes',
                  '--ntasks': 'tasks', '-n': 'tasks', '--ntasks-per-node': 'tasks_per_node',
                  '--partition': 'queue', '-p': 'queue', '--account': 'account', '-A': 'account',
                  '--qos': 'qos', '--gres': 'slurm_gres', '--gpus-per-task': 'slurm_gpus_per_task',
                  '--constraint': 'slurm_constraint', '-C': 'slurm_constraint'},
        'lsf': {'-n': 'cpus', '-q': 'queue', '-gpu': 'lsf_gpu', '-R': 'lsf_resource_requirement'}}
    overridden = {'slurm': {'--job-name', '-J', '--chdir', '-D', '--output', '-o', '--error', '-e'},
                  'lsf': {'-J', '-cwd', '-o', '-oo', '-e', '-eo'}}
    for number, raw in enumerate(lines, 1):
        stripped = raw.strip()
        match = re.match(r'^#(SBATCH|BSUB)\s+(.*)$', stripped)
        if match:
            scheduler = 'slurm' if match[1] == 'SBATCH' else 'lsf'
            active = not (scheduler == 'slurm' and executable_seen)
            if active:
                schedulers.add(scheduler)
            entry = item(number, raw, scheduler=scheduler, active=active, options=[])
            directives.append(entry)
            if not active:
                entry['reason'] = 'Slurm ignores directives after the first executable line'
                continue
            if complex_shell:
                entry['certainty'] = 'unresolved'
                problem(number, 'directive context may be quoted/multiline Shell; review scheduler parsing')
            try:
                tokens = shlex.split(match[2], comments=True)
                index = 0
                while index < len(tokens):
                    option = tokens[index]
                    index += 1
                    if '=' in option and option.startswith('--'):
                        option, value = option.split('=', 1)
                    else:
                        attached = re.fullmatch(r'(-[ncNpAJoDet])(.+)', option)
                        if attached and option not in mapping[scheduler] and option not in overridden[scheduler]:
                            option, value = attached.groups()
                        else:
                            if index >= len(tokens):
                                raise ValueError('option without a value')
                            value = tokens[index]
                            index += 1
                    detail = {'option': option, 'value': value, 'literal': True,
                              'submission_override': option in overridden[scheduler]}
                    entry['options'].append(detail)
                    if option in overridden[scheduler]:
                        if option in {'--output', '--error', '-o', '-oo', '-e', '-eo'}:
                            path_hint(number, raw, value, 'scheduler_output_overridden')
                        continue
                    key = mapping[scheduler].get(option)
                    if key:
                        if key in {'cpus', 'nodes', 'tasks', 'tasks_per_node'}:
                            if not value.isdecimal():
                                raise ValueError('resource integer is not a literal positive count')
                            value = int(value)
                        if key == 'lsf_resource_requirement':
                            value = re.sub(r'\bspan\[hosts=1\]', '', value).strip()
                            if not value:
                                continue
                        spec['resources'][key] = value
                    elif scheduler == 'slurm' and option in {'--mem', '--mem-per-cpu'}:
                        mem = re.fullmatch(r'([1-9][0-9]*)([MG]?)', value, re.I)
                        if not mem:
                            raise ValueError('memory supports integral M/G only')
                        spec['resources'].update(memory_mb=int(mem[1]) * (1024 if mem[2].upper() == 'G' else 1),
                            memory_scope='per_cpu' if option == '--mem-per-cpu' else 'per_node')
                    elif (scheduler == 'slurm' and option in {'--time', '-t'}) or (scheduler == 'lsf' and option == '-W'):
                        if value.isdecimal():
                            minutes = int(value)
                        else:
                            duration = re.fullmatch(r'(?:(\d+)-)?(\d+):([0-5][0-9])(?::([0-5][0-9]))?', value)
                            if not duration or (scheduler == 'lsf' and (duration[1] or duration[4])):
                                raise ValueError('unsupported scheduler duration')
                            if scheduler == 'slurm' and not duration[1] and duration[4] is None:
                                # Slurm MM:SS, unlike LSF HH:MM. Never round requested time.
                                if duration[3] != '00':
                                    raise ValueError('sub-minute Slurm time cannot be represented exactly')
                                minutes = int(duration[2])
                            else:
                                if duration[4] not in (None, '00'):
                                    raise ValueError('sub-minute time cannot be represented exactly')
                                minutes = int(duration[1] or 0) * 1440 + int(duration[2]) * 60 + int(duration[3])
                        spec['resources']['time_minutes'] = minutes
                    else:
                        problem(number, f'unsupported scheduler option {option}; retained as literal evidence')
            except ValueError as exc:
                problem(number, str(exc))
            continue
        if not stripped or stripped.startswith('#'):
            continue
        executable_seen = True
        if commands:
            problem(number, 'executable content after the command cannot be safely reordered into a template')
        if not shell_supported:
            continue
        # Deliberately conservative, including expansion-looking text inside quotes.
        if any(marker in raw for marker in ('$', '`', '\\', ';', '|', '&', '(', ')', '<<')):
            problem(number, 'dynamic expansion, compound command or multiline syntax is outside the static subset')
            continue
        try:
            lexer = shlex.shlex(raw, posix=True, punctuation_chars='<>')
            lexer.whitespace_split = True
            tokens = list(lexer)
            if not tokens:
                continue
            if tokens[0] == 'set':
                if tokens not in [['set', '-euo', 'pipefail'], ['set', '-eu'], ['set', '-e'], ['set', '-u']]:
                    problem(number, 'unsupported shell option change')
                else:
                    evidence.append(item(number, raw, kind='shell_options', tokens=tokens))
                continue
            if tokens[0] == 'export' and len(tokens) == 2 and '=' in tokens[1]:
                name, value = tokens[1].split('=', 1)
                if not re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', name):
                    raise ValueError('invalid export variable')
                spec['environment'][name] = value
                evidence.append(item(number, raw, kind='environment', name=name, value=value))
                continue
            if tokens[0] in ('source', '.'):
                if len(tokens) != 2 or not tokens[1].startswith('/'):
                    raise ValueError('source requires one literal remote absolute path')
                if spec['environment']:
                    problem(number, 'source after export would change initialization order in a generated template')
                spec['init_scripts'].append(tokens[1])
                evidence.append(item(number, raw, kind='init_script', path=tokens[1]))
                path_hint(number, raw, tokens[1], 'remote_dependency')
                continue
            if tokens[0] == 'mkdir' and tokens[1:2] == ['-p']:
                directories = tokens[2:]
                if directories[:1] == ['--']:
                    directories = directories[1:]
                if not directories or any(x.startswith('-') for x in directories):
                    raise ValueError('unsupported mkdir arguments')
                for directory in directories:
                    relative_path(directory)
                    spec.setdefault('output_directories', []).append(directory)
                    path_hint(number, raw, directory, 'output_directory')
                continue
            # shlex discards quoting and descriptor adjacency. Do not infer a redirection
            # from a quoted operator; preserve numeric argv separate from unquoted 2>.
            if re.search(r'''["'][^"']*[<>][^"']*["']''', raw):
                raise ValueError('quoted redirection-looking text requires review')
            if raw.count('2>') > 1 or ('2>' in raw and re.search(r'\b2\s+>', raw)):
                raise ValueError('ambiguous descriptor redirections require review')
            descriptors = re.findall(r'(?:^|\s)([0-9]+)([<>])', raw)
            if any(pair != ('2', '>') for pair in descriptors):
                raise ValueError('only implicit stdin/stdout and explicit 2> descriptors are supported')
            stderr_redirect = bool(re.search(r'(?:^|\s)2>', raw))
            if tokens[0] == 'exec':
                tokens.pop(0)
                if tokens[:1] == ['--']:
                    tokens.pop(0)
            if not tokens or tokens[0] in {'cd', 'if', 'then', 'fi', 'for', 'do', 'done', 'while', 'case', 'esac', 'exit', 'return', 'eval', 'unset', 'read', 'trap', '{', '}', '!', '[[', ']]', '[', ']', 'command', 'builtin', 'time', 'select', 'until', 'coproc', 'declare', 'typeset', 'readonly', 'alias', 'unalias', 'local', 'break', 'continue'} or '=' in tokens[0]:
                raise ValueError('control flow, directory change or shell builtin requires review')
            argv, redirections = [], {}
            index = 0
            while index < len(tokens):
                token = tokens[index]
                index += 1
                if token in ('<', '>'):
                    label = 'stdin' if token == '<' else 'stdout'
                    if token == '>' and stderr_redirect and argv and argv[-1] == '2':
                        argv.pop()
                        label = 'stderr'
                    if index >= len(tokens) or label in redirections:
                        raise ValueError('missing/repeated redirection')
                    path = relative_path(tokens[index])
                    index += 1
                    redirections[label] = path
                    path_hint(number, raw, path, 'input' if label == 'stdin' else 'output')
                elif '<' in token or '>' in token:
                    raise ValueError('append/descriptor redirection requires review')
                else:
                    argv.append(token)
            if not argv:
                raise ValueError('empty command')
            if Path(argv[0]).name in {'srun', 'mpirun', 'mpiexec', 'apptainer', 'singularity'}:
                problem(number, 'existing launcher/container command requires explicit structured resource review')
            commands.append(item(number, raw, argv=argv, redirections=redirections))
            if '/' in argv[0]:
                path_hint(number, raw, argv[0], 'remote_executable', 'inferred')
            for arg in argv[1:]:
                if not arg.startswith('-') and ('.' in arg or '/' in arg):
                    path_hint(number, raw, arg, 'command_argument', 'inferred')
            spec.update(command=argv, **redirections)
        except ValueError as exc:
            problem(number, str(exc))
    scheduler = next(iter(schedulers)) if len(schedulers) == 1 else None
    if scheduler is None:
        problem(1, 'exactly one active scheduler directive family is required for a template draft')
    if len(commands) != 1:
        problem(1, 'template conversion requires exactly one static command')
    outputs = sorted({p['path'] for p in paths if p['role'] == 'output'})
    draft = None
    if not unresolved and scheduler and outputs:
        try:
            script_generate(scheduler, spec)
            draft = {'scheduler': scheduler, 'spec': spec, 'outputs': outputs}
            if spec.get('stdin'):
                draft['input_files'] = [spec['stdin']]
        except ValueError as exc:
            problem(1, f'draft validation: {exc}')
    return {'ok': True, 'source': source, 'scheduler': scheduler, 'directives': directives,
            'evidence': evidence, 'commands': commands, 'paths': paths, 'unresolved': unresolved,
            'template_draft': draft, 'requires_review': True,
            'extracted_spec': spec,
            'interpreter': {'supported': shell_supported,
                'certainty': 'certain' if lines and lines[0].startswith('#!') else 'inferred'},
            'notes': ['Read-only static subset; no execution, imports, filesystem discovery or automatic template installation.',
                      'Scheduler directives are literal; job_submit overrides run name, cwd and scheduler logs.',
                      'Review all dependencies, output patterns and environment. A draft is not a proof of equivalence; original scripts remain usable with job_prepare.',
                      'Candidate argument paths are inferred, not verified inputs. No draft without explicit body outputs.',
                      'LSF -n alone is ambiguous between MPI ranks and shared-memory slots; review the draft CPU interpretation. Scripts without a shebang have an inferred interpreter.']}
