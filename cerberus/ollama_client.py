"""Talks to Ollama (the local model runner). Port 11434 is checked first.

Nothing here needs the internet - Ollama runs on your own machine.
"""
from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import threading
import time
from typing import Iterator, Optional
from urllib.parse import urlparse

import requests

DEFAULT_OLLAMA = "http://localhost:11434"


class OllamaError(RuntimeError):
    """Raised when Ollama can't be reached or a model call fails."""


class Cancelled(Exception):
    """Raised inside a streaming call when the user pressed 'stop'."""


class ToolsUnsupported(OllamaError):
    """The chosen model cannot do native tool calling."""


# ---------------------------------------------------------------- <think> handling
class ThinkSplitter:
    """Splits a streamed text into ('thinking', ...) and ('content', ...) pieces.

    DeepSeek-R1 may wrap its reasoning in <think>...</think> tags. We keep the reasoning
    separate so it can be shown dimmed and is never passed on as 'the answer'.
    """

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self) -> None:
        self.buf = ""
        self.in_think = False

    def feed(self, text: str) -> list[tuple[str, str]]:
        self.buf += text
        out: list[tuple[str, str]] = []
        while True:
            tag = self.CLOSE if self.in_think else self.OPEN
            kind = "thinking" if self.in_think else "content"
            idx = self.buf.find(tag)
            if idx != -1:
                if idx:
                    out.append((kind, self.buf[:idx]))
                self.buf = self.buf[idx + len(tag):]
                self.in_think = not self.in_think
                continue
            keep = 0  # hold back a possibly-partial tag at the end of the buffer
            for n in range(min(len(tag) - 1, len(self.buf)), 0, -1):
                if tag.startswith(self.buf[-n:]):
                    keep = n
                    break
            emit = self.buf[: len(self.buf) - keep]
            if emit:
                out.append((kind, emit))
            self.buf = self.buf[len(self.buf) - keep:]
            return out

    def flush(self) -> list[tuple[str, str]]:
        rest, self.buf = self.buf, ""
        return [("thinking" if self.in_think else "content", rest)] if rest else []


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def strip_think(text: str) -> str:
    """Remove reasoning blocks from a finished model answer."""
    if "</think>" in text and "<think>" not in text:  # opening tag was swallowed by the template
        text = text.split("</think>", 1)[1]
    text = _THINK_RE.sub("", text)
    return text.replace("<think>", "").strip()


