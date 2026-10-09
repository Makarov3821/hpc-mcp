"""Bounded Python AST evidence extraction, never imports or runs submission wrappers."""

import ast
import re
import warnings


def literal(node, depth=0):
    if depth > 12:
        raise ValueError('literal nesting limit')
    if isinstance(node, ast.Constant) and type(node.value) in (str, int, float, bool, type(None)):
        return node.value
    if isinstance(node, (ast.List, ast.Tuple)) and len(node.elts) <= 128:
        return [literal(n, depth + 1) for n in node.elts]
    if isinstance(node, ast.Dict) and len(node.keys) <= 128:
        keys = [literal(n, depth + 1) for n in node.keys]
        if not all(isinstance(k, str) for k in keys):
            raise ValueError('only string dictionary keys')
        return dict(zip(keys, [literal(n, depth + 1) for n in node.values]))
    raise ValueError('dynamic expression')


def analyze_python(text, source):
    report = {'ok': True, 'source': source, 'interpreter': {'kind': 'python', 'supported': True},
              'constants': [], 'parameters': [], 'evidence': [], 'unresolved': [],
              'template_draft': None, 'requires_review': True,
              'notes': ['Static AST evidence only; no imports, calls, generated-script execution or automatic conversion.',
                        'Constants and embedded strings are candidates, not proof of an active configuration.']}
    def evidence(node, **values):
        return {'source': source['path'], 'line': node.lineno,
                'raw': (ast.get_source_segment(text, node) or '')[:4096],
                'certainty': 'candidate', **values}
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always', SyntaxWarning)
            tree = ast.parse(text)
        for warning in caught:
            report['unresolved'].append({'source': source['path'], 'line': warning.lineno,
                                         'reason': str(warning.message)[:300]})
    except (SyntaxError, RecursionError, MemoryError) as exc:
        report['unresolved'].append({'line': getattr(exc, 'lineno', 1), 'reason': 'Python parse failed: ' + str(exc)[:300]})
        return report
    nodes = []
    for node in ast.walk(tree):
        nodes.append(node)
        if len(nodes) > 20000:
            raise ValueError('Python AST exceeds 20000 nodes')
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            names = node.targets if isinstance(node, ast.Assign) else [node.target]
            try:
                value = literal(node.value)
                for name in names:
                    if isinstance(name, ast.Name):
                        report['constants'].append(evidence(node, name=name.id, value=value))
            except ValueError:
                report['unresolved'].append(evidence(node, reason='top-level assignment requires runtime evaluation'))
    for node in nodes:
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, 'id', '')
            if name == 'add_argument':
                options, keywords = [], {}
                dynamic = False
                for arg in node.args:
                    try:
                        options.append(literal(arg))
                    except ValueError:
                        dynamic = True
                for keyword in node.keywords:
                    try:
                        keywords[keyword.arg or '**'] = literal(keyword.value)
                    except ValueError:
                        keywords[keyword.arg or '**'] = {'expression': ast.get_source_segment(text, keyword.value)}
                        dynamic = True
                report['parameters'].append(evidence(node, options=options, settings=keywords, dynamic=dynamic))
            if name in {'system', 'run', 'Popen', 'call', 'check_call', 'check_output', 'write_text',
                        'write_bytes', 'unlink', 'remove', 'rmtree', 'exec', 'eval', 'open'}:
                report['evidence'].append(evidence(node, kind='potential_side_effect', operation=name))
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
            if re.search(r'#(?:BSUB|SBATCH)|\bmodule\s+(?:load|purge)\b|\bsource\s|\bmpirun\b|\b(?:bsub|sbatch)\b|^/', value):
                report['evidence'].append(evidence(node, kind='embedded_configuration', value=value[:8192]))
        if isinstance(node, ast.JoinedStr):
            raw = ast.get_source_segment(text, node) or ''
            if re.search(r'BSUB|SBATCH|source|module|mpirun|bsub|sbatch', raw):
                report['unresolved'].append(evidence(node, reason='generated script contains dynamic f-string expressions'))
    report['unresolved'].append({'source': source['path'], 'line': 1,
                                'reason': 'wrapper control flow and generated command equivalence require explicit review'})
    # Bound report multiplication even for source containing many repeated literal fragments.
    for key in ('constants', 'parameters', 'evidence', 'unresolved'):
        if len(report[key]) > 256:
            report[key] = report[key][:256]
            report['notes'].append(key + ' truncated to 256 entries')
    return report
