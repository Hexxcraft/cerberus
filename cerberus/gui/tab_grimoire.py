"""Grimoire tab: GitHub + git + VS Code. Every action goes through the same approval gate as the AI's own tool use."""
from __future__ import annotations

import customtkinter as ctk

from . import theme as T
from .widgets import button, entry, hint, section, set_text, textbox


class GrimoireTab(ctk.CTkScrollableFrame):
    def __init__(self, master, app) -> None:
        super().__init__(master, fg_color="transparent")
        self.app = app
        self._build()

    def _build(self) -> None:
        section(self, "GitHub account").pack(anchor="w", padx=8, pady=(6, 0))
        hint(self, "Create a Personal Access Token on github.com (Settings > Developer settings > Personal access tokens) with 'repo' scope. "
                   "It is stored only in this app's data folder and is never shown again.", 900).pack(anchor="w", padx=8)
        g = ctk.CTkFrame(self, fg_color="transparent")
        g.pack(fill="x", padx=8, pady=4)
        self.token = entry(g, 280, "token (leave blank to keep the saved one)", show="*")
        self.token.grid(row=0, column=0, padx=(0, 8), pady=2)
        self.username = entry(g, 180, "GitHub username")
        self.username.grid(row=0, column=1, padx=(0, 8))
        self.git_name = entry(g, 180, "name for commits")
        self.git_name.grid(row=1, column=0, sticky="w", pady=2)
        self.git_email = entry(g, 280, "email for commits")
        self.git_email.grid(row=1, column=1, columnspan=2, sticky="w")
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=8)
        button(row, "Save", self._save, "normal", 90).pack(side="left")
        button(row, "Test login", lambda: self._tool("github_whoami"), "gold", 120).pack(side="left", padx=8)
        button(row, "My repositories", lambda: self._tool("github_list_repos", limit=30), "gold", 160).pack(side="left")
        self.token_state = ctk.CTkLabel(row, text="", font=T.font("body", 14), text_color=T.DIM)
        self.token_state.pack(side="left", padx=12)

        section(self, "Repository").pack(anchor="w", padx=8, pady=(18, 0))
        hint(self, "Folders are relative to your workspace (the sandbox where Cerberus keeps its projects). Use '.' for the workspace itself.", 900).pack(anchor="w", padx=8)
        r1 = ctk.CTkFrame(self, fg_color="transparent")
        r1.pack(fill="x", padx=8, pady=4)
        self.repo = entry(r1, 220, "project folder")
        self.repo.insert(0, ".")
        self.repo.pack(side="left")
        for label, name in (("Status", "git_status"), ("Changes", "git_diff"), ("History", "git_log"), ("Init", "git_init"),
                            ("Pull", "git_pull"), ("Push", "git_push")):
            button(r1, label, lambda n=name: self._tool(n, repo=self._repo()), "normal", 84).pack(side="left", padx=4)
        r2 = ctk.CTkFrame(self, fg_color="transparent")
        r2.pack(fill="x", padx=8, pady=4)
        self.message = entry(r2, 360, "commit message")
        self.message.pack(side="left")
        button(r2, "Commit all", self._commit, "normal", 110).pack(side="left", padx=8)
        self.remote = entry(r2, 320, "https://github.com/you/repo.git")
        self.remote.pack(side="left")
        button(r2, "Set remote", self._set_remote, "normal", 110).pack(side="left", padx=8)
        r3 = ctk.CTkFrame(self, fg_color="transparent")
        r3.pack(fill="x", padx=8, pady=4)
        self.clone_url = entry(r3, 360, "repository URL to clone")
        self.clone_url.pack(side="left")
        self.clone_dir = entry(r3, 180, "into folder")
        self.clone_dir.pack(side="left", padx=8)
        button(r3, "Clone", self._clone, "normal", 90).pack(side="left")
        r4 = ctk.CTkFrame(self, fg_color="transparent")
        r4.pack(fill="x", padx=8, pady=4)
        self.new_repo = entry(r4, 220, "new GitHub repo name")
        self.new_repo.pack(side="left")
        self.private_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(r4, text="private", variable=self.private_var, font=T.font("body", 14), text_color=T.TEXT, fg_color=T.BLOOD,
                        hover_color=T.BLOOD_HI, border_color=T.GOLD_DK).pack(side="left", padx=10)
        button(r4, "Create on GitHub", self._create_repo, "gold", 170).pack(side="left")

        section(self, "VS Code").pack(anchor="w", padx=8, pady=(18, 0))
        v = ctk.CTkFrame(self, fg_color="transparent")
        v.pack(fill="x", padx=8, pady=4)
        self.vs_path = entry(v, 360, "file or folder in workspace")
        self.vs_path.insert(0, ".")
        self.vs_path.pack(side="left")
        button(v, "Open in VS Code", self._vscode, "normal", 150).pack(side="left", padx=8)

        section(self, "Output").pack(anchor="w", padx=8, pady=(18, 0))
        self.output = textbox(self, 220, mono=True)
        self.output.pack(fill="x", padx=8, pady=(2, 6))
        set_text(self.output, "(results appear here; anything that changes or sends data asks your approval first)")
        section(self, "Workspace files").pack(anchor="w", padx=8, pady=(10, 0))
        self.files = textbox(self, 220, mono=True)
        self.files.pack(fill="x", padx=8, pady=(2, 14))

    def on_ready(self) -> None:
        gh = self.app.cfg.get("github", {})
        for box, key in ((self.username, "username"), (self.git_name, "git_name"), (self.git_email, "git_email")):
            box.delete(0, "end")
            if gh.get(key):
                box.insert(0, gh[key])
        self.token_state.configure(text="token saved" if gh.get("token_set") else "no token saved yet",
                                   text_color=T.OK if gh.get("token_set") else T.WARN)

    def on_show(self) -> None:
        self.app.run_bg(lambda: self.app.api.get("/workspace"), self._files_loaded, lambda e: None)

    def _files_loaded(self, ws: dict) -> None:
        lines = [f"Workspace: {ws['root']}", ""]
        for it in ws["items"]:
            depth = it["path"].count("/")
            lines.append("  " * depth + it["path"].split("/")[-1] + ("/" if it["dir"] else f"   ({it['size']:,} bytes)"))
        set_text(self.files, "\n".join(lines) if ws["items"] else lines[0] + "\n(empty)")

    # ------------------------------------------------------------ actions
    def _repo(self) -> str:
        return self.repo.get().strip() or "."

    def _save(self) -> None:
        gh = {"username": self.username.get().strip(), "git_name": self.git_name.get().strip(), "git_email": self.git_email.get().strip()}
        if self.token.get().strip():
            gh["token"] = self.token.get().strip()

        def ok(cfg: dict) -> None:
            self.app.refresh_settings(cfg)
            self.token.delete(0, "end")
            self.on_ready()
            self.app.toast("GitHub details saved.", "ok")
        self.app.run_bg(lambda: self.app.api.put("/settings", {"github": gh}), ok)

    def _tool(self, name: str, **args) -> None:
        set_text(self.output, f"Running {name}... (watch for an approval pop-up)")

        def ok(result: str) -> None:
            set_text(self.output, result)
            if name.startswith("git_"):
                self.on_show()
        self.app.run_bg(lambda: self.app.api.run_tool(name, **args), ok, lambda e: set_text(self.output, f"Error: {e}"))

    def _commit(self) -> None:
        msg = self.message.get().strip()
        if not msg:
            self.app.toast("Write a commit message first.", "warn")
            return
        self._tool("git_commit", message=msg, repo=self._repo())

    def _set_remote(self) -> None:
        if self.remote.get().strip():
            self._tool("git_remote", url=self.remote.get().strip(), repo=self._repo())

    def _clone(self) -> None:
        if self.clone_url.get().strip():
            self._tool("git_clone", url=self.clone_url.get().strip(), folder=self.clone_dir.get().strip())

    def _create_repo(self) -> None:
        if self.new_repo.get().strip():
            self._tool("github_create_repo", name=self.new_repo.get().strip(), description="Created with Cerberus", private=self.private_var.get())

    def _vscode(self) -> None:
        self._tool("vscode_open", path=self.vs_path.get().strip() or ".")
