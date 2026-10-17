"""Error memory: Cerberus writes down what went wrong and shows the relevant lessons to its tool-using Hand next time.

Two kinds of memory exist side by side:
  - council lessons (council.py): mistakes in ANSWERS that the other two heads caught and confirmed.
  - this file: mistakes in ACTIONS and RUNS - a tool called with bad arguments, a change the heads refused, a target that is
    off limits, a run that crashed.

Entries are de-duplicated by a fingerprint with a counter, capped, scrubbed of secrets, and visible/erasable from the Gates tab.
Only lessons that help the model do better (bad arguments, unknown tools, refused or off-limits targets, repeated tool errors)
are fed back into prompts. Environment trouble (Ollama down, too hot, a crash inside Cerberus) is recorded for the review
report only, because telling a model about it would not make it behave any differently.
"""
from __future__ import annotations

import re
import threading
import time

from .telemetry import _signature, redact

KEY = "error_memory"
CAP = 80
ALWAYS_TEACH = {"tool_args", "tool_unknown", "council_refused", "off_limits"}
_lock = threading.Lock()


def load(db) -> list[dict]:
    return db.kv_get(KEY) or []


def record(db, kind: str, where: str, text: str, advice: str = "", teach: bool = False, mode: str = "") -> dict:
    text = redact(text, 240)
    sig = f"{kind}|{where}|{_signature(text)}"
    now = time.time()
    with _lock:
        items = load(db)
        for it in items:
            if it.get("sig") == sig:
                it["count"] = it.get("count", 1) + 1
                it["last"] = now
                break
        else:
            it = {"sig": sig, "kind": kind, "where": where, "text": text, "advice": advice, "teach": teach, "mode": mode,
                  "count": 1, "first": now, "last": now}
            items.append(it)
        items.sort(key=lambda x: x.get("last", 0))
        db.kv_set(KEY, items[-CAP:])
    return it


def forget(db) -> None:
    db.kv_set(KEY, [])


def _teachable(it: dict) -> bool:
    return bool(it.get("teach")) and bool(it.get("advice")) and (it.get("kind") in ALWAYS_TEACH or it.get("count", 1) >= 2)


def lessons_block(db, request: str, k: int = 4) -> str:
    from .memory import keyword_score
    pool = [it for it in load(db) if _teachable(it)]
    if not pool:
        return ""
    ranked = sorted(pool, key=lambda it: (keyword_score(request, it["text"] + " " + it["where"]), it.get("count", 1), it.get("last", 0)),
                    reverse=True)[:k]
    return "Pitfalls from earlier errors (do not repeat them):\n" + "\n".join("- " + it["advice"] for it in ranked)


def summary(db) -> dict:
    items = load(db)
    return {"count": len(items), "teachable": sum(1 for it in items if _teachable(it)),
            "items": sorted(items, key=lambda it: -it.get("last", 0))}


class Watcher:
    """Feed it every event of a run; it files the errors worth remembering. Never raises."""

    def __init__(self, db, mode_key: str) -> None:
        self.db, self.mode = db, mode_key

    def feed(self, ev: dict) -> None:
        try:
            kind = ev.get("type")
            if kind == "tool_result":
                self._tool(ev.get("name", "?"), str(ev.get("result") or ""))
            elif kind == "error":
                self._run_error(ev)
        except Exception:
            pass

    def _tool(self, name: str, result: str) -> None:
        if not result.startswith(("ERROR", "DENIED")):
            return
        first = result.splitlines()[0]
        if first.startswith("DENIED: the user did not approve"):
            return                                                   # the user's choice is not a mistake
        if "blocked for remote clients" in first:
            self._add("remote_limit", name, first, "", False)
        elif first.startswith("ERROR: unknown tool"):
            self._add("tool_unknown", name, first, f"Only call tools that are in your list; '{name}' does not exist.", True)
        elif first.startswith("ERROR: bad arguments"):
            self._add("tool_args", name, first, f"Call {name} with exactly the parameter names it documents, and no others.", True)
        elif first.startswith("DENIED: the three heads"):
            why = re.sub(r"^DENIED: the three heads did not all agree, so nothing was changed\.\s*", "", result.splitlines()[0])
            self._add("council_refused", name, first,
                      f"A {name} change was refused by the heads ({redact(why, 140)}). Propose smaller, clearly justified changes, "
                      "or explain to the user why it cannot be done.", True)
        elif first.startswith("DENIED:"):
            self._add("off_limits", name, first,
                      f"{name} was refused: {redact(first[7:], 140)}. Do not retry that target; tell the user it is off limits.", True)
        else:
            self._add("tool_error", name, first,
                      f"{name} failed with: {redact(first[6:], 120)}. Check the inputs (for example list the folder first) before calling it again.", True)

    def _add(self, kind: str, where: str, text: str, advice: str, teach: bool) -> None:
        record(self.db, kind, where, text, advice, teach, self.mode)

    def _run_error(self, ev: dict) -> None:
        kind = ev.get("kind") or "run_error"
        if kind == "input":
            return                                                   # an empty message is not a mistake worth keeping
        record(self.db, kind, "run", ev.get("detail") or ev.get("message", ""), "", False, self.mode)
