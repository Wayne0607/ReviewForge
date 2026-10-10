"""Run one benchmark in a verified, bounded systemd service on a shared host."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

_SCRIPTS = {"runner": "martian_runner.py", "context": "context_snapshot.py", "judge": "martian_judge.py"}
_MIB = 1024 * 1024


def check_capacity(memory_mb: int, reserve_mb: int, available_bytes: int, disk_reserve_mb: int, disk_free: int) -> None:
    if min(memory_mb, reserve_mb, disk_reserve_mb) <= 0:
        raise ValueError("Memory limits and production reserves must be positive")
    if (memory_mb + reserve_mb) * _MIB > available_bytes:
        raise RuntimeError("Insufficient available memory after reserving production capacity")
    if disk_free < disk_reserve_mb * _MIB:
        raise RuntimeError("Insufficient free disk space after reserving production capacity")


def verify_cgroup_limits(root: Path, memory_bytes: int, cpu_percent: int) -> None:
    """Refuse to import the workload unless the kernel exposes finite limits."""
    memory = (root / "memory.max").read_text().strip()
    swap = (root / "memory.swap.max").read_text().strip()
    quota, period = (root / "cpu.max").read_text().split()
    if memory == "max" or not 0 < int(memory) <= memory_bytes or swap != "0":
        raise RuntimeError("Memory/swap limits were not applied")
    if quota == "max" or not 0 < int(quota) * 100 <= cpu_percent * int(period):
        raise RuntimeError("CPU quota was not applied")


def build_command(args: argparse.Namespace, unit: str) -> list[str]:
    if args.memory_mb <= 0 or not 0 < args.cpu_percent <= 100 or args.runtime_seconds <= 0:
        raise ValueError("Require positive finite memory/runtime limits and CPU quota in (0, 100]")
    root = Path(args.repo_root).resolve()
    properties = {
        "MemoryAccounting": "yes",
        "CPUAccounting": "yes",
        "MemoryHigh": f"{max(1, args.memory_mb * 3 // 4)}M",
        "MemoryMax": f"{args.memory_mb}M",
        "MemorySwapMax": "0",
        "CPUQuota": f"{args.cpu_percent}%",
        "Nice": "10",
        "OOMScoreAdjust": "500",
        "OOMPolicy": "stop",
        "TasksMax": "64",
        "RuntimeMaxSec": f"{args.runtime_seconds}s",
        "TimeoutStopSec": "10s",
        "KillMode": "control-group",
        "Restart": "no",
        "WorkingDirectory": str(root),
    }
    environment = {
        "REVIEWFORGE_REPO_ROOT": str(root),
        "REVIEWFORGE_ENV_FILE": str(Path(args.env_file).resolve()),
        "REVIEWFORGE_SETTINGS_DIR": str(Path(args.settings_dir).resolve()),
        "REVIEWFORGE_SOURCE_REVISION": args.revision,
        "PYTHONPATH": str(root / "backend/src"),
    }
    workload_args = args.workload_args[1:] if args.workload_args[:1] == ["--"] else args.workload_args
    return [
        "systemd-run",
        f"--unit={unit}",
        "--wait",
        "--pipe",
        "--collect",
        *[f"--property={key}={value}" for key, value in properties.items()],
        *[f"--setenv={key}={value}" for key, value in environment.items()],
        "--",
        str(Path(args.python).absolute()),
        str(Path(__file__).resolve()),
        "--verify-and-exec",
        str(args.memory_mb * _MIB),
        str(args.cpu_percent),
        str(Path(args.python).absolute()),
        str(root / "scripts/benchmarks" / _SCRIPTS[args.task]),
        *workload_args,
    ]


def _write_record(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(path)


@contextmanager
def exclusive_host_lock(path: Path):
    import fcntl

    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another evaluation owns the host-wide lock") from exc
        yield


def execute_unit(command: list[str], unit: str, record: Path, payload: dict) -> int:
    _write_record(record, payload)
    returncode = None
    try:
        with record.with_suffix(".log").open("x", encoding="utf-8") as log:
            returncode = subprocess.run(
                command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT
            ).returncode
        return returncode
    finally:
        # Also stops a surviving transient service if the launcher is interrupted.
        subprocess.run(["systemctl", "stop", unit], capture_output=True, check=False)
        payload.update(
            status="completed" if returncode == 0 else "failed",
            returncode=returncode,
            finished_at=datetime.now(UTC).isoformat(),
        )
        _write_record(record, payload)


def run(args: argparse.Namespace) -> int:
    if not sys.platform.startswith("linux") or os.geteuid() != 0:
        raise RuntimeError("Requires Linux root and systemd; no unbounded fallback")
    root = Path(args.repo_root).resolve()
    if root == Path("/opt/reviewforge") or root.is_relative_to(Path("/opt/reviewforge")):
        raise ValueError("Use an isolated source snapshot outside the production checkout")
    if not re.fullmatch(r"[0-9a-f]{40}", args.revision):
        raise ValueError("Record the exact 40-character source revision")
    record = Path(args.record).resolve()
    if not record.is_relative_to(root) or record.exists():
        raise ValueError("Use a new execution record inside the isolated source snapshot")
    unit = "reviewforge-eval-" + uuid.uuid4().hex[:12]
    command = build_command(args, unit)
    controllers = Path("/sys/fs/cgroup/cgroup.controllers").read_text().split()
    if not {"cpu", "memory"}.issubset(controllers):
        raise RuntimeError("Requires cgroup v2 CPU and memory controllers")
    with exclusive_host_lock(Path("/run/lock/reviewforge-eval.lock")):
        active = subprocess.check_output(
            [
                "systemctl",
                "list-units",
                "--type=service",
                "--state=active,activating,deactivating",
                "--plain",
                "--no-legend",
                "reviewforge-eval-*",
            ],
            text=True,
        )
        if active.strip():
            raise RuntimeError("An earlier evaluation service is still active")
        for process in Path("/proc").iterdir():
            if not process.name.isdigit() or int(process.name) == os.getpid():
                continue
            try:
                cmdline = (process / "cmdline").read_bytes()
            except (OSError, PermissionError):
                continue
            if b"/opt/reviewforge-v4-eval/" in cmdline and any(
                argument.endswith(b"/scripts/benchmarks/" + name.encode())
                for argument in cmdline.split(b"\0")
                for name in _SCRIPTS.values()
            ):
                raise RuntimeError(f"Unmanaged evaluation process {process.name} is still running")
        subprocess.run(["systemctl", "is-active", "--quiet", "reviewforge"], check=True)
        available = next(
            int(line.split()[1]) * 1024
            for line in Path("/proc/meminfo").read_text().splitlines()
            if line.startswith("MemAvailable:")
        )
        check_capacity(args.memory_mb, args.reserve_mb, available, args.disk_reserve_mb, shutil.disk_usage(root).free)
        record.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "unit": unit,
            "source_revision": args.revision,
            "task": args.task,
            "memory_mb": args.memory_mb,
            "cpu_percent": args.cpu_percent,
            "runtime_seconds": args.runtime_seconds,
            "production_reserve_mb": args.reserve_mb,
            "available_bytes_before_start": available,
            "status": "running",
            "started_at": datetime.now(UTC).isoformat(),
        }
        return execute_unit(command, unit, record, payload)


def main() -> None:
    if sys.argv[1:2] == ["--verify-and-exec"]:
        group = next(
            line.split("::", 1)[1] for line in Path("/proc/self/cgroup").read_text().splitlines() if "::" in line
        )
        verify_cgroup_limits(Path("/sys/fs/cgroup") / group.lstrip("/"), int(sys.argv[2]), int(sys.argv[3]))
        os.execv(sys.argv[4], sys.argv[4:])
    parser = argparse.ArgumentParser()
    for option in ["repo-root", "python", "env-file", "settings-dir", "revision", "record"]:
        parser.add_argument("--" + option, required=True)
    for option in ["memory-mb", "reserve-mb", "disk-reserve-mb", "cpu-percent", "runtime-seconds"]:
        parser.add_argument("--" + option, required=True, type=int)
    parser.add_argument("--task", required=True, choices=tuple(_SCRIPTS))
    parser.add_argument("workload_args", nargs=argparse.REMAINDER)

    def terminate(_signum, _frame):
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, terminate)
    raise SystemExit(run(parser.parse_args()))


if __name__ == "__main__":
    main()
