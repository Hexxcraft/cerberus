"""Gates tab: safety controls, local models, cloud handoff keys, LAN access and the workspace folder."""
from __future__ import annotations

import tkinter.filedialog as fd
import threading

import customtkinter as ctk

from . import theme as T
from ..council import LEVELS
from .widgets import button, entry, hint, section, set_text, textbox


class GatesTab(ctk.CTkScrollableFrame):
    def __init__(self, master, app) -> None:
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.provider_ids: dict[str, str] = {}   # label -> id
        self._build()

    def _row(self) -> ctk.CTkFrame:
        r = ctk.CTkFrame(self, fg_color="transparent")
        r.pack(fill="x", padx=8, pady=3)
        return r

    def _build(self) -> None:
        self._build_overview()
        # ---- safety
        section(self, "Safety").pack(anchor="w", padx=8, pady=(6, 0))
        hint(self, "By default every action that changes files, runs commands, publishes to GitHub or moves the mouse/keyboard asks you first "
                   "(and refuses if you do not answer in 3 minutes). Autonomous Agent Mode (header switch) skips the questions; it always "
                   "resets to OFF when the app starts, and 'Seal the Gates' turns it off instantly.", 940).pack(anchor="w", padx=8)
        self.audit = textbox(self, 150, mono=True)
        self.audit.pack(fill="x", padx=8, pady=4)
        button(self, "Refresh audit log", self._load_audit, "gold", 170).pack(anchor="w", padx=8)

        # ---- models
        section(self, "The three heads (local models)").pack(anchor="w", padx=8, pady=(20, 0))
        hint(self, "Type a model name or just its start (for example 'llama3' matches 'llama3:latest').", 940).pack(anchor="w", padx=8)
        r = self._row()
        self.models: dict[str, ctk.CTkEntry] = {}
        for role, label in (("reasoner", "Reasoner"), ("generalist", "Voice"), ("editor", "Editor")):
            ctk.CTkLabel(r, text=label, font=T.font("body", 15), text_color=T.DIM).pack(side="left")
            e = entry(r, 150)
            e.pack(side="left", padx=(6, 16))
            self.models[role] = e
        r2 = self._row()
        ctk.CTkLabel(r2, text="Context size", font=T.font("body", 15), text_color=T.DIM).pack(side="left")
        self.num_ctx = entry(r2, 90)
        self.num_ctx.pack(side="left", padx=(6, 16))
        button(r2, "Save models", self._save_models, "normal", 130).pack(side="left")
        button(r2, "Start Ollama", self._start_ollama, "gold", 130).pack(side="left", padx=8)
        self.installed = ctk.CTkLabel(self, text="", font=T.font("body", 14), text_color=T.DIM, anchor="w", justify="left", wraplength=900)
        self.installed.pack(anchor="w", padx=8)

        # ---- per-mode models, fast chat, downloads
        section(self, "Model upgrades").pack(anchor="w", padx=8, pady=(20, 0))
        hint(self, "Give one head a better model for one rite only (for example Forge -> Voice -> qwen2.5-coder). "
                   "Leave the model blank and press Set to remove an override.", 940).pack(anchor="w", padx=8)
        m = self._row()
        self.ov_mode = ctk.StringVar(value="Forge")
        ctk.CTkOptionMenu(m, values=["Forge", "Scribe", "Seance", "Tutor", "Treasury", "Hunt"], variable=self.ov_mode, width=130,
                          fg_color=T.PANEL2, button_color=T.BLOOD_DK, font=T.font("body", 14)).pack(side="left")
        self.ov_role = ctk.StringVar(value="generalist")
        ctk.CTkOptionMenu(m, values=["reasoner", "generalist", "editor"], variable=self.ov_role, width=130, fg_color=T.PANEL2,
                          button_color=T.BLOOD_DK, font=T.font("body", 14)).pack(side="left", padx=8)
        self.ov_model = entry(m, 200, "model name")
        self.ov_model.pack(side="left", padx=(0, 8))
        button(m, "Set", self._set_override, "normal", 70).pack(side="left")
        self.ov_text = hint(self, "", 940)
        self.ov_text.pack(anchor="w", padx=8)
        e = self._row()
        ctk.CTkLabel(e, text="Memory model", font=T.font("body", 15), text_color=T.DIM).pack(side="left")
        self.embed_model = entry(e, 170)
        self.embed_model.pack(side="left", padx=(6, 16))
        ctk.CTkLabel(e, text="Vision model", font=T.font("body", 15), text_color=T.DIM).pack(side="left")
        self.vision_model = entry(e, 170)
        self.vision_model.pack(side="left", padx=(6, 16))
        button(e, "Save", self._save_aux, "normal", 80).pack(side="left")
        self.rec_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.rec_frame.pack(fill="x", padx=8, pady=3)
        self.pull_state = ctk.CTkLabel(self, text="", font=T.font("body", 14), text_color=T.DIM, anchor="w")
        self.pull_state.pack(anchor="w", padx=8)

        # ---- rituals
        section(self, "Rituals (Cerberus speaks first)").pack(anchor="w", padx=8, pady=(20, 0))
        hint(self, "Scheduled messages from your profile's daily rhythm. They appear here as a toast and in the chat, and as phone "
                   "notifications on the web page. Switch one off to silence it; 'Now' fires it once to test.", 940).pack(anchor="w", padx=8)
        self.rit_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.rit_frame.pack(fill="x", padx=8, pady=3)
        self.rit_vars: dict[str, ctk.BooleanVar] = {}

        # ---- cloud
        section(self, "Cloud Handoff").pack(anchor="w", padx=8, pady=(20, 0))
        hint(self, "When the header switch is on, the three heads finish locally, then Cerberus squeezes their work into one short prompt "
                   "and sends only that to the cloud model you choose. Keys are stored locally and never displayed again.", 940).pack(anchor="w", padx=8)
        c1 = self._row()
        ctk.CTkLabel(c1, text="Provider", font=T.font("body", 15), text_color=T.DIM).pack(side="left")
        self.prov_var = ctk.StringVar()
        self.prov_menu = ctk.CTkOptionMenu(c1, values=["..."], variable=self.prov_var, width=200, fg_color=T.PANEL2,
                                           button_color=T.BLOOD_DK, font=T.font("body", 14), command=self._provider_picked)
        self.prov_menu.pack(side="left", padx=(6, 16))
        ctk.CTkLabel(c1, text="Model", font=T.font("body", 15), text_color=T.DIM).pack(side="left")
        self.cmodel = entry(c1, 210)
        self.cmodel.pack(side="left", padx=(6, 16))
        ctk.CTkLabel(c1, text="Max handoff tokens", font=T.font("body", 15), text_color=T.DIM).pack(side="left")
        self.budget = entry(c1, 80)
        self.budget.pack(side="left", padx=6)
        c2 = self._row()
        ctk.CTkLabel(c2, text="API key", font=T.font("body", 15), text_color=T.DIM).pack(side="left")
        self.key = entry(c2, 420, "paste a key to save or replace it", show="*")
        self.key.pack(side="left", padx=(6, 10))
        self.key_state = ctk.CTkLabel(c2, text="", font=T.font("body", 14), text_color=T.DIM)
        self.key_state.pack(side="left")
        c3 = self._row()
        self.cloud_enable = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(c3, text="Cloud Handoff enabled", variable=self.cloud_enable, font=T.font("head", 13), text_color=T.GOLD,
                      progress_color=T.GOLD_DK, button_color=T.GOLD).pack(side="left")
        button(c3, "Save cloud settings", self._save_cloud, "normal", 190).pack(side="left", padx=16)

        # ---- LAN
        section(self, "Home Wi-Fi access (other devices)").pack(anchor="w", padx=8, pady=(20, 0))
        self.lan_text = hint(self, "", 940)
        self.lan_text.pack(anchor="w", padx=8)
        l = self._row()
        self.token_box = entry(l, 360, "", "")
        self.token_box.pack(side="left")
        button(l, "Show token", self._show_token, "normal", 120).pack(side="left", padx=8)
        button(l, "Copy token", self._copy_token, "normal", 120).pack(side="left")

        # ---- workspace
        section(self, "Workspace folder (the sandbox)").pack(anchor="w", padx=8, pady=(20, 0))
        hint(self, "Cerberus can only read and write files inside this folder. Generated projects land here.", 940).pack(anchor="w", padx=8)
        w = self._row()
        self.ws = entry(w, 560)
        self.ws.pack(side="left")
        button(w, "Browse", self._browse, "normal", 90).pack(side="left", padx=8)
        button(w, "Save", self._save_ws, "normal", 80).pack(side="left")

        # ---- backup
        section(self, "Backup and restore").pack(anchor="w", padx=8, pady=(20, 0))
        self.data_text = hint(self, "", 940)
        self.data_text.pack(anchor="w", padx=8)
        hint(self, "A backup holds your chats, profile, agents and settings. API keys and tokens are NOT included (they are sealed to this "
                   "Windows account); re-enter them after restoring on another PC. A copy is also made automatically once a day.", 940).pack(anchor="w", padx=8)
        b = self._row()
        button(b, "Export backup...", self._export, "gold", 170).pack(side="left")
        button(b, "Restore from backup...", self._restore, "normal", 200).pack(side="left", padx=8)
        ctk.CTkLabel(self, text="", height=20).pack()

    # ------------------------------------------------------------ overview: thinking, this computer, access, plugins, projects
    def _build_overview(self) -> None:
        section(self, "Start here").pack(anchor="w", padx=8, pady=(6, 0))
        hint(self, "Pick a rite on the left, type, press Enter. Cerberus never changes your computer without asking you first, and "
                   "'Seal the Gates' in the top corner stops everything at once. This page is where you adjust how it works; "
                   "you never have to touch it for normal use.", 940).pack(anchor="w", padx=8)

        section(self, "Thinking: how carefully the heads work").pack(anchor="w", padx=8, pady=(18, 0))
        self.think_var = ctk.StringVar(value="Careful")
        self.think_seg = ctk.CTkSegmentedButton(self, values=[v["label"] for v in LEVELS.values()], variable=self.think_var,
                                                command=self._think_picked, font=T.font("body", 15), fg_color=T.PANEL2,
                                                selected_color=T.BLOOD_DK, selected_hover_color=T.BLOOD, unselected_color=T.PANEL,
                                                unselected_hover_color=T.PANEL2)
        self.think_seg.pack(anchor="w", padx=8, pady=4)
        self.think_text = hint(self, "", 940)
        self.think_text.pack(anchor="w", padx=8)

        section(self, "This computer").pack(anchor="w", padx=8, pady=(18, 0))
        self.sys_text = hint(self, "Reading your computer...", 940)
        self.sys_text.pack(anchor="w", padx=8)
        r = self._row()
        button(r, "Tune for this computer", self._tune, "gold", 210).pack(side="left")
        button(r, "Refresh", self._load_overview, "normal", 100).pack(side="left", padx=8)

        section(self, "The council's record").pack(anchor="w", padx=8, pady=(18, 0))
        self.council_text = hint(self, "", 940)
        self.council_text.pack(anchor="w", padx=8)
        r = self._row()
        button(r, "Forget what it learned", self._forget_lessons, "normal", 210).pack(side="left")

        section(self, "Full computer access").pack(anchor="w", padx=8, pady=(18, 0))
        hint(self, "Lets the heads look at and change files anywhere on this computer. Every change must be approved by all three heads "
                   "AND by you, can be undone ('undo that' in chat), and deleted files are kept, not destroyed. Windows, Program Files, "
                   "passwords, keys and Cerberus's own files stay off limits even when this is on. Phones and other devices never get it.", 940
             ).pack(anchor="w", padx=8)
        self.access_var = ctk.BooleanVar(value=True)
        r = self._row()
        ctk.CTkSwitch(r, text="Full computer access", variable=self.access_var, font=T.font("head", 13), text_color=T.GOLD,
                      progress_color=T.GOLD_DK, button_color=T.GOLD, command=self._save_access).pack(side="left")

        section(self, "Plugins").pack(anchor="w", padx=8, pady=(18, 0))
        self.plugin_text = hint(self, "", 940)
        self.plugin_text.pack(anchor="w", padx=8)
        r = self._row()
        button(r, "Open plugins folder", self._open_plugins, "normal", 200).pack(side="left")

        section(self, "Your projects").pack(anchor="w", padx=8, pady=(18, 0))
        hint(self, "Register your own programs here so Cerberus can run them by name (all three heads must approve each run). "
                   "The run command is saved by you; the heads can only pick a project, never invent a command.", 940).pack(anchor="w", padx=8)
        self.proj_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.proj_frame.pack(fill="x", padx=8, pady=3)
        r = self._row()
        self.p_name = entry(r, 110, "name")
        self.p_name.pack(side="left", padx=(0, 6))
        self.p_path = entry(r, 210, "folder")
        self.p_path.pack(side="left", padx=(0, 6))
        button(r, "Browse", self._browse_project, "normal", 80).pack(side="left", padx=(0, 6))
        self.p_run = entry(r, 170, "command, e.g. python main.py")
        self.p_run.pack(side="left", padx=(0, 6))
        self.p_about = entry(r, 130, "what it does")
        self.p_about.pack(side="left", padx=(0, 6))
        button(r, "Add", self._add_project, "normal", 70).pack(side="left")
        self.projects: dict = {}

        section(self, "Resume Studio (Hunt)").pack(anchor="w", padx=8, pady=(18, 0))
        hint(self, "Connects your Resume project to the Hunt rite: Cerberus finds jobs, tailors your own resume bullets to one of them, "
                   "then opens its application page and fills in your contact details. It never submits anything and never uploads a file; "
                   "you review and send it yourself. Say 'find jobs for ...' in Hunt, then 'tailor job 3', then 'stage job 3'.", 940).pack(anchor="w", padx=8)
        self.resume_state = hint(self, "", 940)
        self.resume_state.pack(anchor="w", padx=8)
        r = self._row()
        self.r_folder = entry(r, 480, "Resume project folder (leave blank to look in the usual place)")
        self.r_folder.pack(side="left")
        button(r, "Browse", lambda: self._pick_dir(self.r_folder), "normal", 80).pack(side="left", padx=8)
        r = self._row()
        self.r_base = entry(r, 480, "your base resume (.docx or text file); the only source of facts")
        self.r_base.pack(side="left")
        button(r, "Browse", self._pick_resume_file, "normal", 80).pack(side="left", padx=8)
        r = self._row()
        self.r_name = entry(r, 200, "your name")
        self.r_name.pack(side="left", padx=(0, 6))
        self.r_email = entry(r, 230, "email")
        self.r_email.pack(side="left", padx=(0, 6))
        self.r_phone = entry(r, 150, "phone")
        self.r_phone.pack(side="left", padx=(0, 6))
        self.r_linkedin = entry(r, 260, "LinkedIn address")
        self.r_linkedin.pack(side="left")
        hint(self, "Your own job feeds, one web address per line, with {query} and {location} where they belong "
                   "(blank = the built-in Remotive and Arbeitnow boards).", 940).pack(anchor="w", padx=8)
        self.r_feeds = textbox(self, 70, mono=True)
        self.r_feeds.pack(fill="x", padx=8, pady=3)
        r = self._row()
        button(r, "Save Resume Studio", self._save_resume, "normal", 200).pack(side="left")

    def _pick_dir(self, box) -> None:
        d = fd.askdirectory(title="Choose the folder")
        if d:
            box.delete(0, "end")
            box.insert(0, d)

    def _pick_resume_file(self) -> None:
        f = fd.askopenfilename(title="Choose your base resume", filetypes=[("Resume", "*.docx *.txt *.md"), ("All files", "*.*")])
        if f:
            self.r_base.delete(0, "end")
            self.r_base.insert(0, f)

    def _load_resume(self) -> None:
        def ok(d: dict) -> None:
            for box, val in ((self.r_folder, d["path"]), (self.r_base, d["base_resume"]), (self.r_name, d["applicant"]["name"]),
                             (self.r_email, d["applicant"]["email"]), (self.r_phone, d["applicant"]["phone"]),
                             (self.r_linkedin, d["applicant"]["linkedin"])):
                box.delete(0, "end")
                box.insert(0, val)
            self.r_feeds.delete("1.0", "end")
            self.r_feeds.insert("1.0", "\n".join(d["feed_urls"]))
            self.resume_state.configure(text=(f"Found the Resume project at {d['folder']}." if d["folder"] else f"Not ready: {d['problem']}")
                                        + (f"  Last search: '{d['last_query']}' ({d['jobs']} jobs)." if d.get("jobs") else ""))
        self.app.run_bg(lambda: self.app.api.get("/resume"), ok, lambda e: self.resume_state.configure(text=str(e)))

    def _save_resume(self) -> None:
        body = {"path": self.r_folder.get().strip(), "base_resume": self.r_base.get().strip(),
                "feed_urls": self.r_feeds.get("1.0", "end").strip(),
                "applicant": {"name": self.r_name.get(), "email": self.r_email.get(), "phone": self.r_phone.get(),
                              "linkedin": self.r_linkedin.get()}}
        self.app.run_bg(lambda: self.app.api.put("/resume", body), lambda d: (self._load_resume(), self.app.toast("Resume Studio saved.", "ok")))

    def show_thinking(self, key: str) -> None:
        if key in LEVELS:
            self.think_var.set(LEVELS[key]["label"])
            self.think_text.configure(text=LEVELS[key]["blurb"])

    def _think_picked(self, label: str) -> None:
        key = next((k for k, v in LEVELS.items() if v["label"] == label), "careful")
        self.show_thinking(key)
        self.app.run_bg(lambda: self.app.api.put("/settings", {"thinking": key}), self.app.refresh_settings)

    def _load_overview(self) -> None:
        def sys_ok(d: dict) -> None:
            rec, pr = d["recommended"], d["pressure"]
            tier = {"strong": "strong", "good": "capable", "light": "modest"}.get(rec["tier"], rec["tier"])
            now = f"  Free memory right now: {pr['free_gb']} GB." + ("  (Low: Cerberus will switch to Quick until it recovers.)" if pr["low"] else "")
            s = d["settings"]
            self.sys_text.configure(text=f"{d['summary']}.\nThis is a {tier} machine. {rec['note']}{now}\n"
                                         f"Currently using a memory window of {s['num_ctx']} and keeping models loaded for {s['keep_alive']}; "
                                         f"recommended: {rec['num_ctx']} and {rec['keep_alive']}.")
            c = d["council"]
            self.council_text.configure(
                text=f"Answers checked: {c['checked']}  |  clean first time: {c['clean']}  |  mistakes caught: {c['caught']}  |  "
                     f"fixed: {c['fixed']}  |  false alarms overruled: {c['false_alarms']}\n"
                     f"Changes to your computer approved: {c['changes_approved']}  |  blocked by a head: {c['changes_blocked']}\n"
                     f"Lessons remembered from past mistakes: {d['lessons']}")
            self.access_var.set(d["full_access"])
            self.show_thinking(d["thinking"]["current"])
        self.app.run_bg(lambda: self.app.api.get("/system"), sys_ok, lambda e: self.sys_text.configure(text=f"Could not read the computer: {e}"))

        def plug_ok(d: dict) -> None:
            rows = d["plugins"]
            lines = [f"{p['file']}: " + (", ".join(p["tools"]) if p["ok"] else f"FAILED ({p['error']})") for p in rows]
            self.plugin_text.configure(text=f"Drop a .py file in {d['folder']} (a template is already there) and restart Cerberus. "
                                            "Only add plugins you wrote or trust.\nLoaded: " + ("; ".join(lines) if lines else "none yet"))
            self._plugin_folder = d["folder"]
        self.app.run_bg(lambda: self.app.api.get("/plugins"), plug_ok, lambda e: None)
        self.app.run_bg(lambda: self.app.api.get("/projects"), self._projects_loaded, lambda e: None)

    def _tune(self) -> None:
        def ok(r: dict) -> None:
            self.app.toast(f"Tuned: memory window {r['applied']['num_ctx']}, models kept loaded {r['applied']['keep_alive']}.", "ok")
            self._load_overview()
        self.app.run_bg(lambda: self.app.api.post("/system/tune"), ok)

    def _forget_lessons(self) -> None:
        self.app.run_bg(lambda: self.app.api.delete("/council/lessons"),
                        lambda r: (self.app.toast("Forgotten.", "ok"), self._load_overview()))

    def _save_access(self) -> None:
        want = self.access_var.get()
        self.app.run_bg(lambda: self.app.api.put("/settings", {"full_access": want}),
                        lambda cfg: (self.app.refresh_settings(cfg), self.app.toast(
                            "Full computer access is ON (every change still needs all three heads and you)." if want
                            else "Full computer access is OFF.", "warn" if want else "ok")))

    def _open_plugins(self) -> None:
        import os
        folder = getattr(self, "_plugin_folder", "")
        if folder and os.path.isdir(folder):
            os.startfile(folder)  # noqa: S606 - opens the plugins folder in Explorer

    def _projects_loaded(self, projects: dict) -> None:
        self.projects = projects
        for w in self.proj_frame.winfo_children():
            w.destroy()
        if not projects:
            ctk.CTkLabel(self.proj_frame, text="No projects registered yet.", font=T.font("body", 14), text_color=T.DIM).pack(anchor="w")
        for name, spec in projects.items():
            line = ctk.CTkFrame(self.proj_frame, fg_color="transparent")
            line.pack(fill="x", pady=1)
            ctk.CTkLabel(line, text=f"{name}: {spec.get('about', '')}  [{spec.get('path', '')}  runs: {spec.get('run', '')}]",
                         font=T.font("body", 14), text_color=T.DIM, anchor="w", wraplength=760, justify="left").pack(side="left")
            button(line, "Remove", lambda n=name: self._remove_project(n), "normal", 80).pack(side="right")

    def _save_projects(self, projects: dict) -> None:
        self.app.run_bg(lambda: self.app.api.put("/projects", projects),
                        lambda r: (self._projects_loaded(r), self.app.toast("Projects saved.", "ok")))

    def _add_project(self) -> None:
        name, path, run = self.p_name.get().strip(), self.p_path.get().strip(), self.p_run.get().strip()
        if not (name and path and run):
            self.app.toast("A project needs a name, a folder and a command.", "warn")
            return
        self._save_projects({**self.projects, name: {"path": path, "run": run, "about": self.p_about.get().strip()}})
        for box in (self.p_name, self.p_path, self.p_run, self.p_about):
            box.delete(0, "end")

    def _remove_project(self, name: str) -> None:
        self._save_projects({k: v for k, v in self.projects.items() if k != name})

    def _browse_project(self) -> None:
        d = fd.askdirectory(title="Choose the project's folder")
        if d:
            self.p_path.delete(0, "end")
            self.p_path.insert(0, d)

    # ------------------------------------------------------------ data
    def on_ready(self) -> None:
        cfg = self.app.cfg
        for role, e in self.models.items():
            e.delete(0, "end")
            e.insert(0, cfg["models"].get(role, ""))
        self.num_ctx.delete(0, "end")
        self.num_ctx.insert(0, str(cfg["num_ctx"]))
        self.provider_ids = {p["label"]: pid for pid, p in cfg["cloud"]["providers"].items()}
        self.prov_menu.configure(values=list(self.provider_ids))
        cur = cfg["cloud"]["provider"]
        self.prov_var.set(cfg["cloud"]["providers"][cur]["label"])
        self._provider_picked(self.prov_var.get())
        self.budget.delete(0, "end")
        self.budget.insert(0, str(cfg["cloud"]["max_handoff_tokens"]))
        self.cloud_enable.set(cfg["cloud"]["enabled"])
        self.ws.delete(0, "end")
        self.ws.insert(0, cfg["workspace"])
        self._show_overrides()
        self.show_thinking(cfg.get("thinking", "careful"))
        self.access_var.set(cfg.get("full_access", True))
        self._load_resume()
        for box, key in ((self.embed_model, "embed_model"), (self.vision_model, "vision_model")):
            box.delete(0, "end")
            box.insert(0, cfg.get(key, ""))

    def _show_overrides(self) -> None:
        mm = self.app.cfg.get("mode_models") or {}
        names = {m["key"]: m["label"] for m in self.app.modes}
        lines = [f"{names.get(mode, mode)}: " + ", ".join(f"{role} -> {name}" for role, name in roles.items())
                 for mode, roles in mm.items() if roles]
        self.ov_text.configure(text="Active overrides: " + ("; ".join(lines) if lines else "none"))

    def _set_override(self) -> None:
        mm = {k: dict(v) for k, v in (self.app.cfg.get("mode_models") or {}).items()}
        keys = {m["label"]: m["key"] for m in self.app.modes}
        mode, role, name = keys.get(self.ov_mode.get(), "coding"), self.ov_role.get(), self.ov_model.get().strip()
        if name:
            mm.setdefault(mode, {})[role] = name
        else:
            mm.get(mode, {}).pop(role, None)
        mm = {k: v for k, v in mm.items() if v}

        def ok(cfg: dict) -> None:
            self.app.refresh_settings(cfg)
            self._show_overrides()
            self.app.toast("Overrides saved.", "ok")
        self.app.run_bg(lambda: self.app.api.put("/settings", {"mode_models": mm}), ok)

    def _save_aux(self) -> None:
        patch = {"embed_model": self.embed_model.get().strip(), "vision_model": self.vision_model.get().strip()}
        self.app.run_bg(lambda: self.app.api.put("/settings", patch),
                        lambda cfg: (self.app.refresh_settings(cfg), self.app.toast("Saved.", "ok")))

    def _load_rituals(self) -> None:
        def ok(rows: list) -> None:
            for w in self.rit_frame.winfo_children():
                w.destroy()
            self.rit_vars = {}
            for r in rows:
                line = ctk.CTkFrame(self.rit_frame, fg_color="transparent")
                line.pack(fill="x", pady=1)
                var = ctk.BooleanVar(value=r["enabled"])
                self.rit_vars[r["id"]] = var
                ctk.CTkSwitch(line, text=f"{r['time']}  {r['title']}  ({r['days']})", variable=var, font=T.font("body", 14),
                              text_color=T.GOLD, progress_color=T.GOLD_DK, button_color=T.GOLD, command=self._save_rituals).pack(side="left")
                button(line, "Now", lambda i=r["id"]: self._fire_ritual(i), "normal", 70).pack(side="right")
        self.app.run_bg(lambda: self.app.api.get("/rituals"), ok, lambda e: None)

    def _save_rituals(self) -> None:
        off = [i for i, v in self.rit_vars.items() if not v.get()]
        patch = {"rituals": {"enabled": True, "use_model": True, "off": off}}
        self.app.run_bg(lambda: self.app.api.put("/settings", patch), self.app.refresh_settings)

    def _fire_ritual(self, rid: str) -> None:
        self.app.toast("Summoning it...", "info")
        self.app.run_bg(lambda: self.app.api.post(f"/rituals/{rid}/fire", timeout=120), lambda r: self.app.toast("Delivered.", "ok"))

    def _load_recommended(self) -> None:
        def ok(rows: list) -> None:
            for w in self.rec_frame.winfo_children():
                w.destroy()
            for r in rows:
                line = ctk.CTkFrame(self.rec_frame, fg_color="transparent")
                line.pack(fill="x", pady=1)
                state = "installed" if r["installed"] else f"{r['size']}"
                ctk.CTkLabel(line, text=f"{r['name']}  ({state}) - {r['why']}", font=T.font("body", 14), text_color=T.DIM,
                             anchor="w", wraplength=700, justify="left").pack(side="left")
                if not r["installed"]:
                    button(line, "Download", lambda n=r["name"]: self._pull(n), "gold", 100).pack(side="right")
        self.app.run_bg(lambda: self.app.api.get("/models/recommended"), ok, lambda e: None)

    def _pull(self, name: str) -> None:
        def progress(ch: dict) -> None:
            if ch.get("error"):
                msg = f"Download of {name} failed: {ch['error']}"
            elif ch.get("done"):
                msg = f"{name} downloaded."
            elif ch.get("total"):
                msg = f"Downloading {name}: {100 * ch.get('completed', 0) // ch['total']}%"
            else:
                msg = f"{name}: {ch.get('status', '')}"
            self.app.post_ui(lambda m: self.pull_state.configure(text=m), msg)
            if ch.get("done"):
                self.app.post_ui(lambda _: self._load_recommended(), None)
        self.pull_state.configure(text=f"Starting download of {name}...")
        threading.Thread(target=lambda: self.app.api.stream_pull(name, progress), daemon=True).start()

    def on_show(self) -> None:
        self.cloud_enable.set(self.app.cfg["cloud"]["enabled"])
        self._load_overview()
        self._load_audit()
        inst = self.app.status.get("installed") or []
        self.installed.configure(text="Installed in Ollama: " + (", ".join(inst) if inst else "(none found - is Ollama running?)"))
        self._load_recommended()
        self._load_rituals()
        self._load_data_info()
        self.app.run_bg(lambda: self.app.api.get("/network"), self._net_loaded, lambda e: None)

    def _load_audit(self) -> None:
        self.app.run_bg(lambda: self.app.api.get("/safety/audit", limit=80),
                        lambda lines: set_text(self.audit, "\n".join(reversed(lines)) or "(nothing recorded yet)"), lambda e: None)

    def _net_loaded(self, n: dict) -> None:
        ips = ", ".join(f"http://{ip}:{n['port']}" for ip in n["ips"]) or "(no network detected)"
        state = "The server is listening on your home network." if n["lan"] else \
            "The server is currently private to this computer. To let phones and other computers use Cerberus, close the app and run scripts\\start_lan_server.bat."
        self.lan_text.configure(text=f"{state}\nOther devices open: {ips}  - they must enter the token below the first time.")
        self._token = n.get("token", "")

    def _show_token(self) -> None:
        self.token_box.delete(0, "end")
        self.token_box.insert(0, getattr(self, "_token", ""))

    def _copy_token(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(getattr(self, "_token", ""))
        self.app.toast("Token copied.", "ok")

    # ------------------------------------------------------------ cloud
    def _provider_picked(self, label: str) -> None:
        pid = self.provider_ids.get(label)
        if not pid:
            return
        p = self.app.cfg["cloud"]["providers"][pid]
        self.cmodel.delete(0, "end")
        self.cmodel.insert(0, p["model"])
        self.key.delete(0, "end")
        self.key_state.configure(text=f"key saved ({p['api_key_hint']})" if p["api_key_set"] else "no key saved",
                                 text_color=T.OK if p["api_key_set"] else T.WARN)

    def _save_cloud(self) -> None:
        pid = self.provider_ids.get(self.prov_var.get())
        if not pid:
            return
        try:
            budget = int(self.budget.get().strip())
        except ValueError:
            self.app.toast("Max handoff tokens must be a whole number.", "warn")
            return
        prov: dict = {"model": self.cmodel.get().strip()}
        if self.key.get().strip():
            prov["api_key"] = self.key.get().strip()
        patch = {"cloud": {"enabled": self.cloud_enable.get(), "provider": pid, "max_handoff_tokens": budget, "providers": {pid: prov}}}

        def ok(cfg: dict) -> None:
            self.app.refresh_settings(cfg)
            self._provider_picked(self.prov_var.get())
            self.app.toast("Cloud settings saved.", "ok")
        self.app.run_bg(lambda: self.app.api.put("/settings", patch), ok)

    # ------------------------------------------------------------ models / ollama / workspace
    def _save_models(self) -> None:
        try:
            ctx = int(self.num_ctx.get().strip())
        except ValueError:
            self.app.toast("Context size must be a whole number (e.g. 8192).", "warn")
            return
        patch = {"models": {r: e.get().strip() for r, e in self.models.items() if e.get().strip()}, "num_ctx": ctx}
        self.app.run_bg(lambda: self.app.api.put("/settings", patch), lambda cfg: (self.app.refresh_settings(cfg), self.app.toast("Heads assigned.", "ok")))

    def _start_ollama(self) -> None:
        self.app.toast("Waking Ollama...", "info")

        def ok(r: dict) -> None:
            self.app.status = r["status"]
            self.app._apply_status()
            self.app.toast(r["message"], "ok" if r["status"]["ollama_up"] else "warn")
            self.on_show()
        self.app.run_bg(lambda: self.app.api.post("/ollama/start", timeout=60), ok)

    def _export(self) -> None:
        path = fd.asksaveasfilename(title="Save the backup", defaultextension=".zip", filetypes=[("Backup", "*.zip")],
                                    initialfile="cerberus-backup.zip")
        if path:
            self.app.run_bg(lambda: self.app.api.post("/backup", {"path": path}, timeout=120),
                            lambda r: self.app.toast(f"Backup saved ({r['size'] // 1024} KB).", "ok"))

    def _restore(self) -> None:
        path = fd.askopenfilename(title="Choose a Cerberus backup", filetypes=[("Backup", "*.zip")])
        if path:
            self.app.run_bg(lambda: self.app.api.post("/backup/restore", {"path": path}, timeout=120),
                            lambda r: self.app.toast(r["message"], "warn"))

    def _load_data_info(self) -> None:
        def ok(r: dict) -> None:
            self.data_text.configure(text=f"Your data lives in: {r['data_dir']}  (outside OneDrive and the project folder).\n"
                                          f"Automatic backups: {r['folder']}")
        self.app.run_bg(lambda: self.app.api.get("/backup/list"), ok, lambda e: None)

    def _browse(self) -> None:
        d = fd.askdirectory(title="Choose the workspace folder", initialdir=self.ws.get() or None)
        if d:
            self.ws.delete(0, "end")
            self.ws.insert(0, d)

    def _save_ws(self) -> None:
        path = self.ws.get().strip()
        if path:
            self.app.run_bg(lambda: self.app.api.put("/settings", {"workspace": path}),
                            lambda cfg: (self.app.refresh_settings(cfg), self.app.toast("Workspace set.", "ok")))
