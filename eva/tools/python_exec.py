"""
tools/python_exec.py — Python 代码执行（进程隔离）
"""

import subprocess
import sys
import tempfile
import os

PYTHON_EXEC_SCHEMA = {
    "type": "function",
    "function": {
        "name": "python_exec",
        "description": (
            "Execute a Python code snippet, returning stdout/stderr and exit code. "
            "Useful for data processing, computation, calling Python libraries, etc. "
            "Code runs in an isolated subprocess; each call gets a fresh environment."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code":    {"type": "string", "description": "Python code to execute"},
                "timeout": {"type": "integer", "description": "Timeout in seconds, default 30", "default": 30},
            },
            "required": ["code"],
        },
    },
}


def python_exec(code: str, timeout: int = 30) -> dict:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py",
                                    delete=False, encoding="utf-8") as f:
        f.write(code)
        tmp = f.name
    try:
        proc = subprocess.run(
            [sys.executable, tmp],
            capture_output=True, text=True, timeout=timeout
        )
        output = proc.stdout + proc.stderr
        return {
            "rc":        proc.returncode,
            "output":    output[:8000],
            "truncated": len(output) > 8000,
        }
    except subprocess.TimeoutExpired:
        return {"rc": -1, "output": f"Execution timed out (>{timeout}s)", "truncated": False}
    except Exception as e:
        return {"rc": -1, "output": str(e), "truncated": False}
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
