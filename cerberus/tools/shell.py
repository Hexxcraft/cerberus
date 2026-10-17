"""Run a small allow-list of developer commands (python, pip, pytest, npm, ...) inside the workspace.

No shell is used, so tricks like `a && b`, pipes or redirects are just passed as plain text and do nothing. (Windows .cmd
launchers such as npm are the exception, so arguments with cmd.exe metacharacters are refused for those.)
"""
from __future__ import annotations

import shlex
import shutil
import sys

from .. import guard
from ..config import get_workspace
from ..safety import Denied, safe_path
from .base import tool

# git is deliberately NOT here: it has its own tools so that `push` always asks for "publish" approval.
ALLOWED = {"python", "py", "pip", "pytest", "node", "npm", "npx", "tsc", "dotnet", "cargo", "go"}
ALLOWED_PY_MODULES = {"pytest", "unittest", "pip", "venv", "http.server", "compileall"}
SHIM_UNSAFE = set('&|<>^%"\r\n')      # cmd.exe would act on these inside an argument handed to a .cmd/.bat launcher
NODE_INLINE = {"-e", "--eval", "-p", "--print", "-pe", "-ep", "--input-type"}


def check_shim_args(args: list[str]) -> None:
    """npm/npx/tsc/code are .cmd launchers on Windows; cmd.exe would interpret & | < > ^ % in their arguments."""
    for a in args:
        if SHIM_UNSAFE & set(a):
            raise Denied("That argument contains a character (& | < > ^ % or a quote) that is not allowed for this program.")


def _check_python_flags(argv: list[str]) -> None:
    """Inline code (-c) and unapproved modules (-m) are refused, including when glued into a flag cluster like -Sc or -Im."""
    args = argv[1:]
    skip = False
    for i, a in enumerate(args):
        if skip:                                     # the value of -W / -X
            skip = False
            continue
        if not a.startswith("-"):
            return                                   # first non-flag is the script; everything after is its own arguments
        if a in ("-W", "-X"):
            skip = True
            continue
        if a.startswith(("-W", "-X")):
            continue                                 # value attached, e.g. -Xutf8 or -Wignore
        if a == "-m":
            if i + 1 >= len(args) or args[i + 1] not in ALLOWED_PY_MODULES:
                raise Denied(f"Only these modules may be run with -m: {', '.join(sorted(ALLOWED_PY_MODULES))}.")
            return
        if not a.startswith("--") and {"c", "m"} & set(a[1:]):
            raise Denied("'python -c' (inline code) and combined -m flags are blocked; write a file and run it instead.")


@tool("run_command", "Shell Commands",
      "Run a developer command in the workspace (python, pip, pytest, node, npm, npx, tsc, dotnet, cargo, go). "
      "Example: 'python main.py' or 'pytest -q'.",
      {"command": "the command line", "folder": "subfolder to run in (optional)", "timeout": "seconds, max 120 (optional)"},
      risk="write")
def run_command(ctx, command: str, folder: str = ".", timeout: int = 60) -> str:
    argv = shlex.split(command, posix=(sys.platform != "win32"))
    if not argv:
        raise ValueError("empty command")
    exe = argv[0].lower().removesuffix(".exe")
    if exe not in ALLOWED:
        raise Denied(f"'{argv[0]}' is not on the allow-list: {', '.join(sorted(ALLOWED))}.")
    if exe in ("python", "py"):
        _check_python_flags(argv)
    if exe == "node" and any(a in NODE_INLINE for a in argv[1:]):
        raise Denied("Inline node code is blocked; write a file and run it instead.")
    cwd = safe_path(folder)
    cwd.mkdir(parents=True, exist_ok=True)
    resolved = shutil.which(argv[0])
    if not resolved:
        return f"'{argv[0]}' is not installed or not on PATH."
    if resolved.lower().endswith((".cmd", ".bat")):
        check_shim_args(argv[1:])
    argv[0] = resolved
    limit = max(1, min(int(timeout), 120))
    res = guard.child_run(argv, str(cwd), limit)
    if res["timed_out"]:
        return f"Timed out after {timeout}s (the command and anything it started were stopped)."
    out = res["out"] + (("\n[stderr]\n" + res["err"]) if res["err"] else "")
    return f"exit code {res['code']} (in {cwd.relative_to(get_workspace()).as_posix() or '.'})\n{out[-4000:]}"
