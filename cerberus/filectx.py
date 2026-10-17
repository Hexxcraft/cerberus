"""File browser and "point at files for context".

The user browses folders (Library tab / GET /api/files/browse) and attaches files to a message; their text is added to what
every head sees for that one request. It reuses the Computer tools' rules, so secrets, Cerberus's own data and network paths
are never listed or read, and with Full computer access switched off only the workspace is reachable. Local use only: the
server refuses these endpoints and the `context_files` chat option for phones and other devices.

Browsing never opens files (only names and sizes), so cloud-synced folders such as OneDrive do not start downloading
everything; a file is read only when it is previewed or attached.
"""
from __future__ import annotations

import os
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from . import config
from .safety import Denied
from .tools import computer

MAX_FILES = 8
PER_FILE_CHARS = 12_000
TOTAL_CHARS = 24_000
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_ENTRIES = 600
PREVIEW_CHARS = 4_000

TEXT_EXT = {".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg",
            ".conf", ".xml", ".html", ".htm", ".css", ".js", ".ts", ".tsx", ".jsx", ".py", ".pyw", ".java", ".c", ".h", ".cpp", ".hpp",
            ".cs", ".go", ".rs", ".rb", ".php", ".sh", ".bat", ".ps1", ".sql", ".tex", ".bib", ".srt", ".vtt", ".lua", ".kt", ".swift",
            ".r", ".m", ".gd", ".gradle", ".properties", ".gitignore"}
DOCX_EXT = {".docx"}
HIDE = {"$recycle.bin", "system volume information", "$winreagent", "config.msi"}
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _kind(path: Path, is_dir: bool) -> str:
    if is_dir:
        return "folder"
    ext = path.suffix.lower()
    if ext in TEXT_EXT or path.name.lower() in {"readme", "license", "makefile", "dockerfile"}:
        return "text"
    return "docx" if ext in DOCX_EXT else "other"


def _scope_ok(p: Path) -> None:
    if not config.load_settings().get("full_access", True) and not computer._in_workspace(p):
        raise Denied("Full computer access is switched off, so only the workspace folder can be browsed. "
                     "It can be switched on in the Gates tab.")


def check(raw: str) -> Path:
    """Resolve a path the user typed or clicked and refuse anything off limits."""
    p = computer.resolve(raw)
    computer.check_read(p)
    _scope_ok(p)
    return p


