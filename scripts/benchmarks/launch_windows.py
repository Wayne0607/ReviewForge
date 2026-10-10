"""Bound a Windows benchmark and all descendants before loading its workload."""

from __future__ import annotations

import argparse
import ctypes as c
import os
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

from benchmark_support import exclusive_file_lock
from launch_isolated import _SCRIPTS, _write_record, check_capacity

_MIB = 1024 * 1024
_MEMORY_FLAGS = 0x200 | 0x2000 | 0x20  # JOB_MEMORY, KILL_ON_JOB_CLOSE, PRIORITY_CLASS
_CPU_FLAGS = 0x1 | 0x4  # ENABLE, HARD_CAP


class BasicLimits(c.Structure):
    _fields_ = [
        ("process_time", c.c_int64),
        ("job_time", c.c_int64),
        ("flags", c.c_uint32),
        ("min_working_set", c.c_size_t),
        ("max_working_set", c.c_size_t),
        ("active_processes", c.c_uint32),
        ("affinity", c.c_size_t),
        ("priority", c.c_uint32),
        ("scheduling", c.c_uint32),
    ]


class ExtendedLimits(c.Structure):
    _fields_ = [
        ("basic", BasicLimits),
        ("io", c.c_uint64 * 6),
        ("process_memory", c.c_size_t),
        ("job_memory", c.c_size_t),
        ("peak_process_memory", c.c_size_t),
        ("peak_job_memory", c.c_size_t),
    ]


class CpuLimits(c.Structure):
    _fields_ = [("flags", c.c_uint32), ("rate", c.c_uint32)]


class MemoryStatus(c.Structure):
    _fields_ = [("length", c.c_uint32), ("load", c.c_uint32), ("values", c.c_uint64 * 7)]


