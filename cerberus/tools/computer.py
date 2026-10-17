"""Whole-computer access: look at and change files anywhere on this PC.

The rules that make that safe, in order:
  * Only runs when the request comes from this computer (never from a phone) and "full access" is on in the Gates tab.
  * HARD DENYLIST (checked before anyone is asked): Windows and program folders, drive roots, Cerberus's own program and data,
    password/key files and browser/credential stores. These are refused no matter what the heads or the user say.
  * Every change is shown to all three heads, which must ALL approve it (council), and then to you (the Gates prompt)
    unless Autonomous mode is on.
  * Nothing is ever really deleted or overwritten: the old version goes to Cerberus's undo folder first, and pc_undo restores it.
"""
from __future__ import annotations

import difflib
import fnmatch
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Optional

from .. import config, guard, hardware
from ..safety import Denied
from .base import ToolContext, tool

MAX_READ_CHARS = 200_000
MAX_WRITE_BYTES = 2_000_000
MAX_BACKUP_BYTES = 500 * 1024 * 1024
MAX_TREE_BYTES = 1024 * 1024 * 1024
MAX_TREE_FILES = 20_000
UNDO_KEEP = 200
SKIP_DIRS = {"$recycle.bin", "system volume information", "node_modules", ".git", ".venv", "__pycache__", "windows",
             "appdata", "$windows.~bt", "recovery"}

RISKY_SUFFIXES = {".exe", ".dll", ".sys", ".msi", ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".vbe", ".wsf", ".scr", ".com",
                  ".reg", ".lnk", ".pif", ".hta", ".scf", ".jse", ".wsh", ".url", ".jar", ".msc", ".cpl", ".inf", ".docm", ".xlsm"}
SECRET_FILE = re.compile(r"^(\.env(\..*)?|.*\.(pem|key|pfx|p12|kdbx|ppk|keystore|jks)|id_(rsa|dsa|ecdsa|ed25519).*|"
                         r"\.netrc|\.npmrc|\.pypirc|\.git-credentials|\.gitconfig|credentials(\..*)?|hosts\.yml|"
                         r"wallet\.dat|ntuser\.dat.*)$", re.I)
SECRET_DIR_NAMES = {".ssh", ".aws", ".gnupg", ".azure", ".kube", ".docker"}


# ---------------------------------------------------------------- where things are
def _local_appdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))


def _roaming_appdata() -> Path:
    return Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))


def _norm(p: Path | str) -> str:
    return os.path.normcase(str(p)).rstrip("\\/")


def _under(p: Path | str, base: Path | str) -> bool:
    a, b = _norm(p), _norm(base)
    return a == b or a.startswith(b + os.sep)


def _env_dir(name: str) -> Optional[Path]:
    v = os.environ.get(name)
    return Path(v) if v else None


def _secret_trees() -> list[Path]:
    home, local, roaming = Path.home(), _local_appdata(), _roaming_appdata()
    trees = [home / n for n in SECRET_DIR_NAMES]
    trees += [home / ".config" / "gh", roaming / "GitHub CLI", roaming / "discord", roaming / "Slack", roaming / "Signal",
              roaming / "Microsoft" / "Credentials", roaming / "Microsoft" / "Protect", roaming / "Microsoft" / "Vault",
              local / "Microsoft" / "Credentials", local / "Microsoft" / "Vault",
              local / "Google" / "Chrome" / "User Data", local / "Microsoft" / "Edge" / "User Data",
              local / "Vivaldi", local / "BraveSoftware", roaming / "Mozilla" / "Firefox", roaming / "Opera Software",
              config.DATA_DIR]
    win = _env_dir("SystemRoot")
    if win:
        trees.append(win / "System32" / "config")
    return trees


