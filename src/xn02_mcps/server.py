"""Official MCP Python SDK v2 stdio interface."""

from .service import ClusterService
from .jobs import JobService
from .config import Cluster, ConfigManager
from dataclasses import asdict


def create_server(service: ClusterService, jobs: JobService, config: ConfigManager | None = None):
    from mcp.server import MCPServer

    server = MCPServer("xn02-clusters")

    @server.tool()
    def settings_get(cluster: str | None = None) -> dict:
        """Discover every cluster setting and server startup path.

        Without cluster, returns a complete default settings template; otherwise returns
        the effective cluster settings. Startup paths require restarting the server to change.
        """
        settings = asdict(service._cluster(cluster)) if cluster else asdict(
            Cluster("example", "ssh-alias", "lsf", "/shared/jobs"))
        settings.pop("name")
        return {"ok": True, "settings": settings, "state_dir": str(jobs.history.root),
                "config_path": str(config.path) if config else None}

    @server.tool()
    def cluster_configure(cluster: str, settings: dict) -> dict:
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
    def cluster_check(cluster: str) -> dict:
        """Check SSH, configured environment, scheduler commands and directory access.

        Does not submit jobs or create directories. Shared storage remains unverified.
        """
        return service.cluster_check(cluster)

    @server.tool()
    def cluster_info(cluster: str) -> dict:
        """Query visible LSF queues or Slurm partitions with structured resource information."""
        return service.cluster_info(cluster)

    @server.tool()
    def job_prepare(cluster: str, input_dir: str, script: str,
                    outputs: list[str] | None = None, output_mode: str | None = None,
                    output_exclude: list[str] | None = None, input_exclude: list[str] | None = None,
                    max_input_bytes: int | None = None) -> dict:
        """Create a local immutable input snapshot and reviewable submission plan.

        Script is relative to input_dir. Outputs are rsync include patterns.
        Does not upload or execute the script. Rejects symlinks and arrays.
        """
        return jobs.job_prepare(cluster, input_dir, script, outputs, output_mode, output_exclude,
                                input_exclude, max_input_bytes)

    @server.tool()
    def job_submit(run_id: str) -> dict:
        """Upload, verify and submit a prepared run. Repeated calls never blindly resubmit.

        Executes the user's batch script on the configured cluster.
        """
        return jobs.job_submit(run_id)

    @server.tool()
    def job_list(cluster: str | None = None, limit: int = 50, offset: int = 0) -> dict:
        """Read persistent local execution history with pagination."""
        return jobs.job_list(cluster, limit, offset)

    @server.tool()
    def job_get(run_id: str) -> dict:
        """Read a saved plan, file manifests and execution events without querying the cluster."""
        return jobs.job_get(run_id)

    @server.tool()
    def job_status(run_id: str) -> dict:
        """Query the scheduler; unavailable records retain the last known state."""
        return jobs.job_status(run_id)

    @server.tool()
    def job_recover(run_id: str) -> dict:
        """Recover an ambiguous submission using its remote receipt without submitting again."""
        return jobs.job_recover(run_id)

    @server.tool()
    def job_logs(run_id: str, stream: str = "stdout", lines: int = 100) -> dict:
        """Read the last 1..1000 lines of the run's stdout or stderr log."""
        return jobs.job_logs(run_id, stream, lines)

    @server.tool()
    def job_cancel(run_id: str) -> dict:
        """Request cancellation of a confirmed job; query status to confirm the outcome."""
        return jobs.job_cancel(run_id)

    @server.tool()
    def job_sync(run_id: str, mode: str | None = None, includes: list[str] | None = None,
                 excludes: list[str] | None = None, destination: str | None = None,
                 layout: str | None = None, overwrite: str | None = None,
                 checksum: bool | None = None, compress: bool | None = None,
                 timeout: int | None = None) -> dict:
        """Download outputs using fully configurable rules; null arguments use saved/default settings.

        mode: all/filtered; includes/excludes: relative rsync patterns (excludes win).
        Explicit includes implies filtered unless mode is set. destination: local directory.
        layout: snapshot/direct; overwrite: error/replace/merge. replace archives old data;
        merge overwrites colliding files and retains unmatched files. timeout: 1..86400 seconds.
        Internal receipts and symlinks are always excluded. Running/unknown outputs are partial.
        """
        return jobs.job_sync(run_id, mode, includes, excludes, destination, layout, overwrite,
                             checksum, compress, timeout)

    return server
