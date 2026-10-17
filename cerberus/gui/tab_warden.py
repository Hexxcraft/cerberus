"""Warden tab: the wasted-time tally, the lists it watches, how strict it is, and the hardware guard's readout."""
from __future__ import annotations

import time

import customtkinter as ctk

from . import theme as T
from .widgets import button, entry, hint, section, set_text, textbox

LISTS = (("games", "Game programs (exact file names, one per line)", 5),
         ("game_paths", "Game folders (any program running from a path containing one of these counts as a game)", 4),
         ("sites", "Websites (words looked for in the browser's window title)", 5),
         ("apps_ignore", "Never nag about these programs (Cyberpunk 2077 is here on purpose)", 2))
NUMBERS = (("grace_minutes", "Minutes of leisure before the first alert"),
           ("final_level", "Alert number that becomes the final warning"),
           ("reply_seconds", "Seconds you get to answer an alert"),
           ("final_seconds", "Seconds the final warning lasts before it closes the thing"),
           ("reset_minutes", "Minutes away from a game/site before its timer starts over"),
           ("away_minutes", "Minutes without touching mouse or keyboard counted as 'away'"),
           ("max_excuses_per_day", "Excuses accepted per game/site per day"),
           ("max_excuse_minutes", "Longest pass an excuse can buy (minutes)"))


