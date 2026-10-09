"""Ordered, explicitly reviewed compute environment setup; no discovery or evaluation."""

import re
import shlex

from .execution import absolute


def render_setup(steps):
    if not isinstance(steps, list) or len(steps) > 128:
        raise ValueError('setup_steps must be a list of at most 128 steps')
    lines, environment = [], {}
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError('setup step must be an object')
        kind = step.get('kind')
        if kind == 'source' and set(step) == {'kind', 'path'}:
            lines.append('source -- ' + shlex.quote(absolute(step['path'], 'setup source')))
        elif kind == 'module_load' and set(step) == {'kind', 'modules'}:
            modules = step['modules']
            if (not isinstance(modules, list) or not modules or len(modules) > 32
                    or any(not isinstance(m, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./+:-]*', m)
                           for m in modules)):
                raise ValueError('module_load requires literal module identifiers')
            lines.append('module load ' + shlex.join(modules))
        elif kind == 'module_purge' and set(step) == {'kind'}:
            lines.append('module purge')
        elif kind == 'export' and set(step) == {'kind', 'name', 'value'}:
            name, value = step['name'], step['value']
            if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name)
                    or not isinstance(value, str) or any(c in value for c in '\x00\r\n')):
                raise ValueError('export requires a variable name and literal single-line value')
            environment[name] = value
            lines.append('export ' + name + '=' + shlex.quote(value))
        else:
            raise ValueError('setup step accepts source, module_load, module_purge or export')
    return lines, environment
