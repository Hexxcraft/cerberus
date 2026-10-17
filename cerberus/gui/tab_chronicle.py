"""Chronicle tab: what Cerberus has learned from its mistakes, and the review report built from its run log."""
from __future__ import annotations

import os
import time

import customtkinter as ctk

from . import theme as T
from .widgets import button, hint, section, set_text, textbox


class ChronicleTab(ctk.CTkScrollableFrame):
    def __init__(self, master, app) -> None:
        super().__init__(master, fg_color="transparent")
        self.app = app
        self._build()

    def _build(self) -> None:
        section(self, "Mistakes it remembers").pack(anchor="w", padx=8, pady=(6, 0))
        hint(self, "When a tool call fails, a change is refused, or something is off limits, Cerberus writes it down. Lessons that help are "
                   "shown to it before it acts again, so it stops repeating them. Secrets are scrubbed before anything is saved.", 900).pack(anchor="w", padx=8)
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=8, pady=4)
        button(row, "Refresh", self.load_mistakes, "gold", 100).pack(side="left")
        button(row, "Forget all of it", self._forget, "danger", 150).pack(side="left", padx=8)
        self.mistake_state = ctk.CTkLabel(row, text="", font=T.font("body", 14), text_color=T.DIM)
        self.mistake_state.pack(side="left", padx=8)
        self.mistakes = textbox(self, 200, mono=True)
        self.mistakes.pack(fill="x", padx=8, pady=(2, 6))

        section(self, "Review report").pack(anchor="w", padx=8, pady=(16, 0))
        hint(self, "Every request is logged (how long each step took, which tools ran, what failed, what the heads caught) so that, when it is "
                   "time to upgrade Cerberus, the report shows what to tweak. Copy it and hand it to whoever is doing the upgrade.", 900).pack(anchor="w", padx=8)
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=8, pady=4)
        ctk.CTkLabel(bar, text="Look back:", font=T.font("body", 14), text_color=T.DIM).pack(side="left")
        self.days = ctk.StringVar(value="30 days")
        ctk.CTkOptionMenu(bar, values=["7 days", "30 days", "90 days"], variable=self.days, width=110, fg_color=T.PANEL2,
                          button_color=T.BLOOD_DK, font=T.font("body", 14), command=lambda _v: self.load_report()).pack(side="left", padx=8)
        button(bar, "Refresh", self.load_report, "gold", 100).pack(side="left")
        button(bar, "Copy report", self._copy, "normal", 130).pack(side="left", padx=8)
        button(bar, "Open log folder", self._open_logs, "normal", 150).pack(side="left")
        self.report = textbox(self, 420, mono=True)
        self.report.pack(fill="x", padx=8, pady=(2, 6))

        section(self, "Logging").pack(anchor="w", padx=8, pady=(16, 0))
        self.log_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(self, text="Keep the review log", variable=self.log_var, command=self._log_changed, font=T.font("body", 14),
                        text_color=T.TEXT, fg_color=T.BLOOD, hover_color=T.BLOOD_HI, border_color=T.GOLD_DK).pack(anchor="w", padx=8, pady=4)
        self.where = hint(self, "", 900)
        self.where.pack(anchor="w", padx=8, pady=(0, 14))

    # ---------------------------------------------------------------- lifecycle
    def on_ready(self) -> None:
        tel = self.app.cfg.get("telemetry") or {}
        self.log_var.set(bool(tel.get("enabled", True)))
        self._set_where()

    def on_show(self) -> None:
        self.load_mistakes()
        self.load_report()

    def _set_where(self) -> None:
        try:
            from .. import telemetry
            where = str(telemetry.log_dir())
        except Exception:  # noqa: BLE001 - informational only
            where = "the Cerberus data folder, in a 'logs' subfolder"
        self.where.configure(text=f"The log lives in {where}. From a terminal, the same report is: python -m cerberus.telemetry --days 30")

    # ---------------------------------------------------------------- mistakes
    def load_mistakes(self) -> None:
        def ok(r: dict) -> None:
            items = r.get("items") or []
            self.mistake_state.configure(text=f"{r.get('count', 0)} remembered, {r.get('teachable', 0)} shown to it before acting")
            if not items:
                set_text(self.mistakes, "(nothing yet)")
                return
            blocks = []
            for it in items[:40]:
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(it.get("last", 0)))
                head = f"{when}  x{it.get('count', 1)}  [{it.get('kind')}] {it.get('where')}"
                body = f"  {it.get('text', '')}"
                advice = f"\n  lesson: {it['advice']}" if it.get("advice") else ""
                blocks.append(f"{head}\n{body}{advice}")
            set_text(self.mistakes, "\n\n".join(blocks))
        self.app.run_bg(lambda: self.app.api.get("/mistakes"), ok, lambda e: set_text(self.mistakes, str(e)))

    def _forget(self) -> None:
        import tkinter.messagebox as mb
        if not mb.askyesno("Forget mistakes", "Erase everything Cerberus wrote down about its own errors? It will make them again."):
            return
        self.app.run_bg(lambda: self.app.api.delete("/mistakes"), lambda r: (self.load_mistakes(), self.app.toast("Its mistake memory is cleared.", "ok")))

    # ---------------------------------------------------------------- report
    def load_report(self) -> None:
        days = int(self.days.get().split()[0])
        set_text(self.report, "(reading the log...)")
        self.app.run_bg(lambda: self.app.api.get_text("/telemetry/report", days=days, text="true"),
                        lambda txt: set_text(self.report, txt), lambda e: set_text(self.report, str(e)))

    def _copy(self) -> None:
        text = self.report.get("1.0", "end").strip()
        self.clipboard_clear()
        self.clipboard_append(text)
        self.app.toast("The report is on your clipboard.", "ok")

    def _open_logs(self) -> None:
        try:
            from .. import telemetry
            folder = telemetry.log_dir()
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(str(folder))  # noqa: S606 - opens a folder in Explorer
        except Exception as exc:  # noqa: BLE001
            self.app.toast(f"Could not open the folder: {exc}", "error")

    def _log_changed(self) -> None:
        want = self.log_var.get()
        tel = {**(self.app.cfg.get("telemetry") or {}), "enabled": want}
        self.app.run_bg(lambda: self.app.api.put("/settings", {"telemetry": tel}),
                        lambda cfg: (self.app.refresh_settings(cfg),
                                     self.app.toast("The review log is on." if want else "The review log is off; nothing new is written.", "ok" if want else "warn")))
