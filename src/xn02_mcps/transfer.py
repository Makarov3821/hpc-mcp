"""rsync transfers reuse noninteractive OpenSSH options."""

from pathlib import Path
import shlex
import subprocess
import tempfile

from .config import Cluster
from .ssh import CommandResult, ssh_options


class Transfer:
    def run(self, cluster: Cluster, local: Path, remote: str, download=False,
            patterns: list[str] | None = None, mode="filtered", excludes=None,
            checksum=None, compress=None, timeout=None) -> CommandResult:
        args = ["rsync", "-rt", "--protect-args", "--no-links",
                "--prune-empty-dirs", "-e", shlex.join(["ssh", *ssh_options(cluster)])]
        if cluster.transfer_checksum if checksum is None else checksum:
            args.append("--checksum")
        if cluster.transfer_compress if compress is None else compress:
            args.append("--compress")
        if download:
            args.extend(["--exclude=/.xn02-*/", "--exclude=/.xn02-*"])
            for pattern in excludes or []:
                args.append(f"--exclude={pattern}")
            if mode == "filtered":
                args.append("--include=*/")
                for pattern in patterns or []:
                    args.append(f"--include={pattern}")
                args.append("--exclude=*")
        target = f"{cluster.ssh_host}:{remote.rstrip('/')}/"
        sources = [target, str(local) + "/"] if download else [str(local) + "/", target]
        args.extend(["--", *sources])
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            try:
                result = subprocess.run(args, stdout=out, stderr=err,
                                        timeout=cluster.transfer_timeout if timeout is None else timeout,
                                        check=False)
                code, error = result.returncode, None
            except subprocess.TimeoutExpired:
                code, error = -1, "transfer_timeout"
            except OSError as exc:
                return CommandResult(-1, stderr=str(exc), error="rsync_unavailable")
            out.seek(0)
            err.seek(0)
            return CommandResult(code, out.read(4000).decode(errors="replace"),
                                 err.read(4000).decode(errors="replace"), error)
