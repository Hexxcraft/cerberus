"""File tools. Everything is sandboxed to the workspace folder (set in the Grimoire tab)."""
from __future__ import annotations

from .. import guard
from ..config import get_workspace
from ..safety import safe_path
from .base import tool

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv"}


@tool("list_files", "File Reading", "List files and folders in the workspace.",
      {"path": "folder relative to workspace, default '.' (optional)"})
def list_files(ctx, path: str = ".") -> str:
    base = safe_path(path)
    if not base.exists():
        return f"'{path}' does not exist."
    ws = get_workspace()
    lines = []
    for p in sorted(base.rglob("*")):
        if any(part in SKIP_DIRS for part in p.relative_to(base).parts):
            continue
        rel = p.relative_to(ws).as_posix()
        lines.append(rel + ("/" if p.is_dir() else f"  ({p.stat().st_size} bytes)"))
        if len(lines) >= 200:
            lines.append("...(more files not shown)")
            break
    return "\n".join(lines) or "(empty)"


@tool("read_file", "File Reading", "Read a text file from the workspace.",
      {"path": "file path relative to workspace", "max_chars": "limit, default 8000 (optional)"})
def read_file(ctx, path: str, max_chars: int = 8000) -> str:
    p = safe_path(path)
    if not p.is_file():
        return f"File '{path}' not found."
    return p.read_text(encoding="utf-8", errors="replace")[: int(max_chars)]


@tool("write_file", "File Editing", "Create or overwrite a text file in the workspace.",
      {"path": "file path relative to workspace", "content": "the full file contents"}, risk="write")
def write_file(ctx, path: str, content: str) -> str:
    p = safe_path(path)
    guard.assert_space(p, len(content.encode("utf-8", errors="replace")))
    p.parent.mkdir(parents=True, exist_ok=True)
    existed = p.exists()
    p.write_text(content, encoding="utf-8")
    return f"{'Overwrote' if existed else 'Created'} {p.relative_to(get_workspace()).as_posix()} ({len(content)} chars)."


@tool("append_file", "File Editing", "Append text to the end of a file in the workspace (creates it if missing).",
      {"path": "file path relative to workspace", "content": "text to add"}, risk="write")
def append_file(ctx, path: str, content: str) -> str:
    p = safe_path(path)
    guard.assert_space(p, len(content.encode("utf-8", errors="replace")))
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(content)
    return f"Appended {len(content)} chars to {p.relative_to(get_workspace()).as_posix()}."
