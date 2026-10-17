"""Settings: where files live, defaults, and thread-safe load/save of settings.json.

Everything private (API keys, profile, chat history) lives in the data folder, which is OUTSIDE the project and
outside OneDrive: %LOCALAPPDATA%\\Cerberus on Windows (override with the CERBERUS_DATA_DIR environment variable).
Secrets inside settings.json are encrypted with Windows DPAPI (see vault.py).
"""
from __future__ import annotations

import copy
import json
import os
import secrets
import shutil
import sys
import threading
from pathlib import Path

from . import vault

if getattr(sys, "frozen", False):                      # PyInstaller build: bundled files live under _MEIPASS
    ROOT = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
else:
    ROOT = Path(__file__).resolve().parent.parent
LEGACY_DATA_DIR = ROOT / "data"


def _default_data_dir() -> Path:
    if os.environ.get("CERBERUS_DATA_DIR"):
        return Path(os.environ["CERBERUS_DATA_DIR"]).expanduser()
    if os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "Cerberus"
    if sys.platform == "win32":                         # some launchers strip the variable; the folder is always here
        return Path.home() / "AppData" / "Local" / "Cerberus"
    return LEGACY_DATA_DIR


DATA_DIR = _default_data_dir()
ASSETS_DIR = ROOT / "assets"
DEFAULT_WORKSPACE = (Path(sys.executable).parent if getattr(sys, "frozen", False) else ROOT) / "workspace"
SETTINGS_FILE = DATA_DIR / "settings.json"
DB_FILE = DATA_DIR / "cerberus.db"
AUDIT_LOG = DATA_DIR / "audit.log"

DEFAULT_PORT = 8777

DEFAULT_SETTINGS: dict = {
    # --- Ollama (local models) ---
    "ollama_url": "http://localhost:11434",
    # Each head is a "role"; the value is a model name or a prefix (e.g. "llama3" matches "llama3:latest").
    "models": {
        "reasoner": "deepseek-r1",   # the deep thinker
        "generalist": "llama3",      # the voice / builder
        "editor": "mistral",         # the quick critic / editor
    },
    "num_ctx": 8192,
    "keep_alive": "10m",
    # Per-mode overrides: {"coding": {"generalist": "qwen2.5-coder"}} swaps one head's model for that mode only.
    "mode_models": {},
    # How hard the heads work: "quick" = one head for small talk, no checking; "careful" = all three heads, and the two
    # that did not write the answer check it and make the writer fix mistakes; "meticulous" = that, plus every step is checked.
    "thinking": "careful",
    # Let the heads read/edit files anywhere on this computer (each change needs ALL THREE heads to agree, plus your OK).
    "full_access": True,
    # Set once the first-launch hardware tuning has run (it never overrides what you change afterwards).
    "auto_tuned": False,
    # Side projects Cerberus may run for you: {"Name": {"path": "C:\\...", "run": "python main.py", "about": "..."}}
    "projects": {},
    # Semantic memory: an Ollama embedding model (ollama pull nomic-embed-text). Falls back to keywords if missing.
    "embed_model": "nomic-embed-text",
    # Screen reading: an Ollama vision model (ollama pull qwen2.5vl:7b / llama3.2-vision / llava / moondream).
    "vision_model": "qwen2.5vl",
    # Browser automation drives your installed Microsoft Edge; False = you can watch it work.
    "browser_headless": False,
    # Proactive rituals (06:00 briefing, 17:00 Mind & Soul, 20:00 reading, blackout warnings). `off` lists ritual ids to silence.
    "rituals": {"enabled": True, "off": [], "use_model": True},
    # Hardware safety limits (clamped in guard.py; the guard itself cannot be switched off).
    "guard": {"gpu_warm": 78, "gpu_hot": 86, "min_free_gb": 1.5, "min_disk_gb": 5.0, "child_mem_gb": 8.0, "child_procs": 64},
    # show_workings: show the stage titles, thinking, council notes and technical detail in chat (off = answers only).
    "show_workings": False,
    # Review log of every run (timings, tools, errors) in DATA_DIR/logs. `previews` keeps ~200 chars of each request/answer.
    "telemetry": {"enabled": True, "previews": True, "keep_months": 6},
    # Resume Studio (the Resume project, wired into Hunt). Edited through PUT /api/resume (local only).
    "resume": {"path": "", "feed_urls": [], "base_resume": "",
               "applicant": {"name": "", "email": "", "phone": "", "linkedin": ""}},
    # The Warden: watches games and time-sink sites, nags, shames, tallies, and closes them. Edited only through
    # PUT /api/warden/settings (local only), never by the AI's tools.
    "warden": {
        "enabled": True,
        "games": ["eldenring.exe", "fortniteclient-win64-shipping.exe", "valorant-win64-shipping.exe", "leagueclient.exe",
                  "league of legends.exe", "dota2.exe", "cs2.exe", "csgo.exe", "overwatch.exe", "minecraft.windows.exe",
                  "gta5.exe", "rocketleague.exe", "apexlegends.exe", "r5apex.exe", "witcher3.exe",
                  "bg3.exe", "bg3_dx11.exe", "hollow knight.exe", "terraria.exe", "stardew valley.exe",
                  "destiny2.exe", "starfield.exe", "fallout4.exe", "skyrimse.exe", "rdr2.exe", "helldivers2.exe"],
        "game_paths": ["\\steamapps\\common\\", "\\epic games\\", "\\gog galaxy\\games\\", "\\riot games\\", "\\xboxgames\\"],
        "sites": ["YouTube", "Twitch", "Reddit", "/ X", "Facebook", "Instagram", "TikTok", "Netflix", "Hulu", "Disney+",
                  "Prime Video", "Crunchyroll", "Imgur", "9GAG"],
        "apps_ignore": ["cyberpunk2077.exe"],
        "grace_minutes": 10,
        "gaps_minutes": [7, 5, 4, 3, 2, 2, 1],
        "final_level": 5,
        "reply_seconds": 120,
        "final_seconds": 45,
        "reset_minutes": 10,
        "away_minutes": 5,
        "max_excuses_per_day": 3,
        "max_excuse_minutes": 60,
    },
    # --- Cloud handoff (frontier models) ---
    "cloud": {
        "enabled": False,
        "provider": "openai",
        "max_handoff_tokens": 1500,
        "providers": {},  # filled lazily: {"openai": {"api_key": "", "model": "gpt-4o"}, ...}
    },
    # --- GitHub / VS Code ---
    "github": {"token": "", "username": "", "git_name": "", "git_email": ""},
    "vscode_command": "code",
    # --- Files the AI may touch ---
    "workspace": str(DEFAULT_WORKSPACE),
    # --- Learning about you ---
    "learning_enabled": True,
    "user_name": "",
    # --- Server ---
    "server": {"host": "127.0.0.1", "port": DEFAULT_PORT, "lan": False},
    "api_token": "",
    # --- UI ---
    "mode": "conversation",
}

