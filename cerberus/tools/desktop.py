"""Desktop automation with pyautogui. EVERY action here is risk="control" and local_only:

 * it always asks for approval unless Autonomous Agent Mode is on,
 * it is refused outright when the request came from another device,
 * pyautogui's FAILSAFE is on: slam the mouse into any screen corner to abort instantly.
"""
from __future__ import annotations

import time
from datetime import datetime

from ..config import get_workspace
from .base import tool

_pg = None


def pg():
    """Import pyautogui lazily (it needs a real screen) and configure its safety features."""
    global _pg
    if _pg is None:
        import pyautogui
        pyautogui.FAILSAFE = True   # mouse to a corner = emergency stop
        pyautogui.PAUSE = 0.4       # small pause between actions so you can see what happens
        _pg = pyautogui
    return _pg


def _check_xy(x: int, y: int) -> None:
    w, h = pg().size()
    if not (0 <= x < w and 0 <= y < h):
        raise ValueError(f"Coordinates ({x},{y}) are off-screen (screen is {w}x{h}).")


@tool("screen_info", "Screen Control", "Get the screen size and current mouse position.", risk="safe", local_only=True)
def screen_info(ctx) -> str:
    w, h = pg().size()
    x, y = pg().position()
    return f"Screen {w}x{h}; mouse at ({x},{y})."


@tool("screenshot", "Screen Control", "Save a screenshot into workspace/screenshots and return its path.",
      risk="control", local_only=True)
def screenshot(ctx) -> str:
    out = get_workspace() / "screenshots"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"screen_{datetime.now():%Y%m%d_%H%M%S}.png"
    pg().screenshot(str(path))
    return f"Saved {path}"


@tool("mouse_move", "Screen Control", "Move the mouse pointer.",
      {"x": "pixels from left", "y": "pixels from top"}, risk="control", local_only=True)
def mouse_move(ctx, x: int, y: int) -> str:
    _check_xy(int(x), int(y))
    pg().moveTo(int(x), int(y), duration=0.4)
    return f"Moved to ({x},{y})."


@tool("click", "Screen Control", "Click the mouse at a screen position.",
      {"x": "pixels from left", "y": "pixels from top", "button": "left|right (optional)",
       "clicks": "1 or 2 (optional)"}, risk="control", local_only=True)
def click(ctx, x: int, y: int, button: str = "left", clicks: int = 1) -> str:
    _check_xy(int(x), int(y))
    if button not in ("left", "right"):
        raise ValueError("button must be left or right")
    pg().click(int(x), int(y), clicks=max(1, min(int(clicks), 2)), button=button)
    return f"Clicked {button} x{clicks} at ({x},{y})."


@tool("type_text", "Screen Control", "Type text with the keyboard into whatever window is focused.",
      {"text": "text to type (max 2000 chars)"}, risk="control", local_only=True)
def type_text(ctx, text: str) -> str:
    if len(text) > 2000:
        raise ValueError("Text too long (max 2000 characters).")
    pg().write(text, interval=0.02)
    return f"Typed {len(text)} characters."


@tool("press_keys", "Screen Control", "Press a key or a combination, e.g. 'enter' or 'ctrl+s'.",
      {"keys": "keys joined by +"}, risk="control", local_only=True)
def press_keys(ctx, keys: str) -> str:
    parts = [k.strip().lower() for k in keys.split("+") if k.strip()]
    bad = [k for k in parts if k not in pg().KEYBOARD_KEYS]
    if bad or not parts:
        raise ValueError(f"Unknown key(s): {bad or keys}")
    pg().hotkey(*parts)
    return f"Pressed {'+'.join(parts)}."


@tool("scroll", "Screen Control", "Scroll the mouse wheel (positive = up, negative = down).",
      {"amount": "clicks, e.g. -5"}, risk="control", local_only=True)
def scroll(ctx, amount: int) -> str:
    pg().scroll(max(-50, min(int(amount), 50)))
    return f"Scrolled {amount}."


@tool("autofill_form", "Form Autofill",
      "Fill a form: after a countdown, types each value then presses Tab to move to the next field. "
      "Click into the FIRST field during the countdown.",
      {"values": "list of strings, one per field in tab order", "countdown": "seconds to wait, default 6 (optional)",
       "submit": "true to press Enter at the end (optional, default false)"}, risk="control", local_only=True)
def autofill_form(ctx, values: list, countdown: int = 6, submit: bool = False) -> str:
    if not isinstance(values, list) or not values or len(values) > 60:
        raise ValueError("values must be a list of 1-60 strings.")
    p = pg()
    time.sleep(max(3, min(int(countdown), 30)))  # you click the first field now
    for i, v in enumerate(values):
        if ctx.cancel is not None and ctx.cancel.is_set():
            return f"Stopped after {i} fields."
        p.write(str(v), interval=0.01)
        if i < len(values) - 1:
            p.press("tab")
    if submit:
        p.press("enter")
    return f"Filled {len(values)} fields{' and submitted' if submit else ' (not submitted - review it first)'}."
