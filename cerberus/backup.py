"""Backup and restore: one zip with the database, settings (without secrets) and the audit log.

Secrets (API keys, tokens) are deliberately left out: they are sealed to this Windows account, and a backup that
can be copied to a USB stick or OneDrive should not carry them. After a restore you re-enter keys in the Gates tab.
A restore is staged and applied the next time Cerberus starts (before the database is opened), and the current
data is backed up automatically first.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path

from . import config, vault

MEMBERS = ("cerberus.db", "settings.json", "audit.log")
KEEP_AUTO = 7
MANIFEST = "manifest.json"


def backups_dir() -> Path:
    d = config.DATA_DIR / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _snapshot_db(src: str, dest: Path) -> None:
    """Consistent copy of a live SQLite file (the app may be writing to it)."""
    a, b = sqlite3.connect(src), sqlite3.connect(dest)
    try:
        a.backup(b)
    finally:
        b.close()
        a.close()


def create_backup(db, dest: Path | str | None = None, label: str = "backup") -> Path:
    """Write a backup zip. `db` is the open Database. Returns the zip path."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = Path(dest) if dest else backups_dir() / f"cerberus-{label}-{stamp}.zip"
    if target.is_dir():
        target = target / f"cerberus-{label}-{stamp}.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        snap = Path(tmp) / "cerberus.db"
        _snapshot_db(db.path, snap)
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(snap, "cerberus.db")
            z.writestr("settings.json", json.dumps(vault.strip(config.load_settings()), indent=2))
            if (config.DATA_DIR / "audit.log").exists():
                z.write((config.DATA_DIR / "audit.log"), "audit.log")
            z.writestr(MANIFEST, json.dumps({"app": "cerberus", "created": stamp, "secrets": "excluded"}))
    return target


def auto_backup(db) -> Path | None:
    """At most one automatic backup per day; keeps the newest KEEP_AUTO."""
    today = time.strftime("%Y%m%d")
    d = backups_dir()
    if any(d.glob(f"cerberus-auto-{today}-*.zip")):
        return None
    path = create_backup(db, label="auto")
    for old in sorted(d.glob("cerberus-auto-*.zip"))[:-KEEP_AUTO]:
        old.unlink(missing_ok=True)
    return path


def _validate(path: Path) -> None:
    if not path.is_file() or not zipfile.is_zipfile(path):
        raise ValueError("That is not a Cerberus backup (not a zip file).")
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        if "cerberus.db" not in names or MANIFEST not in names:
            raise ValueError("That zip is not a Cerberus backup (no database or manifest inside).")
        with tempfile.TemporaryDirectory() as tmp:
            z.extract("cerberus.db", tmp)
            try:
                con = sqlite3.connect(Path(tmp) / "cerberus.db")
                ok = con.execute("PRAGMA integrity_check").fetchone()[0]
                con.close()
            except sqlite3.DatabaseError as exc:
                raise ValueError(f"The database inside is damaged: {exc}")
            if ok != "ok":
                raise ValueError("The database inside the backup failed its integrity check.")


def pending_dir() -> Path:
    return config.DATA_DIR / "pending_restore"


def stage_restore(db, path: Path | str) -> str:
    """Validate the zip, take a safety backup of the current state, and queue the restore for the next start."""
    path = Path(path)
    _validate(path)
    safety = create_backup(db, label="before-restore")
    p = pending_dir()
    shutil.rmtree(p, ignore_errors=True)
    p.mkdir(parents=True)
    with zipfile.ZipFile(path) as z:
        for name in MEMBERS:                              # fixed names only: nothing in the zip picks its own path
            if name in z.namelist():
                (p / name).write_bytes(z.read(name))
    return f"Restore queued. Restart Cerberus to apply it. (Your current data was saved first as {safety.name}.)"


def apply_pending_restore() -> str | None:
    """Call at startup BEFORE the database is opened."""
    p = pending_dir()
    if not (p / "cerberus.db").exists():
        return None
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    for ext in ("", "-wal", "-shm"):
        (config.DATA_DIR / f"cerberus.db{ext}").unlink(missing_ok=True)
    shutil.copy2(p / "cerberus.db", config.DATA_DIR / "cerberus.db")
    if (p / "settings.json").exists():
        restored = json.loads((p / "settings.json").read_text(encoding="utf-8"))
        current = config.load_settings()
        merged = config._deep_merge(config.DEFAULT_SETTINGS, restored)
        for key in ("api_token",):                         # secrets in the backup are blank: keep this machine's own
            merged[key] = current.get(key, "")
        merged["github"]["token"] = current.get("github", {}).get("token", "")
        for pid, spec in (merged.get("cloud", {}).get("providers") or {}).items():
            spec["api_key"] = (current.get("cloud", {}).get("providers", {}).get(pid) or {}).get("api_key", "")
        config.save_settings(merged)
    if (p / "audit.log").exists():
        shutil.copy2(p / "audit.log", (config.DATA_DIR / "audit.log"))
    shutil.rmtree(p, ignore_errors=True)
    return "Restored your backup."


def startup_housekeeping() -> list[str]:
    """Run once at launch, before the server and database start: move legacy data, apply a queued restore."""
    notes = []
    for step in (config.migrate_legacy_data, apply_pending_restore):
        try:
            msg = step()
        except Exception as exc:                          # report it; never block the app from opening
            msg = f"{step.__name__} failed: {exc}"
        if msg:
            notes.append(msg)
            print(msg)
    return notes
