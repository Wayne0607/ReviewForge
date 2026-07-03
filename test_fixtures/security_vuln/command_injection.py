"""Command Injection variants across languages.

Purpose: verify security_reviewer catches ALL command injection patterns.
"""
import os
import subprocess


# ============================================================
# Python Command Injection variants
# ============================================================

def cmd_os_system(user_file: str):
    """os.system with user input"""
    os.system("cat " + user_file)


def cmd_os_popen(user_host: str):
    """os.popen with user input"""
    os.popen("ping -c 1 " + user_host)


def cmd_subprocess_shell(user_path: str):
    """subprocess with shell=True and user input"""
    subprocess.call(f"ls -la {user_path}", shell=True)


def cmd_subprocess_popen(user_cmd: str):
    """subprocess.Popen with user input"""
    subprocess.Popen(["bash", "-c", user_cmd])


def cmd_subprocess_run(user_url: str):
    """subprocess.run with shell=True"""
    subprocess.run("curl " + user_url, shell=True)


# Go command injection (embedded in Python for testing)
GO_CMD_INJECTION_EXAMPLE = """
cmd := exec.Command("sh", "-c", userInput)
cmd.Run()
"""

# Rust command injection (embedded for reference)
RUST_CMD_INJECTION_EXAMPLE = """
Command::new(user_input).spawn().unwrap();
"""

# Ruby command injection (embedded for reference)
RUBY_CMD_INJECTION_EXAMPLE = """
system("rm -rf \#{user_dir}")
`cat \#{user_file}`
"""

# Java command injection (embedded for reference)
JAVA_CMD_INJECTION_EXAMPLE = """
Runtime.getRuntime().exec("ping " + userHost);
"""
