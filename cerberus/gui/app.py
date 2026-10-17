"""The Cerberus window: header (sigil, switches, kill-switch), mode sidebar, and the six tabs."""
from __future__ import annotations

import queue
import threading
import time
import tkinter.messagebox as mb
from typing import Any, Callable, Optional

import customtkinter as ctk

from . import theme as T
from ..council import LEVELS
from .client import ApiClient, ApiError
from .widgets import ChatView, ConfirmDialog, HeadStrip, SigilCanvas, WardenDialog, button, entry

TAB_NAMES = ["Communion", "Library", "Legion", "Soul Ledger", "Treasury", "Grimoire", "Warden", "Chronicle", "Gates"]
MODE_BLURB = {"coding": "Forge: describe an app; three heads design, build and inspect it.",
              "writing": "Scribe: edit your draft, or let the ghost write in your voice.",
              "conversation": "Seance: speak with the spirit. Id, Super-Ego and Ego answer as one.",
              "teaching": "Tutor: small steps, hands-on, calm. Tell it how you learn best.",
              "wealth": "Treasury: quotes, budgets, plain-spoken money thinking. Not financial advice.",
              "jobs": "Hunt: resumes, cover letters, job searches and form-filling help."}
HOW_TO = ("Type below and press Enter (Shift+Enter for a new line). Under 'Thinking' on the right, Careful means all three "
          "heads work and the two that did not write the answer check it for mistakes. Everything important asks you first; "
          "the Gates tab explains and controls it all.")


