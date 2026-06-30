"""Fixture: command injection via os.system / subprocess shell=True."""
import os
import subprocess


def ping(host):
    os.system("ping -c 1 " + host)  # untrusted host concatenated into a shell command


def list_dir(path):
    return subprocess.run("ls " + path, shell=True, capture_output=True)  # shell=True + concat
