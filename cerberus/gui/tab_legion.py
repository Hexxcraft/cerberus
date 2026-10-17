"""Legion tab: the agent roster, plus a form to create or edit an agent (name, role, backstory, skills, task, schedule)."""
from __future__ import annotations

import json
import time
import tkinter.messagebox as mb
from typing import Optional

import customtkinter as ctk

from . import theme as T
from .widgets import button, entry, hint, section, set_text, textbox

HEADS = {"Voice (Llama 3) - general": "generalist", "Reasoner (DeepSeek-R1) - deep thinking": "reasoner", "Editor (Mistral) - quick": "editor"}
HEAD_BY_ROLE = {v: k for k, v in HEADS.items()}


class LegionTab(ctk.CTkFrame):
    def __init__(self, master, app) -> None:
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.agents: list[dict] = []
        self.sel_id: Optional[int] = None
        self.skill_vars: dict[str, ctk.BooleanVar] = {}
        self._sched_job: Optional[str] = None
        self._sig = ""
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build()
        self.after(3000, self._tick)

    # ------------------------------------------------------------ layout
    def _build(self) -> None:
        left = ctk.CTkFrame(self, fg_color=T.PANEL, border_width=1, border_color=T.EDGE, width=270)
        left.grid(row=0, column=0, sticky="ns", padx=(0, 8), pady=4)
        left.grid_propagate(False)
        section(left, "The Legion").pack(anchor="w", padx=12, pady=(10, 0))
        hint(left, "Gold = active (follows its schedule). Grey = resting.", 240).pack(anchor="w", padx=12)
        self.list_frame = ctk.CTkScrollableFrame(left, fg_color="transparent")
        self.list_frame.pack(fill="both", expand=True, padx=4, pady=6)
        button(left, "+ New agent", self.new_agent, "gold", 240).pack(pady=(0, 10))

        form = ctk.CTkScrollableFrame(self, fg_color=T.PANEL, border_width=1, border_color=T.EDGE)
        form.grid(row=0, column=1, sticky="nsew", pady=4)
        form.grid_columnconfigure(0, weight=1)
        self.form_title = section(form, "New agent")
        self.form_title.pack(anchor="w", padx=12, pady=(10, 4))

        self.name = self._field(form, "Name", entry(form, 400, "e.g. Market Watcher"))
        self.role = self._field(form, "Role (one line: what is this agent?)", entry(form, 600, "e.g. Keeps an eye on my stock watchlist"))
        self.backstory = self._field(form, "Backstory (its personality and how it should behave)", textbox(form, 80))
        ctk.CTkLabel(form, text="Skills (what it is allowed to use)", font=T.font("body", 15), text_color=T.DIM).pack(anchor="w", padx=12, pady=(8, 0))
        self.skill_box = ctk.CTkFrame(form, fg_color="transparent")
        self.skill_box.pack(fill="x", padx=12)
        self.task = self._field(form, "Task (what it does each time it wakes)", textbox(form, 80))

        sched = ctk.CTkFrame(form, fg_color="transparent")
        sched.pack(fill="x", padx=12, pady=(8, 0))
        ctk.CTkLabel(sched, text="Schedule", font=T.font("body", 15), text_color=T.DIM).grid(row=0, column=0, sticky="w")
        self.schedule = entry(sched, 300, "manual")
        self.schedule.grid(row=1, column=0, sticky="w")
        self.schedule.bind("<KeyRelease>", lambda e: self._sched_changed())
        self.sched_note = ctk.CTkLabel(sched, text="", font=T.font("body", 14), text_color=T.DIM, anchor="w")
        self.sched_note.grid(row=1, column=1, padx=10, sticky="w")
        hint(sched, 'Try: "manual", "every 30 minutes", "daily at 9am", "weekdays at 08:30", "weekly mon,fri 17:00".', 700).grid(row=2, column=0, columnspan=2, sticky="w")

        head = ctk.CTkFrame(form, fg_color="transparent")
        head.pack(fill="x", padx=12, pady=(8, 0))
        ctk.CTkLabel(head, text="Thinks with", font=T.font("body", 15), text_color=T.DIM).pack(side="left")
        self.head_var = ctk.StringVar(value=list(HEADS)[0])
        ctk.CTkOptionMenu(head, values=list(HEADS), variable=self.head_var, width=300, fg_color=T.PANEL2, button_color=T.BLOOD_DK,
                          font=T.font("body", 14)).pack(side="left", padx=10)
        self.active_var = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(head, text="Active", variable=self.active_var, font=T.font("head", 13), text_color=T.GOLD,
                      progress_color=T.GOLD_DK, button_color=T.GOLD).pack(side="left", padx=20)
        hint(form, "Agents run in the background with the same safety rules as everything else: anything that changes files or "
                   "sends data waits for your approval unless Autonomous Agent Mode is on.", 760).pack(anchor="w", padx=12, pady=(6, 0))

        row = ctk.CTkFrame(form, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=10)
        button(row, "Save", self.save, "normal", 100).pack(side="left", padx=(0, 8))
        self.run_btn = button(row, "Run now", self.run_now, "gold", 100)
        self.run_btn.pack(side="left", padx=(0, 8))
        button(row, "Delete", self.delete, "danger", 100).pack(side="left")
        self.status = ctk.CTkLabel(row, text="", font=T.font("body", 14), text_color=T.DIM)
        self.status.pack(side="left", padx=14)

        section(form, "Latest report").pack(anchor="w", padx=12, pady=(6, 0))
        self.output = textbox(form, 220)
        self.output.pack(fill="x", padx=12, pady=(2, 14))
        set_text(self.output, "(select an agent and press Run now)")

    def _field(self, parent, label: str, widget):
        ctk.CTkLabel(parent, text=label, font=T.font("body", 15), text_color=T.DIM).pack(anchor="w", padx=12, pady=(8, 0))
        widget.pack(anchor="w", padx=12, fill="x" if isinstance(widget, ctk.CTkTextbox) else None)
        return widget

    # ------------------------------------------------------------ data
    def on_ready(self) -> None:
        self.app.run_bg(lambda: self.app.api.get("/skills"), self._skills_loaded)
        self.refresh()

    def on_show(self) -> None:
        self.refresh()

    def _skills_loaded(self, catalog: dict) -> None:
        for w in self.skill_box.winfo_children():
            w.destroy()
        self.skill_vars = {}
        for i, skill in enumerate(catalog):
            v = ctk.BooleanVar(value=False)
            self.skill_vars[skill] = v
            ctk.CTkCheckBox(self.skill_box, text=skill, variable=v, font=T.font("body", 14), text_color=T.TEXT, fg_color=T.BLOOD,
                            hover_color=T.BLOOD_HI, border_color=T.GOLD_DK, checkbox_width=18, checkbox_height=18
                            ).grid(row=i // 3, column=i % 3, sticky="w", padx=(0, 18), pady=2)
        if self.sel_id is not None:
            self._load_into_form(next((a for a in self.agents if a["id"] == self.sel_id), None))

    def refresh(self) -> None:
        self.app.run_bg(lambda: self.app.api.get("/agents"), self._agents_loaded, lambda e: self.app.toast(str(e), "error"))

    def _tick(self) -> None:
        if self.winfo_ismapped():
            self.app.run_poll("legion", lambda: self.app.api.get("/agents"), self._agents_loaded, lambda e: self.app.toast(str(e), "error"))
        self.after(3000, self._tick)

    def _agents_loaded(self, agents: list[dict]) -> None:
        sig = json.dumps([agents, self.sel_id], sort_keys=True, default=str)
        if sig == self._sig:
            return
        self._sig = sig
        self.agents = agents
        for w in self.list_frame.winfo_children():
            w.destroy()
        for a in agents:
            mark = "running..." if a["running"] else ("active" if a["active"] else "resting")
            b = ctk.CTkButton(self.list_frame, text=f"{a['name']}\n{mark} - {a['schedule_text']}", anchor="w", height=52, corner_radius=6,
                              font=T.font("body", 14), fg_color=T.BLOOD_DK if a["id"] == self.sel_id else "transparent",
                              hover_color=T.PANEL2, border_width=1, border_color=T.GOLD_DK if a["active"] else T.EDGE,
                              text_color=T.GOLD if a["active"] else T.DIM, command=lambda ag=a: self.select(ag))
            b.pack(fill="x", pady=3, padx=2)
        cur = next((a for a in agents if a["id"] == self.sel_id), None)
        if cur:
            self.run_btn.configure(state="disabled" if cur["running"] else "normal")
            self.status.configure(text="Running now..." if cur["running"] else self._last_line(cur))
            if cur.get("last_output") is not None and not getattr(self, "_editing_output", False):
                set_text(self.output, cur["last_output"] or "(no report yet)")

    @staticmethod
    def _last_line(a: dict) -> str:
        if not a.get("last_run"):
            return "Never run."
        when = time.strftime("%b %d %H:%M", time.localtime(a["last_run"]))
        return f"Last run {when}: {a.get('last_status') or '?'}"

    # ------------------------------------------------------------ form
    def select(self, agent: dict) -> None:
        self.sel_id = agent["id"]
        self._load_into_form(agent)
        self._agents_loaded(self.agents)

    def new_agent(self) -> None:
        self.sel_id = None
        self._load_into_form(None)
        self._agents_loaded(self.agents)

    def _load_into_form(self, a: Optional[dict]) -> None:
        self.form_title.configure(text=f"Edit {a['name']}" if a else "New agent")
        for box, key in ((self.name, "name"), (self.role, "role")):
            box.delete(0, "end")
            if a and a[key]:
                box.insert(0, a[key])
        for box, key in ((self.backstory, "backstory"), (self.task, "task")):
            box.delete("1.0", "end")
            box.insert("1.0", a[key] if a else "")
        self.schedule.delete(0, "end")
        self.schedule.insert(0, a["schedule"] if a else "manual")
        for skill, v in self.skill_vars.items():
            v.set(bool(a) and skill in a["skills"])
        self.head_var.set(HEAD_BY_ROLE.get(a["model_role"], list(HEADS)[0]) if a else list(HEADS)[0])
        self.active_var.set(bool(a and a["active"]))
        self.status.configure(text=self._last_line(a) if a else "")
        set_text(self.output, (a or {}).get("last_output") or "(select an agent and press Run now)")
        self._sched_changed()

    def _sched_changed(self) -> None:
        if self._sched_job:
            self.after_cancel(self._sched_job)
        self._sched_job = self.after(350, self._check_schedule)

    def _check_schedule(self) -> None:
        text = self.schedule.get().strip() or "manual"

        def ok(r: dict) -> None:
            self.sched_note.configure(text="understood" if r["ok"] else r["error"], text_color=T.OK if r["ok"] else "#ff7b7b")
        self.app.run_bg(lambda: self.app.api.get("/schedule/check", text=text), ok, lambda e: None)

    def _collect(self) -> dict:
        return {"name": self.name.get().strip(), "role": self.role.get().strip(),
                "backstory": self.backstory.get("1.0", "end").strip(), "task": self.task.get("1.0", "end").strip(),
                "schedule": self.schedule.get().strip() or "manual", "skills": [s for s, v in self.skill_vars.items() if v.get()],
                "model_role": HEADS[self.head_var.get()], "active": self.active_var.get()}

    def save(self) -> None:
        body, sid = self._collect(), self.sel_id
        if not body["name"]:
            self.app.toast("An agent needs a name.", "warn")
            return

        def work() -> dict:
            return self.app.api.put(f"/agents/{sid}", body) if sid is not None else self.app.api.post("/agents", body)

        def ok(agent: dict) -> None:
            self.sel_id = agent["id"]
            self.app.toast(f"Saved {agent['name']}.", "ok")
            self.refresh()
            self._load_into_form(agent)
        self.app.run_bg(work, ok)

    def run_now(self) -> None:
        if self.sel_id is None:
            self.app.toast("Save the agent first.", "warn")
            return
        sid = self.sel_id

        def ok(r: dict) -> None:
            self.app.toast("The agent is awake." if r["started"] else "That agent is already running.", "ok")
            set_text(self.output, "Working... (any action that needs your approval will pop up)")
            self.refresh()
        self.app.run_bg(lambda: self.app.api.post(f"/agents/{sid}/run"), ok)

    def delete(self) -> None:
        if self.sel_id is None:
            return
        if not mb.askyesno("Delete agent", "Banish this agent for good?"):
            return
        sid = self.sel_id

        def ok(_r) -> None:
            self.sel_id = None
            self._load_into_form(None)
            self.refresh()
        self.app.run_bg(lambda: self.app.api.delete(f"/agents/{sid}"), ok)
