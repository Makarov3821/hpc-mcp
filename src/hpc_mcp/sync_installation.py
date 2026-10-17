"""Durable per-file installation intentions and recovery under the existing run lock."""

import os
from pathlib import Path
import re
import shutil

from . import result_install


def execute(jobs, run, journal):
    rid = run['run_id']
    target = Path(journal['target'])
    staging = Path(journal['staging'])
    backup = Path(journal['backup']) if journal['backup'] else None

    def save():
        jobs.history.update(rid, 'sync_installation_progress', sync_installation=journal)

    try:
        if backup and not journal.get('backup_complete'):
            if backup.is_symlink():
                raise ValueError('sync backup cannot be a symlink')
            if not backup.exists():
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target), str(backup))
            from .jobs import file_manifest
            if file_manifest(backup) != journal['backup_manifest']:
                raise ValueError('sync backup is incomplete or changed; inspect recovery')
            journal['backup_complete'] = True
            save()

        def root_ready(fd):
            info = os.fstat(fd)
            identity = [info.st_dev, info.st_ino]
            if journal.get('root_identity'):
                if journal['root_identity'] != identity:
                    raise ValueError('installation destination changed; inspect recovery')
            elif journal['started'] and os.listdir(fd):
                raise ValueError('unidentified recovery destination is not empty')
            journal['root_identity'] = identity
            journal['started'] = True
            save()

        def ready(path, identity):
            journal['files'][path] = identity
            save()

        def installed(path):
            journal['files'][path]['installed'] = True
            save()

        recovering = journal['started']
        if not recovering:
            journal['started'] = True
            save()
        # An empty directory created immediately before a crash is safe to adopt;
        # any files in an unidentified directory must be inspected explicitly.
        result_install.install(staging, target, journal['manifest'], journal['overwrite'],
            journal['inplace'],
            recoveries=journal['files'], on_ready=ready, on_installed=installed,
            on_root=root_ready, allow_existing=(journal['inplace'] or
                journal['overwrite'] == 'merge' or recovering))
        journal['phase'] = 'complete'
        updated = jobs.history.update(rid, 'synced',
            sync_installation=journal, sync_state='complete' if journal['final'] else 'partial',
            output_dir=str(target), output_manifest=journal['manifest'], sync_error=None,
            sync_progress={'installed_bytes': sum(f['size'] for f in journal['manifest']),
                           'files': len(journal['manifest'])},
            sync_transfer_stats=journal['transfer_stats'],
            sync_backup=str(backup) if backup else None)
    except (OSError, ValueError) as error:
        updated = jobs.history.update(rid, 'sync_install_failed', sync_state='failed',
            sync_error=str(error), output_dir=str(staging),
            sync_backup=str(backup) if backup else None)
        return {'ok': False, 'run': updated, 'error': str(error)}
    # Commit success before removing the only resumable staging copy.
    try:
        if staging.exists():
            shutil.rmtree(staging)
        if run.get('project_root'):
            jobs._prune_cache_parents(staging.parent)
    except OSError as error:
        updated = jobs.history.update(rid, 'sync_staging_cleanup_deferred',
            sync_staging_cleanup_error=str(error))
    return {'ok': True, 'run': updated, 'final': journal['final'],
            'resumed': journal['resumed'] or recovering,
            'warning': None if journal['manifest'] else
                'no files matched the configured output patterns'}


def recover(jobs, run, requested, resume):
    """A retry may finish the existing transaction, never silently change its scope."""
    from dataclasses import replace
    from .jobs import TERMINAL
    from .sync_support import preview
    journal = run['sync_installation']
    if not resume or journal['approved_policy'] != requested:
        raise ValueError('pending installation needs resume with its original sync options')
    root = jobs.history.root / run['run_id']
    staging = Path(journal['staging'])
    cache = (Path(run['project_root']) / '.hpc-mcp-sync' / run['run_id']
             if run.get('project_root') else root / 'sync-attempts')
    if (staging.parent != cache or not re.fullmatch('[a-f0-9]{32}', staging.name)
            or staging.is_symlink() or journal['options'] != run['sync_options']
            or journal['target'] != journal['options']['destination']):
        raise ValueError('installation recovery paths or options changed')
    if run.get('project_root'):
        jobs._check_project_cache(Path(run['project_root']), cache)
    elif cache.is_symlink():
        raise ValueError('installation recovery cache cannot be a symlink')
    target = Path(journal['target'])
    if requested['destination'] is not None:
        from .sync_support import validate_destination
        validate_destination(jobs.history, run, target, journal['inplace'])
        if target != Path(requested['destination']):
            raise ValueError('installation recovery destination changed')
    else:
        valid = (target == root / 'outputs' if requested['layout'] == 'direct' else
                 target.parent == root / 'outputs' and re.fullmatch('[a-f0-9]{32}', target.name))
        if not valid or (root / 'outputs').is_symlink():
            raise ValueError('installation recovery destination escaped registered outputs')
    if (not re.fullmatch('[a-f0-9]{32}', journal['attempt_id']) or
            journal['backup'] and (Path(journal['backup']) !=
                                  root / 'sync-history' / journal['attempt_id']
                                  or (root / 'sync-history').is_symlink())):
        raise ValueError('installation recovery backup escaped registered storage')
    status = jobs.job_status(run['run_id'])
    if journal['options']['stable_only'] and (
            not status['ok'] or status['run']['state'] not in TERMINAL):
        raise ValueError('installation recovery requires a fresh terminal scheduler state')
    cluster = jobs._cluster(run)
    options = journal['options']
    policy = replace(cluster, output_mode=options['mode'], output_include=options['includes'],
                     output_exclude=options['excludes'], transfer_timeout=options['timeout'])
    selection = preview(jobs, run, cluster, policy, Path(journal['target']),
        options['max_file_bytes'], options['max_total_bytes'], 0, check_space=False)
    if not selection['ok'] or selection['fingerprint'] != journal['selection']['fingerprint']:
        raise ValueError('remote selection changed during interrupted installation; '
                         'inspect recovery')
    journal['final'] = status['ok'] and status['run']['state'] in TERMINAL
    return execute(jobs, run, journal)