def duration(seconds: float) -> str:
    m = int(seconds // 60)
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


class WardenTab(ctk.CTkScrollableFrame):
    def __init__(self, master, app) -> None:
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.lists: dict[str, ctk.CTkTextbox] = {}
        self.numbers: dict[str, ctk.CTkEntry] = {}
        self._loaded_settings = False
        self._build()
        self.after(10000, self._auto_refresh)

    def _row(self) -> ctk.CTkFrame:
        r = ctk.CTkFrame(self, fg_color="transparent")
        r.pack(fill="x", padx=8, pady=3)
        return r

    def _build(self) -> None:
        section(self, "The Warden: watching your idle hours").pack(anchor="w", padx=8, pady=(6, 0))
        hint(self, "While a game or one of your listed websites is in front of you, the Warden nags you with growing severity, "
                   "reminds you of your three goals, and keeps a tally of the time. Explain what you are really doing and it may "
                   "grant a short pass; ignore it, or give excuses it does not believe, and it warns you and then closes the game "
                   "(or just the browser tab; it never closes your browser). Cyberpunk 2077 is exempt. Only you, at this computer, "
                   "can change these settings: neither the AI heads nor your phone can.", 940).pack(anchor="w", padx=8)
        r = self._row()
        self.on_var = ctk.BooleanVar(value=True)
        ctk.CTkSwitch(r, text="Warden on", variable=self.on_var, font=T.font("head", 13), text_color=T.GOLD, progress_color=T.GOLD_DK,
                      button_color=T.GOLD, command=self._toggle).pack(side="left")
        button(r, "Refresh", self.on_show, "normal", 100).pack(side="left", padx=16)
        self.watching = ctk.CTkLabel(r, text="", font=T.font("body", 14), text_color=T.WARN)
        self.watching.pack(side="left")
        self.goals_text = hint(self, "", 940)
        self.goals_text.pack(anchor="w", padx=8, pady=(4, 0))

        section(self, "The tally").pack(anchor="w", padx=8, pady=(18, 0))
        self.tally_text = hint(self, "Reading the tally...", 940)
        self.tally_text.pack(anchor="w", padx=8)
        self.targets_text = hint(self, "", 940)
        self.targets_text.pack(anchor="w", padx=8)
        section(self, "Recent").pack(anchor="w", padx=8, pady=(10, 0))
        self.log = textbox(self, 170, mono=True)
        self.log.pack(fill="x", padx=8, pady=4)

        section(self, "What it watches and how strict it is").pack(anchor="w", padx=8, pady=(18, 0))
        for key, label, h in LISTS:
            ctk.CTkLabel(self, text=label, font=T.font("body", 14), text_color=T.DIM, anchor="w").pack(anchor="w", padx=8, pady=(6, 0))
            box = textbox(self, 24 * h)
            box.pack(fill="x", padx=8)
            self.lists[key] = box
        gaps = self._row()
        ctk.CTkLabel(gaps, text="Minutes between alerts (comma list, shrinking)", font=T.font("body", 14), text_color=T.DIM).pack(side="left")
        self.gaps = entry(gaps, 200)
        self.gaps.pack(side="left", padx=8)
        for key, label in NUMBERS:
            r = self._row()
            e = entry(r, 70)
            e.pack(side="left")
            ctk.CTkLabel(r, text=label, font=T.font("body", 14), text_color=T.DIM).pack(side="left", padx=8)
            self.numbers[key] = e
        r = self._row()
        button(r, "Save these settings", self._save, "gold", 220).pack(side="left")
        hint(self, "Numbers outside the sensible range are pulled back into it.", 600).pack(side="left", padx=12)

        section(self, "Hardware guard").pack(anchor="w", padx=8, pady=(18, 0))
        self.guard_text = hint(self, "", 940)
        self.guard_text.pack(anchor="w", padx=8)
        ctk.CTkLabel(self, text="", height=20).pack()

    # ------------------------------------------------------------ data
    def on_show(self) -> None:
        self.app.run_poll("warden_tab", lambda: self.app.api.get("/warden"), self._status_loaded,
                          lambda e: self.tally_text.configure(text=f"Could not read the Warden: {e}"))
        self.app.run_poll("guard_tab", lambda: self.app.api.get("/guard"), self._guard_loaded)

    def _auto_refresh(self) -> None:
        if self.winfo_exists():
            if self.winfo_ismapped():
                self.on_show()
            self.after(10000, self._auto_refresh)

    def _status_loaded(self, st: dict) -> None:
        self.on_var.set(st["enabled"])
        self.watching.configure(text=f"Watching now: {st['watching']}" if st["watching"] else "")
        self.goals_text.configure(text="Your goals:  " + "   |   ".join(f"{g['key']}: {g['text'].split(': ', 1)[-1]}" for g in st["goals"]))
        c = st["counts"]
        note = "" if st["supported"] else "\n(The Warden only works on Windows.)"
        if st.get("error"):
            note += f"\nLast problem: {st['error']}"
        self.tally_text.configure(
            text=f"Wasted today: {duration(st['today_s'])}   |   this week: {duration(st['week_s'])}   |   all time: {duration(st['total_s'])}"
                 f"   (since {time.strftime('%Y-%m-%d', time.localtime(st['since']))})\nTime excused by good reasons: {duration(st['excused_s'])}\n"
                 f"Alerts: {c['alerts']}   ignored: {c['ignored']}   excuses accepted: {c['excused']}   rejected: {c['rejected']}   "
                 f"obeyed: {c['obeyed']}   forced closed: {c['closed']}   clicks while idling: {c['clicks']}" + note)
        rows = [f"{t['label']}: {duration(t['s'])}  ({t['alerts']} alerts, {t['closed']} closed)" for t in st["targets"]]
        self.targets_text.configure(text="Biggest sinks:  " + ("; ".join(rows) if rows else "nothing yet. Good."))
        set_text(self.log, "\n".join(f"{time.strftime('%a %H:%M', time.localtime(e['ts']))}  {e['text']}" for e in st["log"]) or "(nothing yet)")
        if not self._loaded_settings:
            self._fill_settings(st["settings"])
            self._loaded_settings = True

    def _fill_settings(self, cfg: dict) -> None:
        for key, box in self.lists.items():
            box.delete("1.0", "end")
            box.insert("1.0", "\n".join(cfg.get(key, [])))
        self.gaps.delete(0, "end")
        self.gaps.insert(0, ", ".join(str(g) for g in cfg.get("gaps_minutes", [])))
        for key, e in self.numbers.items():
            e.delete(0, "end")
            e.insert(0, str(cfg.get(key, "")))

    def _guard_loaded(self, g: dict) -> None:
        reads = [(label, (g.get(key) or {}).get("temp_c")) for key, label in (("gpu", "graphics card"), ("cpu", "processor"), ("board", "motherboard"))]
        shown = ",  ".join(f"{label} {t:.0f} C" for label, t in reads if t is not None)
        lines = [f"Status: {g['level'].upper()}" + (f"   ({shown})" if shown else ""),
                 "Cerberus slows down at warm, pauses heavy work when hot, refuses to fill your drive, and limits any program it runs "
                 "for you (memory ceiling, process cap, time limit, low priority)."]
        lines += g.get("notes", [])
        self.guard_text.configure(text="\n".join(lines))

    # ------------------------------------------------------------ changes
    def _put(self, patch: dict, done: str = "") -> None:
        def ok(cfg: dict) -> None:
            self._fill_settings(cfg)
            if done:
                self.app.toast(done, "ok")
            self.on_show()
        self.app.run_bg(lambda: self.app.api.put("/warden/settings", patch), ok)

    def _toggle(self) -> None:
        want = self.on_var.get()
        self._put({"enabled": want}, "The Warden is watching." if want else "The Warden is off.")

    def _save(self) -> None:
        patch: dict = {key: [ln.strip() for ln in box.get("1.0", "end").splitlines() if ln.strip()] for key, box in self.lists.items()}
        try:
            patch["gaps_minutes"] = [int(x) for x in self.gaps.get().replace(";", ",").split(",") if x.strip()]
            for key, e in self.numbers.items():
                patch[key] = int(e.get().strip())
        except ValueError:
            self.app.toast("The numbers must be whole numbers.", "warn")
            return
        self._put(patch, "Warden settings saved.")
