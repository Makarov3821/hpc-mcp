"""rsync transfers reuse noninteractive OpenSSH options."""

from pathlib import Path
import shlex
import subprocess
import tempfile
import time

from .config import Cluster
from .ssh import CommandResult, ssh_options


class Transfer:
    def run(self, cluster: Cluster, local: Path, remote: str, download=False,
            patterns: list[str] | None = None, mode="filtered", excludes=None,
            checksum=None, compress=None, timeout=None, dry_run=False, resume=False,
            max_file_bytes=None, max_total_bytes=None, progress=None) -> CommandResult:
        args = ["rsync", "-rt", "--protect-args", "--no-links",
                "--prune-empty-dirs", "-e", shlex.join(["ssh", *ssh_options(cluster)])]
        if cluster.transfer_checksum if checksum is None else checksum:
            args.append("--checksum")
        if cluster.transfer_compress if compress is None else compress:
            args.append("--compress")
        if download:
            args.extend(["--exclude=/.hpc-mcp-*/", "--exclude=/.hpc-mcp-*",
                         "--exclude=/.xn02-*/", "--exclude=/.xn02-*"])
            for pattern in excludes or []:
                args.append(f"--exclude={pattern}")
            if mode == "filtered":
                args.append("--include=*/")
                for pattern in patterns or []:
                    args.append(f"--include={pattern}")
                args.append("--exclude=*")
        if dry_run:
            args.extend(["--dry-run", "--itemize-changes", "--out-format=HPC_FILE|%i|%l|%M|%n"])
        elif download and resume:
            args.extend(["--partial-dir=.hpc-mcp-transfer-partial", "--no-whole-file"])
        if download:
            args.append("--exclude=.hpc-mcp-transfer-partial/")
            if not dry_run:
                args.append("--stats")
        if max_file_bytes is not None and not dry_run:
            args.append(f"--max-size={max_file_bytes}")
        target = f"{cluster.ssh_host}:{remote.rstrip('/')}/"
        sources = [target, str(local) + "/"] if download else [str(local) + "/", target]
        args.extend(["--", *sources])
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            try:
                process = subprocess.Popen(args, stdout=out, stderr=err)
                started = time.monotonic()
                code, error = None, None
                try:
                    while process.poll() is None:
                        elapsed = time.monotonic() - started
                        if elapsed > (cluster.transfer_timeout if timeout is None else timeout):
                            error = "transfer_timeout"
                            break
                        current = 0
                        if download and (progress or max_total_bytes):
                            for path in local.rglob("*"):
                                try:
                                    if path.is_file() and not path.is_symlink():
                                        current += path.stat().st_size
                                except FileNotFoundError:
                                    continue
                        if max_total_bytes is not None and current > 2 * max_total_bytes:
                            error = "transfer_size_limit"
                            break
                        if progress:
                            progress({"staged_bytes": current, "elapsed_seconds": round(elapsed, 2)})
                        time.sleep(0.1)
                    if error:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                    code = process.wait()
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
            except subprocess.TimeoutExpired:
                code, error = -1, "transfer_timeout"
            except OSError as exc:
                return CommandResult(-1, stderr=str(exc), error="rsync_unavailable")
            output_limit = cluster.ssh_output_limit if dry_run else 4000
            oversized = dry_run and out.tell() > output_limit
            out.seek(0)
            err.seek(0)
            return CommandResult(code, out.read(output_limit).decode(errors="replace"),
                                 err.read(4000).decode(errors="replace"), "output_limit" if oversized else error)
