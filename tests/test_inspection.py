from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from hpc_mcp.config import Cluster, load_config
from hpc_mcp.schedulers import parse_lsf, parse_slurm
from hpc_mcp.service import ClusterService
from hpc_mcp.ssh import CommandResult, SSHTransport

FIXTURES = Path(__file__).parent / "fixtures"


class ParserTests(unittest.TestCase):
    def test_lsf_extended_header_from_cluster(self):
        queues = parse_lsf((FIXTURES / "bqueues_extended.txt").read_text())
        self.assertEqual(len(queues), 13)
        by_name = {q["name"]: q for q in queues}
        self.assertEqual(by_name["AMD9965"]["running_slots"], 1536)
        self.assertEqual(by_name["xppn3"]["per_host_slot_limit"], 28)
        self.assertEqual(by_name["RTX3070"]["extra_fields"], {"RSV": "0", "PJOBS": "892"})

    def test_lsf_column_order_and_inserted_extension(self):
        text = (FIXTURES / "bqueues_extended.txt").read_text()
        rows = [line.split() for line in text.splitlines()]
        order = [2, 0, 11, 10, 9, 8, 7, 6, 5, 4, 3, 1, 12]
        reordered = "\n".join(" ".join(row[i] for i in order) for row in rows)
        self.assertEqual(parse_lsf(reordered), parse_lsf(text))

    def test_lsf_rejects_missing_duplicate_or_misaligned_columns(self):
        rows = [line.split() for line in (FIXTURES / "bqueues_extended.txt").read_text().splitlines()]
        malformed = [
            "\n".join(" ".join(row[:9] + row[10:]) for row in rows),
            " ".join(rows[0] + ["RUN"]),
            " ".join(rows[0]) + "\n" + " ".join(rows[1][:-1]),
        ]
        for text in malformed:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_lsf(text)

    def test_lsf_slots_and_fractional_limits(self):
        queues = parse_lsf((FIXTURES / "bqueues.txt").read_text())
        self.assertEqual(queues[0]["running_slots"], 56)
        self.assertIsNone(queues[0]["max_slots"])
        self.assertEqual(queues[1]["per_processor_slot_limit"], 0.5)
        self.assertEqual(queues[1]["state"], "Closed:Inact")

    def test_slurm_groups_do_not_discard_heterogeneous_resources(self):
        queues = parse_slurm((FIXTURES / "sinfo.txt").read_text())
        self.assertEqual(len(queues), 2)
        self.assertTrue(queues[0]["default"])
        self.assertEqual(queues[0]["node_count"], 3)
        self.assertEqual([g["cpus_per_node"] for g in queues[0]["node_groups"]], [32, 64])
        self.assertEqual(queues[1]["node_groups"][0]["gres"], "gpu:a100:4")

    def test_malformed_output_is_not_an_empty_cluster(self):
        for parser, output in [(parse_lsf, ""), (parse_lsf, "permission denied"),
                               (parse_slurm, "banner"), (parse_slurm, "x|up|1|bad|idle|1|2|-|n")]:
            with self.subTest(output=output), self.assertRaises(ValueError):
                parser(output)
        self.assertEqual(parse_slurm(""), [])
        header = (FIXTURES / "bqueues.txt").read_text().splitlines()[0]
        self.assertEqual(parse_lsf(header), [])

    def test_slurm_empty_partition_has_unknown_node_resources(self):
        queues = parse_slurm("empty|up|INFINITE|0|n/a|N/A|N/A|(null)|\n")
        self.assertEqual(queues[0]["node_count"], 0)
        self.assertIsNone(queues[0]["node_groups"][0]["cpus_per_node"])


