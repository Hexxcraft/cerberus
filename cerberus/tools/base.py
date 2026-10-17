"""Tool registry. A "tool" is one thing Cerberus (or a custom agent) can DO: search the web, edit a file, ...

To add your own tool, write a function and decorate it with @tool(...) in any file under cerberus/tools/,
then import that file in cerberus/tools/__init__.py. It will automatically show up as a skill in the UI.
"""
from __future__ import annotations

import inspect
import json
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional

from ..db import Database
from ..safety import Denied, SafetyGate

MAX_RESULT_CHARS = 6000


@dataclass
class ToolContext:
    """Everything a tool is allowed to know about who is calling it."""
    gate: SafetyGate
    db: Database
    origin: str = "local"          # "local" (this PC's UI) or "remote" (another device on the network)
    actor: str = "cerberus"        # who is acting (shown in the audit log)
    cancel: Optional[threading.Event] = None
    intent: str = ""               # what the user asked for (the heads judge changes against this)


@dataclass
class Tool:
    name: str
    skill: str                     # the friendly skill group shown in the agent form, e.g. "Web Searching"
    description: str
    params: dict[str, str]         # parameter name -> short description (append "(optional)" if optional)
    risk: str                      # safe | write | publish | control
    func: Callable[..., Any]
    local_only: bool = False       # True = refuse when the request came from another device
    council: bool = False          # True = all three heads must approve the exact change before it happens (fails closed)
    preflight: Optional[Callable[..., None]] = None   # (ctx, **args) -> raises Denied for hard-forbidden targets
    preview: Optional[Callable[..., str]] = None      # (ctx, **args) -> plain-language description (1st line = headline)

    def prompt_line(self) -> str:
        args = ", ".join(f'"{k}": <{v}>' for k, v in self.params.items())
        return f'- {self.name}({{{args}}}) -> {self.description}'

    def public(self) -> dict:
        return {"name": self.name, "skill": self.skill, "description": self.description,
                "params": self.params, "risk": self.risk, "local_only": self.local_only, "council": self.council}

    def json_schema(self) -> dict:
        """The parameters as a JSON schema (for native tool calling and constrained-JSON replies)."""
        props, required = {}, []
        for k, desc in self.params.items():
            low = desc.lower()
            kind = ("array" if low.startswith("list") else "boolean" if low.startswith("true") else
                    "integer" if any(w in low for w in ("seconds", "pixels", "clicks", "number 1")) else "string")
            props[k] = {"type": kind, "description": desc}
            if kind == "array":
                props[k]["items"] = {"type": "string"}
            if "(optional)" not in low:
                required.append(k)
        return {"type": "object", "properties": props, "required": required}

    def ollama_spec(self) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": self.json_schema()}}


REGISTRY: dict[str, Tool] = {}


def tool(name: str, skill: str, description: str, params: Optional[dict[str, str]] = None,
         risk: str = "safe", local_only: bool = False, council: bool = False,
         preflight: Optional[Callable] = None, preview: Optional[Callable] = None):
    def wrap(fn: Callable) -> Callable:
        REGISTRY[name] = Tool(name, skill, description, params or {}, risk, fn, local_only, council, preflight, preview)
        return fn
    return wrap


def skills_catalog() -> dict[str, list[str]]:
    """{'Web Searching': ['web_search'], ...} for the UI."""
    out: dict[str, list[str]] = {}
    for t in REGISTRY.values():
        out.setdefault(t.skill, []).append(t.name)
    return dict(sorted(out.items()))


def tools_for_skills(skills: list[str]) -> list[Tool]:
    wanted = {s.lower() for s in skills}
    return [t for t in REGISTRY.values() if t.skill.lower() in wanted]


def tool_prompt(tools: list[Tool]) -> str:
    return "\n".join(t.prompt_line() for t in tools)


def _summarize(name: str, args: dict) -> str:
    text = json.dumps(args, ensure_ascii=False)
    return f"{name} {text[:400]}{'...' if len(text) > 400 else ''}"


SENSITIVE_ARG = ("password", "passwd", "secret", "token", "key", "text", "content", "body")


def _audit_args(args: dict) -> dict:
    """Arguments for the audit log: long values are shortened and anything that may be a password or file body is not kept."""
    out = {}
    for k, v in args.items():
        if any(w in str(k).lower() for w in SENSITIVE_ARG):
            out[k] = f"<{len(str(v))} characters not logged>"
        else:
            s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
            out[k] = s if len(s) <= 200 else s[:200] + f"...[{len(s) - 200} more]"
    return out


def run_tool(name: str, args: Optional[dict], ctx: ToolContext) -> str:
    """Run a tool safely: checks origin, asks the gate for permission, logs, never raises."""
    args = args if isinstance(args, dict) else {}
    t = REGISTRY.get(name)
    if t is None:
        return f"ERROR: unknown tool '{name}'. Available: {', '.join(sorted(REGISTRY))}"
    if t.local_only and ctx.origin != "local":
        ctx.gate.audit(name, "blocked_remote", {"actor": ctx.actor})
        return "ERROR: this tool only works from the computer Cerberus runs on (blocked for remote clients)."
    summary = _summarize(name, args)
    try:
        if t.preflight:
            t.preflight(ctx, **args)
        detail = t.preview(ctx, **args) if t.preview else ""
        if t.council:
            vote = getattr(ctx.gate, "council", None)
            if vote is None:
                return "DENIED: this change needs the three heads to approve it, and no council is available."
            approved, why = vote(name, detail or summary, ctx.intent, ctx.cancel)
            ctx.gate.audit(name, "council_approved" if approved else "council_refused", {"actor": ctx.actor, "votes": why})
            if not approved:
                return ("DENIED: the three heads did not all agree, so nothing was changed. " + why +
                        ". Fix the problem they name (or tell the user why you cannot) before proposing it again.")
        headline = detail.splitlines()[0] if detail else summary
        if not ctx.gate.request(name, t.risk, f"[{ctx.actor}] {headline}"):
            return "DENIED: the user did not approve this action."
        ctx.gate.audit(name, "run", {"actor": ctx.actor, "args": _audit_args(args), "risk": t.risk})
        result = t.func(ctx, **args)
    except Denied as exc:
        return f"DENIED: {exc}"
    except TypeError as exc:
        try:
            inspect.signature(t.func).bind(ctx, **args)
        except TypeError:
            return f"ERROR: bad arguments for {name}: {exc}. Expected params: {json.dumps(t.params)}"
        ctx.gate.audit(name, "error", {"error": str(exc)})          # the tool itself failed; the arguments were fine
        return f"ERROR: TypeError: {exc}"
    except Exception as exc:  # tools must never crash the caller
        ctx.gate.audit(name, "error", {"error": str(exc)})
        return f"ERROR: {type(exc).__name__}: {exc}"
    text = result if isinstance(result, str) else json.dumps(result, indent=1, ensure_ascii=False, default=str)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + f"\n...[truncated, {len(text) - MAX_RESULT_CHARS} more characters]"
    return text
