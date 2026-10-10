"""JSON CLI, usable without installing the MCP SDK."""

import argparse
import json
import os
import sys
import sqlite3

from .config import ConfigManager, load_config
from pathlib import Path
from .service import ClusterService


def main():
    parser = argparse.ArgumentParser(description="Inspect LSF/Slurm clusters over SSH")
    parser.add_argument("--config", default=os.environ.get("HPC_MCP_CONFIG", "clusters.toml"))
    parser.add_argument("--state-dir", default=os.environ.get("HPC_MCP_STATE", ".hpc-mcp"))
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("list")
    workflow_plan = sub.add_parser('workflow-plan')
    workflow_plan.add_argument('name')
    workflow_plan.add_argument('run_ids', nargs='+')
    workflow_plan.add_argument('--dependencies-file', help='JSON list of dependency edges')
    workflow_plan.add_argument('--limits', default='{}', help='JSON concurrency and rate settings')
    workflow_start = sub.add_parser('workflow-start')
    workflow_start.add_argument('workflow_id')
    workflow_start.add_argument('review_token')
    workflow_start.add_argument('--confirmation-note', required=True)
    for action in ('workflow-pause', 'workflow-tick'):
        sub.add_parser(action).add_argument('workflow_id')
    workflow_status = sub.add_parser('workflow-status')
    workflow_status.add_argument('workflow_id', nargs='?')
    workflow_status.add_argument('--limit', type=int, default=50)
    workflow_status.add_argument('--offset', type=int, default=0)
    workflow_retry = sub.add_parser('workflow-retry')
    workflow_retry.add_argument('workflow_id')
    workflow_retry.add_argument('task_ids', nargs='+')
    usage = sub.add_parser('job-usage')
    usage.add_argument('run_id')
    usage.add_argument('--refresh', action=argparse.BooleanOptionalAction, default=True)
    usage_report = sub.add_parser('usage-report')
    usage_report.add_argument('--cluster')
    usage_report.add_argument('--project-root')
    usage_report.add_argument('--since')
    usage_report.add_argument('--until')
    usage_report.add_argument('--limit', type=int, default=500)
    usage_report.add_argument('--offset', type=int, default=0)
    probe = sub.add_parser('cluster-probe', help='Read-only new-cluster onboarding; no software disk search')
    probe.add_argument('ssh_host')
    probe.add_argument('--scheduler', choices=('lsf', 'slurm'))
    probe.add_argument('--work-root')
    probe.add_argument('--path', action='append')
    probe.add_argument('--module-avail', action=argparse.BooleanOptionalAction, default=True)
    probe.add_argument('--max-module-bytes', type=int, default=32768)
    probe.add_argument('--timeout', type=int, default=30)
    probe.add_argument('--cluster')
    probe.add_argument('--queue-details', action='store_true')
    sub.add_parser('onboarding-report').add_argument('report_id')
    legacy_actions = ('profile-draft', 'profile-confirm', 'profile-plan', 'profile-validate')
    for action in legacy_actions:
        sub.add_parser(action, help='Legacy command; reports application migration required')
    sub.add_parser('profile-get').add_argument('profile_id')
    profile_list = sub.add_parser('profile-list')
    profile_list.add_argument('--cluster')
    profile_list.add_argument('--application')
    profile_list.add_argument('--limit', type=int, default=50)
    profile_list.add_argument('--offset', type=int, default=0)
    profile_list.add_argument('--compact', action=argparse.BooleanOptionalAction, default=True)
    for action in ("monitor-start", "monitor-run"):
        monitor = sub.add_parser(action, help="Detached coordinator start or foreground service process")
        monitor.add_argument("--settings", help="JSON settings; omit to reuse persisted defaults")
    sub.add_parser("monitor-stop")
    monitor_status = sub.add_parser("monitor-status")
    monitor_status.add_argument("--limit", type=int, default=50)
    monitor_status.add_argument("--offset", type=int, default=0)
    notifications = sub.add_parser("monitor-notifications")
    notifications.add_argument("--after-id", type=int, default=0)
    notifications.add_argument("--limit", type=int, default=50)
    watch = sub.add_parser("monitor-watch")
    watch.add_argument("run_ids", nargs="+")
    watch.add_argument("--auto-sync", action=argparse.BooleanOptionalAction, default=True)
    watch.add_argument("--sync-options", default="{}")
    watch.add_argument("--cleanup-policy", default="{}")
    watch.add_argument("--reset", action="store_true")
    sub.add_parser("monitor-unwatch").add_argument("run_id")
    for action in ("check", "info"):
        sub.add_parser(action).add_argument("cluster")
    sub.add_parser("serve", help="Run MCP over stdio (requires the mcp extra)")
    for action in ("update-check", "update-plan"):
        update = sub.add_parser(action, help="Check upstream or print safe update commands")
        update.add_argument("--force", action="store_true")
        update.add_argument("--max-age-seconds", type=int, default=86400)
        update.add_argument("--timeout", type=int, default=10)
    inspect = sub.add_parser("script-inspect", help="Read-only Bash inspection and review-only draft")
    inspect.add_argument("script_path")
    inspect.add_argument("--cluster")
    inspect.add_argument("--max-bytes", type=int, default=262144)
    sub.add_parser("gaussian-inspect").add_argument("input_file")
    gaussian = sub.add_parser("gaussian-prepare")
    gaussian.add_argument("cluster")
    gaussian.add_argument("input_file")
    gaussian.add_argument("--project-root", required=True)
    gaussian.add_argument('--parameters', help='JSON registered handler parameters')
    gaussian.add_argument('--compact', action=argparse.BooleanOptionalAction, default=True)
    result = sub.add_parser("gaussian-result")
    result.add_argument("run_id")
    result.add_argument("--log-path")
    result.add_argument("--max-bytes", type=int, default=1048576)
    background = sub.add_parser("sync-start")
    background.add_argument("run_id")
    background.add_argument("--options", default="{}", help="JSON job_sync options")
    sub.add_parser("sync-operation").add_argument("operation_id")
    storage = sub.add_parser("storage-cleanup")
    storage.add_argument("run_id")
    storage.add_argument("--category", action="append", choices=("snapshot", "sync_history", "old_outputs"))
    storage.add_argument("--older-than-seconds", type=int, default=86400)
    storage.add_argument("--apply", action="store_true")
    cache = sub.add_parser("input-cache-cleanup")
    cache.add_argument("--older-than-seconds", type=int, default=86400)
    cache.add_argument("--max-cache-bytes", type=int, default=10737418240)
    cache.add_argument("--apply", action="store_true")
    remote = sub.add_parser("remote-cleanup")
    remote.add_argument("run_id")
    remote.add_argument("--apply", action="store_true")
    preview = sub.add_parser("sync-preview")
    preview.add_argument("run_id")
    preview.add_argument("--mode", choices=("all", "filtered"))
    preview.add_argument("--include", action="append")
    preview.add_argument("--exclude", action="append")
    preview.add_argument("--max-file-bytes", type=int)
    preview.add_argument("--max-total-bytes", type=int)
    preview.add_argument("--reserve-bytes", type=int)
    preview.add_argument("--timeout", type=int)
    generate = sub.add_parser("script-generate", help="Preview a script from a JSON spec file")
    generate.add_argument("scheduler", choices=("lsf", "slurm"))
    generate.add_argument("spec_file")
    generated = sub.add_parser("prepare-generated", help="Generate a script inside the input snapshot")
    generated.add_argument("cluster")
    generated.add_argument("input_dir")
    generated.add_argument("spec_file")
    generated.add_argument("--output", action="append", required=True)
    generated.add_argument("--project-root")
    generated.add_argument("--input-file", action="append")
    generated.add_argument("--script-name", default="hpc-mcp-job.sh")
    generated.add_argument("--output-exclude", action="append")
    generated.add_argument("--input-exclude", action="append")
    generated.add_argument("--max-input-bytes", type=int)
    imported = sub.add_parser("template-import", help="Save a new template version from a JSON file")
    imported.add_argument("name")
    imported.add_argument("definition_file")
    templates_list = sub.add_parser("template-list")
    templates_list.add_argument("--limit", type=int, default=50)
    templates_list.add_argument("--offset", type=int, default=0)
    template_get = sub.add_parser("template-get")
    template_get.add_argument("name")
    template_get.add_argument("--version", type=int)
    template_plan = sub.add_parser("template-plan")
    template_plan.add_argument("name")
    template_plan.add_argument("cluster")
    template_plan.add_argument("input_dir")
    template_plan.add_argument("--parameters", default="{}", help="JSON parameter bindings")
    template_plan.add_argument("--version", type=int)
    template_plan.add_argument("--project-root")
    sub.add_parser("template-run").add_argument("plan_id")
    prepare = sub.add_parser("prepare", help="Snapshot an input directory without executing it")
    prepare.add_argument("cluster")
    prepare.add_argument("input_dir")
    prepare.add_argument("script", help="Script path relative to input_dir")
    prepare.add_argument("--output", action="append", help="Repeatable rsync output include pattern")
    prepare.add_argument("--output-mode", choices=("all", "filtered"))
    prepare.add_argument("--output-exclude", action="append")
    prepare.add_argument("--input-exclude", action="append")
    prepare.add_argument("--max-input-bytes", type=int)
    prepare.add_argument("--project-root", help="Project A; stage downloads here and return to input_dir")
    prepare.add_argument("--input-file", action="append", help="Exact relative input file; repeat for dependencies")
    cleanup = sub.add_parser("cache-cleanup", help="Preview/delete inactive sync attempts for one run")
    cleanup.add_argument("run_id")
    cleanup.add_argument("--older-than-seconds", type=int, default=86400)
    cleanup.add_argument("--apply", action="store_true", help="Delete candidates (default: preview)")
    for action in ("submit", "status", "get", "recover", "cancel"):
        sub.add_parser(action).add_argument("run_id")
    sync = sub.add_parser("sync")
    sync.add_argument("run_id")
    sync.add_argument("--mode", choices=("all", "filtered"))
    sync.add_argument("--include", action="append")
    sync.add_argument("--exclude", action="append")
    sync.add_argument("--destination")
    sync.add_argument("--layout", choices=("snapshot", "direct"))
    sync.add_argument("--overwrite", choices=("error", "replace", "merge"))
    sync.add_argument("--checksum", action=argparse.BooleanOptionalAction, default=None)
    sync.add_argument("--compress", action=argparse.BooleanOptionalAction, default=None)
    sync.add_argument("--timeout", type=int)
    sync.add_argument("--resume", action=argparse.BooleanOptionalAction, default=None)
    sync.add_argument("--max-file-bytes", type=int)
    sync.add_argument("--max-total-bytes", type=int)
    sync.add_argument("--reserve-bytes", type=int)
    sync.add_argument("--stable-only", action="store_true")
    config_get = sub.add_parser("config-get")
    config_get.add_argument("cluster")
    config_set = sub.add_parser("config-set")
    config_set.add_argument("cluster")
    config_set.add_argument("settings", help="JSON object of settings to create/update")
    history = sub.add_parser("history")
    history.add_argument("--cluster")
    history.add_argument("--limit", type=int, default=50)
    history.add_argument("--offset", type=int, default=0)
    logs = sub.add_parser("logs")
    logs.add_argument("run_id")
    logs.add_argument("--stream", choices=("stdout", "stderr"), default="stdout")
    logs.add_argument("--lines", type=int, default=100)
    for action in ('application-list', 'application-install', 'application-update', 'application-get',
                   'application-review-request', 'application-review-submit', 'application-activate', 'application-prepare', 'application-remove',
                   'application-cleanup', 'application-validate'):
        ap = sub.add_parser(action)
        if action in ('application-install', 'application-update'):
            ap.add_argument('bundle_dir')
        elif action == 'application-validate':
            ap.add_argument('run_id')
        elif action not in ('application-list', 'application-cleanup'):
            ap.add_argument('application')
            if action in ('application-activate', 'application-review-submit'):
                ap.add_argument('version', type=int); ap.add_argument('review_token')
                if action == 'application-activate': ap.add_argument('--note', required=True)
                else: ap.add_argument('--report-file', required=True)
            elif action == 'application-review-request':
                ap.add_argument('version', type=int); ap.add_argument('--author-session', required=True)
            elif action in ('application-get', 'application-prepare'):
                ap.add_argument('--version', type=int)
        if action == 'application-prepare':
            ap.add_argument('cluster'); ap.add_argument('input_path'); ap.add_argument('--project-root', required=True)
            ap.add_argument('--parameters', default='{}'); ap.add_argument('--compact', action=argparse.BooleanOptionalAction, default=True)
        if action in ('application-remove', 'application-cleanup'):
            ap.add_argument('--apply', action='store_true')
        if action == 'application-cleanup':
            ap.add_argument('--older-than-seconds', type=int, default=86400)
    args, unknown = parser.parse_known_args()
    if unknown and args.action not in legacy_actions:
        parser.error('unrecognized arguments: ' + ' '.join(unknown))
    try:
        if args.action in legacy_actions:
            raise ValueError('legacy application profile requires migration; use application-install/review-request/review-submit/activate/prepare')
        if args.action.startswith('application-') or args.action == 'gaussian-prepare':
            from .applications import ApplicationService
            from .jobs import JobService
            from .responses import preparation_receipt
            clusters = load_config(args.config) if Path(args.config).exists() else {}
            apps = ApplicationService(JobService(clusters, args.state_dir))
            action = args.action.removeprefix('application-')
            if action == 'list': data = apps.list()
            elif action in ('install', 'update'): data = apps.install(args.bundle_dir)
            elif action == 'get': data = apps.get(args.application, args.version)
            elif action == 'review-request': data = apps.review_request(args.application, args.version, args.author_session)
            elif action == 'review-submit': data = apps.review_submit(args.application, args.version, args.review_token, json.loads(Path(args.report_file).read_text()))
            elif action == 'activate': data = apps.activate(args.application, args.version, args.review_token, args.note)
            elif action == 'remove': data = apps.remove(args.application, not args.apply)
            elif action == 'cleanup': data = apps.cleanup(args.older_than_seconds, not args.apply)
            elif action == 'validate': data = apps.validate(args.run_id)
            elif args.action == 'gaussian-prepare':
                data = preparation_receipt(apps.prepare('gaussian', args.cluster, args.input_file, args.project_root,
                    json.loads(args.parameters) if args.parameters else None), args.compact)
            else:
                data = preparation_receipt(apps.prepare(args.application, args.cluster, args.input_path,
                    args.project_root, json.loads(args.parameters), args.version), args.compact)
            print(json.dumps(data, ensure_ascii=False, indent=2))
            sys.exit(0 if data.get('ok') else 1)
        if args.action == "script-inspect":
            from .profiles import ProfileService
            from .jobs import JobService
            clusters = load_config(args.config) if Path(args.config).exists() else {}
            data = ProfileService(ClusterService(clusters), JobService(clusters, args.state_dir)).inspect(
                args.script_path, args.cluster, args.max_bytes)
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return
        if args.action == "gaussian-inspect":
            from .gaussian import gaussian_inspect
            print(json.dumps(gaussian_inspect(args.input_file), ensure_ascii=False, indent=2))
            return
        if args.action in ("update-check", "update-plan"):
            from .updates import UpdateService
            updates = UpdateService(args.state_dir)
            method = updates.check if args.action == "update-check" else updates.plan
            data = method(args.force, args.max_age_seconds, args.timeout)
            print(json.dumps(data, ensure_ascii=False, indent=2))
            if not data["ok"]:
                sys.exit(1)
            return
        if args.action == "script-generate":
            from .scripts import script_generate
            data = script_generate(args.scheduler, json.loads(Path(args.spec_file).expanduser().read_text()))
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return
        onboarding = args.action.startswith('profile-') or args.action in ('cluster-probe', 'onboarding-report')
        scheduling = args.action.startswith('workflow-') or args.action in ('job-usage', 'usage-report')
        clusters = {} if (onboarding or scheduling or args.action in ("serve", "config-set", "template-import", "template-list", "template-get", "sync-operation", "storage-cleanup", "input-cache-cleanup", "gaussian-result", "monitor-status", "monitor-stop", "monitor-unwatch", "monitor-notifications")) \
            and not Path(args.config).exists() \
            else load_config(args.config)
        config = ConfigManager(args.config, clusters)
        service = ClusterService(clusters)
        if scheduling:
            from .jobs import JobService
            from .workflow import WorkflowService
            from .accounting import AccountingService
            jobs = JobService(clusters, args.state_dir)
            workflows = WorkflowService(jobs)
            if args.action == 'workflow-plan':
                data = workflows.plan(args.name, args.run_ids,
                    json.loads(Path(args.dependencies_file).read_text()) if args.dependencies_file else None,
                    json.loads(args.limits))
            elif args.action == 'workflow-start':
                data = workflows.start(args.workflow_id, args.review_token, args.confirmation_note)
            elif args.action == 'workflow-pause':
                data = workflows.pause(args.workflow_id)
            elif args.action == 'workflow-retry':
                data = workflows.retry(args.workflow_id, args.task_ids)
            elif args.action == 'workflow-tick':
                data = workflows.tick(args.workflow_id)
            elif args.action == 'workflow-status':
                data = workflows.get(args.workflow_id) if args.workflow_id else workflows.list(args.limit, args.offset)
            elif args.action == 'job-usage':
                data = AccountingService(jobs).usage(args.run_id, args.refresh)
            else:
                data = AccountingService(jobs).report(args.cluster, args.project_root, args.since, args.until, args.limit, args.offset)
            print(json.dumps(data, ensure_ascii=False, indent=2))
            if not data.get('ok', True):
                sys.exit(1)
            return
        if onboarding:
            from .profiles import ProfileService
            from .jobs import JobService
            profiles = ProfileService(service, JobService(clusters, args.state_dir))
            if args.action == 'cluster-probe':
                data = profiles.probes.probe(args.ssh_host, args.scheduler, args.work_root,
                    args.path, args.module_avail, args.max_module_bytes, args.timeout, args.cluster, args.queue_details)
            elif args.action == 'onboarding-report':
                data = {'ok': True, 'report': profiles.store.get(args.report_id)}
            elif args.action == 'profile-get':
                data = profiles.get(args.profile_id)
            else:
                data = profiles.list(args.cluster, args.application, args.limit, args.offset, args.compact)
            print(json.dumps(data, ensure_ascii=False, indent=2))
            if not data.get('ok', True):
                sys.exit(1)
            return
        if args.action == "serve":
            from .jobs import JobService
            from .server import create_server
            create_server(service, JobService(service.clusters, args.state_dir), config).run(transport="stdio")
            return
        if args.action == "list":
            data = {"ok": True, "clusters": service.list_clusters()}
        elif args.action == "config-get":
            data = {"ok": True, "settings": config.get(args.cluster)}
        elif args.action == "config-set":
            data = {"ok": True, "settings": config.set(args.cluster, json.loads(args.settings))}
        elif args.action in ("check", "info"):
            method = service.cluster_check if args.action == "check" else service.cluster_info
            data = method(args.cluster)
        else:
            from .jobs import JobService
            jobs = JobService(service.clusters, args.state_dir)
            if args.action.startswith("monitor-"):
                from .monitor import MonitorService
                monitor = MonitorService(jobs)
                if args.action in ("monitor-start", "monitor-run"):
                    data = monitor.start(json.loads(args.settings) if args.settings is not None else None,
                                         foreground=args.action == "monitor-run")
                elif args.action == "monitor-stop":
                    data = monitor.stop()
                elif args.action == "monitor-status":
                    data = monitor.status(args.limit, args.offset)
                elif args.action == "monitor-notifications":
                    data = monitor.notifications(args.after_id, args.limit)
                elif args.action == "monitor-unwatch":
                    data = monitor.unwatch(args.run_id)
                else:
                    data = monitor.watch(args.run_ids, args.auto_sync, json.loads(args.sync_options),
                                         json.loads(args.cleanup_policy), args.reset)
            elif args.action == "gaussian-result":
                from .gaussian import GaussianService
                data = GaussianService(jobs).result(args.run_id, args.log_path, args.max_bytes)
            elif args.action == "sync-preview":
                data = jobs.job_sync_preview(args.run_id, args.mode, args.include, args.exclude,
                    args.max_file_bytes, args.max_total_bytes, args.reserve_bytes, args.timeout)
            elif args.action == "sync-start":
                data = jobs.job_sync_start(args.run_id, json.loads(args.options))
            elif args.action == "sync-operation":
                data = jobs.job_sync_operation(args.operation_id)
            elif args.action == "storage-cleanup":
                data = jobs.job_storage_cleanup(args.run_id, args.category, args.older_than_seconds, not args.apply)
            elif args.action == "input-cache-cleanup":
                data = jobs.input_cache_cleanup(args.older_than_seconds, args.max_cache_bytes, not args.apply)
            elif args.action == "remote-cleanup":
                data = jobs.job_remote_cleanup(args.run_id, not args.apply)
            elif args.action.startswith("template-") or args.action == "prepare-generated":
                from .templates import TemplateService
                templates = TemplateService(jobs)
                if args.action == "template-import":
                    data = templates.template_import(args.name,
                        json.loads(Path(args.definition_file).expanduser().read_text()))
                elif args.action == "template-list":
                    data = templates.template_list(args.limit, args.offset)
                elif args.action == "template-get":
                    data = templates.template_get(args.name, args.version)
                elif args.action == "template-plan":
                    data = templates.template_plan(args.name, args.cluster, args.input_dir,
                        json.loads(args.parameters), args.version, args.project_root)
                elif args.action == "template-run":
                    data = templates.template_run(args.plan_id)
                else:
                    data = templates.job_prepare_generated(args.cluster, args.input_dir,
                        json.loads(Path(args.spec_file).expanduser().read_text()), args.output,
                        args.project_root, args.input_file, args.script_name, args.output_exclude,
                        args.input_exclude, args.max_input_bytes)
            elif args.action == "prepare":
                data = jobs.job_prepare(args.cluster, args.input_dir, args.script, args.output,
                    args.output_mode, args.output_exclude, args.input_exclude, args.max_input_bytes,
                    args.project_root, args.input_file)
            elif args.action == "cache-cleanup":
                data = jobs.job_cache_cleanup(args.run_id, args.older_than_seconds, not args.apply)
            elif args.action == "sync":
                data = jobs.job_sync(args.run_id, args.mode, args.include, args.exclude,
                    args.destination, args.layout, args.overwrite, args.checksum, args.compress, args.timeout, args.resume,
                    args.max_file_bytes, args.max_total_bytes, args.reserve_bytes, args.stable_only)
            elif args.action == "history":
                data = jobs.job_list(args.cluster, args.limit, args.offset)
            elif args.action == "get":
                data = jobs.job_get(args.run_id)
            elif args.action == "logs":
                data = jobs.job_logs(args.run_id, args.stream, args.lines)
            else:
                data = getattr(jobs, "job_" + args.action)(args.run_id)
    except (OSError, ValueError, ImportError, sqlite3.Error) as exc:
        data = {"ok": False, "error": getattr(exc, "code", str(exc))}
        if hasattr(exc, "details"):
            data.update(message=str(exc), **exc.details)
        if args.action == "serve":
            print(json.dumps(data, ensure_ascii=False), file=sys.stderr)
            sys.exit(1)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    sys.exit(0 if data["ok"] else 1)
