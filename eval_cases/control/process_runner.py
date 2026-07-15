"""Process and path helpers."""
import subprocess


def count_lines(input_file: str) -> str:
    """Count lines in a file."""
    result = subprocess.run(
        ["wc", "-l", input_file],
        capture_output=True,
        text=True,
    )
    return result.stdout


def find_log_entries(user_data: str) -> str:
    """Find matching entries in the application log."""
    proc = subprocess.Popen(
        ["grep", user_data, "/var/log/app.log"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, _ = proc.communicate()
    return stdout.decode()


def resolve_child_path(base: str, sub: str) -> str:
    """Resolve a child path while keeping it inside the base directory."""
    import os

    full = os.path.normpath(os.path.join(base, sub))
    if os.path.commonpath((os.path.abspath(base), os.path.abspath(full))) != os.path.abspath(base):
        raise ValueError("Path traversal detected")
    return full


def list_logs() -> str:
    """List the service log directory."""
    result = subprocess.run(
        "ls -la /var/log/",
        shell=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def ping_localhost(constant_host: str = "localhost") -> str:
    """Check connectivity to the configured local host."""
    return subprocess.check_output(["ping", "-c", "1", constant_host]).decode()