def _frozen_trees() -> list[Path]:
    """Folders that nothing may be written to, moved or deleted inside."""
    trees = [config.ROOT]
    for name in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData"):
        d = _env_dir(name)
        if d:
            trees.append(d)
    if os.name == "nt":
        trees += [Path("C:/Windows"), Path("C:/Program Files"), Path("C:/Program Files (x86)"), Path("C:/ProgramData")]
    trees += [_local_appdata(), _roaming_appdata()]
    return trees


def _workspace() -> Path:
    return config.get_workspace()


def _in_workspace(p: Path) -> bool:
    return _under(p, _workspace())


def _exempt_from_freeze(p: Path) -> bool:
    return _in_workspace(p) or _under(p, _local_appdata() / "Temp") or _under(p, Path(os.environ.get("TEMP") or "\0none"))


def _exact_keep() -> list[Path]:
    """Folders that may be written INTO but never moved, deleted or replaced themselves."""
    home = Path.home()
    keep = [home, home.parent, _local_appdata().parent]
    for sub in ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos", "OneDrive"):
        keep.append(home / sub)
    one = _env_dir("OneDrive")
    if one:
        keep += [one, one / "Documents", one / "Desktop", one / "Pictures"]
    return keep


# ---------------------------------------------------------------- resolving and checking paths
def resolve(raw: str) -> Path:
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise Denied("No path was given.")
    text = os.path.expandvars(os.path.expanduser(raw.strip().strip('"')))
    if text.startswith(("\\\\", "//")):
        raise Denied("Network and device paths (\\\\...) are not allowed.")
    p = Path(text)
    if not p.is_absolute():
        p = _workspace() / p                       # a bare name means "in Cerberus's workspace"
    resolved = p.resolve()
    s = str(resolved)
    if s.startswith("\\\\"):
        raise Denied("Network and device paths (\\\\...) are not allowed.")
    if any(":" in part for part in resolved.parts[1:]):
        raise Denied("Paths with ':' (alternate data streams) are not allowed.")
    return resolved


def _require_on() -> None:
    if not config.load_settings().get("full_access", True):
        raise Denied("Full computer access is switched off. The user can switch it on in the Gates tab.")


def check_workspace(ws: Path) -> None:
    """A workspace folder is exempt from the protected-folder freeze, so it must never be one of those folders itself,
    nor contain one (a drive root or your whole user folder would otherwise lift every protection)."""
    ws = ws.resolve()
    if ws.parent == ws or _norm(ws) == _norm(Path.home()):
        raise Denied("The workspace cannot be a whole drive or your whole user folder. Pick a normal folder.")
    for tree in _secret_trees():
        if _under(ws, tree) or _under(tree, ws):
            raise Denied(f"The workspace cannot be (or contain) a protected folder: {tree}")
    if _under(config.ROOT, ws) or (_under(ws, config.ROOT) and not _under(ws, config.DEFAULT_WORKSPACE)):
        raise Denied("The workspace cannot be (or contain) the Cerberus program folder.")
    for tree in _frozen_trees():
        if tree is not config.ROOT and (_under(ws, tree) or _under(tree, ws)):
            raise Denied(f"The workspace cannot be (or contain) a protected folder: {tree}")


def check_read(p: Path) -> None:
    if any(_under(p, t) for t in _secret_trees()):
        raise Denied(f"'{p}' holds passwords, keys or Cerberus's own private data. It is permanently off limits.")
    if SECRET_FILE.match(p.name):
        raise Denied(f"'{p.name}' looks like a secret (key, password or token file). It is permanently off limits.")


