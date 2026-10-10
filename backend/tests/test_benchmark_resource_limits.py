"""Production-capacity and kernel-limit checks for the benchmark launcher."""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts/benchmarks/launch_isolated.py"
_SPEC = importlib.util.spec_from_file_location("benchmark_launcher", _SCRIPT)
launcher = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(launcher)
_MIB = 1024 * 1024


def _args(tmp_path, **overrides):
    values = dict(
        repo_root=str(tmp_path),
        python=str(tmp_path / "python"),
        env_file=str(tmp_path / "credentials.env"),
        settings_dir=str(tmp_path / "settings"),
        revision="a" * 40,
        memory_mb=256,
        reserve_mb=512,
        disk_reserve_mb=1024,
        cpu_percent=25,
        runtime_seconds=120,
        task="context",
        workload_args=["--", "--repo", "keycloak/keycloak", "--pr", "36880"],
    )
    values.update(overrides)
    return argparse.Namespace(**values)


def test_capacity_reserves_production_memory_instead_of_filling_available_space():
    launcher.check_capacity(256, 512, 768 * _MIB, 1024, 1024 * _MIB)
    with pytest.raises(RuntimeError, match="available memory"):
        launcher.check_capacity(256, 512, 767 * _MIB, 1024, 1024 * _MIB)
    with pytest.raises(RuntimeError, match="disk space"):
        launcher.check_capacity(256, 512, 768 * _MIB, 1024, 1023 * _MIB)


@pytest.mark.parametrize(
    "kwargs", [dict(memory_mb=0), dict(cpu_percent=0), dict(cpu_percent=101), dict(runtime_seconds=0)]
)
def test_unbounded_or_invalid_resource_requests_are_rejected(tmp_path, kwargs):
    with pytest.raises(ValueError):
        launcher.build_command(_args(tmp_path, **kwargs), "reviewforge-eval-test")


def test_workload_starts_through_kernel_limit_guard_and_no_secret_values_are_passed(tmp_path):
    command = launcher.build_command(_args(tmp_path), "reviewforge-eval-test")
    assert "--wait" in command and "--scope" not in command
    assert "--property=MemoryMax=256M" in command
    assert "--property=MemorySwapMax=0" in command
    assert "--property=CPUQuota=25%" in command
    assert "--property=RuntimeMaxSec=120s" in command
    assert "--property=KillMode=control-group" in command
    assert "--property=Restart=no" in command
    guard_index = command.index("--verify-and-exec")
    assert command[guard_index + 1 : guard_index + 3] == [str(256 * _MIB), "25"]
    assert command[guard_index + 4].endswith("context_snapshot.py")
    assert command[-4:] == ["--repo", "keycloak/keycloak", "--pr", "36880"]
    assert not any(value.startswith(("--setenv=LLM_API_KEY=", "--setenv=GITHUB_TOKEN=")) for value in command)


@pytest.mark.parametrize(
    "memory,swap,cpu",
    [
        ("max", "0", "25000 100000"),
        (str(257 * _MIB), "0", "25000 100000"),
        (str(256 * _MIB), "max", "25000 100000"),
        (str(256 * _MIB), "0", "max 100000"),
        (str(256 * _MIB), "0", "26000 100000"),
    ],
)
def test_missing_or_looser_kernel_limits_refuse_workload_execution(tmp_path, memory, swap, cpu):
    (tmp_path / "memory.max").write_text(memory)
    (tmp_path / "memory.swap.max").write_text(swap)
    (tmp_path / "cpu.max").write_text(cpu)
    with pytest.raises(RuntimeError):
        launcher.verify_cgroup_limits(tmp_path, 256 * _MIB, 25)


def test_equal_or_stricter_kernel_limits_are_accepted(tmp_path):
    (tmp_path / "memory.max").write_text(str(128 * _MIB))
    (tmp_path / "memory.swap.max").write_text("0")
    (tmp_path / "cpu.max").write_text("10000 100000")
    launcher.verify_cgroup_limits(tmp_path, 256 * _MIB, 25)


@pytest.mark.skipif(sys.platform == "win32", reason="Linux flock protects the shared evaluation host")
def test_second_process_is_rejected_until_host_lock_is_released(tmp_path):
    script = """
import importlib.util
import sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('launcher', sys.argv[1])
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)
try:
    with launcher.exclusive_host_lock(Path(sys.argv[2])):
        pass
except RuntimeError:
    raise SystemExit(23)
"""
    command = [sys.executable, "-c", script, str(_SCRIPT), str(tmp_path / "host.lock")]
    with launcher.exclusive_host_lock(tmp_path / "host.lock"):
        assert subprocess.run(command, capture_output=True, timeout=10).returncode == 23
    assert subprocess.run(command, capture_output=True, timeout=10).returncode == 0


@pytest.mark.parametrize("failure", [SystemExit(143), OSError("unable to launch systemd-run")])
def test_interrupted_launch_stops_only_its_unit_and_persists_failure(tmp_path, monkeypatch, failure):
    calls = []
    record = tmp_path / "execution.json"

    def run(command, **kwargs):
        calls.append(command)
        if command == ["systemd-run"]:
            assert json.loads(record.read_text())["status"] == "running"
            raise failure
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(launcher.subprocess, "run", run)
    with pytest.raises(type(failure)):
        launcher.execute_unit(["systemd-run"], "reviewforge-eval-test", record, {"status": "running"})
    assert calls == [["systemd-run"], ["systemctl", "stop", "reviewforge-eval-test"]]
    payload = json.loads(record.read_text())
    assert payload["status"] == "failed"
    assert payload["returncode"] is None
    assert payload["finished_at"]


def test_memory_or_runtime_termination_is_not_recorded_as_success(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher.subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 137))
    record = tmp_path / "execution.json"
    assert launcher.execute_unit(["systemd-run"], "reviewforge-eval-test", record, {"status": "running"}) == 137
    payload = json.loads(record.read_text())
    assert payload["status"] == "failed" and payload["returncode"] == 137
