"""Plugins: drop a .py file in the plugins folder and its tools appear in Cerberus after a restart.

The folder is  <data folder>/plugins  (Gates tab -> Plugins shows the exact path). Files starting with '_' are ignored; a copy of
the template is placed there for you. A plugin that fails to load is skipped and reported; it can never stop Cerberus starting.
Plugins are ordinary Python run with your own permissions, so only install ones you wrote or trust. Cerberus's own tools
cannot write into this folder (it is on the hard-protected list), so a model cannot install a plugin by itself.
"""
from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

from . import config
from .tools import REGISTRY

TEMPLATE = config.ROOT / "plugins" / "_template.py"
LOADED: list[dict] = []


def plugins_dir() -> Path:
    d = config.DATA_DIR / "plugins"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_plugins() -> list[dict]:
    """Import every plugin once. Returns [{'file','ok','tools','error'}]."""
    LOADED.clear()
    folder = plugins_dir()
    if TEMPLATE.exists() and not (folder / "_template.py").exists():
        try:
            shutil.copy2(TEMPLATE, folder / "_template.py")
        except OSError:
            pass
    for path in sorted(folder.glob("*.py")):
        if path.name.startswith("_"):
            continue
        before = set(REGISTRY)
        entry = {"file": path.name, "ok": True, "tools": [], "error": ""}
        try:
            spec = importlib.util.spec_from_file_location(f"cerberus_plugin_{path.stem}", path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            entry["tools"] = sorted(set(REGISTRY) - before)
        except Exception as exc:                       # a broken plugin must not break Cerberus
            entry.update(ok=False, error=f"{type(exc).__name__}: {exc}")
            for name in set(REGISTRY) - before:        # drop half-registered tools
                REGISTRY.pop(name, None)
        LOADED.append(entry)
    return list(LOADED)


def skills() -> list[str]:
    """Skill names that plugins registered, so the Hand can use them."""
    return sorted({REGISTRY[t].skill for e in LOADED for t in e["tools"] if t in REGISTRY})


def status() -> dict:
    return {"folder": str(plugins_dir()), "plugins": list(LOADED)}
