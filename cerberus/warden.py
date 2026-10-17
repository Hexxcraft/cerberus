"""The Warden: watches for games and time-sink websites, nags with growing severity, shames, keeps a tally, and finally closes them.

How it works (all on this PC, nothing leaves it):
  * Every ~2 s it looks at the window in front (program name, title) and how long since you last touched mouse/keyboard.
    Every ~50 ms it notices mouse-button presses (only "a button went down", never what you typed or where you clicked).
  * Time only counts while the game/site is in front AND you are present (input within `away_minutes`).
  * After `grace_minutes` it pops an alert, then again after shrinking gaps, each harsher, each reminding you of the three
    goals: BODY (work out), MIND (study), SOUL (make something of your own); the wording comes from the profile. You can explain yourself; the generalist and the editor
    heads must BOTH accept the excuse (it fails closed) and it buys a limited pass. A bad answer is a strike; two strikes, or
    ignoring the alert, escalates to a FINAL warning, and if that is ignored too the thing is closed.
  * Games are closed politely (like the X button), forced only if they ignore it. Websites: only the TAB is closed (Ctrl+W);
    a browser is never ended. Protected programs and Cerberus itself are never touched (see winwatch.refusal_reason).
  * Cyberpunk 2077 (the game itself, not a web page about it) is exempt: no counting, no alerts.

The Warden's settings change only through local-only endpoints, never through any AI tool.
"""
from __future__ import annotations

import os
import random
import re
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from . import dossier, winwatch
from .ollama_client import strip_think

EXEMPT_EXES = {"cyberpunk2077.exe"}
EXEMPT_PATH_HINT = "\\cyberpunk 2077\\"
NON_GAME_HINTS = ("launcher", "crashreport", "crashhandler", "redist", "setup", "install", "easyanticheat", "battleye",
                  "editor", "unreal", "updater", "helper")
FALLBACK_SECONDS = 15           # no Cerberus window picked the alert up: show a plain Windows message instead
TICK_SECONDS = 2.0
CLICK_SECONDS = 0.05
SAVE_EVERY = 30.0
MAX_DT = 10.0                   # a sleep/hibernate gap is never counted as time spent
TIERS = {1: "nudge", 2: "firm", 3: "harsh", 4: "brutal"}

SHAME = {
    1: ["{h}, {time} on {target}. A fine use of a finite life.",
        "{h}: {time} of {target}. Noted. Barely.",
        "{target} again, {h}? {time} gone, and no one is coming to give them back."],
    2: ["{h}, {time} on {target} and counting. Today's tally of squandered time: {today}.",
        "You promised yourself better, {h}. {time} of {target} says otherwise.",
        "This is the part where a man with goals closes the tab. {time} so far, {h}."],
    3: ["{time} on {target}, {h}. That is {today} wasted today and {week} this week. Your goals are not going to chase themselves.",
        "Pathetic, {h}. {time} of {target} while the body softens and the mind idles.",
        "Every minute here is one {h} did not spend becoming something. {today} today."],
    4: ["{h}. {time}. {target}. {today} today, {week} this week. Do not make me say it a fifth time.",
        "Enough. You are rotting in front of {target}: {time} this session. Answer, or lose it.",
        "Someone who cannot rule their own evenings rules nothing. {time} of {target}. Explain, or go."],
    5: ["FINAL WARNING, {h}. {target} closes when the clock runs out. Save now.",
        "Out of patience. {time} on {target}; it is ending unless you give a real reason. Save now.",
        "Last chance, {h}. Real reason, or {target} is closed."],
}

LEISURE_WORDS = ("bored", "boredom", "relax", "chill", "for fun", "just because", "killing time", "no reason", "nothing",
                 "idk", "dunno", "procrastinat", "unwind", "wasting", "lazy", "can't be bothered", "cant be bothered")