def kernel():
    if os.name != "nt":
        raise RuntimeError("Requires Windows Job Objects; no unbounded fallback")
    api = c.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ([c.c_void_p, c.c_wchar_p], c.c_void_p),
        "OpenJobObjectW": ([c.c_uint32, c.c_int, c.c_wchar_p], c.c_void_p),
        "SetInformationJobObject": ([c.c_void_p, c.c_int, c.c_void_p, c.c_uint32], c.c_int),
        "QueryInformationJobObject": ([c.c_void_p, c.c_int, c.c_void_p, c.c_uint32, c.c_void_p], c.c_int),
        "AssignProcessToJobObject": ([c.c_void_p, c.c_void_p], c.c_int),
        "IsProcessInJob": ([c.c_void_p, c.c_void_p, c.c_void_p], c.c_int),
        "GetCurrentProcess": ([], c.c_void_p),
        "TerminateJobObject": ([c.c_void_p, c.c_uint32], c.c_int),
        "CloseHandle": ([c.c_void_p], c.c_int),
        "GlobalMemoryStatusEx": ([c.c_void_p], c.c_int),
    }
    for name, (args, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes, function.restype = args, result
    return api


def checked(value):
    if not value:
        raise c.WinError(c.get_last_error())
    return value


def query_limits(api, job):
    memory, cpu = ExtendedLimits(), CpuLimits()
    checked(api.QueryInformationJobObject(job, 9, c.byref(memory), c.sizeof(memory), None))
    checked(api.QueryInformationJobObject(job, 15, c.byref(cpu), c.sizeof(cpu), None))
    return memory, cpu


def verify_limits(memory, cpu, memory_bytes: int, cpu_percent: int) -> None:
    if memory.basic.flags & _MEMORY_FLAGS != _MEMORY_FLAGS or not 0 < memory.job_memory <= memory_bytes:
        raise RuntimeError("Job memory, priority or process-tree cleanup limit was not applied")
    if cpu.flags & _CPU_FLAGS != _CPU_FLAGS or not 0 < cpu.rate <= cpu_percent * 100:
        raise RuntimeError("Job CPU hard cap was not applied")


def guarded_workload(name: str, memory_bytes: int, cpu_percent: int, script: str, args: list[str]) -> None:
    api = kernel()
    job = checked(api.OpenJobObjectW(0x1 | 0x4, False, name))  # ASSIGN_PROCESS, QUERY
    try:
        checked(api.AssignProcessToJobObject(job, api.GetCurrentProcess()))
        belongs = c.c_int()
        checked(api.IsProcessInJob(api.GetCurrentProcess(), job, c.byref(belongs)))
        if not belongs.value:
            raise RuntimeError("Workload was not assigned to its resource group")
        verify_limits(*query_limits(api, job), memory_bytes, cpu_percent)
    finally:
        # Only the parent retains a handle: parent death must kill the tree.
        api.CloseHandle(job)
    sys.argv = [script, *args]
    sys.path.insert(0, str(Path(script).parent))
    runpy.run_path(script, run_name="__main__")


def run(args: argparse.Namespace) -> int:
    api = kernel()
    if min(args.memory_mb, args.reserve_mb, args.runtime_seconds) <= 0 or not 0 < args.cpu_percent <= 100:
        raise ValueError("Require finite positive limits and a production/workstation memory reserve")
    if not re.fullmatch(r"[0-9a-f]{40}", args.revision):
        raise ValueError("Record the exact source revision")
    root = Path(args.repo_root).resolve()
    record = Path(args.record).resolve()
    if not root.is_dir() or (root / ".git").exists() or record.exists():
        raise ValueError("Requires an existing isolated source snapshot and a new execution record")
    available = MemoryStatus(length=c.sizeof(MemoryStatus))
    checked(api.GlobalMemoryStatusEx(c.byref(available)))
    check_capacity(
        args.memory_mb, args.reserve_mb, available.values[1], args.disk_reserve_mb, shutil.disk_usage(root).free
    )
    record.parent.mkdir(parents=True, exist_ok=True)
    name = "Local\\ReviewForge-Eval-" + uuid.uuid4().hex
    with exclusive_file_lock(Path(tempfile.gettempdir()) / "reviewforge-eval-windows.lock", blocking=False):
        job = checked(api.CreateJobObjectW(None, name))
        payload = {
            "job": name,
            "source_revision": args.revision,
            "task": args.task,
            "memory_mb": args.memory_mb,
            "cpu_percent": args.cpu_percent,
            "runtime_seconds": args.runtime_seconds,
            "reserve_mb": args.reserve_mb,
            "available_bytes_before_start": available.values[1],
            "status": "running",
            "started_at": datetime.now(UTC).isoformat(),
        }
        _write_record(record, payload)
        returncode = None
        try:
            memory = ExtendedLimits()
            memory.basic.flags, memory.basic.priority = _MEMORY_FLAGS, 0x4000  # BELOW_NORMAL_PRIORITY_CLASS
            memory.job_memory = args.memory_mb * _MIB
            cpu = CpuLimits(flags=_CPU_FLAGS, rate=args.cpu_percent * 100)
            checked(api.SetInformationJobObject(job, 9, c.byref(memory), c.sizeof(memory)))
            checked(api.SetInformationJobObject(job, 15, c.byref(cpu), c.sizeof(cpu)))
            verify_limits(*query_limits(api, job), memory.job_memory, args.cpu_percent)
            environment = os.environ.copy()
            for key in ["GITHUB_TOKEN", "LLM_API_KEY", "LLM_BASE_URL", "REVIEWFORGE_MODEL"]:
                environment.pop(key, None)
            environment.update(
                {
                    "REVIEWFORGE_REPO_ROOT": str(root),
                    "REVIEWFORGE_SOURCE_REVISION": args.revision,
                    "REVIEWFORGE_ENV_FILE": str(Path(args.env_file).resolve()),
                    "REVIEWFORGE_SETTINGS_DIR": str(Path(args.settings_dir).resolve()),
                    "PYTHONPATH": str(root / "backend/src"),
                    "PYTHONIOENCODING": "utf-8",
                    "PYTHONUTF8": "1",
                }
            )
            workload_args = args.workload_args[1:] if args.workload_args[:1] == ["--"] else args.workload_args
            command = [
                args.python,
                str(Path(__file__).resolve()),
                "--guard",
                name,
                str(memory.job_memory),
                str(args.cpu_percent),
                str(root / "scripts/benchmarks" / _SCRIPTS[args.task]),
                *workload_args,
            ]
            with record.with_suffix(".log").open("x", encoding="utf-8") as log:
                try:
                    returncode = subprocess.run(
                        command,
                        cwd=root,
                        env=environment,
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        timeout=args.runtime_seconds,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    ).returncode
                except subprocess.TimeoutExpired:
                    payload["error"] = "runtime-limit"
                    returncode = 124
        finally:
            # Covers redirectors, workload children, timeouts and interrupted launchers.
            if not api.TerminateJobObject(job, 124 if returncode is None else returncode):
                payload["cleanup_error"] = c.get_last_error()
                returncode = 1
            peak = None
            try:
                limits, _ = query_limits(api, job)
                peak = limits.peak_job_memory
            except OSError:
                pass
            finally:
                api.CloseHandle(job)
            payload.update(
                status="completed" if returncode == 0 else "failed",
                returncode=returncode,
                peak_job_memory_bytes=peak,
                finished_at=datetime.now(UTC).isoformat(),
            )
            _write_record(record, payload)
        return returncode


def main() -> None:
    if sys.argv[1:2] == ["--guard"]:
        guarded_workload(sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), sys.argv[5], sys.argv[6:])
        return
    parser = argparse.ArgumentParser()
    for option in ["repo-root", "python", "env-file", "settings-dir", "revision", "record"]:
        parser.add_argument("--" + option, required=True)
    for option in ["memory-mb", "reserve-mb", "disk-reserve-mb", "cpu-percent", "runtime-seconds"]:
        parser.add_argument("--" + option, required=True, type=int)
    parser.add_argument("--task", required=True, choices=tuple(_SCRIPTS))
    parser.add_argument("workload_args", nargs=argparse.REMAINDER)
    raise SystemExit(run(parser.parse_args()))


if __name__ == "__main__":
    main()
