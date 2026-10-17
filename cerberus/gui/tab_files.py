"""Library tab: browse this computer and pick files whose text Cerberus reads along with your next message.

Browsing shows names and sizes only; a file is opened only when you preview or attach it. Secrets, Cerberus's own data and system
folders never appear (the server applies the same rules as the Computer tools).
"""
from __future__ import annotations

import tkinter as tk

import customtkinter as ctk

from . import theme as T
from .widgets import button, hint, section, set_text, textbox


def _human(n) -> str:
    if n is None:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


class FilesTab(ctk.CTkFrame):
    def __init__(self, master, app) -> None:
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.entries: list[dict] = []
        self.here = ""
        self.parent_path = ""
        self.loaded = False
        self._build()

    def _build(self) -> None:
        self.grid_columnconfigure(0, weight=3)
        self.grid_columnconfigure(1, weight=2)
        self.grid_rowconfigure(3, weight=1)
        section(self, "Point Cerberus at files").grid(row=0, column=0, columnspan=2, sticky="w", padx=8, pady=(6, 0))
        hint(self, "Pick text files or Word (.docx) documents, press 'Attach to my next message', and Cerberus reads them along with what you "
                   "type. Folders open with a double-click. Nothing is read until you preview or attach it.", 900).grid(
            row=1, column=0, columnspan=2, sticky="w", padx=8)
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=2, column=0, columnspan=2, sticky="ew", padx=8, pady=6)
        button(bar, "Starting places", lambda: self.open(""), "gold", 130).pack(side="left")
        self.up_btn = button(bar, "Up one folder", self._up, "normal", 130)
        self.up_btn.pack(side="left", padx=8)
        self.path_label = ctk.CTkLabel(bar, text="", font=T.font("body", 14), text_color=T.DIM, anchor="w")
        self.path_label.pack(side="left", padx=6)

        left = ctk.CTkFrame(self, fg_color=T.PANEL, border_width=1, border_color=T.EDGE)
        left.grid(row=3, column=0, sticky="nsew", padx=(8, 4), pady=(0, 6))
        left.grid_rowconfigure(0, weight=1)
        left.grid_columnconfigure(0, weight=1)
        self.list = tk.Listbox(left, selectmode="extended", activestyle="none", bg=T.PANEL, fg=T.TEXT, selectbackground=T.BLOOD_DK,
                               selectforeground=T.BONE, highlightthickness=0, borderwidth=0, font=T.font("body", 15), exportselection=False)
        self.list.grid(row=0, column=0, sticky="nsew", padx=(6, 0), pady=6)
        sb = ctk.CTkScrollbar(left, command=self.list.yview)
        sb.grid(row=0, column=1, sticky="ns", pady=6)
        self.list.configure(yscrollcommand=sb.set)
        self.list.bind("<Double-Button-1>", self._double)
        self.list.bind("<<ListboxSelect>>", self._selected)

        right = ctk.CTkFrame(self, fg_color="transparent")
        right.grid(row=3, column=1, sticky="nsew", padx=(4, 8), pady=(0, 6))
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)
        self.preview_title = section(right, "Preview")
        self.preview_title.grid(row=0, column=0, sticky="w")
        self.preview = textbox(right, 200, mono=True)
        self.preview.grid(row=1, column=0, sticky="nsew")
        set_text(self.preview, "(select one file to see its first lines)")

        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.grid(row=4, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 8))
        self.attach_btn = button(foot, "Attach to my next message", self._attach, "normal", 230)
        self.attach_btn.pack(side="left")
        self.note = ctk.CTkLabel(foot, text="", font=T.font("body", 14), text_color=T.DIM, anchor="w")
        self.note.pack(side="left", padx=12)

    # ---------------------------------------------------------------- lifecycle
    def on_show(self) -> None:
        if not self.loaded:
            self.open("")

    # ---------------------------------------------------------------- browsing
    def open(self, path: str) -> None:
        def ok(r: dict) -> None:
            self.loaded = True
            self.here, self.parent_path, self.entries = r["path"], r.get("parent") or "", r["entries"]
            self.path_label.configure(text=self.here or "Choose a starting place")
            self.up_btn.configure(state="normal" if (self.here and not r.get("roots")) else "disabled")
            self.list.delete(0, "end")
            for i, e in enumerate(self.entries):
                if e["dir"]:
                    label = f"[folder]  {e['name']}"
                else:
                    label = f"{e['name']}    {_human(e['size'])}"
                self.list.insert("end", label)
                if not e["dir"] and not e.get("attachable"):
                    self.list.itemconfig(i, fg=T.DIM)
            if not self.entries:
                self.list.insert("end", "(empty)")
            self.note.configure(text="Only the first 600 items are shown." if r.get("truncated") else "")
            set_text(self.preview, "(select one file to see its first lines)")

        def err(exc: Exception) -> None:
            self.note.configure(text=str(exc), text_color="#ff7b7b")
        self.note.configure(text_color=T.DIM)
        self.app.run_bg(lambda: self.app.api.get("/files/browse", path=path), ok, err)

    def _up(self) -> None:
        self.open(self.parent_path)

    def _double(self, _event) -> None:
        sel = self.list.curselection()
        if len(sel) != 1 or sel[0] >= len(self.entries):
            return
        e = self.entries[sel[0]]
        if e["dir"]:
            self.open(e["path"])
        else:
            self._preview(e)

    def _selected(self, _event) -> None:
        sel = [i for i in self.list.curselection() if i < len(self.entries)]
        if len(sel) == 1 and not self.entries[sel[0]]["dir"]:
            self._preview(self.entries[sel[0]])

    def _preview(self, e: dict) -> None:
        if not e.get("attachable"):
            set_text(self.preview, f"{e['name']}\n\nCerberus can read text files and Word (.docx) documents. This one cannot be attached.")
            return
        stamp = e["path"]
        self._want = stamp

        def ok(r: dict) -> None:
            if getattr(self, "_want", "") == stamp:
                tail = "\n\n[...more in the file]" if r.get("truncated") else ""
                set_text(self.preview, (r["text"] or "(the file is empty)") + tail)

        def err(exc: Exception) -> None:
            if getattr(self, "_want", "") == stamp:
                set_text(self.preview, str(exc))
        self.app.run_bg(lambda: self.app.api.get("/files/preview", path=e["path"]), ok, err)

    # ---------------------------------------------------------------- attaching
    def _attach(self) -> None:
        chosen = [self.entries[i] for i in self.list.curselection() if i < len(self.entries)]
        files = [e["path"] for e in chosen if not e["dir"] and e.get("attachable")]
        skipped = len(chosen) - len(files)
        if not files:
            self.note.configure(text="Select one or more text or Word files first." + (" Folders and other file types cannot be attached." if skipped else ""),
                                text_color=T.WARN)
            return
        self.app.attach_files(files)
        if skipped:
            self.app.toast(f"{skipped} item(s) could not be attached (folders or unsupported types).", "warn")
