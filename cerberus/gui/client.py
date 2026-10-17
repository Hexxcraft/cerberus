"""Tiny HTTP client the desktop app uses to talk to the Cerberus server (same API your phone uses)."""
from __future__ import annotations

import json
from typing import Any, Callable, Optional

import requests


class ApiError(RuntimeError):
    pass


class ApiClient:
    def __init__(self, base_url: str, token: str = "") -> None:
        self.base = base_url.rstrip("/")
        self.session = requests.Session()
        self._chat_response: Optional[requests.Response] = None
        if token:
            self.session.headers["X-Cerberus-Token"] = token

    def _call(self, method: str, path: str, **kw) -> Any:
        try:
            r = self.session.request(method, self.base + "/api" + path, timeout=kw.pop("timeout", 30), **kw)
        except requests.RequestException as exc:
            raise ApiError(f"Cannot reach the Cerberus server: {exc}") from exc
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise ApiError(str(detail))
        return r.json() if r.content else None

    def get(self, path: str, /, **params) -> Any:
        return self._call("GET", path, params=params or None)

    def get_text(self, path: str, /, **params) -> str:
        try:
            r = self.session.get(self.base + "/api" + path, params=params or None, timeout=30)
        except requests.RequestException as exc:
            raise ApiError(f"Cannot reach the Cerberus server: {exc}") from exc
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise ApiError(str(detail))
        return r.text

    def post(self, path: str, body: Optional[dict] = None, timeout: int = 30) -> Any:
        return self._call("POST", path, json=body if body is not None else {}, timeout=timeout)

    def put(self, path: str, body: dict) -> Any:
        return self._call("PUT", path, json=body)

    def delete(self, path: str, /, **params) -> Any:
        return self._call("DELETE", path, params=params or None)

    def run_tool(self, name: str, **args) -> str:
        """Run a tool through the server's safety gate. Can wait (up to 3 min) while you approve it."""
        return self.post(f"/tools/{name}", {"args": args}, timeout=200)["result"]

    def stream_pull(self, name: str, on_progress: Callable[[dict], None]) -> None:
        """Blocking: download a model via Ollama and report progress chunks. Run this in a thread."""
        try:
            with self.session.post(self.base + "/api/models/pull", json={"name": name}, stream=True, timeout=(5, 3600)) as r:
                if r.status_code >= 400:
                    on_progress({"error": r.text[:200], "done": True})
                    return
                for raw in r.iter_lines():
                    if raw:
                        on_progress(json.loads(raw))
        except (requests.RequestException, ValueError) as exc:
            on_progress({"error": str(exc), "done": True})

    def stream_chat(self, body: dict, on_event: Callable[[dict], None], should_stop: Callable[[], bool]) -> None:
        """Blocking: read the NDJSON stream and call on_event for each event. Run this in a thread."""
        saw_done = False
        try:
            with self.session.post(self.base + "/api/chat", json=body, stream=True, timeout=(5, 600)) as r:
                self._chat_response = r
                if r.status_code >= 400:
                    on_event({"type": "error", "message": r.text[:300]})
                    on_event({"type": "done"})
                    return
                for raw in r.iter_lines():
                    if should_stop():
                        return
                    if raw:
                        ev = json.loads(raw)
                        saw_done = saw_done or ev.get("type") == "done"
                        on_event(ev)
        except (requests.RequestException, ValueError) as exc:
            if not should_stop():
                on_event({"type": "error", "message": f"Connection lost: {exc}"})
                on_event({"type": "done"})
            return
        finally:
            self._chat_response = None
        if not saw_done and not should_stop():
            on_event({"type": "error", "message": "The connection closed before the answer finished."})
            on_event({"type": "done"})

    def abort_chat(self) -> None:
        """Close the running chat stream from another thread, so a silent connection is released straight away. The server
        cancels the run when the stream closes."""
        resp = getattr(self, "_chat_response", None)
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass
