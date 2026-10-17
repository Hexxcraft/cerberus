"""Reusable gothic widgets: the animated sigil, the chat view, the permission pop-up and a few small helpers."""
from __future__ import annotations

import math
import tkinter as tk
from typing import Callable, Optional

import customtkinter as ctk

from . import theme as T

HEAD_COLORS = {"reasoner": T.BLOOD_HI, "generalist": T.GOLD, "editor": T.BONE}
HEAD_NAMES = {"reasoner": "Reasoner", "generalist": "Voice", "editor": "Editor"}


class SigilCanvas(tk.Canvas):
    """A slowly turning pentagram in a ring. Three dots on the ring are Cerberus' three heads; the speaking head blazes."""

    def __init__(self, master, size: int = 72, **kw) -> None:
        super().__init__(master, width=size, height=size, bg=T.BG, highlightthickness=0, **kw)
        self.size, self.angle, self.busy, self.active = size, 0.0, False, None
        c, r = size / 2, size / 2 - 5
        self.c, self.r = c, r
        self.ring = self.create_oval(c - r, c - r, c + r, c + r, outline=T.BLOOD_DK, width=2)
        self.lines = [self.create_line(0, 0, 0, 0, fill=T.BLOOD, width=2) for _ in range(5)]
        self.dots = {role: self.create_oval(0, 0, 0, 0, fill=col, outline="") for role, col in HEAD_COLORS.items()}
        self._alive = True
        self._tick()

    def set_busy(self, busy: bool, active: Optional[str] = None) -> None:
        self.busy, self.active = busy, active if busy else None

    def _tick(self) -> None:
        if not self._alive or not self.winfo_exists():
            return
        self.angle += 0.045 if self.busy else 0.008
        c, r = self.c, self.r * 0.86
        pts = [(c + r * math.cos(self.angle - math.pi / 2 + i * 2 * math.pi / 5),
                c + r * math.sin(self.angle - math.pi / 2 + i * 2 * math.pi / 5)) for i in range(5)]
        pulse = 0.5 + 0.5 * math.sin(self.angle * 6)
        color = T.BLOOD_HI if self.busy and pulse > 0.5 else T.BLOOD
        for i, line in enumerate(self.lines):
            a, b = pts[i], pts[(i + 2) % 5]
            self.coords(line, a[0], a[1], b[0], b[1])
            self.itemconfigure(line, fill=color)
        for i, (role, dot) in enumerate(self.dots.items()):
            a = -math.pi / 2 + i * 2 * math.pi / 3 - self.angle * 0.5
            x, y = c + self.r * math.cos(a), c + self.r * math.sin(a)
            rad = 6 if role == self.active else 3.5
            self.coords(dot, x - rad, y - rad, x + rad, y + rad)
        self.after(50, self._tick)

    def destroy(self) -> None:
        self._alive = False
        super().destroy()


class HeadStrip(ctk.CTkFrame):
    """Three labels showing which head (and model) is speaking right now."""

    def __init__(self, master) -> None:
        super().__init__(master, fg_color=T.PANEL, corner_radius=6, border_width=1, border_color=T.EDGE)
        self.labels: dict[str, ctk.CTkLabel] = {}
        for i, role in enumerate(("reasoner", "generalist", "editor")):
            lbl = ctk.CTkLabel(self, text=f"{HEAD_NAMES[role]}\n-", font=T.font("body", 13), text_color=T.DIM, justify="center")
            lbl.grid(row=0, column=i, padx=14, pady=4, sticky="ew")
            self.grid_columnconfigure(i, weight=1)
            self.labels[role] = lbl

    def set_models(self, models: dict) -> None:
        for role, lbl in self.labels.items():
            name = models.get(role) or "(not installed)"
            lbl.configure(text=f"{HEAD_NAMES[role]}\n{name}")

    def highlight(self, role: Optional[str], note: str = "") -> None:
        for r, lbl in self.labels.items():
            lbl.configure(text_color=HEAD_COLORS[r] if r == role else T.DIM)


