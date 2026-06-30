"""Command injection patterns for evaluation."""

import os
import subprocess


def ping_host(host):
    """Command injection via os.system."""
    os.system("ping -c 1 " + host)


def list_dir(dirname):
    """Command injection via subprocess with shell=True."""
    result = subprocess.run("ls " + dirname, shell=True, capture_output=True, text=True)
    return result.stdout


def run_arbitrary(cmd):
    """Command injection via subprocess.call with shell=True."""
    subprocess.call(cmd, shell=True)


def lookup_domain(domain):
    """Command injection via os.popen."""
    output = os.popen("nslookup " + domain).read()
    return output


def safe_ping(host):
    """Safe: subprocess without shell=True. Should NOT be flagged."""
    result = subprocess.run(["ping", "-c", "1", host], capture_output=True, text=True)
    return result.stdout
