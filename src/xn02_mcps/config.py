"""Validated local configuration; SSH credentials stay in OpenSSH configuration."""

from dataclasses import asdict, dataclass, field
from pathlib import Path
import re
import tomllib
import json
import os
import tempfile
import fcntl


@dataclass(frozen=True)
class Cluster:
    name: str
    ssh_host: str
    scheduler: str
    work_root: str
    init_scripts: tuple[str, ...] = ()
    connect_timeout: int = 10
    command_timeout: int = 30
    transfer_timeout: int = 300
    output_mode: str = "all"
    output_include: list[str] = field(default_factory=lambda: ["**"])
    output_exclude: list[str] = field(default_factory=list)
    sync_layout: str = "snapshot"
    sync_overwrite: str = "error"
    transfer_checksum: bool = True
    transfer_compress: bool = False
    max_input_bytes: int = 1024 ** 3
    input_exclude: list[str] = field(default_factory=lambda: [
        ".git", ".venv", "__pycache__", ".aws", ".ssh", ".codex", ".agents", ".xn02",
        "clusters.toml",
    ])
    ssh_output_limit: int = 1024 ** 2

    def __post_init__(self):
        if not isinstance(self.ssh_host, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.@:-]*", self.ssh_host
        ):
            raise ValueError("ssh_host must be a hostname or SSH alias, never an option")
        if self.scheduler not in ("lsf", "slurm"):
            raise ValueError("scheduler must be lsf or slurm")
        if not isinstance(self.init_scripts, (list, tuple)):
            raise ValueError("init_scripts must be an array of remote absolute paths")
        for path in (self.work_root, *self.init_scripts):
            if not isinstance(path, str) or not path.startswith("/") or any(
                c in path for c in "\x00\r\n"
            ):
                raise ValueError("remote paths must be absolute, without NUL or newlines")
        for value in (self.connect_timeout, self.command_timeout):
            if type(value) is not int or not 1 <= value <= 300:
                raise ValueError("timeouts must be integers between 1 and 300 seconds")
        if type(self.transfer_timeout) is not int or not 1 <= self.transfer_timeout <= 86400:
            raise ValueError("transfer_timeout must be an integer between 1 and 86400 seconds")
        if self.output_mode not in ("all", "filtered"):
            raise ValueError("output_mode must be all or filtered")
        if self.sync_layout not in ("snapshot", "direct"):
            raise ValueError("sync_layout must be snapshot or direct")
        if self.sync_overwrite not in ("error", "replace", "merge"):
            raise ValueError("sync_overwrite must be error, replace or merge")
        for flag in (self.transfer_checksum, self.transfer_compress):
            if type(flag) is not bool:
                raise ValueError("transfer flags must be booleans")
        for number in (self.max_input_bytes, self.ssh_output_limit):
            if type(number) is not int or number < 1:
                raise ValueError("byte limits must be positive integers")
        for patterns in (self.output_include, self.output_exclude, self.input_exclude):
            validate_patterns(patterns)


def validate_patterns(patterns):
    if not isinstance(patterns, list) or any(
        not isinstance(p, str) or not p or p.startswith("/") or ".." in p.split("/")
        or any(c in p for c in "\x00\r\n\\") for p in patterns
    ):
        raise ValueError("patterns must be a list of relative strings without '..' or control characters")


class ConfigManager:
    """Atomically persist complete validated cluster settings, shared with running services."""

    def __init__(self, path, clusters):
        self.path = Path(path).expanduser().resolve()
        self.clusters = clusters

    def get(self, name: str) -> dict:
        if name not in self.clusters:
            raise ValueError(f"unknown cluster: {name}")
        settings = asdict(self.clusters[name])
        settings.pop("name")
        return settings

    def set(self, name: str, settings: dict) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise ValueError("cluster name must contain letters, digits, '.', '_' or '-'")
        if not isinstance(settings, dict) or "name" in settings:
            raise ValueError("settings must be an object without the name field")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(self.path.suffix + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            current = load_config(self.path) if self.path.exists() else {}
            values = asdict(current[name]) if name in current else {}
            values.pop("name", None)
            values.update(settings)
            try:
                current[name] = Cluster(name=name, **values)
            except TypeError as exc:
                raise ValueError(str(exc)) from exc
            lines = []
            for key, cluster in current.items():
                lines.append(f"[clusters.{json.dumps(key)}]")
                for option, value in asdict(cluster).items():
                    if option == "name":
                        continue
                    lines.append(f"{option} = {json.dumps(value, ensure_ascii=False)}")
                lines.append("")
            # JSON scalars/arrays used here are also valid TOML values.
            descriptor, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".clusters-")
            try:
                with os.fdopen(descriptor, "w") as file:
                    file.write("\n".join(lines))
                    file.flush()
                    os.fsync(file.fileno())
                load_config(temporary)
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            self.clusters.clear()
            self.clusters.update(current)
        return self.get(name)


def load_config(path: str | Path) -> dict[str, Cluster]:
    with Path(path).open("rb") as file:
        data = tomllib.load(file)
    if set(data) != {"clusters"} or not isinstance(data["clusters"], dict):
        raise ValueError("configuration must contain a [clusters.<name>] table")
    clusters = {}
    for name, values in data["clusters"].items():
        if not isinstance(values, dict):
            raise ValueError(f"cluster {name} must be a table")
        try:
            clusters[name] = Cluster(name=name, **values)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid cluster {name}: {exc}") from exc
    return clusters
