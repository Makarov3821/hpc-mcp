"""Bounded, noninteractive SSH commands with framed stdout."""

from dataclasses import dataclass
import shlex
import subprocess
import tempfile
import uuid

from .config import Cluster


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and self.error is None

    def diagnostic(self) -> dict:
        return {"returncode": self.returncode, "error": self.error,
                "stderr": self.stderr[-4000:], "stdout": self.stdout[-4000:]}


def ssh_options(cluster: Cluster) -> list[str]:
    return [
        "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
        "-o", "ConnectionAttempts=1", "-o", f"ConnectTimeout={cluster.connect_timeout}",
        "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
    ]


class SSHTransport:
    def run(self, cluster: Cluster, command: str) -> CommandResult:
        token = "HPC_MCP_" + uuid.uuid4().hex
        begin, end = token + "_BEGIN", token + "_END"
        setup = "\n".join(f". {shlex.quote(p)}" for p in cluster.init_scripts)
        script = (
            f"set -e\n{setup}\nexport LC_ALL=C\nset +e\n"
            f"printf '%s\\n' {begin}\n( set -e\n{command}\n)\n"
            f"hpc_mcp_rc=$?\nprintf '%s\\n' {end}\nexit \"$hpc_mcp_rc\"\n"
        )
        argv = ["ssh", "-T", *ssh_options(cluster), cluster.ssh_host, "bash -s"]
        # File-backed capture prevents large output from being buffered in memory.
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            failure = None
            try:
                process = subprocess.run(
                    argv, input=script.encode(), stdout=out, stderr=err,
                    timeout=cluster.command_timeout, check=False,
                )
                code = process.returncode
            except subprocess.TimeoutExpired:
                code, failure = -1, "timeout"
            except OSError as exc:
                return CommandResult(-1, stderr=str(exc), error="ssh_unavailable")
            oversized = out.tell() > cluster.ssh_output_limit or err.tell() > cluster.ssh_output_limit
            out.seek(0)
            err.seek(0)
            stdout = out.read(cluster.ssh_output_limit).decode("utf-8", errors="replace")
            stderr = err.read(cluster.ssh_output_limit).decode("utf-8", errors="replace")
        if oversized:
            return CommandResult(code, stderr=stderr[-4000:], error="output_limit")
        start = stdout.find(begin + "\n")
        stop = stdout.find(end + "\n", start + len(begin) + 1)
        if start >= 0 and stop >= 0:
            stdout = stdout[start + len(begin) + 1:stop]
        elif failure is None:
            failure = "remote_initialization_failed" if code else "invalid_response"
        if code == 255 and failure != "timeout":
            failure = "ssh_connection_failed"
        return CommandResult(code, stdout, stderr, failure)
