"""Safety: every risky action goes through the SafetyGate.

Risk levels
  safe     read-only things (web search, reading files in the workspace, stock quotes)
  write    changes files / runs commands inside the workspace, local git changes
  publish  sends something out to the world (git push, GitHub issues / new repos)
  control  moves the mouse / presses keys on your real screen

Unless "Autonomous Agent Mode" is switched on, every action above `safe` waits for you to click
Approve in the UI (or via the API). If nobody answers within the timeout the action is DENIED.
The "Seal the Gates" kill-switch denies everything pending and switches autonomy off.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import AUDIT_LOG, DATA_DIR, get_workspace

AUDIT_MAX_BYTES = 5_000_000

RISKS = ("safe", "write", "publish", "control")
CONFIRM_TIMEOUT = 180  # seconds


class Denied(Exception):
    """The user (or the safety rules) refused an action."""


@dataclass
class Pending:
    id: str
    tool: str
    risk: str
    summary: str
    created: float = field(default_factory=time.time)
    decision: Optional[bool] = None
    event: threading.Event = field(default_factory=threading.Event, repr=False)

    def public(self) -> dict:
        return {"id": self.id, "tool": self.tool, "risk": self.risk, "summary": self.summary,
                "age": round(time.time() - self.created, 1), "timeout": CONFIRM_TIMEOUT}


class SafetyGate:
    def __init__(self) -> None:
        self.autonomous = False  # never persisted: you must opt in again every launch
        # council(tool, detail, goal, cancel) -> (approved, explanation). Set by the core; None means "no council": changes that
        # need the three heads' approval are refused.
        self.council = None
        self._pending: dict[str, Pending] = {}
        self._lock = threading.RLock()
        self._audit_lock = threading.Lock()

    # -- autonomy switch
    def set_autonomous(self, on: bool) -> None:
        self.autonomous = bool(on)
        self.audit("safety", "autonomous_mode", {"enabled": self.autonomous})

    # -- confirmations
    def request(self, tool: str, risk: str, summary: str, wait: bool = True) -> bool:
        """Ask permission for an action. Returns True if allowed."""
        if risk == "safe":
            return True
        if self.autonomous:
            self.audit(tool, "auto-approved", {"risk": risk, "summary": summary})
            return True
        p = Pending(uuid.uuid4().hex[:8], tool, risk, summary)
        with self._lock:
            self._pending[p.id] = p
        if not wait:
            return False
        approved = p.event.wait(CONFIRM_TIMEOUT) and bool(p.decision)
        with self._lock:
            self._pending.pop(p.id, None)
        self.audit(tool, "approved" if approved else "denied", {"risk": risk, "summary": summary})
        return approved

    def pending(self) -> list[dict]:
        with self._lock:
            return [p.public() for p in self._pending.values() if not p.event.is_set()]

    def decide(self, pending_id: str, approve: bool) -> bool:
        with self._lock:
            p = self._pending.get(pending_id)
        if not p:
            return False
        p.decision = approve
        p.event.set()
        return True

    def seal(self) -> int:
        """Kill-switch: deny everything waiting and turn autonomy off."""
        self.autonomous = False
        n = 0
        with self._lock:
            for p in list(self._pending.values()):
                p.decision = False
                p.event.set()
                n += 1
        self.audit("safety", "seal_the_gates", {"denied": n})
        return n

    # -- audit log
    def audit(self, tool: str, event: str, detail: Optional[dict] = None) -> None:
        line = json.dumps({"t": time.strftime("%Y-%m-%d %H:%M:%S"), "tool": tool, "event": event,
                           "detail": detail or {}}, ensure_ascii=False)
        with self._audit_lock:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            try:
                if AUDIT_LOG.exists() and AUDIT_LOG.stat().st_size > AUDIT_MAX_BYTES:
                    AUDIT_LOG.replace(AUDIT_LOG.with_name(AUDIT_LOG.name + ".1"))    # keep one older generation
            except OSError:
                pass
            with open(AUDIT_LOG, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    @staticmethod
    def read_audit(limit: int = 100) -> list[str]:
        if not AUDIT_LOG.exists():
            return []
        with open(AUDIT_LOG, "rb") as fh:                      # only the tail: the log can be large
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 400_000))
            data = fh.read()
        return data.decode("utf-8", errors="replace").splitlines()[-limit:]


# ---------------------------------------------------------------- file sandbox
def safe_path(relative: str, base: Optional[Path] = None) -> Path:
    """Resolve `relative` inside the workspace; refuse anything that escapes it (.., absolute paths)."""
    base = (base or get_workspace()).resolve()
    candidate = (base / relative).resolve() if not Path(relative).is_absolute() else Path(relative).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        raise Denied(f"Path '{relative}' is outside the workspace ({base}). Blocked by the sandbox.")
    return candidate