GOAL_WORDS = ("workout", "work out", "gym", "exercise", "lift", "training", "study", "studying", "learn", "course", "tutorial",
              "lecture", "certification", "network", "linux", "python", "coding", "programming", "code", "debug", "docs",
              "write", "writing", "book", "chapter", "manuscript", "research", "work",
              "mod", "reference", "inspiration", "worldbuilding", "campaign")

DEFAULT_KEYS = {"enabled": bool, "games": list, "game_paths": list, "sites": list, "apps_ignore": list,
                "grace_minutes": int, "gaps_minutes": list, "final_level": int, "reply_seconds": int,
                "final_seconds": int, "reset_minutes": int, "away_minutes": int, "max_excuses_per_day": int,
                "max_excuse_minutes": int}
INT_LIMITS = {"grace_minutes": (1, 120), "final_level": (2, 10), "reply_seconds": (20, 900), "final_seconds": (15, 300),
              "reset_minutes": (1, 120), "away_minutes": (1, 60), "max_excuses_per_day": (0, 20),
              "max_excuse_minutes": (5, 240)}


def clean_settings(patch: dict) -> dict:
    """Validate a Warden settings change. Unknown keys are dropped; numbers are clamped; lists are capped."""
    if not isinstance(patch, dict):
        raise ValueError("Settings must be an object.")
    out: dict = {}
    for key, want in DEFAULT_KEYS.items():
        if key not in patch:
            continue
        val = patch[key]
        if want is bool:
            out[key] = bool(val)
        elif want is int:
            try:
                lo, hi = INT_LIMITS[key]
                out[key] = max(lo, min(hi, int(val)))
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be a whole number.")
        elif key == "gaps_minutes":
            if not isinstance(val, list) or not val or len(val) > 12:
                raise ValueError("gaps_minutes must be a list of 1 to 12 whole numbers.")
            try:
                out[key] = [max(1, min(60, int(v))) for v in val]
            except (TypeError, ValueError):
                raise ValueError("gaps_minutes must be a list of whole numbers.")
        else:
            if not isinstance(val, list) or len(val) > 150:
                raise ValueError(f"{key} must be a list of at most 150 entries.")
            items = [str(v).strip()[:120] for v in val if str(v).strip()]
            out[key] = list(dict.fromkeys(items))
    return out


def fmt_minutes(seconds: float) -> str:
    m = int(seconds // 60)
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m} min"


def blank_tally(now: float) -> dict:
    return {"since": now, "total_s": 0.0, "excused_s": 0.0, "alerts": 0, "ignored": 0, "closed": 0, "excused": 0,
            "rejected": 0, "obeyed": 0, "clicks": 0, "days": {}, "targets": {}, "log": [],
            "excuses": {"date": "", "by": {}}}