class ChatView(ctk.CTkTextbox):
    """Read-only rich text area. Tags give each kind of message its own colour and font."""

    def __init__(self, master, **kw) -> None:
        super().__init__(master, fg_color=T.PANEL, text_color=T.TEXT, border_width=1, border_color=T.EDGE, wrap="word",
                         font=T.font("body", 17), corner_radius=6, **kw)
        tb = self._textbox  # CustomTkinter hides tag fonts, so configure the inner Tk widget directly
        tb.tag_config("user", foreground=T.GOLD, lmargin1=8, lmargin2=8, spacing1=10, spacing3=4)
        tb.tag_config("who", foreground=T.BLOOD_HI, font=(T.family("head"), 12, "bold"), spacing1=12)
        tb.tag_config("stage", foreground=T.DIM, font=(T.family("body"), 13, "italic"), spacing1=8)
        tb.tag_config("dim", foreground=T.DIM, font=(T.family("body"), 15))
        tb.tag_config("bright", foreground=T.TEXT)
        tb.tag_config("final", foreground=T.BONE, lmargin1=8, lmargin2=8)
        tb.tag_config("err", foreground="#ff7b7b")
        tb.tag_config("ok", foreground=T.OK)
        tb.tag_config("code", foreground="#d8c9a0", font=(T.family("mono"), 13), background="#120b0f")
        self.configure(state="disabled")

    def _append(self, text: str, tag: str) -> None:
        at_bottom = self._textbox.yview()[1] > 0.97
        self.configure(state="normal")
        self._textbox.insert("end", text, tag)
        self.configure(state="disabled")
        if at_bottom:
            self._textbox.see("end")

    def clear(self) -> None:
        self.configure(state="normal")
        self._textbox.delete("1.0", "end")
        self.configure(state="disabled")

    def user(self, text: str) -> None:
        self._append("\nYou\n", "who")
        self._append(text + "\n", "user")

    def stage(self, title: str, model: str = "") -> None:
        self._append(f"\n  {title}" + (f"  ({model})" if model else "") + "\n", "stage")

    def token(self, text: str, bright: bool) -> None:
        self._append(text, "bright" if bright else "dim")

    def note(self, text: str, tag: str = "stage") -> None:
        self._append(f"\n{text}\n", tag)

    def answer(self, text: str, who: str = "Cerberus") -> None:
        self._append(f"\n{who}\n", "who")
        in_code = False
        for ln in text.splitlines(keepends=True):
            if ln.lstrip().startswith("```"):
                in_code = not in_code
                self._append(ln, "code")
            else:
                self._append(ln, "code" if in_code else "final")
        self._append("\n", "final")


class ConfirmDialog(ctk.CTkToplevel):
    """'A pact is requested': shown whenever Cerberus wants to do something above 'safe'."""

    RISK_TEXT = {"write": ("CHANGES FILES", T.WARN), "publish": ("SENDS DATA OUT", T.BLOOD_HI), "control": ("TAKES THE MOUSE/KEYBOARD", T.BLOOD_HI)}

    def __init__(self, master, pending: dict, decide: Callable[[str, bool], None]) -> None:
        super().__init__(master, fg_color=T.BG)
        self.pending, self.decide, self.done = pending, decide, False
        self.title("A pact is requested")
        self.geometry("560x300")
        self.attributes("-topmost", True)
        label, color = self.RISK_TEXT.get(pending["risk"], (pending["risk"].upper(), T.WARN))
        ctk.CTkLabel(self, text="A pact is requested", font=T.font("title", 28), text_color=T.BLOOD).pack(pady=(12, 0))
        ctk.CTkLabel(self, text=label, font=T.font("head", 13, "bold"), text_color=color).pack()
        box = ctk.CTkTextbox(self, height=110, fg_color=T.PANEL, text_color=T.TEXT, font=T.font("mono", 13), wrap="word")
        box.pack(fill="x", padx=18, pady=8)
        box.insert("1.0", pending["summary"])
        box.configure(state="disabled")
        self.timer = ctk.CTkLabel(self, text="", font=T.font("body", 13), text_color=T.DIM)
        self.timer.pack()
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(pady=8)
        ctk.CTkButton(row, text="Grant", width=130, command=lambda: self._finish(True), font=T.font("head", 14), **T.button_style()).pack(side="left", padx=8)
        ctk.CTkButton(row, text="Refuse", width=130, command=lambda: self._finish(False), font=T.font("head", 14), **T.button_style("gold")).pack(side="left", padx=8)
        self.protocol("WM_DELETE_WINDOW", lambda: self._finish(False))
        self.remaining = int(pending.get("timeout", 180) - pending.get("age", 0))
        self._countdown()

    def _countdown(self) -> None:
        if self.done or not self.winfo_exists():
            return
        self.timer.configure(text=f"Refused automatically in {self.remaining}s")
        self.remaining -= 1
        if self.remaining < 0:
            self._finish(False)
        else:
            self.after(1000, self._countdown)

    def _finish(self, approve: bool) -> None:
        if self.done:
            return
        self.done = True
        self.decide(self.pending["id"], approve)
        self.destroy()

    def close_silently(self) -> None:
        self.done = True
        if self.winfo_exists():
            self.destroy()


