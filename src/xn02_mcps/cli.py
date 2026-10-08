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
    parser.add_argument("--config", default=os.environ.get("XN02_CONFIG", "clusters.toml"))
    parser.add_argument("--state-dir", default=os.environ.get("XN02_STATE", ".xn02"))
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("list")
    for action in ("check", "info"):
        sub.add_parser(action).add_argument("cluster")
    sub.add_parser("serve", help="Run MCP over stdio (requires the mcp extra)")
    prepare = sub.add_parser("prepare", help="Snapshot an input directory without executing it")
    prepare.add_argument("cluster")
    prepare.add_argument("input_dir")
    prepare.add_argument("script", help="Script path relative to input_dir")
    prepare.add_argument("--output", action="append", help="Repeatable rsync output include pattern")
    prepare.add_argument("--output-mode", choices=("all", "filtered"))
    prepare.add_argument("--output-exclude", action="append")
    prepare.add_argument("--input-exclude", action="append")
    prepare.add_argument("--max-input-bytes", type=int)
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
        clusters = {} if args.action == "config-set" and not Path(args.config).exists() \
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
            if args.action == "prepare":
                data = jobs.job_prepare(args.cluster, args.input_dir, args.script, args.output,
                    args.output_mode, args.output_exclude, args.input_exclude, args.max_input_bytes)
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