def check_write(p: Path, *, destructive: bool = False) -> None:
    """Hard rules for changing `p`. destructive=True (move/delete/replace) is stricter for important folders."""
    check_read(p)
    if p.parent == p or (len(p.parts) <= 1):
        raise Denied("A drive root cannot be changed.")
    if len(p.parts) == 2 and not _in_workspace(p):
        raise Denied("Files directly in a drive's top folder (like C:\\) are not changed. Pick a folder inside it.")
    for tree in _frozen_trees():
        if _under(p, tree) and not _exempt_from_freeze(p):
            raise Denied(f"'{p}' is inside a protected folder ({tree}): Windows, installed programs, application data or "
                         "Cerberus itself. Changes there are never allowed.")
    if destructive:
        if any(_norm(p) == _norm(k) for k in _exact_keep()):
            raise Denied(f"'{p}' is one of your main folders; it cannot be moved or deleted as a whole.")
        for tree in _frozen_trees() + _secret_trees():
            if _under(tree, p) and _norm(tree) != _norm(p):
                raise Denied(f"'{p}' contains protected folders ({tree}), so it cannot be moved or deleted as a whole.")
        for keep in _exact_keep():
            if _under(keep, p) and _norm(keep) != _norm(p):
                raise Denied(f"'{p}' contains one of your main folders ({keep}), so it cannot be moved or deleted as a whole.")
    if p.suffix.lower() in RISKY_SUFFIXES and not _in_workspace(p):
        raise Denied(f"Cerberus will not create or change '{p.suffix}' programs and shortcuts outside its workspace.")


