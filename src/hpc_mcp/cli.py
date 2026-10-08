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
    for action in ("check", "info"):
        sub.add_parser(action).add_argument("cluster")
    sub.add_parser("serve", help="Run MCP over stdio (requires the mcp extra)")
    for action in ("update-check", "update-plan"):
        update = sub.add_parser(action, help="Check upstream or print safe update commands")
        update.add_argument("--force", action="store_true")
        update.add_argument("--max-age-seconds", type=int, default=86400)
        update.add_argument("--timeout", type=int, default=10)
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
    args = parser.parse_args()
    try:
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
        clusters = {} if args.action in ("config-set", "template-import", "template-list", "template-get") \
            and not Path(args.config).exists() \
            else load_config(args.config)
        config = ConfigManager(args.config, clusters)
        service = ClusterService(clusters)
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
            if args.action.startswith("template-") or args.action == "prepare-generated":
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
                    args.destination, args.layout, args.overwrite, args.checksum, args.compress, args.timeout)
            elif args.action == "history":
                data = jobs.job_list(args.cluster, args.limit, args.offset)
            elif args.action == "get":
                data = jobs.job_get(args.run_id)
            elif args.action == "logs":
                data = jobs.job_logs(args.run_id, args.stream, args.lines)
            else:
                data = getattr(jobs, "job_" + args.action)(args.run_id)
    except (OSError, ValueError, ImportError, sqlite3.Error) as exc:
        data = {"ok": False, "error": str(exc)}
        if args.action == "serve":
            print(json.dumps(data, ensure_ascii=False), file=sys.stderr)
            sys.exit(1)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    sys.exit(0 if data["ok"] else 1)
