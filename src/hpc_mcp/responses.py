"""Small preparation receipts; full snapshots remain available through job_get."""


def preparation_receipt(result, compact=True):
    if type(compact) is not bool:
        raise ValueError('compact must be boolean')
    if not compact:
        return result
    run = result['run']
    receipt = {'ok': result['ok'], 'compact': True, 'run': {key: run[key] for key in
        ('run_id', 'cluster', 'scheduler', 'phase', 'state', 'remote_dir', 'input_dir', 'project_root',
         'script', 'outputs', 'profile') if key in run},
        'resources': run.get('generation', {}).get('spec', {}).get('resources', {}),
        'input_count': len(run['manifest']), 'input_bytes': sum(e['size'] for e in run['manifest']),
        'details_tool': 'job_get', 'notes': result.get('notes', [])}
    if run.get('template', {}).get('application_plugin'):
        receipt['plugin'] = {key: run['template']['application_plugin'][key] for key in
            ('application', 'version', 'review_token', 'parameters')}
    if 'plan_id' in result:
        receipt['plan_id'] = result['plan_id']
    if 'gaussian' in result:
        receipt['gaussian'] = {key: result['gaussian'][key] for key in
            ('kind', 'input', 'changes', 'diff', 'log', 'expected_sections',
             'original_sha256', 'effective_sha256')}
        receipt['run']['application'] = receipt['gaussian']
    return receipt