class ConfigTests(unittest.TestCase):
    def test_example_loads(self):
        clusters = load_config("clusters.example.toml")
        self.assertEqual(set(clusters), {"lsf_example", "slurm_example"})

    def test_invalid_values(self):
        base = dict(name="test", ssh_host="cluster", scheduler="lsf", work_root="/shared/jobs")
        for changes in [{"ssh_host": "-oProxyCommand=bad"}, {"scheduler": "pbs"},
                        {"work_root": "~/jobs"}, {"command_timeout": True},
                        {"init_scripts": "/etc/profile"}, {"init_scripts": ["relative"]}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                Cluster(**(base | changes))

    def test_unknown_fields_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.toml"
            path.write_text('[clusters.a]\nssh_host="a"\nscheduler="lsf"\n'
                            'work_root="/jobs"\npassword="secret"\n')
            with self.assertRaises(ValueError):
                load_config(path)


class FakeTransport:
    def __init__(self, results):
        self.results = iter(results)
        self.commands = []

    def run(self, cluster, command):
        self.commands.append(command)
        return next(self.results)


def environment_output(missing=()):
    fields = {"hostname": "login", "user": "scientist", "home": "/home/scientist",
              "shell": "/bin/bash", "cwd": "/home/scientist"}
    for command in ("sbatch", "squeue", "sinfo", "scancel", "sacct", "rsync"):
        fields[f"command.{command}"] = "" if command in missing else "/bin/" + command
    for key in ("exists", "readable", "writable", "traversable"):
        fields[f"work_root.{key}"] = "true"
    return "\n".join(f"{key}\t{value}" for key, value in fields.items())


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.cluster = Cluster("a", "alias", "slurm", "/shared/jobs")

    def test_check_warns_on_optional_command_and_queries_scheduler(self):
        transport = FakeTransport([
            CommandResult(0, environment_output(missing=("sacct",))),
            CommandResult(0, (FIXTURES / "sinfo.txt").read_text()),
        ])
        result = ClusterService({"a": self.cluster}, transport).cluster_check("a")
        self.assertTrue(result["ok"])
        self.assertEqual(result["shared_storage"], "unverified")
        self.assertEqual(next(c for c in result["checks"] if c["name"] == "sacct")["status"],
                         "warning")
        self.assertEqual(len(transport.commands), 2)

    def test_failed_query_fails_readiness(self):
        transport = FakeTransport([CommandResult(0, environment_output()),
                                   CommandResult(1, stderr="access denied")])
        result = ClusterService({"a": self.cluster}, transport).cluster_check("a")
        self.assertFalse(result["ok"])
        self.assertEqual(result["checks"][-1]["status"], "failed")

    def test_ssh_failure_is_not_missing_remote_commands(self):
        transport = FakeTransport([CommandResult(255, stderr="Permission denied",
                                                error="ssh_connection_failed")])
        result = ClusterService({"a": self.cluster}, transport).cluster_check("a")
        self.assertFalse(result["ok"])
        self.assertNotIn("environment", result)
        self.assertEqual(len(transport.commands), 1)

    def test_malformed_probe_fails(self):
        service = ClusterService({"a": self.cluster}, FakeTransport([CommandResult(0, "junk")]))
        self.assertFalse(service.cluster_check("a")["ok"])

    def test_parse_error_is_explicit(self):
        service = ClusterService({"a": self.cluster}, FakeTransport([CommandResult(0, "junk")]))
        result = service.cluster_info("a")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["error"], "parse_error")
        self.assertNotIn("queues", result)

    def test_unknown_cluster_rejected(self):
        with self.assertRaises(ValueError):
            ClusterService({"a": self.cluster}).cluster_info("unknown")

    def test_lsf_check_accepts_extended_queue_output(self):
        cluster = Cluster("a", "alias", "lsf", "/shared/jobs")
        probe = environment_output()
        for old, new in [("sbatch", "bsub"), ("squeue", "bjobs"),
                         ("sinfo", "bqueues"), ("scancel", "bkill"), ("sacct", "bhist")]:
            probe = probe.replace(old, new)
        probe += "\ncommand.bacct\t/bin/bacct"
        transport = FakeTransport([CommandResult(0, probe), CommandResult(
            0, (FIXTURES / "bqueues_extended.txt").read_text()
        )])
        result = ClusterService({"a": cluster}, transport).cluster_check("a")
        self.assertTrue(result["ok"])
        self.assertEqual(result["checks"][-1]["detail"]["queue_count"], 13)


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.cluster = Cluster("a", "alias", "lsf", "/jobs")

    def test_real_shell_framing_and_remote_exit_status(self):
        real_run = subprocess.run
        captured = []

        def local_shell(argv, **kwargs):
            captured.append(argv)
            # Exercise the actual framed remote shell without contacting a server.
            script = b"printf 'login banner\\n'\n" + kwargs.pop("input")
            return real_run(["bash", "-s"], input=script, **kwargs)

        with patch("hpc_mcp.ssh.subprocess.run", side_effect=local_shell):
            result = SSHTransport().run(self.cluster, "printf 'payload\\n'; exit 7")
        self.assertEqual(result.stdout, "payload\n")
        self.assertEqual(result.returncode, 7)
        self.assertIsNone(result.error)
        self.assertIn("StrictHostKeyChecking=yes", captured[0])
        self.assertIn("BatchMode=yes", captured[0])
        self.assertEqual(captured[0][-2:], ["alias", "bash -s"])

    def test_initialization_failure(self):
        real_run = subprocess.run
        cluster = Cluster("a", "alias", "lsf", "/jobs", ("/nonexistent-hpc-mcp-init",))
        with patch("hpc_mcp.ssh.subprocess.run",
                   side_effect=lambda argv, **kw: real_run(["bash", "-s"], **kw)):
            result = SSHTransport().run(cluster, "printf should-not-run")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "remote_initialization_failed")

    def test_timeout_and_unavailable_ssh(self):
        for exception, expected in [(subprocess.TimeoutExpired("ssh", 1), "timeout"),
                                    (FileNotFoundError("ssh"), "ssh_unavailable")]:
            with self.subTest(expected=expected), patch("hpc_mcp.ssh.subprocess.run",
                                                      side_effect=exception):
                self.assertEqual(SSHTransport().run(self.cluster, "true").error, expected)

    def test_large_output_is_not_parsed(self):
        def oversized(argv, **kwargs):
            kwargs["stdout"].write(b"x" * 1_048_577)
            return subprocess.CompletedProcess(argv, 0)

        with patch("hpc_mcp.ssh.subprocess.run", side_effect=oversized):
            result = SSHTransport().run(self.cluster, "true")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "output_limit")
        self.assertEqual(result.stdout, "")

    def test_path_with_shell_metacharacters_is_literal(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "jobs $(touch injected) 'quoted'"
            directory.mkdir()
            cluster = Cluster("a", "alias", "slurm", str(directory))
            captured = []

            class LocalTransport:
                def run(self, cluster, script):
                    captured.append(script)
                    process = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
                    return CommandResult(process.returncode, process.stdout, process.stderr)

            # Missing scheduler commands may fail readiness; the directory probe must remain literal.
            result = ClusterService({"a": cluster}, LocalTransport()).cluster_check("a")
            checks = {c["name"]: c for c in result["checks"]}
            self.assertEqual(checks["work_root.exists"]["status"], "passed")
            self.assertFalse(Path("injected").exists())


if __name__ == "__main__":
    unittest.main()
