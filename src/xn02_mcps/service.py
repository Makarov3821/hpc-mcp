"""Cluster environment inspection shared by CLI and MCP."""

from datetime import datetime, timezone
import shlex

from .config import Cluster
from .schedulers import COMMANDS, LSF_QUERY, SLURM_QUERY, parse_lsf, parse_slurm
from .ssh import SSHTransport


class ClusterService:
    def __init__(self, clusters: dict[str, Cluster], transport=None):
        self.clusters = clusters
        self.transport = transport if transport is not None else SSHTransport()

    def list_clusters(self) -> list[dict]:
        return [{"name": c.name, "ssh_host": c.ssh_host, "scheduler": c.scheduler,
                 "work_root": c.work_root} for c in self.clusters.values()]

    def _cluster(self, name: str) -> Cluster:
        if name not in self.clusters:
            raise ValueError(f"unknown cluster: {name}")
        return self.clusters[name]

    def cluster_check(self, name: str) -> dict:
        cluster = self._cluster(name)
        commands = (*COMMANDS[cluster.scheduler], "rsync")
        script = [
            "printf 'hostname\\t%s\\n' \"$(hostname)\"",
            "printf 'user\\t%s\\n' \"$(id -un)\"",
            "printf 'home\\t%s\\n' \"$HOME\"",
            "printf 'shell\\t%s\\n' \"$SHELL\"",
            "printf 'cwd\\t%s\\n' \"$PWD\"",
        ]
        for command in commands:
            script.append(
                f"printf 'command.{command}\\t%s\\n' \"$(command -v {command} || true)\""
            )
        root = shlex.quote(cluster.work_root)
        for label, flag in (("exists", "d"), ("readable", "r"),
                            ("writable", "w"), ("traversable", "x")):
            script.append(
                f"if test -{flag} {root}; then printf 'work_root.{label}\\ttrue\\n'; "
                f"else printf 'work_root.{label}\\tfalse\\n'; fi"
            )
        result = self.transport.run(cluster, "\n".join(script))
        response = {"cluster": name, "scheduler": cluster.scheduler, "ok": False,
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                    "shared_storage": "unverified", "checks": []}
        if not result.ok:
            response["checks"].append({"name": "ssh_environment", "status": "failed",
                                       "detail": result.diagnostic()})
            return response
        values = {}
        for line in result.stdout.splitlines():
            key, separator, value = line.partition("\t")
            if not separator or key in values:
                response["checks"].append({"name": "response", "status": "failed",
                                           "detail": "malformed environment response"})
                return response
            values[key] = value
        expected = {"hostname", "user", "home", "shell", "cwd"}
        expected.update(f"command.{c}" for c in commands)
        expected.update(f"work_root.{key}" for key in
                        ("exists", "readable", "writable", "traversable"))
        if set(values) != expected or not values["hostname"] or not values["user"]:
            response["checks"].append({"name": "response", "status": "failed",
                                       "detail": "incomplete environment response"})
            return response
        checks = response["checks"]
        checks.append({"name": "ssh_environment", "status": "passed"})
        response["environment"] = {key: values[key] for key in
                                   ("hostname", "user", "home", "shell", "cwd")}
        optional = {"rsync", "sacct", "bhist", "bacct"}
        for command in commands:
            available = bool(values[f"command.{command}"])
            checks.append({"name": command,
                           "status": "passed" if available else
                           ("warning" if command in optional else "failed"),
                           "path": values[f"command.{command}"] or None})
        for label in ("exists", "readable", "writable", "traversable"):
            checks.append({"name": f"work_root.{label}",
                           "status": "passed" if values[f"work_root.{label}"] == "true"
                           else "failed", "path": cluster.work_root})
        # A working executable is not proof of scheduler connectivity or permission.
        queues = self.cluster_info(name)
        checks.append({"name": "queue_query", "status": "passed" if queues["ok"] else "failed",
                       "detail": {"queue_count": len(queues.get("queues", []))}
                       if queues["ok"] else queues.get("error")})
        response["ok"] = all(check["status"] != "failed" for check in checks)
        response["warnings"] = ["共享存储尚未通过计算节点验证；目录检查不创建文件。",
                                "队列可见性不等于当前用户具备提交权限。"]
        return response

    def cluster_info(self, name: str) -> dict:
        cluster = self._cluster(name)
        command = LSF_QUERY if cluster.scheduler == "lsf" else SLURM_QUERY
        result = self.transport.run(cluster, command)
        response = {"cluster": name, "scheduler": cluster.scheduler, "ok": False,
                    "queried_at": datetime.now(timezone.utc).isoformat(), "command": command}
        if not result.ok:
            response["error"] = result.diagnostic()
            return response
        try:
            parser = parse_lsf if cluster.scheduler == "lsf" else parse_slurm
            response["queues"] = parser(result.stdout)
        except ValueError as exc:
            response["error"] = {"error": "parse_error", "message": str(exc),
                                  "stdout": result.stdout[:4000]}
            return response
        response["ok"] = True
        response["stderr"] = result.stderr[-4000:]
        response["scope"] = "scheduler-visible queues; submission authorization is unverified"
        return response
