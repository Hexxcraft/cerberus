"""Soul Ledger tab: what Cerberus has learned about you, and where you teach it more."""
from __future__ import annotations

import tkinter.messagebox as mb

import customtkinter as ctk

from . import theme as T
from .widgets import button, entry, hint, section, set_text, textbox


class SoulTab(ctk.CTkScrollableFrame):
    def __init__(self, master, app) -> None:
        super().__init__(master, fg_color="transparent")
        self.app = app
        self._build()

    def _build(self) -> None:
        self.bond = ctk.CTkLabel(self, text="Bond: ...", font=T.font("head", 22), text_color=T.GOLD, anchor="w")
        self.bond.pack(anchor="w", padx=8, pady=(6, 0))
        hint(self, "Cerberus learns from every conversation, on this computer only. Nothing here leaves your machine "
                   "(unless Cloud Handoff sends a compressed summary of one request).", 900).pack(anchor="w", padx=8)

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=8, pady=(10, 0))
        ctk.CTkLabel(row, text="What should it call you?", font=T.font("body", 15), text_color=T.DIM).pack(side="left")
        self.name = entry(row, 220, "your name")
        self.name.pack(side="left", padx=10)
        button(row, "Save", self._save_name, "normal", 80).pack(side="left")
        self.learn_var = ctk.BooleanVar(value=True)
        ctk.CTkSwitch(row, text="Learning enabled", variable=self.learn_var, command=self._toggle_learning, font=T.font("head", 13),
                      text_color=T.GOLD, progress_color=T.GOLD_DK, button_color=T.GOLD).pack(side="left", padx=30)

        section(self, "What I have noticed about your habits").pack(anchor="w", padx=8, pady=(16, 0))
        self.habits = textbox(self, 80)
        self.habits.pack(fill="x", padx=8)

        section(self, "What I know about you (facts)").pack(anchor="w", padx=8, pady=(16, 0))
        hint(self, "Learned automatically from chat, or add your own. Click the x to forget one.", 900).pack(anchor="w", padx=8)
        self.facts_frame = ctk.CTkFrame(self, fg_color=T.PANEL, border_width=1, border_color=T.EDGE)
        self.facts_frame.pack(fill="x", padx=8, pady=4)
        add = ctk.CTkFrame(self, fg_color="transparent")
        add.pack(fill="x", padx=8)
        self.fkey = entry(add, 200, "topic (e.g. favorite language)")
        self.fkey.pack(side="left")
        self.fval = entry(add, 340, "value (e.g. Python)")
        self.fval.pack(side="left", padx=8)
        button(add, "Remember", self._add_fact, "normal", 110).pack(side="left")

        section(self, "Teach me your voice").pack(anchor="w", padx=8, pady=(18, 0))
        hint(self, "Paste things you have written (essays, emails, posts) or your resume. The Scribe's ghost-writer imitates "
                   "this style; Hunt uses your resume.", 900).pack(anchor="w", padx=8)
        srow = ctk.CTkFrame(self, fg_color="transparent")
        srow.pack(fill="x", padx=8, pady=(4, 0))
        self.kind_var = ctk.StringVar(value="writing")
        ctk.CTkOptionMenu(srow, values=["writing", "resume", "notes"], variable=self.kind_var, width=120, fg_color=T.PANEL2,
                          button_color=T.BLOOD_DK, font=T.font("body", 14)).pack(side="left")
        self.stitle = entry(srow, 300, "title (optional)")
        self.stitle.pack(side="left", padx=8)
        self.sample = textbox(self, 120)
        self.sample.pack(fill="x", padx=8, pady=4)
        brow = ctk.CTkFrame(self, fg_color="transparent")
        brow.pack(fill="x", padx=8)
        button(brow, "Add sample", self._add_sample, "normal", 120).pack(side="left")
        button(brow, "Rebuild style card", self._rebuild, "gold", 170).pack(side="left", padx=8)
        self.samples_frame = ctk.CTkFrame(self, fg_color=T.PANEL, border_width=1, border_color=T.EDGE)
        self.samples_frame.pack(fill="x", padx=8, pady=6)
        ctk.CTkLabel(self, text="Style card (how you write)", font=T.font("body", 15), text_color=T.DIM).pack(anchor="w", padx=8)
        self.style = textbox(self, 110)
        self.style.pack(fill="x", padx=8, pady=(0, 6))

        section(self, "The unmaking").pack(anchor="w", padx=8, pady=(16, 0))
        button(self, "Forget everything about me", self._forget, "danger", 260).pack(anchor="w", padx=8, pady=(4, 20))

    # ------------------------------------------------------------ data
    def on_ready(self) -> None:
        self.name.delete(0, "end")
        if self.app.cfg.get("user_name"):
            self.name.insert(0, self.app.cfg["user_name"])
        self.learn_var.set(self.app.cfg.get("learning_enabled", True))

    def on_show(self) -> None:
        self.app.run_bg(lambda: self.app.api.get("/profile"), self._loaded, lambda e: self.app.toast(str(e), "error"))

    def _loaded(self, p: dict) -> None:
        b = p["bond"]
        nxt = f" - next level at {b['next_at']}" if b.get("next_at") else ""
        self.bond.configure(text=f"Bond: {b['level']}  ({b['interactions']} words exchanged{nxt})")
        set_text(self.habits, "\n".join(f"- {h}" for h in p["habits"]) or "Not enough conversation yet. Talk to me a little more.")
        for w in self.facts_frame.winfo_children():
            w.destroy()
        if not p["facts"]:
            ctk.CTkLabel(self.facts_frame, text="Nothing yet.", font=T.font("body", 14), text_color=T.DIM).pack(padx=10, pady=6, anchor="w")
        for f in p["facts"]:
            r = ctk.CTkFrame(self.facts_frame, fg_color="transparent")
            r.pack(fill="x", padx=6, pady=1)
            ctk.CTkButton(r, text="x", width=26, height=22, fg_color="transparent", hover_color=T.BLOOD_DK, text_color=T.BLOOD_HI,
                          command=lambda k=f["key"]: self._del_fact(k)).pack(side="left")
            ctk.CTkLabel(r, text=f"{f['key']}: {f['value']}", font=T.font("body", 15), text_color=T.TEXT, anchor="w").pack(side="left", padx=6)
        for w in self.samples_frame.winfo_children():
            w.destroy()
        if not p["samples"]:
            ctk.CTkLabel(self.samples_frame, text="No samples saved.", font=T.font("body", 14), text_color=T.DIM).pack(padx=10, pady=6, anchor="w")
        for s in p["samples"]:
            r = ctk.CTkFrame(self.samples_frame, fg_color="transparent")
            r.pack(fill="x", padx=6, pady=1)
            ctk.CTkButton(r, text="x", width=26, height=22, fg_color="transparent", hover_color=T.BLOOD_DK, text_color=T.BLOOD_HI,
                          command=lambda i=s["id"]: self._del_sample(i)).pack(side="left")
            ctk.CTkLabel(r, text=f"[{s['kind']}] {s['title']}  ({s['chars']} chars)", font=T.font("body", 14), text_color=T.TEXT).pack(side="left", padx=6)
        set_text(self.style, p["style"]["text"] or "(no style card yet - add writing samples, then rebuild)")

    # ------------------------------------------------------------ actions
    def _save_name(self) -> None:
        name = self.name.get().strip()
        self.app.run_bg(lambda: self.app.api.put("/settings", {"user_name": name}),
                        lambda cfg: (self.app.refresh_settings(cfg), self.app.toast("I shall remember that name.", "ok")))

    def _toggle_learning(self) -> None:
        v = self.learn_var.get()
        self.app.run_bg(lambda: self.app.api.put("/settings", {"learning_enabled": v}),
                        lambda cfg: (self.app.refresh_settings(cfg), self.app.toast("Learning is on." if v else "Learning is paused.", "ok")))

    def _add_fact(self) -> None:
        k, v = self.fkey.get().strip(), self.fval.get().strip()
        if not k or not v:
            self.app.toast("Give both a topic and a value.", "warn")
            return

        def ok(_r) -> None:
            self.fkey.delete(0, "end")
            self.fval.delete(0, "end")
            self.on_show()
        self.app.run_bg(lambda: self.app.api.post("/profile/facts", {"key": k, "value": v}), ok)

    def _del_fact(self, key: str) -> None:
        self.app.run_bg(lambda: self.app.api.delete(f"/profile/facts/{key}"), lambda r: self.on_show())

    def _add_sample(self) -> None:
        body = {"kind": self.kind_var.get(), "title": self.stitle.get().strip(), "content": self.sample.get("1.0", "end").strip()}

        def ok(_r) -> None:
            self.sample.delete("1.0", "end")
            self.stitle.delete(0, "end")
            self.app.toast("Sample saved. Rebuild the style card to learn from it.", "ok")
            self.on_show()
        self.app.run_bg(lambda: self.app.api.post("/samples", body), ok)

    def _del_sample(self, sid: int) -> None:
        self.app.run_bg(lambda: self.app.api.delete(f"/samples/{sid}"), lambda r: self.on_show())

    def _rebuild(self) -> None:
        self.app.toast("Studying your writing... (up to a minute)", "info")
        self.app.run_bg(lambda: self.app.api.post("/profile/style/rebuild", timeout=240),
                        lambda r: (self.app.toast("Style card rebuilt.", "ok"), self.on_show()))

    def _forget(self) -> None:
        if mb.askyesno("Forget everything", "Erase everything learned since your profile was loaded: facts, habits, samples, style card and chat "
                                            "history? The profile facts stay (delete those one by one with the x). This cannot be undone."):
            self.app.run_bg(lambda: self.app.api.post("/profile/forget"),
                            lambda r: (self.app.toast("All forgotten.", "ok"), self.app.chat.clear(), self.on_show()))