def _is_text_file(p: Path) -> bool:
    try:
        with open(p, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return False
    return b"\x00" not in head or head[:2] in (b"\xff\xfe", b"\xfe\xff")


def _read_text(p: Path, limit: int) -> str:
    with open(p, "rb") as fh:
        raw = fh.read(limit * 4 + 4)
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8-sig", errors="replace")


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def _tree_stats(p: Path) -> tuple[int, int]:
    """(bytes, files) of a folder, stopping early once it is clearly too big."""
    total = files = 0
    for root, _dirs, names in os.walk(p):
        for name in names:
            files += 1
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
            if total > MAX_TREE_BYTES or files > MAX_TREE_FILES:
                return total, files
    return total, files


# ---------------------------------------------------------------- the undo journal
def undo_dir() -> Path:
    d = config.DATA_DIR / "undo"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _new_entry(kind: str, **meta) -> Path:
    base = undo_dir()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    n = 0
    while True:
        entry = base / f"{stamp}-{n:02d}"
        try:
            entry.mkdir()
            break
        except FileExistsError:
            n += 1
    _save_meta(entry, {"kind": kind, "time": time.strftime("%Y-%m-%d %H:%M:%S"), "undone": False, **meta})
    return entry


def _save_meta(entry: Path, meta: dict) -> None:
    (entry / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")


def _load_meta(entry: Path) -> dict:
    try:
        return json.loads((entry / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _prune_journal() -> None:
    entries = sorted(e for e in undo_dir().iterdir() if e.is_dir())
    for old in entries[:-UNDO_KEEP]:
        shutil.rmtree(old, ignore_errors=True)


def journal(limit: int = 10) -> list[dict]:
    out = []
    for e in sorted((e for e in undo_dir().iterdir() if e.is_dir()), reverse=True)[:limit]:
        m = _load_meta(e)
        if m:
            out.append({"id": e.name, **m})
    return out


def _backup(p: Path, entry: Path) -> None:
    if p.is_dir():
        shutil.copytree(p, entry / "before")
    else:
        shutil.copy2(p, entry / "before")


# ---------------------------------------------------------------- previews (what the heads and you are shown)
def _diff(old: str, new: str, name: str) -> str:
    lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(), f"{name} (now)", f"{name} (after)", lineterm="", n=2))
    text = "\n".join(lines)
    return text if len(text) <= 3500 else text[:3500] + "\n...[diff cut]..."


def _write_preview(ctx, path: str, content: str = "", **_) -> str:
    p = resolve(path)
    size = len(content.encode("utf-8", errors="replace"))
    if p.exists() and p.is_file():
        old = _read_text(p, MAX_READ_CHARS) if _is_text_file(p) else ""
        return (f"Replace the file {p} ({len(content.splitlines())} lines, {_human(size)}); the old version is kept for undo\n"
                f"{_diff(old, content, p.name)}")
    where = "" if p.parent.exists() else " (its folder will be created)"
    return f"Create the new file {p}{where} ({len(content.splitlines())} lines, {_human(size)})\n{content[:3000]}"


def _append_preview(ctx, path: str, content: str = "", **_) -> str:
    p = resolve(path)
    return f"Add {len(content.splitlines())} line(s) to the end of {p}\n{content[:3000]}"


def _move_preview(ctx, source: str, destination: str, **_) -> str:
    s, d = resolve(source), resolve(destination)
    return f"Move {'folder' if s.is_dir() else 'file'} {s} to {d}"


def _delete_preview(ctx, path: str, **_) -> str:
    p = resolve(path)
    if p.is_dir():
        size, files = _tree_stats(p)
        return f"Delete the folder {p} ({files} files, {_human(size)}); it is moved to Cerberus's undo folder, not destroyed"
    return f"Delete the file {p} ({_human(p.stat().st_size) if p.exists() else 'missing'}); it is moved to Cerberus's undo folder, not destroyed"


def _mkdir_preview(ctx, path: str, **_) -> str:
    return f"Create the folder {resolve(path)}"


# ---------------------------------------------------------------- guards (run before the heads or the user are asked)
def _guard_read(ctx: ToolContext, path: str = ".", **_) -> None:
    _require_on()
    check_read(resolve(path))


def _guard_write(ctx: ToolContext, path: str = "", content: str = "", **_) -> None:
    _require_on()
    p = resolve(path)
    check_write(p)
    if not isinstance(content, str):
        raise Denied("content must be text.")
    if len(content.encode("utf-8", errors="replace")) > MAX_WRITE_BYTES:
        raise Denied(f"That is more than {_human(MAX_WRITE_BYTES)} of text; write it in smaller pieces.")
    guard.assert_space(p, len(content.encode("utf-8", errors="replace")))
    if p.exists():
        if p.is_dir():
            raise Denied(f"'{p}' is a folder, not a file.")
        if not _is_text_file(p):
            raise Denied(f"'{p.name}' is not a text file; Cerberus only edits text files.")
        if p.stat().st_size > MAX_BACKUP_BYTES:
            raise Denied("That file is too large to keep an undo copy of, so it will not be changed.")


def _guard_move(ctx: ToolContext, source: str = "", destination: str = "", **_) -> None:
    _require_on()
    s, d = resolve(source), resolve(destination)
    if not s.exists():
        raise Denied(f"'{s}' does not exist.")
    check_write(s, destructive=True)
    if d.is_dir():                                  # moving into a folder keeps the name
        d = d / s.name
    check_write(d)
    if d.exists():
        raise Denied(f"'{d}' already exists. Cerberus never overwrites by moving; pick another name or move the old one first.")
    if _under(d, s):
        raise Denied("A folder cannot be moved into itself.")


def _guard_delete(ctx: ToolContext, path: str = "", **_) -> None:
    _require_on()
    p = resolve(path)
    if not p.exists():
        raise Denied(f"'{p}' does not exist.")
    check_write(p, destructive=True)
    if p.is_dir():
        size, files = _tree_stats(p)
        if size > MAX_TREE_BYTES or files > MAX_TREE_FILES:
            raise Denied("That folder is too big for Cerberus to remove safely; do this one yourself.")
    elif p.stat().st_size > MAX_BACKUP_BYTES:
        raise Denied("That file is too large to keep an undo copy of, so it will not be deleted.")


def _guard_mkdir(ctx: ToolContext, path: str = "", **_) -> None:
    _require_on()
    check_write(resolve(path))


# ---------------------------------------------------------------- reading tools
@tool("pc_info", "Computer", "Describe this computer: hardware, drives, and where the user's main folders are.",
      risk="safe", local_only=True, preflight=lambda ctx, **_: _require_on())
def pc_info(ctx: ToolContext) -> str:
    hw = hardware.detect(config.DATA_DIR)
    home = Path.home()
    folders = [str(f) for f in (home / "Desktop", home / "Documents", home / "Downloads", home / "Pictures",
                                _env_dir("OneDrive") or home / "OneDrive") if f.exists()]
    drives = []
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        root = f"{letter}:\\"
        if os.path.exists(root):
            try:
                u = shutil.disk_usage(root)
                drives.append(f"{root} {_human(u.free)} free of {_human(u.total)}")
            except OSError:
                drives.append(root)
    return (f"{hardware.describe(hw)}\nFree memory now: {hw['ram_free_gb']} GB\nDrives: " + "; ".join(drives) +
            "\nMain folders: " + "; ".join(folders) + f"\nCerberus workspace: {_workspace()}")


@tool("pc_list", "Computer", "List the files and folders inside a folder anywhere on this computer (with sizes).",
      {"path": "folder path, e.g. C:\\Users\\name\\Documents", "max_items": "number 1-300, default 100 (optional)"},
      risk="safe", local_only=True, preflight=_guard_read)
def pc_list(ctx: ToolContext, path: str, max_items: int = 100) -> str:
    p = resolve(path)
    if not p.exists():
        return f"'{p}' does not exist."
    if not p.is_dir():
        return f"'{p}' is a file ({_human(p.stat().st_size)}), not a folder."
    limit = max(1, min(int(max_items), 300))
    rows = []
    try:
        entries = sorted(os.scandir(p), key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
    except PermissionError:
        return f"Windows does not let Cerberus open '{p}' (no permission)."
    for e in entries:
        try:
            check_read(Path(e.path))
        except Denied:
            continue
        try:
            rows.append(f"[folder] {e.name}" if e.is_dir(follow_symlinks=False) else f"{e.name}  ({_human(e.stat().st_size)})")
        except OSError:
            rows.append(f"{e.name}  (unreadable)")
    more = f"\n...and {len(rows) - limit} more" if len(rows) > limit else ""
    return f"{p}  ({len(rows)} items)\n" + "\n".join(rows[:limit]) + more


@tool("pc_read", "Computer", "Read a text file anywhere on this computer (documents, code, notes, configs).",
      {"path": "file path", "max_chars": "number 100-200000, default 8000 (optional)"},
      risk="safe", local_only=True, preflight=_guard_read)
def pc_read(ctx: ToolContext, path: str, max_chars: int = 8000) -> str:
    p = resolve(path)
    if not p.exists():
        return f"'{p}' does not exist."
    if p.is_dir():
        return f"'{p}' is a folder; use pc_list."
    size = p.stat().st_size
    if not _is_text_file(p):
        return f"'{p.name}' is a binary file ({_human(size)}); Cerberus can only read text files."
    limit = max(100, min(int(max_chars), MAX_READ_CHARS))
    text = _read_text(p, limit)
    cut = f"\n...[showing the first {limit} characters of {_human(size)}]" if len(text) > limit else ""
    return text[:limit] + cut


@tool("pc_find", "Computer", "Search a folder tree for files by name (wildcards ok) and optionally by words inside them.",
      {"root": "folder to search in", "name": "file name or pattern, e.g. *.docx or invoice",
       "contains": "text that must appear inside the file (optional)", "max_results": "number 1-100, default 30 (optional)"},
      risk="safe", local_only=True, preflight=lambda ctx, root=".", **_: _guard_read(ctx, root))
def pc_find(ctx: ToolContext, root: str, name: str = "*", contains: str = "", max_results: int = 30) -> str:
    base = resolve(root)
    if not base.is_dir():
        return f"'{base}' is not a folder."
    pattern = name if any(c in name for c in "*?[") else f"*{name}*"
    limit = max(1, min(int(max_results), 100))
    deadline, found, scanned = time.time() + 12, [], 0
    needle = contains.lower()
    for dirpath, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS and not any(_under(Path(dirpath) / d, t) for t in _secret_trees())]
        for f in files:
            scanned += 1
            if not fnmatch.fnmatch(f.lower(), pattern.lower()):
                continue
            fp = Path(dirpath) / f
            try:
                check_read(fp)
                if needle:
                    if fp.stat().st_size > 2_000_000 or not _is_text_file(fp) or needle not in _read_text(fp, 500_000).lower():
                        continue
                found.append(f"{fp}  ({_human(fp.stat().st_size)})")
            except (Denied, OSError):
                continue
            if len(found) >= limit:
                break
        if len(found) >= limit or time.time() > deadline:
            break
    note = "" if time.time() <= deadline else "\n(stopped after 12 seconds; narrow the folder for a fuller search)"
    return (f"{len(found)} match(es) after looking at {scanned} files:\n" + "\n".join(found) + note) if found \
        else f"Nothing matched '{name}' after looking at {scanned} files.{note}"


@tool("pc_history", "Computer", "Show the most recent changes Cerberus made to the computer (and which can be undone).",
      risk="safe", local_only=True)
def pc_history(ctx: ToolContext) -> str:
    rows = journal(10)
    if not rows:
        return "No changes have been made yet."
    return "\n".join(f"{r['id']}  {r['time']}  {r['kind']}  {r.get('path') or r.get('source', '')}"
                     + ("  (undone)" if r.get("undone") else "") for r in rows)


# ---------------------------------------------------------------- changing tools (council + you must approve)
def _newline_style(p: Path, content: str) -> str:
    try:
        if p.exists() and b"\r\n" in p.read_bytes()[:20000]:
            return content.replace("\r\n", "\n").replace("\n", "\r\n")
    except OSError:
        pass
    return content


def _atomic_write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.cerberus-tmp")
    tmp.write_text(content, encoding="utf-8", newline="")
    os.replace(tmp, p)


@tool("pc_write", "Computer", "Create a text file, or replace one, anywhere allowed on this computer. Gives the complete new content.",
      {"path": "file path", "content": "the complete new text of the file"},
      risk="write", local_only=True, council=True, preflight=_guard_write, preview=_write_preview)
def pc_write(ctx: ToolContext, path: str, content: str) -> str:
    p = resolve(path)
    existed = p.exists()
    entry = _new_entry("write", path=str(p), existed=existed)
    try:
        if existed:
            _backup(p, entry)
        _atomic_write(p, _newline_style(p, content))
    except Exception:
        shutil.rmtree(entry, ignore_errors=True)
        raise
    _prune_journal()
    ctx.gate.audit("pc_write", "wrote", {"path": str(p), "undo": entry.name})
    return f"{'Replaced' if existed else 'Created'} {p}. Undo id: {entry.name}"


@tool("pc_append", "Computer", "Add text to the end of a text file (creates it if missing).",
      {"path": "file path", "content": "the text to add"},
      risk="write", local_only=True, council=True, preflight=_guard_write, preview=_append_preview)
def pc_append(ctx: ToolContext, path: str, content: str) -> str:
    p = resolve(path)
    existed = p.exists()
    entry = _new_entry("append", path=str(p), existed=existed)
    try:
        if existed:
            _backup(p, entry)
        old = _read_text(p, MAX_READ_CHARS * 5) if existed else ""
        _atomic_write(p, _newline_style(p, old + content))
    except Exception:
        shutil.rmtree(entry, ignore_errors=True)
        raise
    _prune_journal()
    ctx.gate.audit("pc_append", "wrote", {"path": str(p), "undo": entry.name})
    return f"Added text to {p}. Undo id: {entry.name}"


@tool("pc_move", "Computer", "Move or rename a file or folder (never overwrites something that already exists).",
      {"source": "current path", "destination": "new path (or a folder to move it into)"},
      risk="write", local_only=True, council=True, preflight=_guard_move, preview=_move_preview)
def pc_move(ctx: ToolContext, source: str, destination: str) -> str:
    s, d = resolve(source), resolve(destination)
    if d.is_dir():
        d = d / s.name
    d.parent.mkdir(parents=True, exist_ok=True)
    entry = _new_entry("move", source=str(s), path=str(d))
    try:
        shutil.move(str(s), str(d))
    except Exception:
        shutil.rmtree(entry, ignore_errors=True)
        raise
    _prune_journal()
    ctx.gate.audit("pc_move", "moved", {"from": str(s), "to": str(d), "undo": entry.name})
    return f"Moved {s} to {d}. Undo id: {entry.name}"


@tool("pc_delete", "Computer", "Delete a file or folder. It is moved to Cerberus's undo folder, so it can be brought back.",
      {"path": "file or folder path"},
      risk="write", local_only=True, council=True, preflight=_guard_delete, preview=_delete_preview)
def pc_delete(ctx: ToolContext, path: str) -> str:
    p = resolve(path)
    entry = _new_entry("delete", path=str(p), was_dir=p.is_dir())
    try:
        shutil.move(str(p), str(entry / "item"))
    except Exception:
        shutil.rmtree(entry, ignore_errors=True)
        raise
    _prune_journal()
    ctx.gate.audit("pc_delete", "removed", {"path": str(p), "undo": entry.name})
    return f"Deleted {p} (kept in the undo folder). Undo id: {entry.name}"


@tool("pc_mkdir", "Computer", "Create a folder (and any missing parent folders).",
      {"path": "folder path"},
      risk="write", local_only=True, council=True, preflight=_guard_mkdir, preview=_mkdir_preview)
def pc_mkdir(ctx: ToolContext, path: str) -> str:
    p = resolve(path)
    if p.exists():
        return f"{p} already exists."
    entry = _new_entry("mkdir", path=str(p))
    p.mkdir(parents=True, exist_ok=True)
    _prune_journal()
    ctx.gate.audit("pc_mkdir", "made", {"path": str(p), "undo": entry.name})
    return f"Created folder {p}. Undo id: {entry.name}"


@tool("pc_undo", "Computer", "Undo one of Cerberus's earlier changes to the computer (the most recent one unless an id is given).",
      {"id": "undo id from pc_history (optional)"}, risk="write", local_only=True,
      preflight=lambda ctx, **_: _require_on())
def pc_undo(ctx: ToolContext, id: str = "") -> str:
    rows = [r for r in journal(UNDO_KEEP) if not r.get("undone")]
    entry_meta = next((r for r in rows if not id or r["id"] == id), None)
    if not entry_meta:
        return "There is nothing to undo." if not id else f"No undoable change with id {id}."
    entry = undo_dir() / entry_meta["id"]
    kind, target = entry_meta["kind"], Path(entry_meta["path"])
    check_write(target, destructive=False)
    if kind in ("write", "append"):
        if target.exists():
            shutil.move(str(target), str(entry / "after"))
        if entry_meta.get("existed"):
            shutil.copy2(entry / "before", target)
        note = f"Restored the earlier version of {target}" if entry_meta.get("existed") else f"Removed the file {target} that was created"
    elif kind == "move":
        src = Path(entry_meta["source"])
        if src.exists() or not target.exists():
            return f"Cannot undo: {src} is occupied or {target} is gone."
        src.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target), str(src))
        note = f"Moved {target.name} back to {src}"
    elif kind == "delete":
        if target.exists():
            return f"Cannot undo: something already exists at {target}."
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(entry / "item"), str(target))
        note = f"Brought back {target}"
    elif kind == "mkdir":
        try:
            target.rmdir()
        except OSError:
            return f"Cannot undo: {target} is not empty any more."
        note = f"Removed the empty folder {target}"
    else:
        return "Unknown kind of change."
    entry_meta = {k: v for k, v in entry_meta.items() if k != "id"}
    entry_meta["undone"] = True
    _save_meta(entry, entry_meta)
    ctx.gate.audit("pc_undo", "undone", {"id": entry.name, "kind": kind})
    return note