class Warden:
    def __init__(self, core, probes=winwatch, clock: Callable[[], float] = time.time,
                 rng: Optional[random.Random] = None, judge: Optional[Callable[..., dict]] = None) -> None:
        self.core = core
        self.p = probes
        self.clock = clock
        self.rng = rng or random.Random()
        self.judge = judge or self._judge
        self.lock = threading.RLock()
        self.sessions: dict[str, dict] = {}
        self.alert: Optional[dict] = None
        self._alert_seq = 0
        self._last_tick: Optional[float] = None
        self._cur_key: Optional[str] = None
        self._buttons = (False, False, False)
        self._last_save = 0.0
        self._own: tuple[float, set] = (0.0, set())
        self.last_error = ""
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        saved = {}
        try:
            saved = core.db.kv_get("warden_tally") or {}
        except Exception:
            saved = {}
        self.tally = {**blank_tally(clock()), **saved}

    # ------------------------------------------------------------ settings
    def cfg(self) -> dict:
        return self.core.settings().get("warden") or {}

    def update_settings(self, patch: dict) -> dict:
        clean = clean_settings(patch)
        self.core.update_settings({"warden": clean})
        self._audit("settings", {"changed": sorted(clean)})
        return self.cfg()

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if os.environ.get("CERBERUS_NO_WARDEN") or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="warden")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        with self.lock:
            self._save(self.clock(), force=True)

    def _run(self) -> None:
        next_tick = 0.0
        while not self._stop.is_set():
            try:
                self.poll_clicks()
                now = time.monotonic()
                if now >= next_tick:
                    next_tick = now + TICK_SECONDS
                    self.tick()
            except Exception as exc:                                  # the watcher must never die quietly
                if str(exc) != self.last_error:
                    self.last_error = str(exc)
                    self._audit("error", {"error": f"{type(exc).__name__}: {exc}"})
            self._stop.wait(CLICK_SECONDS)

    # ------------------------------------------------------------ helpers
    def _audit(self, event: str, detail: dict) -> None:
        try:
            self.core.gate.audit("warden", event, detail)
        except Exception:
            pass

    def _day(self, now: float) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(now))

    def _log(self, now: float, text: str) -> None:
        log = self.tally["log"]
        log.append({"ts": now, "text": text})
        del log[:-200]

    def _save(self, now: float, force: bool = False) -> None:
        if not force and now - self._last_save < SAVE_EVERY:
            return
        self._last_save = now
        days = self.tally["days"]
        for d in sorted(days)[:-120]:
            del days[d]
        try:
            self.core.db.kv_set("warden_tally", self.tally)
        except Exception:
            pass

    def _own_pids(self, now: float) -> set:
        if now - self._own[0] > 30:
            try:
                self._own = (now, set(self.p.own_pids()))
            except Exception:
                self._own = (now, set())
        return self._own[1]

    def _is_own(self, fg: dict, now: float) -> bool:
        return fg.get("pid") in self._own_pids(now) or fg.get("exe") == "cerberus.exe" or fg.get("title", "").lower().startswith("cerberus")

    def _target_stat(self, label: str, kind: str) -> dict:
        return self.tally["targets"].setdefault(label, {"kind": kind, "s": 0.0, "alerts": 0, "closed": 0, "clicks": 0})

    def _times(self, now: float) -> tuple[float, float]:
        days = self.tally["days"]
        today = days.get(self._day(now), 0.0)
        week = sum(days.get(self._day(now - 86400 * i), 0.0) for i in range(7))
        return today, week

    # ------------------------------------------------------------ what is in front?
    def classify(self, fg: Optional[dict], cfg: Optional[dict] = None, now: float = 0.0) -> dict:
        """{'kind': 'none'|'neutral'|'exempt'|'game'|'site', ...}. Cyberpunk 2077 the program is exempt; a web page about it is not."""
        cfg = cfg if cfg is not None else self.cfg()
        if not fg:
            return {"kind": "none"}
        if self._is_own(fg, now):
            return {"kind": "neutral"}
        exe, path, title = fg.get("exe", ""), (fg.get("path") or "").lower(), fg.get("title", "")
        ignore = {str(e).lower() for e in cfg.get("apps_ignore", [])} | EXEMPT_EXES
        if exe in ignore or (EXEMPT_PATH_HINT in path and exe not in winwatch.BROWSER_EXES):
            return {"kind": "exempt"}
        if exe in winwatch.BROWSER_EXES:
            low = title.lower()
            for kw in cfg.get("sites", []):
                k = str(kw).strip().lower()
                if k and re.search(r"(?<!\w)" + re.escape(k) + r"(?!\w)", low):
                    label = "X (Twitter)" if k == "/ x" else str(kw).strip()
                    return {"kind": "site", "key": "site:" + k, "label": label, "kw": k}
            return {"kind": "none"}
        if exe in winwatch.PROTECTED_EXES:
            return {"kind": "none"}
        games = {str(g).lower() for g in cfg.get("games", [])}
        stem = Path(exe).stem
        if exe in games:
            return {"kind": "game", "key": "game:" + exe, "label": self._game_label(fg, cfg)}
        if path and not any(h in stem for h in NON_GAME_HINTS):
            for hint in cfg.get("game_paths", []):
                if str(hint).lower() in path:
                    return {"kind": "game", "key": "game:" + exe, "label": self._game_label(fg, cfg)}
        return {"kind": "none"}

    def _game_label(self, fg: dict, cfg: dict) -> str:
        path = fg.get("path") or ""
        for hint in cfg.get("game_paths", []):
            idx = path.lower().find(str(hint).lower())
            if idx >= 0:
                rest = path[idx + len(hint):].split("\\")
                if len(rest) > 1 and rest[0]:
                    return rest[0]
        return Path(fg.get("exe", "game")).stem.replace("_", " ").title()

    def _site_matches(self, kw: str, title: str) -> bool:
        return bool(re.search(r"(?<!\w)" + re.escape(kw) + r"(?!\w)", (title or "").lower()))

    # ------------------------------------------------------------ clicks (button-down edges only)
    def poll_clicks(self) -> int:
        buttons = self.p.mouse_buttons()
        new = sum(1 for was, now in zip(self._buttons, buttons) if now and not was)
        self._buttons = tuple(buttons)
        if new and self._cur_key:
            with self.lock:
                s = self.sessions.get(self._cur_key)
                if s:
                    s["clicks"] += new
                    self.tally["clicks"] += new
                    self._target_stat(s["label"], s["kind"])["clicks"] += new
        return new

    # ------------------------------------------------------------ the policy
    def tick(self, now: Optional[float] = None) -> None:
        now = self.clock() if now is None else now
        with self.lock:
            cfg = self.cfg()
            dt = 0.0 if self._last_tick is None else max(0.0, min(now - self._last_tick, MAX_DT))
            self._last_tick = now
            if not cfg.get("enabled", True):
                self.sessions.clear()
                self.alert = None
                self._cur_key = None
                return
            fg = self.p.foreground()
            present = self.p.idle_seconds() < int(cfg.get("away_minutes", 5)) * 60
            c = self.classify(fg, cfg, now)
            kind = c["kind"]
            self._cur_key = c.get("key") if kind in ("game", "site") else None
            if kind == "exempt":                                     # Cyberpunk 2077: nothing counts, nothing nags
                return
            self._account(now, dt, present, c, fg, cfg)
            self._maybe_alert(now, present, cfg)
            self._advance_alert(now, dt, present, c, fg, cfg)
            self._save(now)

    def _account(self, now: float, dt: float, present: bool, c: dict, fg: Optional[dict], cfg: dict) -> None:
        kind = c["kind"]
        reset_s = int(cfg.get("reset_minutes", 10)) * 60
        if kind in ("game", "site"):
            s = self.sessions.get(c["key"])
            if s is None:
                s = {"key": c["key"], "kind": kind, "label": c["label"], "kw": c.get("kw", ""), "engaged": 0.0,
                     "excused": 0.0, "off_for": 0.0, "clicks": 0, "alert_count": 0, "strikes": 0,
                     "next_at": int(cfg.get("grace_minutes", 10)) * 60.0, "pass_until": 0.0,
                     "hwnd": 0, "pid": 0, "path": "", "title": ""}
                self.sessions[c["key"]] = s
            s.update(hwnd=fg["hwnd"], pid=fg["pid"], path=fg["path"], title=fg["title"], off_for=0.0)
            if present and dt:
                stat = self._target_stat(s["label"], kind)
                if now < s["pass_until"]:
                    s["excused"] += dt
                    self.tally["excused_s"] += dt
                else:
                    s["engaged"] += dt
                    self.tally["total_s"] += dt
                    stat["s"] += dt
                    day = self._day(now)
                    self.tally["days"][day] = self.tally["days"].get(day, 0.0) + dt
            others = [o for k, o in self.sessions.items() if k != c["key"]]
        elif kind == "neutral":
            others = []
        else:
            others = list(self.sessions.values())
        for o in others:
            o["off_for"] += dt
            if o["off_for"] >= reset_s:
                del self.sessions[o["key"]]

    def _maybe_alert(self, now: float, present: bool, cfg: dict) -> None:
        if self.alert is not None or not present or not self._cur_key:
            return
        s = self.sessions.get(self._cur_key)
        if not s or now < s["pass_until"] or s["engaged"] < s["next_at"]:
            return
        s["alert_count"] += 1
        level = s["alert_count"]
        gaps = cfg.get("gaps_minutes") or [5]
        s["next_at"] = s["engaged"] + gaps[min(level - 1, len(gaps) - 1)] * 60
        final = level >= int(cfg.get("final_level", 5))
        self._alert_seq += 1
        self.alert = {"id": self._alert_seq, "key": s["key"], "level": level, "phase": "final" if final else "ask",
                      "created": now, "delivered": False, "fallback": False, "judging": False, "strikes": 0,
                      "reason": "", "remaining": float(cfg.get("final_seconds" if final else "reply_seconds", 60))}
        self._compose(s, now)
        self.tally["alerts"] += 1
        self._target_stat(s["label"], s["kind"])["alerts"] += 1
        self._log(now, f"Alert {level} ({'final' if final else TIERS.get(level, 'brutal')}) on {s['label']} after {fmt_minutes(s['engaged'])}.")
        self._audit("alert", {"target": s["label"], "level": level, "phase": self.alert["phase"]})

    def _compose(self, s: dict, now: float) -> None:
        a = self.alert
        assert a is not None
        today, week = self._times(now)
        h = self.rng.choice(dossier.honorifics())
        level = min(a["level"], 5)
        tier = 5 if a["phase"] == "final" else min(level, 4)
        line = self.rng.choice(SHAME[tier]).format(h=h, time=fmt_minutes(s["engaged"]), target=s["label"],
                                                   today=fmt_minutes(today), week=fmt_minutes(week))
        goal_list = dossier.goals()
        pick = goal_list[(a["level"] - 1) % 3]
        goals = "Your three goals: " + "  |  ".join(f"{k}: {v.split(': ')[-1]}" for k, v in goal_list) + f".  Start with {pick[0]}."
        a["text"] = line
        a["goals"] = goals
        a["tier"] = TIERS.get(a["level"], "brutal") if a["phase"] == "ask" else "final"

    def _advance_alert(self, now: float, dt: float, present: bool, c: dict, fg: Optional[dict], cfg: dict) -> None:
        a = self.alert
        if a is None:
            return
        s = self.sessions.get(a["key"])
        if s is None:
            self._resolve(now, "left")
            return
        if s["kind"] == "game" and not self.p.alive(s["pid"]):
            self._resolve(now, "obeyed")
            return
        if s["kind"] == "site" and fg and fg["hwnd"] == s["hwnd"] and c["kind"] != "site":
            self._resolve(now, "obeyed")                              # same browser window, different page: you left
            return
        if not a["delivered"]:
            if now - a["created"] >= FALLBACK_SECONDS and not a["fallback"]:
                a["fallback"] = a["delivered"] = True
                text = f"{a['text']}\n\nOpen Cerberus to answer. {int(a['remaining'])} seconds."
                threading.Thread(target=self._safe_box, args=("Cerberus Warden", text), daemon=True).start()
            return
        if a["judging"] or not present:
            return
        a["remaining"] -= dt
        if a["remaining"] > 0:
            return
        if a["phase"] == "ask":
            a["phase"] = "final"
            a["remaining"] = float(cfg.get("final_seconds", 45))
            self.tally["ignored"] += 1
            self._compose(s, now)
            self._log(now, f"Ignored the alert about {s['label']}; it was escalated to FINAL.")
        else:
            self._close(now, forced=True)

    def _safe_box(self, title: str, text: str) -> None:
        try:
            self.p.message_box(title, text)
        except Exception:
            pass

    def _resolve(self, now: float, outcome: str) -> None:
        a, self.alert = self.alert, None
        if a is None:
            return
        s = self.sessions.get(a["key"])
        label = s["label"] if s else a["key"]
        if outcome == "obeyed":
            self.tally["obeyed"] += 1
            self._log(now, f"Left {label} when told to.")
            self._audit("obeyed", {"target": label})

    # ------------------------------------------------------------ closing
    def _close(self, now: float, forced: bool) -> dict:
        a = self.alert
        s = self.sessions.get(a["key"]) if a else None
        if a is None or s is None:
            self.alert = None
            return {"closed": False, "detail": "Nothing to close."}
        label = s["label"]
        if s["kind"] == "game":
            res = self.p.close_app(s["path"], s["pid"])
            left = res.get("method") == "gone"
        else:
            fg = self.p.foreground()
            res = None
            if fg and fg["hwnd"] != s["hwnd"]:
                if self._is_own(fg, now):
                    self.p.focus(s["hwnd"])                          # our own dialog holds focus: hand it back to the page
                else:
                    res = {"closed": True, "left": True, "detail": "You were already in something else."}
            if res is None:
                kw = s["kw"]
                res = self.p.close_tab(s["hwnd"], lambda t, kw=kw: self._site_matches(kw, t))
            left = bool(res.get("left"))
        self.alert = None
        if res.get("closed") and left:
            self.tally["obeyed"] += 1
            self._log(now, f"Left {label} before it was closed.")
            return res
        if res.get("closed"):
            self.tally["closed"] += 1
            if not forced:
                self.tally["obeyed"] += 1
            self._target_stat(label, s["kind"])["closed"] += 1
            how = "you told me to" if not forced else "you ignored every warning"
            self._log(now, f"CLOSED {label} after {fmt_minutes(s['engaged'])} ({how}).")
            self.sessions.pop(s["key"], None)
            self.core.db.add_notification("warden", f"Closed {label}", f"{label} was closed after {fmt_minutes(s['engaged'])}. {res.get('detail', '')}")
            self._audit("closed", {"target": label, "forced": forced, "detail": res.get("detail", "")})
        else:
            s["next_at"] = s["engaged"] + 120
            self._log(now, f"Could not close {label}: {res.get('detail', '')}")
            self.core.db.add_notification("warden", f"Could not close {label}", res.get("detail", "It would not close."))
            self._audit("close_failed", {"target": label, "detail": res.get("detail", "")})
        self._save(now, force=True)
        return res

    # ------------------------------------------------------------ the UI side
    def _public_alert(self, a: Optional[dict]) -> Optional[dict]:
        if a is None:
            return None
        s = self.sessions.get(a["key"]) or {}
        return {"id": a["id"], "phase": a["phase"], "level": a["level"], "tier": a.get("tier", ""), "text": a.get("text", ""),
                "goals": a.get("goals", ""), "label": s.get("label", ""), "kind": s.get("kind", ""),
                "remaining": max(0, int(a["remaining"])), "strikes": a["strikes"], "reason": a["reason"],
                "judging": a["judging"], "minutes": int(s.get("engaged", 0) // 60)}

    def pending(self) -> Optional[dict]:
        """The open alert, if any. A UI calling this counts as 'delivered', which starts the countdown."""
        with self.lock:
            if self.alert is not None:
                self.alert["delivered"] = True
            return self._public_alert(self.alert)

    def _alert_or_raise(self, alert_id: int) -> dict:
        if self.alert is None or self.alert["id"] != alert_id:
            raise ValueError("That alert is no longer open.")
        return self.alert

    def obey(self, alert_id: int) -> dict:
        with self.lock:
            self._alert_or_raise(alert_id)
            res = self._close(self.clock(), forced=False)
            return {"ok": bool(res.get("closed")), "detail": res.get("detail", "")}

    def answer(self, alert_id: int, text: str) -> dict:
        text = (text or "").strip()[:800]
        with self.lock:
            a = self._alert_or_raise(alert_id)
            if a["judging"]:
                raise ValueError("Still weighing your last answer.")
            s = self.sessions.get(a["key"])
            if s is None:
                raise ValueError("That alert is no longer open.")
            cfg = self.cfg()
            now = self.clock()
            ex = self.tally["excuses"]
            if ex.get("date") != self._day(now):
                ex["date"], ex["by"] = self._day(now), {}
            used = ex["by"].get(s["key"], 0)
            cap = int(cfg.get("max_excuses_per_day", 3))
            snap = {"label": s["label"], "kind": s["kind"], "title": s["title"], "minutes": int(s["engaged"] // 60)}
            a["judging"] = True
        verdict: dict
        try:
            if len(text) < 8:
                verdict = {"accept": False, "minutes": 0, "reason": "That is not an explanation."}
            elif used >= cap:
                verdict = {"accept": False, "minutes": 0, "reason": f"You have already used your {cap} excuses for {snap['label']} today."}
            else:
                verdict = self.judge(text, snap, cfg)
        except Exception as exc:
            verdict = {"accept": False, "minutes": 0, "reason": f"Could not weigh that ({type(exc).__name__}); treated as rejected."}
        with self.lock:
            a["judging"] = False
            now = self.clock()                                       # the judges can take minutes; the pass starts from the verdict
            if self.alert is not a:
                return {"accepted": False, "stale": True, "reason": "The alert closed while I was thinking."}
            s = self.sessions.get(a["key"])
            if s is None:
                self.alert = None
                return {"accepted": False, "stale": True, "reason": "You had already left."}
            if verdict["accept"]:
                minutes = max(5, min(int(verdict.get("minutes") or 15), int(cfg.get("max_excuse_minutes", 60))))
                s["pass_until"] = now + minutes * 60
                s["alert_count"] = 0
                s["strikes"] = 0
                s["next_at"] = s["engaged"] + (cfg.get("gaps_minutes") or [7])[0] * 60
                ex["by"][s["key"]] = used + 1
                self.tally["excused"] += 1
                self._log(now, f"Excused {minutes} min on {s['label']}: \"{text[:100]}\"")
                self._audit("excused", {"target": s["label"], "minutes": minutes, "reason": text[:200]})
                self.alert = None
                self._save(now, force=True)
                return {"accepted": True, "minutes": minutes, "reason": verdict.get("reason", "")}
            a["strikes"] += 1
            a["reason"] = verdict.get("reason", "Not good enough.")
            self.tally["rejected"] += 1
            self._log(now, f"Rejected excuse on {s['label']}: \"{text[:100]}\"")
            if a["strikes"] >= 2 and a["phase"] == "ask":
                a["phase"] = "final"
                a["remaining"] = float(cfg.get("final_seconds", 45))
                self._compose(s, now)
            return {"accepted": False, "reason": a["reason"], "alert": self._public_alert(a)}

    # ------------------------------------------------------------ the judges (generalist AND editor must agree)
    def _judge(self, text: str, snap: dict, cfg: dict) -> dict:
        orch = self.core.orchestrator
        try:
            up = self.core.ollama.is_up()
        except Exception:
            up = False
        if not up:
            return self._heuristic(text, snap, cfg)
        try:
            return self._judge_models(text, snap, orch)
        except Exception:
            return self._heuristic(text, snap, cfg)

    def _judge_models(self, text: str, snap: dict, orch) -> dict:
        mapping = orch.model_map()
        strict = ("This is a video game. Accept only a SPECIFIC reason tied to one of the goals (for example testing a mod for "
                  "their own project, or research for something they are creating). 'Relaxing', 'a break' or 'just one game' is REJECT."
                  if snap["kind"] == "game" else
                  "This is a time-sink website. Accept only a concrete task tied to one of the goals (a tutorial, a lecture, "
                  "a workout video, research for their own project). Entertainment, browsing and 'a quick break' are REJECT.")
        goal_text = ", ".join(f"{k} ({v.split(': ')[-1]})" for k, v in dossier.goals())
        system = (f"You are the strict gatekeeper of {dossier.plain_name()}'s time. Their only three goals: {goal_text}. "
                  "They are on a leisure target and claim a reason. Decide if it is genuine and tied "
                  f"to a goal. {strict} Reply in EXACTLY this format and nothing else:\n"
                  "VERDICT: ACCEPT or REJECT\nMINUTES: whole number 5-60 (how long a pass is fair; 0 if rejecting)\n"
                  "REASON: one short blunt sentence")
        user = (f"Target: {snap['label']} ({snap['kind']}). Window title: {snap['title'][:120]!r}. "
                f"Time on it so far: {snap['minutes']} min.\nTheir explanation: {text}")
        votes = []
        for role in ("generalist", "editor"):
            raw = strip_think(self.core.ollama.chat(orch._model_for(role, mapping),
                                                    [{"role": "system", "content": system}, {"role": "user", "content": user}],
                                                    0.1, None, False, 120) or "")
            votes.append(self._parse_verdict(raw))
        if all(v["accept"] for v in votes):
            return {"accept": True, "minutes": min(v["minutes"] for v in votes), "reason": votes[0]["reason"]}
        no = next(v for v in votes if not v["accept"])
        return {"accept": False, "minutes": 0, "reason": no["reason"] or "Not good enough."}

    @staticmethod
    def _parse_verdict(raw: str) -> dict:
        m = list(re.finditer(r"VERDICT:\s*(ACCEPT|REJECT)", raw, re.I))
        accept = bool(m) and m[-1].group(1).upper() == "ACCEPT"
        mins = re.search(r"MINUTES:\s*(\d+)", raw, re.I)
        why = re.search(r"REASON:\s*(.+)", raw, re.I)
        return {"accept": accept, "minutes": int(mins.group(1)) if mins else 15,
                "reason": (why.group(1).strip()[:200] if why else "")}

    def _heuristic(self, text: str, snap: dict, cfg: dict) -> dict:
        low = text.lower()
        need = 40 if snap["kind"] == "game" else 25
        if len(text) < need:
            return {"accept": False, "minutes": 0, "reason": "Too thin. Say what you are actually doing."}
        if any(w in low for w in LEISURE_WORDS):
            return {"accept": False, "minutes": 0, "reason": "That sounds like leisure, not a goal."}
        if not any(w in low for w in GOAL_WORDS + dossier.goal_words()):
            return {"accept": False, "minutes": 0, "reason": "I hear no body, mind or soul in that."}
        return {"accept": True, "minutes": 10 if snap["kind"] == "game" else 15,
                "reason": "Accepted on keywords; the models were unavailable to check it."}

    # ------------------------------------------------------------ for the UI
    def status(self) -> dict:
        with self.lock:
            now = self.clock()
            cfg = self.cfg()
            today, week = self._times(now)
            cur = self.sessions.get(self._cur_key or "")
            sessions = [{"label": s["label"], "kind": s["kind"], "minutes": int(s["engaged"] // 60),
                         "excused_minutes": int(s["excused"] // 60), "clicks": s["clicks"], "alerts": s["alert_count"],
                         "pass_left": max(0, int(s["pass_until"] - now))} for s in self.sessions.values()]
            t = self.tally
            top = sorted(t["targets"].items(), key=lambda kv: -kv[1]["s"])[:15]
            return {"enabled": bool(cfg.get("enabled", True)), "watching": cur["label"] if cur else "",
                    "alert": self._public_alert(self.alert), "sessions": sessions,
                    "today_s": int(today), "week_s": int(week), "total_s": int(t["total_s"]), "excused_s": int(t["excused_s"]),
                    "counts": {k: t[k] for k in ("alerts", "ignored", "closed", "excused", "rejected", "obeyed", "clicks")},
                    "since": t["since"], "targets": [{"label": k, **v, "s": int(v["s"])} for k, v in top],
                    "log": t["log"][-25:][::-1], "days": dict(sorted(t["days"].items())[-14:]),
                    "settings": cfg, "supported": bool(getattr(self.p, "WIN", True)), "error": self.last_error,
                    "goals": [{"key": k, "text": v} for k, v in dossier.goals()]}
