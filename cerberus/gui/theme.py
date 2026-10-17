"""Look & feel: a dark, blood-and-gold gothic palette plus the bundled blackletter fonts."""
from __future__ import annotations

import ctypes
import sys

from ..config import ASSETS_DIR

# palette
BG = "#0a0608"          # near-black with a hint of wine
PANEL = "#150d12"
PANEL2 = "#1d1219"
EDGE = "#3d1a22"
BLOOD = "#a3151f"
BLOOD_DK = "#5c0d14"
BLOOD_HI = "#d12a36"
GOLD = "#c9a24a"
GOLD_DK = "#7d6428"
TEXT = "#e8dfd0"
DIM = "#8d8294"
OK = "#5fae6b"
WARN = "#d9a441"
BONE = "#f3ead7"

# font roles -> candidate family names (first one installed wins)
FAMILIES = {
    "title": ["UnifrakturCook", "UnifrakturMaguntia", "Old English Text MT", "Georgia"],
    "head": ["Cinzel Decorative", "Cinzel", "Georgia"],
    "body": ["IM FELL English", "IM Fell English", "IM FELL English PRO", "Georgia"],
    "mono": ["Cascadia Mono", "Consolas", "Courier New"],
}
FONT_FILES = ["UnifrakturCook-Bold.ttf", "CinzelDecorative-Bold.ttf", "CinzelDecorative-Regular.ttf", "IMFeENrm28P.ttf"]

_resolved: dict[str, str] = {}


def load_fonts() -> int:
    """Register the bundled fonts for this process only (Windows). Returns how many loaded."""
    if sys.platform != "win32":
        return 0
    loaded = 0
    FR_PRIVATE = 0x10
    for name in FONT_FILES:
        path = ASSETS_DIR / "fonts" / name
        if path.exists() and ctypes.windll.gdi32.AddFontResourceExW(str(path), FR_PRIVATE, 0):
            loaded += 1
    return loaded


def resolve_fonts(root) -> dict[str, str]:
    """Pick the first installed family for each role (call once a Tk root exists)."""
    import tkinter.font as tkfont
    installed = {f.lower(): f for f in tkfont.families(root)}
    for role, options in FAMILIES.items():
        _resolved[role] = next((installed[o.lower()] for o in options if o.lower() in installed), options[-1])
    return dict(_resolved)


def family(role: str) -> str:
    return _resolved.get(role) or FAMILIES[role][-1]


def font(role: str, size: int = 14, weight: str = "normal", slant: str = "roman"):
    import customtkinter as ctk
    return ctk.CTkFont(family=family(role), size=size, weight=weight, slant=slant)


def button_style(kind: str = "normal") -> dict:
    """Common CTkButton colours: normal (wine), gold (outlined), danger (blood red)."""
    if kind == "gold":
        return dict(fg_color="transparent", border_width=1, border_color=GOLD_DK, text_color=GOLD, hover_color=PANEL2)
    if kind == "danger":
        return dict(fg_color=BLOOD, hover_color=BLOOD_HI, text_color=BONE)
    return dict(fg_color=BLOOD_DK, hover_color=BLOOD, text_color=BONE)
