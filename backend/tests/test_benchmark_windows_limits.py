"""Real Windows kernel smoke tests for bounded benchmark process trees."""

from __future__ import annotations

import ctypes
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def windows_launcher(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts/benchmarks"))
    return importlib.import_module("launch_windows")


@pytest.mark.parametrize("missing", ["memory-flag", "memory-cap", "cpu-flag", "cpu-cap"])
def test_workload_refuses_missing_or_looser_kernel_caps(windows_launcher, missing):
    memory, cpu = windows_launcher.ExtendedLimits(), windows_launcher.CpuLimits()
    memory.basic.flags, memory.job_memory = windows_launcher._MEMORY_FLAGS, 128 * 1024 * 1024
    cpu.flags, cpu.rate = windows_launcher._CPU_FLAGS, 1000
    if missing == "memory-flag":
        memory.basic.flags = 0
    elif missing == "memory-cap":
        memory.job_memory *= 2
    elif missing == "cpu-flag":
        cpu.flags = 0
    else:
        cpu.rate += 1
    with pytest.raises(RuntimeError):
        windows_launcher.verify_limits(memory, cpu, 128 * 1024 * 1024, 10)


def _run_fixture(module, tmp_path, source, runtime=20):
    root = tmp_path / "snapshot"
    scripts = root / "scripts/benchmarks"
    scripts.mkdir(parents=True)
    (scripts / "context_snapshot.py").write_text(source, encoding="utf-8")
    record = root / "execution.json"
    command = [
        sys.executable,
        module.__file__,
        "--repo-root",
        str(root),
        "--python",
        sys.executable,
        "--env-file",
        str(root / ".env"),
        "--settings-dir",
        str(root / "settings"),
        "--revision",
        "a" * 40,
        "--record",
        str(record),
        "--memory-mb",
        "128",
        "--reserve-mb",
        "256",
        "--disk-reserve-mb",
        "64",
        "--cpu-percent",
        "10",
        "--runtime-seconds",
        str(runtime),
        "--task",
        "context",
    ]
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=runtime + 15, creationflags=subprocess.CREATE_NO_WINDOW
    )
    assert record.exists(), result.stderr
    return (
        result,
        json.loads(record.read_text(encoding="utf-8")),
        record.with_suffix(".log").read_text(encoding="utf-8"),
        scripts,
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects require a Windows host")
def test_real_workload_runs_only_after_kernel_membership_and_limit_verification(windows_launcher, tmp_path):
    result, record, log, _ = _run_fixture(windows_launcher, tmp_path, "print('guarded workload ran — 中文')")
    assert result.returncode == 0, log + result.stderr
    assert record["status"] == "completed"
    assert 0 < record["peak_job_memory_bytes"] <= 128 * 1024 * 1024
    assert "guarded workload ran — 中文" in log


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects require a Windows host")
def test_real_job_memory_limit_rejects_allocation_and_records_failure(windows_launcher, tmp_path):
    result, record, log, _ = _run_fixture(windows_launcher, tmp_path, "payload = bytes(512 * 1024 * 1024)")
    assert result.returncode != 0
    assert record["status"] == "failed"
    assert "MemoryError" in log


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects require a Windows host")
def test_timeout_kills_workload_grandchild_as_well_as_python_redirector(windows_launcher, tmp_path):
    source = """import pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
pathlib.Path(__file__).with_suffix('.pid').write_text(str(child.pid))
time.sleep(60)
"""
    result, record, log, scripts = _run_fixture(windows_launcher, tmp_path, source, runtime=3)
    assert result.returncode == 124, log + result.stderr
    assert record["status"] == "failed" and record["error"] == "runtime-limit"
    pid = int((scripts / "context_snapshot.pid").read_text())
    api = windows_launcher.kernel()
    api.OpenProcess.argtypes, api.OpenProcess.restype = (
        [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32],
        ctypes.c_void_p,
    )
    api.WaitForSingleObject.argtypes, api.WaitForSingleObject.restype = (
        [ctypes.c_void_p, ctypes.c_uint32],
        ctypes.c_uint32,
    )
    process = api.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE, read-only observation
    if process:
        try:
            assert api.WaitForSingleObject(process, 5000) == 0
        finally:
            api.CloseHandle(process)