class CerberusApp(ctk.CTk):
    def __init__(self, base_url: str, token: str = "", on_close: Optional[Callable[[], None]] = None) -> None:
        T.load_fonts()
        super().__init__(fg_color=T.BG)
        T.resolve_fonts(self)
        ctk.set_appearance_mode("dark")
        self.title("Cerberus")
        self.geometry("1280x820")
        self.minsize(1040, 680)
        self.api = ApiClient(base_url, token)
        self.on_close_cb = on_close
        self.ui_q: "queue.Queue[tuple[Callable, Any]]" = queue.Queue()
        self.modes: list[dict] = []
        self.mode = "conversation"
        self.status: dict = {}
        self.cfg: dict = {}
        self.running = False
        self.run_id: Optional[str] = None
        self.stop_flag = False
        self.run_token: Optional[object] = None
        self.shown_pending: dict[str, ConfirmDialog] = {}
        self.warden_dlg: Optional[WardenDialog] = None
        self.last_stage_key = ""
        self.last_stage_text = ""
        self.bright_last = True
        self.saw_files = False
        self.attached: list[str] = []
        self.workings_run = False
        self.tabs_by_name: dict[str, Any] = {}
        self._inflight: set[str] = set()
        self._history_token: Optional[object] = None
        self._closing = False
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(40, self._drain)
        self.run_bg(self._load_initial, self._initial_loaded, self._startup_failed)
        self.after(1500, self._poll_safety)
        self.after(6000, self._poll_status)
        self.after(4000, self._poll_notifications)
        self.after(2000, self._poll_warden)

    # ---------------------------------------------------------------- plumbing
    def run_bg(self, fn: Callable[[], Any], ok: Optional[Callable[[Any], None]] = None,
               err: Optional[Callable[[Exception], None]] = None) -> None:
        """Run `fn` in a thread; call ok(result) / err(exc) back on the UI thread (Tk is not thread-safe)."""
        def work() -> None:
            try:
                result = fn()
            except Exception as exc:  # noqa: BLE001 - reported to the user
                self.ui_q.put((err or self._toast_error, exc))
                return
            if ok:
                self.ui_q.put((ok, result))
        threading.Thread(target=work, daemon=True).start()

    def run_poll(self, key: str, fn: Callable[[], Any], ok: Callable[[Any], None],
                 err: Optional[Callable[[Exception], None]] = None) -> None:
        """run_bg for periodic pollers: skipped while the previous request for `key` is still in flight."""
        if key in self._inflight:
            return
        self._inflight.add(key)

        def done(r: Any) -> None:
            self._inflight.discard(key)
            ok(r)

        def failed(e: Exception) -> None:
            self._inflight.discard(key)
            if err:
                err(e)
        self.run_bg(fn, done, failed)

    def post_ui(self, fn: Callable, arg: Any = None) -> None:
        self.ui_q.put((fn, arg))

    def _drain(self) -> None:
        try:
            while True:
                fn, arg = self.ui_q.get_nowait()
                try:
                    fn(arg)
                except Exception as exc:  # a UI bug must not kill the event pump
                    print("UI callback error:", repr(exc))
        except queue.Empty:
            pass
        self.after(40, self._drain)

    def toast(self, text: str, kind: str = "info") -> None:
        color = {"info": T.DIM, "ok": T.OK, "error": "#ff7b7b", "warn": T.WARN}[kind]
        self.footer_msg.configure(text=text, text_color=color)
        stamp = time.time()
        self._toast_stamp = stamp
        self.after(9000, lambda: self.footer_msg.configure(text="") if getattr(self, "_toast_stamp", 0) == stamp else None)

    def _toast_error(self, exc: Exception) -> None:
        self.toast(str(exc), "error")

    def _close(self) -> None:
        self.stop_flag = True
        self._closing = True
        if self.on_close_cb:
            self.on_close_cb()
        self.destroy()

    # ---------------------------------------------------------------- layout
    def _build(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self._build_header()
        self._build_sidebar()
        self.tabs = ctk.CTkTabview(self, fg_color=T.BG, segmented_button_fg_color=T.PANEL,
                                   segmented_button_selected_color=T.BLOOD_DK, segmented_button_selected_hover_color=T.BLOOD,
                                   segmented_button_unselected_color=T.PANEL, segmented_button_unselected_hover_color=T.PANEL2,
                                   text_color=T.BONE, border_width=1, border_color=T.EDGE, command=self._tab_changed)
        self.tabs.grid(row=1, column=1, sticky="nsew", padx=(0, 10), pady=(0, 4))
        self.tabs._segmented_button.configure(font=T.font("head", 14))
        for name in TAB_NAMES:
            self.tabs.add(name)
        self._build_communion(self.tabs.tab("Communion"))
        from .tab_chronicle import ChronicleTab
        from .tab_files import FilesTab
        from .tab_gates import GatesTab
        from .tab_grimoire import GrimoireTab
        from .tab_legion import LegionTab
        from .tab_soul import SoulTab
        from .tab_treasury import TreasuryTab
        from .tab_warden import WardenTab
        for name, cls in (("Library", FilesTab), ("Legion", LegionTab), ("Soul Ledger", SoulTab), ("Treasury", TreasuryTab),
                          ("Grimoire", GrimoireTab), ("Warden", WardenTab), ("Chronicle", ChronicleTab), ("Gates", GatesTab)):
            tab = cls(self.tabs.tab(name), self)
            tab.pack(fill="both", expand=True)
            self.tabs_by_name[name] = tab
        footer = ctk.CTkFrame(self, fg_color="transparent", height=26)
        footer.grid(row=2, column=0, columnspan=2, sticky="ew", padx=12, pady=(0, 6))
        self.footer_msg = ctk.CTkLabel(footer, text="", font=T.font("body", 14), text_color=T.DIM, anchor="w")
        self.footer_msg.pack(side="left")
        self.bond_label = ctk.CTkLabel(footer, text="", font=T.font("body", 14), text_color=T.GOLD_DK)
        self.bond_label.pack(side="right")

    def _build_header(self) -> None:
        bar = ctk.CTkFrame(self, fg_color=T.BG, height=84)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=(8, 4))
        self.sigil = SigilCanvas(bar, 72)
        self.sigil.pack(side="left", padx=(4, 10))
        titles = ctk.CTkFrame(bar, fg_color="transparent")
        titles.pack(side="left")
        ctk.CTkLabel(titles, text="Cerberus", font=T.font("title", 46), text_color=T.BLOOD).pack(anchor="w")
        self.subtitle = ctk.CTkLabel(titles, text="the three-headed guardian", font=T.font("head", 12), text_color=T.GOLD_DK)
        self.subtitle.pack(anchor="w")

        right = ctk.CTkFrame(bar, fg_color="transparent")
        right.pack(side="right")
        self.seal_btn = button(right, "Seal the Gates", self._seal, "danger", 140)
        self.seal_btn.grid(row=0, column=3, rowspan=2, padx=(14, 4))
        self.cloud_var = ctk.BooleanVar(value=False)
        self.cloud_switch = ctk.CTkSwitch(right, text="Cloud Handoff", variable=self.cloud_var, command=self._toggle_cloud,
                                          font=T.font("head", 13), text_color=T.GOLD, progress_color=T.GOLD_DK, button_color=T.GOLD)
        self.cloud_switch.grid(row=0, column=0, padx=10, pady=2, sticky="w")
        self.auto_var = ctk.BooleanVar(value=False)
        self.auto_switch = ctk.CTkSwitch(right, text="Autonomous Agent Mode", variable=self.auto_var, command=self._toggle_auto,
                                         font=T.font("head", 13), text_color=T.BLOOD_HI, progress_color=T.BLOOD, button_color=T.BLOOD_HI)
        self.auto_switch.grid(row=1, column=0, padx=10, pady=2, sticky="w")
        self.ollama_label = ctk.CTkLabel(right, text="Ollama: ...", font=T.font("body", 14), text_color=T.DIM, anchor="e")
        self.ollama_label.grid(row=0, column=1, columnspan=2, padx=10, sticky="e")
        self.pending_label = ctk.CTkLabel(right, text="", font=T.font("body", 14), text_color=T.WARN, anchor="e")
        self.pending_label.grid(row=1, column=1, columnspan=2, padx=10, sticky="e")

    def _build_sidebar(self) -> None:
        side = ctk.CTkFrame(self, fg_color=T.PANEL, corner_radius=8, border_width=1, border_color=T.EDGE, width=200)
        side.grid(row=1, column=0, sticky="ns", padx=(10, 8), pady=(0, 4))
        side.grid_propagate(False)
        ctk.CTkLabel(side, text="Choose a Rite", font=T.font("head", 14, "bold"), text_color=T.GOLD).pack(pady=(12, 6))
        self.mode_buttons: dict[str, ctk.CTkButton] = {}
        self.side = side

    def _fill_sidebar(self) -> None:
        for m in self.modes:
            b = ctk.CTkButton(self.side, text=m["label"], height=46, corner_radius=6, anchor="w",
                              font=T.font("head", 17), command=lambda k=m["key"]: self.select_mode(k),
                              fg_color="transparent", hover_color=T.PANEL2, text_color=T.TEXT, border_width=1, border_color=T.EDGE)
            b.pack(fill="x", padx=10, pady=4)
            self.mode_buttons[m["key"]] = b

    # ---------------------------------------------------------------- Communion tab
    def _build_communion(self, parent) -> None:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(1, weight=1)
        self.heads = HeadStrip(parent)
        self.heads.grid(row=0, column=0, sticky="ew", pady=(2, 6))
        self.chat = ChatView(parent)
        self.chat.grid(row=1, column=0, sticky="nsew")
        self.blurb = ctk.CTkLabel(parent, text="", font=T.font("body", 14), text_color=T.DIM, anchor="w")
        self.blurb.grid(row=2, column=0, sticky="ew", pady=(4, 0))

        opts = ctk.CTkFrame(parent, fg_color="transparent")
        opts.grid(row=3, column=0, sticky="ew")
        self.think_var = ctk.StringVar(value="Careful")
        self.think_seg = ctk.CTkSegmentedButton(opts, values=[v["label"] for v in LEVELS.values()], variable=self.think_var,
                                                command=self._think_changed, font=T.font("body", 14), fg_color=T.PANEL2,
                                                selected_color=T.BLOOD_DK, selected_hover_color=T.BLOOD, unselected_color=T.PANEL,
                                                unselected_hover_color=T.PANEL2)
        self.think_seg.pack(side="right")
        ctk.CTkLabel(opts, text="Thinking:", font=T.font("body", 14), text_color=T.DIM).pack(side="right", padx=(10, 6))
        self.work_var = ctk.BooleanVar(value=False)
        self.work_check = ctk.CTkCheckBox(opts, text="Show the workings", variable=self.work_var, command=self._workings_changed,
                                          font=T.font("body", 14), text_color=T.DIM, fg_color=T.BLOOD, hover_color=T.BLOOD_HI,
                                          border_color=T.GOLD_DK)
        self.work_check.pack(side="right", padx=(0, 18))
        self.act_var = ctk.BooleanVar(value=False)
        self.act_check = ctk.CTkCheckBox(opts, text="Let Cerberus act (use tools)", variable=self.act_var, font=T.font("body", 14),
                                         text_color=T.TEXT, fg_color=T.BLOOD, hover_color=T.BLOOD_HI, border_color=T.GOLD_DK)
        self.act_check.pack(side="left", padx=(0, 14))
        self.save_var = ctk.BooleanVar(value=False)
        self.save_check = ctk.CTkCheckBox(opts, text="Save files to workspace", variable=self.save_var, font=T.font("body", 14),
                                          text_color=T.TEXT, fg_color=T.BLOOD, hover_color=T.BLOOD_HI, border_color=T.GOLD_DK)
        self.project_entry = entry(opts, 160, "project folder")
        self.verify_var = ctk.BooleanVar(value=False)
        self.verify_check = ctk.CTkCheckBox(opts, text="Run its tests & repair", variable=self.verify_var, font=T.font("body", 14),
                                            text_color=T.TEXT, fg_color=T.BLOOD, hover_color=T.BLOOD_HI, border_color=T.GOLD_DK)
        self.sub_var = ctk.StringVar(value="auto")
        self.sub_menu = ctk.CTkOptionMenu(opts, values=["auto", "ghostwrite", "edit"], variable=self.sub_var, width=130,
                                          fg_color=T.PANEL2, button_color=T.BLOOD_DK, font=T.font("body", 14))
        self.sub_label = ctk.CTkLabel(opts, text="Scribe style:", font=T.font("body", 14), text_color=T.DIM)

        self.attach_bar = ctk.CTkFrame(parent, fg_color="transparent")
        self.attach_bar.grid(row=4, column=0, sticky="ew", pady=(6, 0))
        button(self.attach_bar, "Attach files...", lambda: self.tabs.set("Library"), "gold", 130).pack(side="left")
        self.attach_label = ctk.CTkLabel(self.attach_bar, text="", font=T.font("body", 14), text_color=T.GOLD, anchor="w")
        self.attach_label.pack(side="left", padx=10)
        self.attach_clear = ctk.CTkButton(self.attach_bar, text="remove", width=10, height=18, fg_color="transparent",
                                          hover_color=T.PANEL2, text_color=T.DIM, font=T.font("body", 12), command=self.clear_attached)

        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.grid(row=5, column=0, sticky="ew", pady=(4, 2))
        row.grid_columnconfigure(0, weight=1)
        self.input = ctk.CTkTextbox(row, height=78, fg_color=T.PANEL, border_width=1, border_color=T.EDGE, text_color=T.TEXT,
                                    font=T.font("body", 17), wrap="word")
        self.input.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.input.bind("<Return>", self._enter_pressed)
        btns = ctk.CTkFrame(row, fg_color="transparent")
        btns.grid(row=0, column=1)
        self.send_btn = button(btns, "Invoke", self.send, "normal", 110)
        self.send_btn.pack(pady=(0, 4))
        self.stop_btn = button(btns, "Stop", self.stop, "gold", 110)
        self.stop_btn.pack()
        self.stop_btn.configure(state="disabled")
        clear = ctk.CTkButton(parent, text="clear this rite's history", width=10, height=18, fg_color="transparent", hover_color=T.PANEL2,
                              text_color=T.DIM, font=T.font("body", 12), command=self._clear_history)
        clear.grid(row=6, column=0, sticky="e")

    # ---------------------------------------------------------------- attached files and the "workings" switch
    def attach_files(self, paths: list[str]) -> None:
        for p in paths:
            if p not in self.attached:
                self.attached.append(p)
        self.attached = self.attached[:8]
        self._show_attached()
        self.tabs.set("Communion")

    def clear_attached(self) -> None:
        self.attached = []
        self._show_attached()

    def _show_attached(self) -> None:
        n = len(self.attached)
        if not n:
            self.attach_label.configure(text="")
            self.attach_clear.pack_forget()
            return
        names = ", ".join(p.replace("\\", "/").rsplit("/", 1)[-1] for p in self.attached[:3])
        more = f" and {n - 3} more" if n > 3 else ""
        self.attach_label.configure(text=f"{n} file{'s' if n != 1 else ''} attached to your next message: {names}{more}")
        self.attach_clear.pack(side="left")

    def _workings_changed(self) -> None:
        want = self.work_var.get()
        self.toast("The stages, thinking and council notes will show." if want else "Only answers will show.", "info")
        self.run_bg(lambda: self.api.put("/settings", {"show_workings": want}), self.refresh_settings, lambda e: None)

    def _status_line(self, text: str) -> None:
        self.blurb.configure(text=text)

    def _restore_blurb(self) -> None:
        info = next((m for m in self.modes if m["key"] == self.mode), None)
        if info:
            self.blurb.configure(text=MODE_BLURB.get(self.mode, info["description"]))

    def _enter_pressed(self, event) -> str:
        if event.state & 0x0001:       # Shift+Enter = newline
            return ""
        self.send()
        return "break"

    # ---------------------------------------------------------------- startup
    def _load_initial(self) -> dict:
        deadline = time.time() + 30
        while True:                     # the embedded server may still be starting
            try:
                self.api.get("/status")
                break
            except ApiError:
                if self._closing or time.time() > deadline:
                    raise
                time.sleep(0.4)
        return {"modes": self.api.get("/modes"), "cfg": self.api.get("/settings"), "status": self.api.get("/status")}

    def _startup_failed(self, exc: Exception) -> None:
        self.toast(f"Could not start: {exc}", "error")
        self.chat.note(f"The server did not answer: {exc}", "err")

    def _initial_loaded(self, data: dict) -> None:
        self.modes, self.cfg, self.status = data["modes"], data["cfg"], data["status"]
        self._fill_sidebar()
        self.cloud_var.set(self.cfg["cloud"]["enabled"])
        self.work_var.set(bool(self.cfg.get("show_workings")))
        self._show_thinking(self.cfg.get("thinking", "careful"))
        self._apply_status()
        self.select_mode(self.cfg.get("mode", "conversation"))
        if not self.status.get("ollama_up"):
            self.chat.note("Ollama is not running. Open the Gates tab and press 'Start Ollama', or start it yourself, then speak.", "err")
        for tab in self.tabs_by_name.values():
            if hasattr(tab, "on_ready"):
                tab.on_ready()

    def _apply_status(self) -> None:
        s = self.status
        if s.get("ollama_up"):
            n = len([m for m in s["models"].values() if m])
            self.ollama_label.configure(text=f"Ollama online · {n}/3 heads awake", text_color=T.OK if n == 3 else T.WARN)
        else:
            self.ollama_label.configure(text="Ollama offline", text_color="#ff7b7b")
        self.heads.set_models(s.get("models", {}))
        b = s.get("bond") or {}
        if b:
            self.bond_label.configure(text=f"Bond: {b['level']} ({b['interactions']} words exchanged)")

    def _poll_status(self) -> None:
        def ok(st: dict) -> None:
            self.status = st
            self._apply_status()
        self.run_poll("status", lambda: self.api.get("/status"), ok)
        self.after(6000, self._poll_status)

    # ---------------------------------------------------------------- proactive messages
    def _poll_notifications(self) -> None:
        def ok(rows: list) -> None:
            for n in rows:
                self.chat.note(f"{n['title']}\n{n['body']}", "stage")
                self.toast(f"{n['title']}: {n['body'].splitlines()[0][:120]}", "warn")
                self.bell()
            if rows:
                ids = [n["id"] for n in rows]
                self.run_bg(lambda: self.api.post("/notifications/read", {"ids": ids}), None, lambda e: None)
        self.run_poll("notifications", lambda: self.api.get("/notifications", unread="true"), ok)
        self.after(20000, self._poll_notifications)

    # ---------------------------------------------------------------- the Warden's pop-up
    def _poll_warden(self) -> None:
        self.run_poll("warden", lambda: self.api.get("/warden/pending"), lambda r: self._warden_alert(r.get("alert")))
        self.after(2000, self._poll_warden)

    def _warden_alert(self, alert: Optional[dict]) -> None:
        dlg = self.warden_dlg
        if dlg is not None and not dlg.winfo_exists():
            dlg = self.warden_dlg = None
        if alert is None:
            if dlg is not None:
                dlg.close_silently()
                self.warden_dlg = None
            return
        if dlg is not None and dlg.alert_id == alert["id"]:
            dlg.update_alert(alert)
            return
        if dlg is not None:
            dlg.close_silently()
        self.warden_dlg = WardenDialog(self, alert, self._warden_answer, self._warden_obey)
        self.bell()

    def _warden_answer(self, alert_id: int, text: str) -> None:
        def ok(r: dict) -> None:
            dlg = self.warden_dlg
            if r.get("accepted"):
                self.toast(f"Accepted. You have {r['minutes']} minutes. Do not waste them.", "warn")
            elif not r.get("stale") and dlg is not None and dlg.alert_id == alert_id:
                dlg.rejected(r.get("reason", "Not good enough."), r.get("alert"))
                return
            if dlg is not None and dlg.alert_id == alert_id:
                dlg.close_silently()
                self.warden_dlg = None

        def err(exc: Exception) -> None:
            dlg = self.warden_dlg
            if dlg is not None and dlg.alert_id == alert_id:
                if "no longer open" in str(exc):
                    dlg.close_silently()
                    self.warden_dlg = None
                else:
                    dlg.failed(str(exc))
        self.run_bg(lambda: self.api.post("/warden/answer", {"id": alert_id, "text": text}, timeout=300), ok, err)

    def _warden_obey(self, alert_id: int) -> None:
        def done(r: Any) -> None:
            if self.warden_dlg is not None and self.warden_dlg.alert_id == alert_id:
                self.warden_dlg.close_silently()
                self.warden_dlg = None
            if isinstance(r, dict):
                self.toast(r.get("detail") or "Done.", "ok" if r.get("ok") else "warn")

        def err(exc: Exception) -> None:
            dlg = self.warden_dlg
            if dlg is not None and dlg.alert_id == alert_id:
                if "no longer open" in str(exc):
                    dlg.close_silently()
                    self.warden_dlg = None
                else:
                    dlg.failed(str(exc))
        self.run_bg(lambda: self.api.post("/warden/obey", {"id": alert_id}, timeout=60), done, err)

    # ---------------------------------------------------------------- safety polling & switches
    def _poll_safety(self) -> None:
        def ok(s: dict) -> None:
            self.auto_var.set(s["autonomous"])
            self.pending_label.configure(text=f"{len(s['pending'])} pact(s) awaiting you" if s["pending"] else "")
            ids = {p["id"] for p in s["pending"]}
            for p in s["pending"]:
                if p["id"] not in self.shown_pending:
                    dlg = ConfirmDialog(self, p, self._decide)
                    self.shown_pending[p["id"]] = dlg
            for pid in list(self.shown_pending):
                if pid not in ids:
                    self.shown_pending.pop(pid).close_silently()
        self.run_poll("safety", lambda: self.api.get("/safety"), ok)
        self.after(1500, self._poll_safety)

    def _decide(self, pending_id: str, approve: bool) -> None:
        self.run_bg(lambda: self.api.post(f"/safety/confirm/{pending_id}", {"approve": approve}))

    def _seal(self) -> None:
        def ok(r: dict) -> None:
            self.auto_var.set(False)
            self.toast(f"The gates are sealed. {r['denied']} pending action(s) refused; autonomy is off.", "warn")
        self.run_bg(lambda: self.api.post("/safety/seal"), ok)

    def _toggle_auto(self) -> None:
        want = self.auto_var.get()
        if want and not mb.askyesno(
                "Autonomous Agent Mode",
                "Cerberus will carry out file changes, commands, pushes to GitHub and mouse/keyboard actions WITHOUT asking you.\n\n"
                "This setting resets every time the app starts, and 'Seal the Gates' switches it off instantly.\n\nEnable it?"):
            self.auto_var.set(False)
            return
        self.run_bg(lambda: self.api.post("/safety/autonomous", {"enabled": want}),
                    lambda r: self.toast("Autonomous mode is ON." if r["autonomous"] else "Autonomous mode is off.", "warn" if r["autonomous"] else "ok"))

    def _toggle_cloud(self) -> None:
        want = self.cloud_var.get()
        prov = self.cfg["cloud"]["provider"]
        if want and not self.cfg["cloud"]["providers"][prov]["api_key_set"]:
            self.toast(f"Cloud Handoff is on, but no API key is saved for {self.cfg['cloud']['providers'][prov]['label']} (see Gates).", "warn")

        def ok(cfg: dict) -> None:
            self.cfg = cfg
        self.run_bg(lambda: self.api.put("/settings", {"cloud": {"enabled": want}}), ok)

    def refresh_settings(self, cfg: dict) -> None:
        """Tabs call this after saving settings so the header stays in sync."""
        self.cfg = cfg
        self.cloud_var.set(cfg["cloud"]["enabled"])
        self.work_var.set(bool(cfg.get("show_workings")))
        self._show_thinking(cfg.get("thinking", "careful"))

    def thinking_key(self) -> str:
        label = self.think_var.get()
        return next((k for k, v in LEVELS.items() if v["label"] == label), "careful")

    def _show_thinking(self, key: str) -> None:
        if key in LEVELS:
            self.think_var.set(LEVELS[key]["label"])
            gates = self.tabs_by_name.get("Gates")
            if gates is not None and hasattr(gates, "show_thinking"):
                gates.show_thinking(key)

    def _think_changed(self, _label: str) -> None:
        key = self.thinking_key()
        self.toast(LEVELS[key]["blurb"], "info")
        self.run_bg(lambda: self.api.put("/settings", {"thinking": key}), self.refresh_settings)

    # ---------------------------------------------------------------- modes
    def select_mode(self, key: str) -> None:
        if self.running:
            self.toast("Wait for the current rite to finish (or press Stop).", "warn")
            return
        self.mode = key
        info = next((m for m in self.modes if m["key"] == key), None)
        if not info:
            return
        for k, b in self.mode_buttons.items():
            b.configure(fg_color=T.BLOOD_DK if k == key else "transparent", border_color=T.BLOOD if k == key else T.EDGE)
        self.subtitle.configure(text=info["label"])
        self.blurb.configure(text=MODE_BLURB.get(key, info["description"]))
        self.tabs.set("Communion")
        for w in (self.save_check, self.project_entry, self.verify_check, self.sub_label, self.sub_menu, self.act_check):
            w.pack_forget()
        if info["has_hand"]:
            self.act_check.pack(side="left", padx=(0, 14))
        if key == "coding":
            self.verify_check.pack(side="left", padx=(0, 14))
            self.save_check.pack(side="left", padx=(0, 8))
            self.project_entry.pack(side="left")
        if key == "writing":
            self.sub_label.pack(side="left", padx=(0, 6))
            self.sub_menu.pack(side="left")
        self.chat.clear()
        token = self._history_token = object()     # a slower load for an earlier rite must not paint into this one
        self.run_bg(lambda: self.api.get("/history", mode=key, limit=14),
                    lambda rows: self._show_history(rows) if self._history_token is token and self.mode == key else None)
        self.run_bg(lambda: self.api.put("/settings", {"mode": key}), None, lambda e: None)

    def _show_history(self, rows: list) -> None:
        for r in rows:
            if r["role"] == "user":
                self.chat.user(r["content"])
            else:
                self.chat.answer(r["content"])
        if not rows:
            desc = next((m["description"] for m in self.modes if m["key"] == self.mode), "")
            self.chat.note(f"{desc}\n\n{HOW_TO}", "stage")

    def _clear_history(self) -> None:
        if mb.askyesno("Clear history", "Forget this rite's conversation? (What Cerberus learned about you is kept.)"):
            self.run_bg(lambda: self.api.delete("/history", mode=self.mode), lambda r: self.chat.clear())

    # ---------------------------------------------------------------- chat
    def send(self) -> None:
        text = self.input.get("1.0", "end").strip()
        if not text or self.running:
            return
        self.input.delete("1.0", "end")
        self.chat.user(text)
        options: dict = {"thinking": self.thinking_key()}
        if self.act_check.winfo_ismapped():
            options["allow_actions"] = self.act_var.get()
        if self.mode == "coding":
            options["save_files"] = self.save_var.get()
            options["verify"] = self.verify_var.get()
            if self.project_entry.get().strip():
                options["project"] = self.project_entry.get().strip()
        if self.mode == "writing":
            options["submode"] = self.sub_var.get()
        if self.attached:
            options["context_files"] = list(self.attached)
            self.chat.note("Reading with you: " + ", ".join(p.replace("\\", "/").rsplit("/", 1)[-1] for p in self.attached[:4])
                           + (f" and {len(self.attached) - 4} more" if len(self.attached) > 4 else ""), "stage")
            self.clear_attached()
        self.workings_run = bool(self.work_var.get())
        self.bright_last = self.workings_run and self.mode != "coding" and not self.cloud_var.get()
        self.running, self.stop_flag, self.run_id = True, False, None
        self.last_stage_text, self.saw_files = "", False
        self._status_line("Cerberus is thinking...")
        self.send_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.sigil.set_busy(True)
        body = {"message": text, "mode": self.mode, "options": options}
        token = self.run_token = object()          # events from an earlier, stopped run must never reach this one

        def current() -> bool:
            return self.run_token is token and not self.stop_flag

        def deliver(ev: dict) -> None:
            self.post_ui(lambda e: self._on_event(e) if self.run_token is token else None, ev)

        threading.Thread(target=lambda: self.api.stream_chat(body, deliver, lambda: not current()), daemon=True).start()

    def stop(self) -> None:
        self.stop_flag = True
        self.run_token = None
        if self.run_id:
            rid = self.run_id
            self.run_bg(lambda: self.api.post(f"/chat/{rid}/cancel"), None, lambda e: None)
        self.run_bg(self.api.abort_chat, None, lambda e: None)
        self._finish_run()

    def _finish_run(self) -> None:
        self.running = False
        self._restore_blurb()
        self.send_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.sigil.set_busy(False)
        self.heads.highlight(None)

    def _on_event(self, ev: dict) -> None:
        if self.stop_flag and ev["type"] != "done":
            return
        t = ev["type"]
        if t == "run":
            self.run_id = ev["id"]
        elif t == "start":
            self.heads.set_models({**self.status.get("models", {}), **ev.get("models", {})})
            self.last_stage_key = ev["stages"][-1]["key"] if ev.get("stages") else ""
            if ev.get("missing_models"):
                if self.workings_run:
                    self.chat.note("Some heads are not installed, so another model is covering for them: " + ", ".join(ev["missing_models"]), "err")
                else:
                    self.toast("Some heads are not installed, so another model is covering for them (see Gates).", "warn")
        elif t == "stage_start":
            self.heads.highlight(ev.get("role"))
            self.sigil.set_busy(True, ev.get("role"))
            if self.workings_run:
                self.chat.stage(ev["title"], ev.get("model", ""))
        elif t == "token":
            if self.workings_run and ev["kind"] == "content":
                self.chat.token(ev["text"], self.bright_last and ev["key"] == self.last_stage_key)
        elif t == "stage_end":
            if ev["key"] == self.last_stage_key:
                self.last_stage_text = ev["text"]
            if self.workings_run:
                self.chat.token("\n", False)
        elif t == "council":
            if self.workings_run:
                phase = ev.get("phase")
                if phase == "found":
                    self.chat.note("Mistake caught. " + ev["message"] + " The writing head has to redo it.", "err")
                else:
                    self.chat.note(ev["message"], "ok" if phase == "clean" else "stage")
            else:
                self._status_line("Cerberus is checking the answer...")
        elif t == "tool_call":
            if self.workings_run:
                self.chat.note(f"[tool] {ev['name']} {str(ev['args'])[:140]}", "stage")
            else:
                self._status_line("Cerberus is working on it...")
        elif t == "tool_result":
            if self.workings_run:
                self.chat.note(f"   -> {ev['result'][:260]}", "stage")
        elif t == "cloud_start":
            if self.workings_run:
                self.chat.note(f"Cloud handoff to {ev['provider']} ({ev['model']}): {ev['original_tokens']} tokens squeezed to {ev['sent_tokens']}.", "ok")
            else:
                self._status_line("Asking the cloud model...")
        elif t == "cloud_error":
            if self.workings_run:
                self.chat.note("Cloud handoff failed, so the local answer stands: " + ev["message"], "err")
            else:
                self.toast("The cloud model could not be reached, so the local answer stands.", "warn")
        elif t == "files_attached":
            rows = ev.get("files") or []
            bad = [f"{f['name']}: {f['note']}" for f in rows if not f.get("ok")]
            if bad:
                self.chat.note("Could not read: " + "; ".join(bad), "err")
            if self.workings_run:
                good = [f"{f['name']} ({f['chars']} characters{', ' + f['note'] if f.get('note') else ''})" for f in rows if f.get("ok")]
                if good:
                    self.chat.note("Read: " + ", ".join(good), "ok")
        elif t == "files":
            self.saw_files = True
            if ev.get("saved"):
                self.chat.note("Saved to your workspace: " + ", ".join(ev["saved"]), "ok")
            elif ev.get("error"):
                self.chat.note(ev["error"], "err")
            if ev.get("problems"):
                self.chat.note("Checks still flag: " + "; ".join(ev["problems"]), "err")
        elif t == "final":
            same = ev["source"] == "local" and ev["text"].strip() == self.last_stage_text.strip() and self.bright_last
            if not same:
                self.chat.answer(ev["text"], "Cerberus" if ev["source"] == "local" else "Cerberus (via cloud)")
        elif t == "error":
            self.chat.note(ev["message"], "err")
            if self.workings_run and ev.get("detail"):
                self.chat.note("Technical detail: " + str(ev["detail"])[:600], "stage")
        elif t == "cancelled":
            self.chat.note("(stopped)", "stage")
        elif t == "done":
            self._finish_run()
            self.status["bond"] = None
            self.run_bg(lambda: self.api.get("/status"), lambda st: (self.status.update(st), self._apply_status()), lambda e: None)

    # ---------------------------------------------------------------- tabs
    def _tab_changed(self) -> None:
        tab = self.tabs_by_name.get(self.tabs.get())
        if self.cfg and tab is not None and hasattr(tab, "on_show"):
            tab.on_show()
