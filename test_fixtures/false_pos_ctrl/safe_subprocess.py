"""Safe subprocess patterns — should NOT produce command injection findings.

Purpose: verify the reviewer does NOT flag safe uses of subprocess with list args.
"""
import subprocess


def safe_list_args(input_file: str) -> str:
    """List-arg form — SAFE, no shell injection possible."""
    result = subprocess.run(
        ["wc", "-l", input_file],
        capture_output=True,
        text=True,
    )
    return result.stdout


def safe_no_shell(user_data: str) -> str:
    """Popen with list args and shell=False (default) — SAFE."""
    proc = subprocess.Popen(
        ["grep", user_data, "/var/log/app.log"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, _ = proc.communicate()
    return stdout.decode()


def safe_path_join(base: str, sub: str) -> str:
    """Path join with validation — SAFE."""
    import os

    full = os.path.normpath(os.path.join(base, sub))
    if not full.startswith(os.path.normpath(base)):
        raise ValueError("Path traversal detected")
    # SAFE: path is validated before use
    return full


def safe_shell_with_constants() -> str:
    """Shell=True with NO user input — SAFE (constants only)."""
    result = subprocess.run(
        "ls -la /var/log/",
        shell=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def safe_check_output(constant_host: str = "localhost") -> str:
    """Constant argument — SAFE, no injection vector."""
    return subprocess.check_output(["ping", "-c", "1", constant_host]).decode()