# ---------------------------------------------------------------- detection
def _port_open(host: str, port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _ipv4(url: str) -> str:
    """On Windows 'localhost' tries IPv6 first and stalls for seconds; Ollama listens on IPv4, so use it directly."""
    return url.rstrip("/").replace("//localhost", "//127.0.0.1")


def detect_ollama(preferred: str = DEFAULT_OLLAMA) -> Optional[str]:
    """Return the base URL of a running Ollama: the configured one first, then the default port 11434."""
    candidates = []
    for u in (preferred, DEFAULT_OLLAMA):
        if u and _ipv4(u) not in candidates:
            candidates.append(_ipv4(u))
    for url in candidates:
        p = urlparse(url)
        if _port_open(p.hostname or "127.0.0.1", p.port or 11434):
            try:
                r = requests.get(url + "/api/tags", timeout=3)
                if r.ok:
                    return url
            except requests.RequestException:
                continue
    return None


def start_ollama_server() -> str:
    """Try to launch `ollama serve` in the background. Returns a human-readable status."""
    exe = shutil.which("ollama")
    if not exe:
        return "Ollama is not installed (or not on PATH). Get it from https://ollama.com/download"
    flags = 0x08000000 if hasattr(subprocess, "CREATE_NO_WINDOW") else 0  # hide console on Windows
    subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    for _ in range(20):
        time.sleep(0.5)
        if detect_ollama():
            return "Ollama started."
    return "Started `ollama serve` but it is not answering yet. Give it a few seconds."


# ---------------------------------------------------------------- client
class OllamaClient:
    def __init__(self, base_url: str = DEFAULT_OLLAMA, num_ctx: int = 8192, keep_alive: str = "10m"):
        self.base_url = _ipv4(base_url)
        self.num_ctx = num_ctx
        self.keep_alive = keep_alive
        self._models_cache: tuple[float, list[dict]] = (0.0, [])
        self.tool_support: dict[str, bool] = {}   # model -> does it do native tool calling? (learned on first use)
        self._ctx_limits: dict[str, tuple[float, int]] = {}     # model -> (when looked up, longest context it was trained for)
        self._lock = threading.Lock()

    def ctx_for(self, model: str) -> int:
        """The context window to request: the global setting, but never more than this model can actually use
        (plain llama3 stops at 8192; asking for more is silently wasted memory)."""
        stamp, limit = self._ctx_limits.get(model, (0.0, 0))
        if model not in self._ctx_limits or (limit == 0 and time.time() - stamp > 60):      # a failed lookup is retried after a minute
            limit = 0
            try:
                r = requests.post(f"{self.base_url}/api/show", json={"model": model}, timeout=5)
                if r.status_code == 200:
                    for key, value in (r.json().get("model_info") or {}).items():
                        if key.endswith(".context_length") and isinstance(value, int):
                            limit = value
                            break
            except (requests.RequestException, ValueError):
                limit = 0
            self._ctx_limits[model] = (time.time(), limit)
        limit = self._ctx_limits[model][1]
        return min(self.num_ctx, limit) if limit else self.num_ctx

    # -- connection
    def refresh_url(self) -> bool:
        """Re-detect the server (port 11434 first). Returns True if reachable."""
        found = detect_ollama(self.base_url)
        if found:
            self.base_url = found
            return True
        return False

    def is_up(self) -> bool:
        return self.refresh_url()

    def list_models(self, max_age: float = 5.0) -> list[dict]:
        with self._lock:
            ts, cached = self._models_cache
            if cached and time.time() - ts < max_age:
                return cached
        try:
            r = requests.get(f"{self.base_url}/api/tags", timeout=5)
            r.raise_for_status()
            models = r.json().get("models", [])
        except requests.RequestException as exc:
            raise OllamaError(f"Cannot reach Ollama at {self.base_url}: {exc}") from exc
        with self._lock:
            self._models_cache = (time.time(), models)
        return models

    def installed_names(self) -> list[str]:
        try:
            return [m.get("name", "") for m in self.list_models()]
        except OllamaError:
            return []

    def resolve(self, wanted: str) -> Optional[str]:
        """Map 'llama3' -> 'llama3:latest' (exact match first, then prefix, then substring)."""
        names = self.installed_names()
        if wanted in names:
            return wanted
        w = wanted.lower()
        for n in names:
            if n.lower().startswith(w):
                return n
        for n in names:
            if w in n.lower():
                return n
        return None

    # -- chat
    def chat_stream(self, model: str, messages: list[dict], temperature: float = 0.7,
                    cancel: Optional[threading.Event] = None, json_mode: bool = False,
                    num_predict: Optional[int] = None) -> Iterator[tuple[str, str]]:
        """Yield ('thinking'|'content', text) pieces as the model produces them."""
        options = {"temperature": temperature, "num_ctx": self.ctx_for(model)}
        if num_predict:
            options["num_predict"] = num_predict
        payload = {"model": model, "messages": messages, "stream": True,
                   "options": options, "keep_alive": self.keep_alive}
        if json_mode:
            payload["format"] = "json"
        splitter = ThinkSplitter()
        finished = threading.Event()
        try:
            with requests.post(f"{self.base_url}/api/chat", json=payload, stream=True,
                               timeout=(5, 900)) as resp:
                if resp.status_code != 200:
                    raise OllamaError(f"Ollama error {resp.status_code}: {resp.text[:300]}")
                if cancel is not None:
                    def watch() -> None:            # a model that is silent (still loading) cannot otherwise be stopped
                        while not finished.wait(0.4):
                            if cancel.is_set():
                                resp.close()
                                return
                    threading.Thread(target=watch, daemon=True).start()
                for line in resp.iter_lines():
                    if cancel is not None and cancel.is_set():
                        raise Cancelled()
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except ValueError:
                        continue
                    if chunk.get("error"):
                        raise OllamaError(chunk["error"])
                    msg = chunk.get("message", {})
                    if msg.get("thinking"):
                        yield ("thinking", msg["thinking"])
                    if msg.get("content"):
                        yield from splitter.feed(msg["content"])
                    if chunk.get("done"):
                        break
        except (requests.RequestException, AttributeError, ValueError) as exc:
            if cancel is not None and cancel.is_set():
                raise Cancelled() from exc
            raise OllamaError(f"Lost connection to Ollama: {exc}") from exc
        finally:
            finished.set()
        yield from splitter.flush()

    def chat(self, model: str, messages: list[dict], temperature: float = 0.7,
             cancel: Optional[threading.Event] = None, json_mode: bool = False,
             num_predict: Optional[int] = None) -> str:
        """Blocking helper: returns the final answer with reasoning removed."""
        parts = [t for kind, t in self.chat_stream(model, messages, temperature, cancel, json_mode, num_predict)
                 if kind == "content"]
        return strip_think("".join(parts))

    def chat_once(self, model: str, messages: list[dict], temperature: float = 0.2, tools: Optional[list[dict]] = None,
                  fmt: Optional[dict | str] = None, num_predict: Optional[int] = None) -> dict:
        """One non-streaming call that returns the raw message dict ({content, tool_calls, ...}).

        `tools` = Ollama function schemas (native tool calling); `fmt` = a JSON schema (or "json") that constrains the reply.
        Raises ToolsUnsupported if the model has no tool support."""
        options = {"temperature": temperature, "num_ctx": self.ctx_for(model)}
        if num_predict:
            options["num_predict"] = num_predict
        payload = {"model": model, "messages": messages, "stream": False, "options": options, "keep_alive": self.keep_alive}
        if tools:
            payload["tools"] = tools
        if fmt:
            payload["format"] = fmt
        try:
            r = requests.post(f"{self.base_url}/api/chat", json=payload, timeout=(5, 900))
        except requests.RequestException as exc:
            raise OllamaError(f"Lost connection to Ollama: {exc}") from exc
        if r.status_code != 200:
            if tools and "does not support tools" in r.text:
                self.tool_support[model] = False
                raise ToolsUnsupported(model)
            raise OllamaError(f"Ollama error {r.status_code}: {r.text[:300]}")
        msg = r.json().get("message", {})
        msg["content"] = strip_think(msg.get("content") or "")
        return msg

    # -- embeddings (semantic memory)
    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        try:
            r = requests.post(f"{self.base_url}/api/embed", json={"model": model, "input": texts,
                                                                   "keep_alive": self.keep_alive}, timeout=(5, 120))
        except requests.RequestException as exc:
            raise OllamaError(f"Lost connection to Ollama: {exc}") from exc
        if r.status_code != 200:
            raise OllamaError(f"Embedding failed ({r.status_code}): {r.text[:200]}")
        return r.json().get("embeddings", [])

    # -- downloading models
    def pull_stream(self, name: str) -> Iterator[dict]:
        """Yield progress dicts ({'status','completed','total'}) while Ollama downloads a model."""
        try:
            with requests.post(f"{self.base_url}/api/pull", json={"model": name, "stream": True}, stream=True,
                               timeout=(5, 3600)) as resp:
                if resp.status_code != 200:
                    raise OllamaError(f"Pull failed ({resp.status_code}): {resp.text[:200]}")
                for line in resp.iter_lines():
                    if line:
                        chunk = json.loads(line)
                        if chunk.get("error"):
                            raise OllamaError(chunk["error"])
                        yield chunk
        except requests.RequestException as exc:
            raise OllamaError(f"Lost connection to Ollama: {exc}") from exc
        self._models_cache = (0.0, [])
