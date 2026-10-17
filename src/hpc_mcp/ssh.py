"""Bounded, noninteractive SSH commands with framed stdout."""

from dataclasses import dataclass
import os
import selectors
import shlex
import signal
import subprocess
import time
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
    @staticmethod
    def _capture(argv, script, timeout, limit):
        """Drain bounded pipes while writing stdin; never spool remote output to disk."""
        buffers = {'stdout': bytearray(), 'stderr': bytearray()}
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
        failure = None
        pending = memoryview(script)
        deadline = time.monotonic() + timeout
        def kill():
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            with selectors.DefaultSelector() as selector:
                for stream, name in ((process.stdout, 'stdout'), (process.stderr, 'stderr')):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, name)
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE, 'stdin')
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        failure = 'timeout'
                        break
                    for key, _ in selector.select(min(remaining, 0.1)):
                        if key.data == 'stdin':
                            try:
                                written = os.write(key.fd, pending[:65536])
                                pending = pending[written:]
                            except BrokenPipeError:
                                pending = pending[:0]
                            except BlockingIOError:
                                continue
                            if not pending:
                                selector.unregister(key.fileobj)
                                key.fileobj.close()
                        else:
                            output = buffers[key.data]
                            try:
                                chunk = os.read(key.fd, min(65536, limit - len(output) + 1))
                            except BlockingIOError:
                                continue
                            if not chunk:
                                selector.unregister(key.fileobj)
                                key.fileobj.close()
                                continue
                            room = limit - len(output)
                            output.extend(chunk[:room])
                            if len(chunk) > room:
                                failure = 'output_limit'
                                break
                    if failure:
                        break
            if failure:
                kill()
            try:
                code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                failure = failure or 'timeout'
                kill()
                code = process.wait()
        finally:
            if process.poll() is None:
                kill()
                process.wait()
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
        return code, buffers['stdout'].decode('utf-8', errors='replace'), \
            buffers['stderr'].decode('utf-8', errors='replace'), failure

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
        try:
            code, stdout, stderr, failure = self._capture(
                argv, script.encode(), cluster.command_timeout, cluster.ssh_output_limit)
        except OSError as exc:
            return CommandResult(-1, stderr=str(exc), error="ssh_unavailable")
        if failure == 'output_limit':
            return CommandResult(code, stderr=stderr[-4000:], error="output_limit")
        start = stdout.find(begin + "\n")
        stop = stdout.find(end + "\n", start + len(begin) + 1)
        if start >= 0 and stop >= 0:
            stdout = stdout[start + len(begin) + 1:stop]
        elif failure is None:
            failure = "remote_initialization_failed" if code else "invalid_response"
        if failure == 'timeout':
            code = -1
        if code == 255 and failure != "timeout":
            failure = "ssh_connection_failed"
        return CommandResult(code, stdout, stderr, failure)
