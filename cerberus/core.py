"""The Cerberus core: one object that owns the database, Ollama client, safety gate, orchestrator and agents."""
from __future__ import annotations

import threading
from typing import Callable, Optional

from . import __version__, dossier, hardware, learning, plugins
from .agents import AgentManager
from .cloud import PROVIDERS
from .config import DATA_DIR, get_workspace, load_settings, update_settings
from .council import Council
from .db import Database
from .memory import Memory
from .ollama_client import OllamaClient, _ipv4
from .orchestrator import Orchestrator
from .rituals import Rituals
from .safety import SafetyGate
from .tools import ToolContext
from .warden import Warden


class Cerberus:
    def __init__(self, db_path=None) -> None:
        s = load_settings()
        get_workspace()  # make sure the workspace folder exists
        self.db = Database(db_path)
        dossier.seed(self.db)
        self.ollama = OllamaClient(s["ollama_url"], s["num_ctx"], s["keep_alive"])
        self.gate = SafetyGate()
        self.memory = Memory(self)
        self.memory.prune()
        self.orchestrator = Orchestrator(self)
        self.council = Council(self)
        self.gate.council = lambda tool, detail, goal, cancel: self.council.vote_change(tool, detail, goal, cancel)
        self._auto_tune()
        plugins.load_plugins()
        self.agents = AgentManager(self)
        self.rituals = Rituals(self)
        self.warden = Warden(self)
        self.runs: dict[str, threading.Event] = {}

    # -- hardware
    def hardware(self) -> dict:
        return hardware.detect(DATA_DIR)

    def _auto_tune(self) -> None:
        """First launch only: size the context window and model keep-alive to this computer. Later changes are never overridden."""
        s = load_settings()
        if s.get("auto_tuned"):
            return
        try:
            rec = hardware.recommend(self.hardware())
            self.update_settings({"num_ctx": rec["num_ctx"], "keep_alive": rec["keep_alive"], "auto_tuned": True})
        except Exception:
            pass                                        # tuning is a nicety; never block startup

    # -- settings
    def settings(self) -> dict:
        return load_settings()

    def update_settings(self, patch: dict, replace: tuple = ()) -> dict:
        s = update_settings(patch, replace)
        self.ollama.num_ctx = int(s["num_ctx"])
        self.ollama.keep_alive = s["keep_alive"]
        if s["ollama_url"] and _ipv4(s["ollama_url"]) != self.ollama.base_url:
            self.ollama.base_url = _ipv4(s["ollama_url"])
        return s

    def public_settings(self, origin: str = "local") -> dict:
        """Settings safe to send to a UI: API keys and tokens are replaced by 'is set' flags and a short hint.
        Another device on the network additionally never sees personal or machine-specific sections."""
        s = load_settings()
        hidden = ("api_token", "cloud", "github") + (("resume", "projects", "warden", "workspace") if origin != "local" else ())
        out = {k: v for k, v in s.items() if k not in hidden}
        providers = {}
        for pid, spec in PROVIDERS.items():
            saved = (s["cloud"].get("providers") or {}).get(pid, {})
            key = saved.get("api_key", "")
            providers[pid] = {"label": spec["label"], "model": saved.get("model") or spec["model"],
                              "default_model": spec["model"], "api_key_set": bool(key),
                              "api_key_hint": f"...{key[-4:]}" if len(key) >= 8 else ("set" if key else "")}
        out["cloud"] = {"enabled": s["cloud"]["enabled"], "provider": s["cloud"]["provider"],
                        "max_handoff_tokens": s["cloud"]["max_handoff_tokens"], "providers": providers}
        gh = s["github"]
        out["github"] = {"token_set": bool(gh.get("token")), "git_name": gh.get("git_name", ""),
                         "git_email": gh.get("git_email", ""), "username": gh.get("username", "")}
        out["autonomous"] = self.gate.autonomous
        return out

    # -- helpers
    def tool_ctx(self, origin: str = "local", actor: str = "user", cancel: Optional[threading.Event] = None) -> ToolContext:
        return ToolContext(gate=self.gate, db=self.db, origin=origin, actor=actor, cancel=cancel)

    def llm_fn(self, role: str = "editor") -> Callable[[list[dict]], str]:
        return lambda messages: self.orchestrator.llm(role, messages, 0.3)

    def status(self) -> dict:
        up = self.ollama.is_up()
        installed = self.ollama.installed_names() if up else []
        mapping = self.orchestrator.model_map() if up else {r: None for r in self.settings()["models"]}
        return {"version": __version__, "ollama_up": up, "ollama_url": self.ollama.base_url, "installed": installed,
                "models": mapping, "autonomous": self.gate.autonomous, "pending": len(self.gate.pending()),
                "cloud_enabled": self.settings()["cloud"]["enabled"], "bond": learning.bond(self.db),
                "workspace": str(get_workspace()), "thinking": self.council.level()["key"],
                "full_access": bool(self.settings().get("full_access", True))}

    def shutdown(self) -> None:
        from .tools import browser
        browser.shutdown()
        self.warden.stop()
        self.agents.stop()          # waits for the scheduler's workers, so none is still using the database below
        self.db.close()
