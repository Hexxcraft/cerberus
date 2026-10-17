"""Side-project registry: lets Cerberus run the projects YOU registered (Settings -> "projects"), and nothing else.

The command to run always comes from your settings, never from the model: the model only picks a registered project by name.
Register one in the Gates tab or with PUT /api/projects:
    {"Name": {"path": "C:\\\\Projects\\\\thing", "run": "python main.py", "about": "what it does"}}
"""
from __future__ import annotations

import shlex
import shutil
import sys
from pathlib import Path

from .. import config, guard
from ..safety import Denied
from .base import ToolContext, tool


def registry() -> dict[str, dict]:
    raw = config.load_settings().get("projects") or {}
    return {str(k): v for k, v in raw.items() if isinstance(v, dict)}


def _find(name: str) -> tuple[str, dict]:
    reg = registry()
    for key, spec in reg.items():
        if key.lower() == (name or "").strip().lower():
            return key, spec
    known = ", ".join(reg) or "none registered yet"
    raise Denied(f"No registered project called '{name}'. Registered: {known}.")


def _guard_run(ctx: ToolContext, name: str = "", **_) -> None:
    key, spec = _find(name)
    folder = Path(str(spec.get("path", ""))).expanduser()
    if not folder.is_dir():
        raise Denied(f"The folder for '{key}' does not exist: {folder}")
    if not str(spec.get("run", "")).strip():
        raise Denied(f"'{key}' has no run command registered.")


def _preview(ctx: ToolContext, name: str = "", **_) -> str:
    key, spec = _find(name)
    return f"Run the registered project '{key}': `{spec['run']}` in {spec['path']}"


@tool("project_list", "Projects", "List the side projects the user has registered with Cerberus (name, folder, what it does).",
      risk="safe", local_only=True)
def project_list(ctx: ToolContext) -> str:
    reg = registry()
    if not reg:
        return "No projects are registered yet."
    return "\n".join(f"{k}: {v.get('about', '')} [folder: {v.get('path', '?')}; runs: {v.get('run', '(nothing)')}]"
                     for k, v in reg.items())


@tool("project_run", "Projects", "Run one of the user's registered side projects with its saved command and show the output.",
      {"name": "the registered project name", "timeout": "seconds, max 300 (optional)"},
      risk="control", local_only=True, council=True, preflight=_guard_run, preview=_preview)
def project_run(ctx: ToolContext, name: str, timeout: int = 120) -> str:
    key, spec = _find(name)
    argv = [a[1:-1] if len(a) > 1 and a[0] == a[-1] and a[0] in "\"'" else a
            for a in shlex.split(str(spec["run"]), posix=(sys.platform != "win32"))]
    resolved = shutil.which(argv[0]) if argv else None
    if not resolved:
        return f"'{argv[0] if argv else ''}' is not installed or not on PATH."
    argv[0] = resolved
    ctx.gate.audit("project_run", "started", {"project": key, "command": spec["run"]})
    limit = max(1, min(int(timeout), 300))
    res = guard.child_run(argv, str(Path(spec["path"]).expanduser()), limit)
    if res["timed_out"]:
        return f"'{key}' was still running after {timeout}s and it, with anything it started, was stopped."
    out = res["out"] + (("\n[stderr]\n" + res["err"]) if res["err"] else "")
    return f"{key}: exit code {res['code']}\n{out[-4000:]}"
