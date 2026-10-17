"""Shared test helpers: run everything in a throw-away folder so tests never touch your real data/ or workspace/."""
from __future__ import annotations

import tempfile
from pathlib import Path

from cerberus import config, guard, safety


class Isolated:
    """Context manager: redirects settings, audit log and workspace into a temp dir. Also stops tests from reading the
    real graphics card, so results never depend on this computer's temperature."""

    def __enter__(self) -> Path:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._saved = (config.DATA_DIR, config.SETTINGS_FILE, config._cache, safety.AUDIT_LOG, safety.DATA_DIR, guard._probe_gpu,
                       guard._probe_cpu, guard._probe_board)
        config.DATA_DIR = root / "data"
        config.SETTINGS_FILE = config.DATA_DIR / "settings.json"
        config._cache = None
        safety.AUDIT_LOG = config.DATA_DIR / "audit.log"
        safety.DATA_DIR = config.DATA_DIR
        guard._probe_gpu = guard._probe_cpu = guard._probe_board = lambda: None
        guard.reset_cache()
        config.update_settings({"workspace": str(root / "workspace")})
        return root

    def __exit__(self, *exc) -> None:
        (config.DATA_DIR, config.SETTINGS_FILE, config._cache, safety.AUDIT_LOG, safety.DATA_DIR, guard._probe_gpu,
         guard._probe_cpu, guard._probe_board) = self._saved
        guard.reset_cache()
        try:
            self._tmp.cleanup()
        except OSError:
            pass


class FakeOllama:
    """Stands in for Ollama so the pipeline can be tested without models."""
    base_url = "http://fake"

    def __init__(self, reply: str = "A fine answer.") -> None:
        self.reply = reply
        self.calls: list[tuple[str, list[dict]]] = []
        self.num_ctx, self.keep_alive = 8192, "1m"
        self.installed = ["deepseek-r1:latest", "llama3:latest", "mistral:latest"]
        self.tool_support: dict[str, bool] = {}

    def is_up(self) -> bool: return True
    def refresh_url(self) -> bool: return True
    def installed_names(self) -> list[str]: return list(self.installed)
    def resolve(self, wanted: str):
        return next((n for n in self.installed_names() if n.startswith(wanted)), None)

    def chat_stream(self, model, messages, temperature=0.7, cancel=None):
        self.calls.append((model, messages))
        reply = self.reply_fn(model, messages) if getattr(self, "reply_fn", None) else self.reply
        yield "thinking", "hmm"
        if getattr(self, "reply_fn", None):
            yield "content", reply
            return
        for word in reply.split(" "):
            yield "content", word + " "

    def chat(self, model, messages, temperature=0.7, cancel=None, json_mode=False, num_predict=None) -> str:
        self.calls.append((model, messages))
        return self.reply_fn(model, messages) if getattr(self, "reply_fn", None) else self.reply

    def chat_once(self, model, messages, temperature=0.2, tools=None, fmt=None, num_predict=None) -> dict:
        self.calls.append((model, messages))
        return {"content": self.reply}

    def embed(self, model, texts):
        """Deterministic bag-of-words vectors: texts sharing words are close. Fails if 'embed_broken' is set."""
        if getattr(self, "embed_broken", False):
            from cerberus.ollama_client import OllamaError
            raise OllamaError("embedding down")
        self.embed_calls = getattr(self, "embed_calls", 0) + 1
        out = []
        for t in texts:
            v = [0.0] * 64
            for w in __import__("re").findall(r"[a-z0-9]{3,}", t.lower()):
                v[hash(w) % 64] += 1.0
            out.append(v)
        return out
