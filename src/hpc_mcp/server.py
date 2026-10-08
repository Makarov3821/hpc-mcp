"""Official MCP Python SDK v2 stdio interface."""

from .service import ClusterService
from .jobs import JobService
from .config import Cluster, ConfigManager
from dataclasses import asdict
from typing import Any
from .scripts import Resources, script_generate as generate_script
from .templates import TemplateService
from .updates import UpdateService


def create_server(service: ClusterService, jobs: JobService, config: ConfigManager | None = None):
    from mcp.server import MCPServer

    server = MCPServer("hpc-mcp", instructions=(
        "At session start, call update_check once and tell the user if an update is available. "
        "An unavailable check means unknown, not up to date. Use update_plan for upgrade commands; "
        "stop this MCP connection before executing them externally, then restart with the same "
        "absolute config and state paths. Never reset local changes or resubmit jobs for an update."))
    templates = TemplateService(jobs)
    updates = UpdateService(jobs.history.root)

    @server.tool()
    def update_check(force: bool = False, max_age_seconds: int = 86400,
                     timeout: int = 10) -> dict[str, Any]:
        """Check official upstream once at session start and tell the user if an update exists.

        HTTPS GitHub check, cached for max_age_seconds (0..604800); timeout 1..60 per request.
        Offline results are unknown. No installation changes. force bypasses cache.
        """
        return updates.check(force, max_age_seconds, timeout)

    @server.tool()
    def update_plan(force: bool = False, max_age_seconds: int = 86400,
                    timeout: int = 10) -> dict[str, Any]:
        """Return pinned fast-forward Git/pip commands for a clean official main checkout.

        Run commands externally in order after stopping the MCP connection; stop on failure.
        Never executes the plan. Restart with the same config/state paths afterwards.
        """
        return updates.plan(force, max_age_seconds, timeout)

    @server.tool()
    def settings_get(cluster: str | None = None) -> dict[str, Any]:
        """Discover every cluster setting and server startup path.

        Without cluster, returns a complete default settings template; otherwise returns
        the effective cluster settings. Startup paths require restarting the server to change.
        """
        settings = asdict(service._cluster(cluster)) if cluster else asdict(
            Cluster("example", "ssh-alias", "lsf", "/shared/jobs"))
        settings.pop("name")
        return {"ok": True, "settings": settings, "state_dir": str(jobs.history.root),
                "script_resources": asdict(Resources()),
                "template_parameter_types": ["string", "integer", "boolean"],
                "updates": {"cached_check": updates.cached(), "check_tool": "update_check",
                            "plan_tool": "update_plan", "max_age_seconds": 86400,
                            "timeout": 10, "force": False},
                "config_path": str(config.path) if config else None}

    @server.tool()
    def script_generate(scheduler: str, spec: dict) -> dict[str, Any]:
        """Preview a single-node/single-task Bash script; no files, execution or submission.

        spec: command argv (required), resources, environment, compute init_scripts,
        relative stdin/stdout/stderr. resources: cpus, queue, memory_mb, memory_scope,
        time_minutes, account/qos (Slurm), lsf_resource_requirement (LSF).
        Slurm memory_scope: job/per_cpu; LSF: lsf_reservation (scope is site-dependent).
        init_scripts are trusted remote absolute paths. Commands/values are shell-quoted.
        """
        return generate_script(scheduler, spec)

    @server.tool()
    def job_prepare_generated(cluster: str, input_dir: str, spec: dict, outputs: list[str],
                              project_root: str | None = None, input_files: list[str] | None = None,
                              script_name: str = "hpc-mcp-job.sh", output_exclude: list[str] | None = None,
                              input_exclude: list[str] | None = None,
                              max_input_bytes: int | None = None) -> dict[str, Any]:
        """Generate and snapshot a script without writing to the original input directory.

        spec follows script_generate. Explicit outputs required; declared stdin is automatically
        added to a provided input_files list. script_name must not collide with an input.
        Returns a prepared run; job_submit(run_id) is the separate execution step.
        """
        return templates.job_prepare_generated(cluster, input_dir, spec, outputs, project_root,
            input_files, script_name, output_exclude, input_exclude, max_input_bytes)

    @server.tool()
    def template_import(name: str, definition: dict) -> dict[str, Any]:
        """Save a validated structured template as a new immutable version in agent state.

        definition: scheduler, spec (script_generate), outputs; optional script_name,
        input_files, input_exclude, output_exclude, max_input_bytes, parameters.
        parameters declares name -> {type: string/integer/boolean, default?, description?}.
        Values use {{name}} placeholders; no evaluation or arbitrary script import.
        Every import creates a new version. Does not submit or modify original inputs.
        """
        return templates.template_import(name, definition)

    @server.tool()
    def template_list(limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """List the latest version of saved templates with pagination."""
        return templates.template_list(limit, offset)

    @server.tool()
    def template_get(name: str, version: int | None = None) -> dict[str, Any]:
        """Read an immutable template version; omitted version selects latest."""
        return templates.template_get(name, version)

    @server.tool()
    def template_plan(name: str, cluster: str, input_dir: str,
                      parameters: dict | None = None, version: int | None = None,
                      project_root: str | None = None) -> dict[str, Any]:
        """Bind a template and prepare one immutable run without upload/submission.

        Returns plan_id (=run_id), rendered script and snapshot manifest. Required parameters
        have no default. Script, input/output paths and parameters are pinned to the saved version.
        Original input directory is not modified. Replanning creates a new independent run.
        """
        return templates.template_plan(name, cluster, input_dir, parameters, version, project_root)

    @server.tool()
    def template_run(plan_id: str) -> dict[str, Any]:
        """Upload and submit an existing template plan using its pinned snapshot and version.

        Repeated execution uses job_submit's duplicate protection and receipt recovery.
        Editing a template or original inputs does not change this plan.
        """
        return templates.template_run(plan_id)

    @server.tool()
    def cluster_configure(cluster: str, settings: dict) -> dict[str, Any]:
        """Create/update a configured cluster using settings_get's settings keys.

        Atomically persists TOML and applies settings immediately. Changing a prepared
        run's destination/environment does not migrate that run. init_scripts execute
        trusted remote initialization files. Credentials stay in OpenSSH configuration.
        """
        if config is None:
            raise ValueError("configuration persistence is unavailable")
        return {"ok": True, "settings": config.set(cluster, settings)}

    @server.tool()
    def cluster_list() -> list[dict]:
        """List locally configured cluster names and connection destinations."""
        return service.list_clusters()

    @server.tool()
    def cluster_check(cluster: str) -> dict[str, Any]:
        """Check SSH, configured environment, scheduler commands and directory access.

        Does not submit jobs or create directories. Shared storage remains unverified.
        """
        return service.cluster_check(cluster)

    @server.tool()
    def cluster_info(cluster: str) -> dict[str, Any]:
        """Query visible LSF queues or Slurm partitions with structured resource information."""
        return service.cluster_info(cluster)

    @server.tool()
    def job_prepare(cluster: str, input_dir: str, script: str,
                    outputs: list[str] | None = None, output_mode: str | None = None,
                    output_exclude: list[str] | None = None, input_exclude: list[str] | None = None,
                    max_input_bytes: int | None = None, project_root: str | None = None,
                    input_files: list[str] | None = None) -> dict[str, Any]:
        """Create a local immutable input snapshot and reviewable submission plan.

        Script is relative to input_dir. Outputs are rsync include patterns.
        Does not upload or execute the script. Rejects symlinks and arrays.
        project_root: project A; downloads stage there and return to input_dir by default.
        Project tasks require explicit filtered outputs. input_files selects exact relative
        files plus the script, instead of the whole directory; include all needed dependencies.
        """
        return jobs.job_prepare(cluster, input_dir, script, outputs, output_mode, output_exclude,
                                input_exclude, max_input_bytes, project_root, input_files)

    @server.tool()
    def job_submit(run_id: str) -> dict[str, Any]:
        """Upload, verify and submit a prepared run. Repeated calls never blindly resubmit.

        Executes the user's batch script on the configured cluster.
        """
        return jobs.job_submit(run_id)

    @server.tool()
    def job_list(cluster: str | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """Read persistent local execution history with pagination."""
        return jobs.job_list(cluster, limit, offset)

    @server.tool()
    def job_get(run_id: str) -> dict[str, Any]:
        """Read a saved plan, file manifests and execution events without querying the cluster."""
        return jobs.job_get(run_id)

    @server.tool()
    def job_status(run_id: str) -> dict[str, Any]:
        """Query the scheduler; unavailable records retain the last known state."""
        return jobs.job_status(run_id)

    @server.tool()
    def job_recover(run_id: str) -> dict[str, Any]:
        """Recover an ambiguous submission using its remote receipt without submitting again."""
        return jobs.job_recover(run_id)

    @server.tool()
    def job_logs(run_id: str, stream: str = "stdout", lines: int = 100) -> dict[str, Any]:
        """Read the last 1..1000 lines of the run's stdout or stderr log."""
        return jobs.job_logs(run_id, stream, lines)

    @server.tool()
    def job_cancel(run_id: str) -> dict[str, Any]:
        """Request cancellation of a confirmed job; query status to confirm the outcome."""
        return jobs.job_cancel(run_id)

    @server.tool()
    def job_cache_cleanup(run_id: str, older_than_seconds: int = 86400,
                          dry_run: bool = True) -> dict[str, Any]:
        """Preview/delete inactive sync attempts for a run, under its operation lock.

        Never removes snapshots, installed results, history or remote data. Use dry_run=false
        to apply. Scheduling is external; this tool does not start a background timer.
        """
        return jobs.job_cache_cleanup(run_id, older_than_seconds, dry_run)

    @server.tool()
    def job_sync(run_id: str, mode: str | None = None, includes: list[str] | None = None,
                 excludes: list[str] | None = None, destination: str | None = None,
                 layout: str | None = None, overwrite: str | None = None,
                 checksum: bool | None = None, compress: bool | None = None,
                 timeout: int | None = None) -> dict[str, Any]:
        """Download outputs using fully configurable rules; null arguments use saved/default settings.

        mode: all/filtered; includes/excludes: relative rsync patterns (excludes win).
        Explicit includes implies filtered unless mode is set. destination: local directory.
        layout: snapshot/direct; overwrite: error/replace/merge. replace archives old data;
        merge overwrites colliding files and retains unmatched files. timeout: 1..86400 seconds.
        Internal receipts and symlinks are always excluded. Running/unknown outputs are partial.
        Project tasks return to input_dir by default; uploaded inputs cannot be overwritten.
        """
        return jobs.job_sync(run_id, mode, includes, excludes, destination, layout, overwrite,
                             checksum, compress, timeout)

    return server