_lock = threading.RLock()
_cache: dict | None = None


def _deep_merge(base: dict, over: dict) -> dict:
    """Return base updated with over, recursing into nested dicts."""
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_settings(force: bool = False) -> dict:
    """Load settings from disk (cached). Missing keys are filled from defaults."""
    global _cache
    with _lock:
        if _cache is not None and not force:
            return _cache
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        stored: dict = {}
        if SETTINGS_FILE.exists():
            try:
                stored = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                stored = {}
        upgrade = vault.has_plaintext(stored)               # keys saved by an older version: seal them now
        _cache = _deep_merge(DEFAULT_SETTINGS, vault.unseal(stored))
        if not _cache.get("api_token"):
            _cache["api_token"] = secrets.token_urlsafe(24)
            upgrade = True
        if upgrade:
            _write(_cache)
        return _cache


def _write(settings: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(vault.seal(settings), indent=2), encoding="utf-8")
    tmp.replace(SETTINGS_FILE)


def save_settings(settings: dict) -> None:
    global _cache
    with _lock:
        _cache = settings
        _write(settings)


def update_settings(patch: dict, replace: tuple = ()) -> dict:
    """Merge `patch` into the settings, save, and return the new settings. Keys in `replace` overwrite instead of merging."""
    with _lock:
        base = copy.deepcopy(load_settings())
        for key in replace:
            base.pop(key, None)
        merged = _deep_merge(base, patch)
        save_settings(merged)
        return merged


def get_workspace() -> Path:
    ws = Path(load_settings().get("workspace") or DEFAULT_WORKSPACE)
    ws.mkdir(parents=True, exist_ok=True)
    return ws.resolve()


def migrate_legacy_data() -> str | None:
    """One-time move of the old <project>/data folder to the data folder. Copies (never deletes), then renames the old
    folder to data.migrated so the plaintext keys in it are not mistaken for live data. Returns a message, or None."""
    legacy = LEGACY_DATA_DIR
    if legacy.resolve() == DATA_DIR.resolve() or not legacy.is_dir():
        return None
    if (DATA_DIR / "cerberus.db").exists() or (DATA_DIR / "settings.json").exists():
        return None
    if not (legacy / "cerberus.db").exists() and not (legacy / "settings.json").exists():
        return None
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for item in legacy.iterdir():
        if item.suffix == ".png":                      # old smoke-test screenshots
            continue
        dest = DATA_DIR / item.name
        if item.is_dir():
            shutil.copytree(item, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dest)
    for name in ("cerberus.db", "settings.json"):
        if (legacy / name).exists() and (DATA_DIR / name).stat().st_size != (legacy / name).stat().st_size:
            raise OSError(f"Copy of {name} to {DATA_DIR} is incomplete; the old folder was left untouched.")
    global _cache
    with _lock:
        _cache = None
        load_settings()                                # re-saves the settings with the secrets sealed
    note = f"Moved your data from {legacy} to {DATA_DIR}."
    try:
        legacy.rename(legacy.with_name("data.migrated"))
        note += " The old folder is now 'data.migrated' - delete it once you are happy (it holds your old plaintext keys)."
    except OSError:
        note += " Could not rename the old folder; delete it yourself (it holds your old plaintext keys)."
    return note