class WardenDialog(ctk.CTkToplevel):
    """The Warden's pop-up. Its close button does nothing: answer it, obey it, or let the clock run out."""

    COLORS = {"ask": T.WARN, "final": T.BLOOD_HI}

    def __init__(self, master, alert: dict, on_answer: Callable[[int, str], None], on_obey: Callable[[int], None]) -> None:
        super().__init__(master, fg_color=T.BG)
        self.alert_id, self.on_answer, self.on_obey, self.done = alert["id"], on_answer, on_obey, False
        self.remaining = alert["remaining"]
        self.title("Cerberus Warden")
        self.geometry("640x470")
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", self._refuse_close)
        self.head = ctk.CTkLabel(self, text="", font=T.font("title", 30), text_color=T.BLOOD)
        self.head.pack(pady=(12, 0))
        self.text = ctk.CTkLabel(self, text="", font=T.font("body", 18), text_color=T.TEXT, wraplength=590, justify="left")
        self.text.pack(padx=24, pady=(8, 4))
        self.goals = ctk.CTkLabel(self, text="", font=T.font("body", 14), text_color=T.GOLD, wraplength=590, justify="left")
        self.goals.pack(padx=24, pady=2)
        self.timer = ctk.CTkLabel(self, text="", font=T.font("head", 16, "bold"), text_color=T.WARN)
        self.timer.pack(pady=(6, 2))
        self.verdict = ctk.CTkLabel(self, text="", font=T.font("body", 14), text_color="#ff7b7b", wraplength=590, justify="left")
        self.verdict.pack(padx=24)
        self.box = ctk.CTkTextbox(self, height=80, fg_color=T.PANEL, border_width=1, border_color=T.EDGE, text_color=T.TEXT,
                                  font=T.font("body", 15), wrap="word")
        self.box.pack(fill="x", padx=24, pady=8)
        self.box.bind("<Return>", self._enter)
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(pady=6)
        self.send_btn = ctk.CTkButton(row, text="Submit my reason", width=170, command=self._submit, font=T.font("head", 14), **T.button_style())
        self.send_btn.pack(side="left", padx=8)
        self.obey_btn = ctk.CTkButton(row, text="", width=190, command=self._obey, font=T.font("head", 14), **T.button_style("gold"))
        self.obey_btn.pack(side="left", padx=8)
        self.update_alert(alert)
        self.after(150, self._grab)
        self._countdown()

    def _grab(self) -> None:
        if self.winfo_exists():
            self.lift()
            self.focus_force()
            self.box.focus_set()

    def _refuse_close(self) -> None:
        self.bell()
        self.verdict.configure(text="No hiding. Answer it, or close the thing yourself.")

    def update_alert(self, alert: dict) -> None:
        self.remaining = alert["remaining"]
        final = alert["phase"] == "final"
        what = "game" if alert["kind"] == "game" else "tab"
        self.head.configure(text="LAST WARNING" if final else "The Warden speaks", text_color=self.COLORS["final" if final else "ask"])
        self.text.configure(text=alert["text"])
        self.goals.configure(text=alert["goals"])
        self.obey_btn.configure(text=f"Fine. Close the {what}.")
        if alert.get("reason"):
            self.verdict.configure(text="Rejected: " + alert["reason"])
        busy = alert.get("judging", False)
        self.send_btn.configure(state="disabled" if busy else "normal", text="Weighing it..." if busy else "Submit my reason")
        self._paint_timer(final)

    def _paint_timer(self, final: bool) -> None:
        tail = "It will be closed when this reaches zero." if final else "No answer escalates to the final warning."
        self.timer.configure(text=f"{max(0, int(self.remaining))}s   {tail}", text_color=T.BLOOD_HI if final else T.WARN)

    def _countdown(self) -> None:
        if self.done or not self.winfo_exists():
            return
        self.remaining -= 1
        self._paint_timer("LAST" in self.head.cget("text"))
        self.after(1000, self._countdown)

    def _enter(self, event) -> str:
        if event.state & 0x0001:
            return ""
        self._submit()
        return "break"

    def _submit(self) -> None:
        text = self.box.get("1.0", "end").strip()
        if not text:
            self.verdict.configure(text="Say something first.", text_color=T.WARN)
            return
        self.send_btn.configure(state="disabled", text="Weighing it...")
        self.verdict.configure(text="")
        self.on_answer(self.alert_id, text)

    def _obey(self) -> None:
        self.obey_btn.configure(state="disabled")
        self.on_obey(self.alert_id)

    def rejected(self, reason: str, alert: Optional[dict]) -> None:
        self.verdict.configure(text="Rejected: " + reason, text_color="#ff7b7b")
        self.send_btn.configure(state="normal", text="Submit my reason")
        self.box.delete("1.0", "end")
        if alert:
            self.update_alert(alert)
            self.verdict.configure(text="Rejected: " + reason)

    def failed(self, message: str) -> None:
        self.verdict.configure(text=message, text_color="#ff7b7b")
        self.send_btn.configure(state="normal", text="Submit my reason")
        self.obey_btn.configure(state="normal")

    def close_silently(self) -> None:
        self.done = True
        if self.winfo_exists():
            self.destroy()


