"""The operator profile: who Cerberus works for, and how it talks to them.

Cerberus ships with a neutral example profile (cerberus/seed/profile.example.json). To make it yours, put a `profile.json`
in the Cerberus data folder (or point CERBERUS_PROFILE at a file); see docs/PROFILE.md. Load order:

    1. the file named by the CERBERUS_PROFILE environment variable
    2. <data folder>/profile.json
    3. the bundled example profile

On first launch (and whenever the profile's "version" is raised) the profile is seeded: its dossier text is stored in the
database, its facts go into the Soul Ledger (never overwriting a fact you already edited), and the user name is set if blank.
The profile also supplies the "voice" instructions (honorifics, bluntness, blackout hours) added to what Cerberus says.
Personal data lives only in that profile file, outside the repository.
"""
from __future__ import annotations

import json
import os
import random
import time
from pathlib import Path
from typing import Optional

from . import config
from .db import Database

SEED_VERSION = 1
EXAMPLE_FILE = Path(__file__).parent / "seed" / "profile.example.json"
DEFAULT_GOALS = [("BODY", "develop your body: work out"), ("MIND", "develop your mind: study"),
                 ("SOUL", "develop your soul: make something of your own")]
DEFAULT_VOICE = [
    "Be their companion and constructive rival: direct, a little dry, with real loyalty and respect.",
    "Keep chat replies to 1-2 punchy sentences unless they ask for a deep-dive, document, code or architecture breakdown.",
    "Never coddle or flatter. Challenge weak premises and excuses, point out blind spots, push them to execute.",
]
_cache: dict = {"key": None, "profile": None}


def profile_path() -> Path:
    """The profile file in use (the bundled example when you have not made your own)."""
    env = os.environ.get("CERBERUS_PROFILE")
    if env and Path(env).expanduser().is_file():
        return Path(env).expanduser()
    own = Path(config.DATA_DIR) / "profile.json"
    return own if own.is_file() else EXAMPLE_FILE


def _read(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("profile must be a JSON object")
    dossier = data.get("dossier") or ""
    if not dossier and data.get("dossier_file"):
        try:
            dossier = (path.parent / str(data["dossier_file"])).read_text(encoding="utf-8-sig")
        except OSError:
            dossier = ""
    facts = []
    for item in data.get("facts") or []:
        if isinstance(item, (list, tuple)) and len(item) == 3 and all(isinstance(x, str) for x in item):
            facts.append((item[0], item[1], item[2]))
    goals = [(str(g[0]), str(g[1])) for g in (data.get("goals") or []) if isinstance(g, (list, tuple)) and len(g) == 2][:3]
    name = str(data.get("name") or "").strip()
    return {
        "version": int(data.get("version") or 1),
        "name": name,
        "plain_name": str(data.get("plain_name") or name.split(" ")[0] or "friend"),
        "honorifics": [str(h) for h in (data.get("honorifics") or []) if str(h).strip()] or ["Boss"],
        "facts": facts,
        "dossier": dossier,
        "voice": [str(v) for v in (data.get("voice") or []) if str(v).strip()] or list(DEFAULT_VOICE),
        "goals": goals if len(goals) == 3 else list(DEFAULT_GOALS),
        "goal_words": [str(w).lower() for w in (data.get("goal_words") or [])],
        "core_fact_keys": set(data.get("core_fact_keys") or ["name"]),
    }


def profile() -> dict:
    """The active profile, re-read when the file changes. A broken personal profile falls back to the example."""
    path = profile_path()
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        key = (str(path), 0)
    if _cache["key"] != key:
        try:
            prof = _read(path)
        except (OSError, ValueError):
            prof = _read(EXAMPLE_FILE)
        _cache.update(key=key, profile=prof)
    return _cache["profile"]


def honorifics() -> list[str]:
    return profile()["honorifics"]


def plain_name() -> str:
    return profile()["plain_name"]


def goals() -> list[tuple[str, str]]:
    return profile()["goals"]


def goal_words() -> tuple[str, ...]:
    return tuple(profile()["goal_words"])


def core_fact_keys() -> set:
    return profile()["core_fact_keys"]


# ---------------------------------------------------------------- seeding
def seed(db: Database) -> bool:
    """Load the profile into the database if this version has not been loaded yet. Returns True if it did."""
    prof = profile()
    version = max(SEED_VERSION, prof["version"])
    if db.kv_get("seed_version", 0) >= version:
        return False
    if prof["dossier"]:
        db.kv_set("dossier", prof["dossier"])
    now = time.time()
    for key, value, category in prof["facts"]:
        if not db.one("SELECT id FROM facts WHERE key=?", (key,)):
            db.execute("INSERT INTO facts(key,value,category,confidence,source,first_seen,last_seen) VALUES(?,?,?,?,?,?,?)",
                       (key, value, category, 1.0, "dossier", now, now))
    if prof["name"] and not config.load_settings().get("user_name"):
        config.update_settings({"user_name": prof["name"]})
    db.kv_set("seed_version", version)
    return True


# ---------------------------------------------------------------- voice
def is_blackout(now: Optional[time.struct_time] = None) -> bool:
    """True during the weekday blackout: 22:00 on a night before a workday, or before 05:00 on a workday morning."""
    t = now or time.localtime()
    if t.tm_hour >= 22:
        return t.tm_wday in (6, 0, 1, 2, 3)   # Sun-Thu night (Mon=0)
    if t.tm_hour < 5:
        return t.tm_wday in (0, 1, 2, 3, 4)   # Mon-Fri early morning
    return False


def voice_block(now: Optional[time.struct_time] = None, rng: Optional[random.Random] = None) -> str:
    """Instructions that make every reply sound like Cerberus talking to its user."""
    rng = rng or random
    prof = profile()
    t = now or time.localtime()
    titles = prof["honorifics"]
    picks = ", ".join(f'"{h}"' for h in rng.sample(titles, min(3, len(titles))))
    lines = [
        "HOW TO TALK TO THIS USER (applies to what you say to them; never put it inside code, files, or documents written for other people):",
        f"- Address them with a title. Pick one such as {picks}, or invent a fun variation. Use one title per reply, naturally, and vary them.",
        f"- When you are being plain and blunt with no sugar-coating, call them {prof['plain_name']} instead.",
        *[f"- {line}" for line in prof["voice"]],
    ]
    if is_blackout(t):
        lines.append(f"- BLACKOUT: it is {time.strftime('%I:%M %p', t)}, inside their weekday 10 PM-5 AM screen blackout. Answer in ONE "
                     "short sentence at most, start no new tasks, and tell them to put the damn phone down and go to sleep.")
    return "\n".join(lines)
