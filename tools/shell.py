"""
tools/shell.py — shell 命令执行
"""

import subprocess

SHELL_EXEC_SCHEMA = {
    "type": "function",
    "function": {
        "name": "shell_exec",
        "description": "Execute a shell command, returning stdout+stderr and exit code. Useful for file operations, system calls, invoking external programs, etc.",
        "parameters": {
            "type": "object",
            "properties": {
                "cmd":     {"type": "string", "description": "Shell command to execute"},
                "timeout": {"type": "integer", "description": "Timeout in seconds, default 30", "default": 30},
            },
            "required": ["cmd"],
        },
    },
}


def shell_exec(cmd: str, timeout: int = 30) -> dict:
    try:
        proc = subprocess.run(
            cmd, shell=True, capture_output=True,
            text=True, timeout=timeout
        )
        output = proc.stdout + proc.stderr
        return {
            "rc":     proc.returncode,
            "output": output[:8000],
            "truncated": len(output) > 8000,
        }
    except subprocess.TimeoutExpired:
        return {"rc": -1, "output": f"Command timed out (>{timeout}s)", "truncated": False}
    except Exception as e:
        return {"rc": -1, "output": str(e), "truncated": False}