def section(master, text: str) -> ctk.CTkLabel:
    return ctk.CTkLabel(master, text=text, font=T.font("head", 16, "bold"), text_color=T.GOLD, anchor="w")


def hint(master, text: str, wrap: int = 520) -> ctk.CTkLabel:
    return ctk.CTkLabel(master, text=text, font=T.font("body", 14), text_color=T.DIM, anchor="w", justify="left", wraplength=wrap)


def entry(master, width: int = 220, placeholder: str = "", show: str = "") -> ctk.CTkEntry:
    return ctk.CTkEntry(master, width=width, placeholder_text=placeholder, show=show, fg_color=T.PANEL, border_color=T.EDGE,
                        text_color=T.TEXT, font=T.font("body", 15))


def textbox(master, height: int = 100, mono: bool = False, **kw) -> ctk.CTkTextbox:
    return ctk.CTkTextbox(master, height=height, fg_color=T.PANEL, border_width=1, border_color=T.EDGE, text_color=T.TEXT,
                          font=T.font("mono", 13) if mono else T.font("body", 15), wrap="word", **kw)


def button(master, text: str, command, kind: str = "normal", width: int = 120, **kw) -> ctk.CTkButton:
    return ctk.CTkButton(master, text=text, command=command, width=width, font=T.font("head", 13), **T.button_style(kind), **kw)


def set_text(box: ctk.CTkTextbox, text: str) -> None:
    box.configure(state="normal")
    box.delete("1.0", "end")
    box.insert("1.0", text)
    box.configure(state="disabled")
