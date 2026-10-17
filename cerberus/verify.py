"""Run the tests of freshly generated code in a throw-away folder so the Repairer can fix real failures, not just syntax errors.

The files are written to a temporary directory (never your workspace), run with a short timeout, no shell and a scrubbed environment
(no API keys). Running generated code is still running code, so the orchestrator asks the SafetyGate first.
"""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

TIMEOUT = 45
OUTPUT_TAIL = 2500
TEST_FILE = re.compile(r"(^|/)(test_[^/]+|[^/]+_test)\.py$")
SAFE_ENV = ("SYSTEMROOT", "SYSTEMDRIVE", "PATH", "PATHEXT", "COMSPEC", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL", "HOME", "USERPROFILE")


def python_exe() -> Optional[str]:
    """A real python interpreter (not the frozen Cerberus .exe)."""
    if not getattr(sys, "frozen", False):
        return sys.executable
    return shutil.which("python") or shutil.which("py")


def plan(files: list[dict]) -> list[tuple[str, list[str]]]:
    """(label, argv) checks that make sense for these files. Empty list = nothing runnable."""
    paths = [f["path"] for f in files]
    steps: list[tuple[str, list[str]]] = []
    py = python_exe()
    if py and any(TEST_FILE.search(p) for p in paths):
        uses_pytest = any(TEST_FILE.search(f["path"]) and re.search(r"^\s*(import|from)\s+pytest\b", f["content"], re.M) for f in files)
        if uses_pytest and importlib.util.find_spec("pytest"):
            steps.append(("pytest", [py, "-m", "pytest", "-q", "-x", "--no-header", "-p", "no:cacheprovider"]))
        else:
            steps.append(("unittest", [py, "-m", "unittest", "discover", "-s", ".", "-t", ".", "-v"]))
    node = shutil.which("node")
    if node:
        for p in paths:
            if p.endswith((".js", ".mjs", ".cjs")) and not p.startswith(("node_modules/", "dist/")):
                steps.append((f"node --check {p}", [node, "--check", p]))
    return steps


def _env() -> dict[str, str]:
    env = {k: os.environ[k] for k in SAFE_ENV if k in os.environ}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_checks(files: list[dict], timeout: int = TIMEOUT) -> dict:
    """Run every planned check. Result: {'ran': bool, 'ok': bool, 'summary': str, 'failures': [str], 'output': str}."""
    steps = plan(files)
    if not steps:
        return {"ran": False, "ok": True, "summary": "no tests to run", "failures": [], "output": ""}
    failures: list[str] = []
    notes: list[str] = []
    with tempfile.TemporaryDirectory(prefix="cerberus_verify_") as tmp:
        root = Path(tmp).resolve()
        try:
            for f in files:
                target = (root / f["path"]).resolve()
                if root not in target.parents:               # absolute path or '..' escape: skip it
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(f["content"], encoding="utf-8")
                if TEST_FILE.search(f["path"]):              # let unittest discover tests that sit in sub-folders
                    for parent in target.parents:
                        if parent == root:
                            break
                        (parent / "__init__.py").touch(exist_ok=True)
        except (OSError, ValueError) as exc:                 # odd generated file names (too long, reserved) must not crash the run
            return {"ran": False, "ok": True, "summary": f"could not set up the checks ({exc})", "failures": [], "output": ""}
        for label, argv in steps:
            try:
                proc = subprocess.run(argv, cwd=str(root), capture_output=True, text=True, errors="replace", timeout=timeout,
                                      stdin=subprocess.DEVNULL, env=_env())
                out = ((proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")).strip()
                ok = proc.returncode == 0
            except subprocess.TimeoutExpired:
                out, ok = f"Timed out after {timeout}s (infinite loop or waiting for input?).", False
            except OSError as exc:
                out, ok = f"Could not start: {exc}", False
            if ok:
                notes.append(f"{label}: passed")
            else:
                failures.append(f"{label} FAILED:\n{out[-OUTPUT_TAIL:]}")
    ok = not failures
    summary = "; ".join(notes) if ok else f"{len(failures)} check(s) failed" + (f" ({'; '.join(notes)} passed)" if notes else "")
    return {"ran": True, "ok": ok, "summary": summary, "failures": failures, "output": "\n\n".join(failures)}