def roots() -> list[dict]:
    home = Path.home()
    cand = [("Workspace", config.get_workspace())]
    if config.load_settings().get("full_access", True):
        one = computer._env_dir("OneDrive")
        cand += [("Desktop", home / "Desktop"), ("Documents", home / "Documents"), ("Downloads", home / "Downloads"),
                 ("Pictures", home / "Pictures")]
        if one:
            cand += [("OneDrive", one), ("OneDrive Documents", one / "Documents")]
        cand += [("Home", home)] + [(f"Drive {L}:", Path(f"{L}:\\")) for L in "CDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.exists(f"{L}:\\")]
    seen, out = set(), []
    for name, path in cand:
        key = str(path).lower()
        if key not in seen and path.exists():
            seen.add(key)
            out.append({"name": name, "path": str(path)})
    return out


def browse(raw: str = "") -> dict:
    """List one folder (names and sizes only). An empty path lists the starting places."""
    if not (raw or "").strip():
        return {"path": "", "parent": None, "entries": [{"name": r["name"], "path": r["path"], "dir": True, "size": None, "kind": "folder",
                                                         "attachable": False} for r in roots()], "truncated": False, "roots": True}
    p = check(raw)
    if not p.exists():
        raise Denied(f"'{p}' does not exist.")
    if not p.is_dir():
        raise Denied(f"'{p}' is a file, not a folder.")
    entries, truncated = [], False
    try:
        scan = sorted(os.scandir(p), key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
    except PermissionError:
        raise Denied(f"Windows does not let Cerberus open '{p}' (no permission).")
    for e in scan:
        if e.name.lower() in HIDE:
            continue
        ep = Path(e.path)
        try:
            computer.check_read(ep)
        except Denied:
            continue
        try:
            is_dir = e.is_dir(follow_symlinks=False)
            size = None if is_dir else e.stat().st_size
        except OSError:
            continue
        kind = _kind(ep, is_dir)
        entries.append({"name": e.name, "path": str(ep), "dir": is_dir, "size": size, "kind": kind,
                        "attachable": kind in ("text", "docx") and (size or 0) <= MAX_FILE_BYTES})
        if len(entries) >= MAX_ENTRIES:
            truncated = True
            break
    parent = str(p.parent) if p.parent != p else ""
    if parent:
        try:
            check(parent)
        except Denied:
            parent = ""
    return {"path": str(p), "parent": parent, "entries": entries, "truncated": truncated, "roots": False}


def _docx_text(p: Path) -> str:
    with zipfile.ZipFile(p) as z:
        info = z.getinfo("word/document.xml")
        if info.file_size > 30 * 1024 * 1024:
            raise ValueError("document is too large")
        root = ET.fromstring(z.read("word/document.xml"))
    paras = []
    for para in root.iter(f"{_W}p"):
        bits = []
        for node in para.iter():
            if node.tag == f"{_W}t" and node.text:
                bits.append(node.text)
            elif node.tag in (f"{_W}tab",):
                bits.append("\t")
            elif node.tag in (f"{_W}br", f"{_W}cr"):
                bits.append("\n")
        paras.append("".join(bits))
    return "\n".join(paras).strip()


def read_text(p: Path, limit: int) -> tuple[str, int, str]:
    """(text up to `limit` chars, total chars if known else -1, note). Raises Denied for things that cannot be read."""
    if not p.exists():
        raise Denied(f"'{p.name}' does not exist.")
    if p.is_dir():
        raise Denied("That is a folder; pick the files inside it.")
    size = p.stat().st_size
    if size > MAX_FILE_BYTES:
        raise Denied(f"'{p.name}' is {computer._human(size)}, too large to attach.")
    ext = p.suffix.lower()
    if ext in DOCX_EXT:
        try:
            full = _docx_text(p)
        except (zipfile.BadZipFile, KeyError, ET.ParseError, ValueError, OSError):
            raise Denied(f"'{p.name}' is not a readable Word document.")
        return full[:limit], len(full), ""
    if ext in TEXT_EXT or computer._is_text_file(p):
        text = computer._read_text(p, limit)
        return text[:limit], (len(text) if len(text) <= limit else -1), ""
    raise Denied(f"'{p.name}' is a {ext or 'binary'} file. Cerberus can read text files and Word (.docx) documents; "
                 "export it as text or .docx to use it.")


def preview(raw: str, chars: int = PREVIEW_CHARS) -> dict:
    p = check(raw)
    text, total, _ = read_text(p, chars)
    return {"name": p.name, "path": str(p), "size": p.stat().st_size, "text": text, "truncated": total == -1 or total > chars}


def load(paths: list, total_budget: int = TOTAL_CHARS, per_file: int = PER_FILE_CHARS) -> tuple[str, list[dict]]:
    """Read the attached files. Returns (text block for the prompt, one report row per file: ok / chars / note)."""
    report: list[dict] = []
    parts: list[str] = []
    left = total_budget
    clean = [str(x) for x in (paths or []) if isinstance(x, str) and x.strip()]
    for raw in list(dict.fromkeys(clean))[:MAX_FILES]:
        row = {"path": raw, "name": Path(raw).name or raw, "ok": False, "chars": 0, "note": ""}
        report.append(row)
        if left <= 200:
            row["note"] = "left out: the attached files were already as much as the heads can read at once"
            continue
        try:
            p = check(raw)
            row["name"] = p.name
            text, total, _ = read_text(p, min(per_file, left))
        except (Denied, OSError) as exc:
            row["note"] = re.sub(r"\s+", " ", str(exc))[:200]
            continue
        cut = total == -1 or total > len(text)
        if not text.strip():
            row["note"] = "the file is empty"
            continue
        row.update(ok=True, chars=len(text), note="only the first part fit" if cut else "")
        parts.append(f"--- FILE: {p.name}  ({p}) ---\n{text.rstrip()}" + ("\n[...file continues; only the start is shown]" if cut else ""))
        left -= len(text)
    if len(clean) > MAX_FILES:
        report.append({"path": "", "name": f"{len(clean) - MAX_FILES} more", "ok": False, "chars": 0,
                       "note": f"only the first {MAX_FILES} files are used"})
    return "\n\n".join(parts), report
