"""The tool loop: lets a local model USE tools.

Three strategies, tried in this order for each model (what a model can do is learned on first use):
  native  - Ollama's built-in tool calling (best, but only some models support it, e.g. mistral, llama3.1, qwen2.5)
  schema  - every reply is forced to be valid JSON by Ollama's constrained decoding: {"action","tool","args","report"}
  text    - last resort: the model is asked to print one JSON line; we hunt for it in the text

All three feed results back as "tool results" until the model answers in plain text. Every call goes through run_tool(),
so the safety gate, sandbox and audit log apply no matter which strategy is used.
"""
from __future__ import annotations

import json
import threading
from typing import Iterator, Optional

from .ollama_client import OllamaClient, OllamaError, ToolsUnsupported
from .tools import Tool, ToolContext, run_tool, tool_prompt

LOOP_RULES = """You can use tools to act. To use ONE tool, reply with ONLY a JSON object, nothing else:
{"tool": "<tool name>", "args": {<arguments>}}

Available tools:
%s

After each call you will receive "TOOL RESULT". Then call another tool or finish.
When you are finished, reply in plain text (NOT JSON) with a short report of what you did and found.
Rules: never invent tool results; if a result says DENIED or ERROR, say so honestly and do not retry the same call;
use at most %d tool calls; if no tool is needed, just answer in plain text.
Tool arguments must be real literal values (copy text from earlier results in full), never placeholders or code."""

NATIVE_RULES = ("You have tools. Call a tool whenever the request needs one (files, web, memory...). Never pretend to have done "
                "something you did not do with a tool. If a result says DENIED or ERROR, say so honestly and do not repeat the "
                "call. Use at most %d tool calls. Tool arguments must be real literal values (copy text from earlier results in full, "
                "never placeholders or code). If asked to remember something about the user, use the remember tool. When the "
                "job is done, or no tool is needed, answer in plain text with a short report.")

SCHEMA_RULES = """You can use tools to act. Every reply MUST be one JSON object:
- To use a tool: {"action": "tool", "tool": "<tool name>", "args": {<arguments>}}
- When finished (or if no tool is needed): {"action": "final", "report": "<your answer or short report in plain text>"}

Available tools:
%s

After each tool call you will receive "TOOL RESULT". Never invent results; if a result says DENIED or ERROR, say so honestly
and do not repeat that call. Use at most %d tool calls.
Tool arguments must be real literal values: copy text from earlier TOOL RESULTs in full (apply any requested change yourself,
e.g. uppercase it) - never write placeholders, templates or code in args."""


def extract_action(text: str) -> Optional[dict]:
    """Find the first JSON object in `text` that has a "tool" key. Tolerates ```json fences and chatter."""
    i = 0
    while True:
        start = text.find("{", i)
        if start == -1:
            return None
        depth, in_str, esc = 0, False, False
        for j in range(start, len(text)):
            ch = text[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:j + 1])
                        if isinstance(obj, dict) and isinstance(obj.get("tool"), str):
                            return obj
                    except json.JSONDecodeError:
                        pass
                    break
        i = start + 1


def _calls_from_text(text: str, names: set[str]) -> list[tuple[str, dict]]:
    """Recover tool calls a model printed as text: {"tool":..}, or Mistral's [TOOL_CALLS] [{"name":..,"arguments":..}]."""
    action = extract_action(text)
    if action:
        return [(action["tool"], action.get("args") or {})]
    start = text.find("[")
    while start != -1:
        try:
            arr, _ = json.JSONDecoder().raw_decode(text[start:])
        except json.JSONDecodeError:
            arr = None
        if isinstance(arr, list) and arr and all(isinstance(x, dict) and x.get("name") in names for x in arr):
            return [(x["name"], x.get("arguments") if isinstance(x.get("arguments"), dict) else {}) for x in arr]
        start = text.find("[", start + 1)
    return []


def _schema_for(names: list[str]) -> dict:
    return {"type": "object",
            "properties": {"action": {"type": "string", "enum": ["tool", "final"]},
                           "tool": {"type": "string", "enum": names},
                           "args": {"type": "object"},
                           "report": {"type": "string"}},
            "required": ["action"]}


def _result_for(name: str, args: dict, names: set[str], last_call: Optional[str], ctx: ToolContext) -> tuple[str, str]:
    signature = f"{name}:{json.dumps(args, sort_keys=True, default=str)}"
    if name not in names:
        return f"ERROR: tool '{name}' is not available to you. Available: {', '.join(sorted(names))}", signature
    if signature == last_call:
        return "ERROR: you just made this exact call. Do not repeat it; finish or try something different.", signature
    return run_tool(name, args, ctx), signature


def tool_loop(client: OllamaClient, model: str, system: str, goal: str, tools: list[Tool], ctx: ToolContext,
              max_steps: int = 6, temperature: float = 0.2, cancel: Optional[threading.Event] = None,
              strategy: str = "auto") -> Iterator[dict]:
    """Generator. Yields tool_call / tool_result events and finally {"type": "loop_final", "text": ..., "strategy": ...}."""
    if not tools:
        yield {"type": "loop_final", "strategy": "none",
               "text": client.chat(model, [{"role": "system", "content": system}, {"role": "user", "content": goal}],
                                   temperature, cancel)}
        return
    can_raw = hasattr(client, "chat_once")
    order = {"auto": ["native", "schema", "text"], "native": ["native", "text"], "schema": ["schema", "text"],
             "text": ["text"]}[strategy] if can_raw else ["text"]
    if "native" in order and getattr(client, "tool_support", {}).get(model) is False:
        order.remove("native")
    for i, strat in enumerate(order):
        runner = {"native": _run_native, "schema": _run_schema, "text": _run_text}[strat]
        try:
            yield from runner(client, model, system, goal, tools, ctx, max_steps, temperature, cancel)
            return
        except (ToolsUnsupported, _Unusable):
            if i == len(order) - 1:
                raise OllamaError(f"{model} could not follow any tool protocol.")
            continue


class _Unusable(Exception):
    """A strategy produced nothing usable before doing anything; try the next one."""


def _stopped() -> dict:
    return {"type": "loop_final", "text": "(stopped)", "strategy": "stopped"}


# ---------------------------------------------------------------- strategy 1: native tool calling
def _run_native(client, model, system, goal, tools, ctx, max_steps, temperature, cancel) -> Iterator[dict]:
    names = {t.name for t in tools}
    specs = [t.ollama_spec() for t in tools]
    messages = [{"role": "system", "content": system + "\n\n" + NATIVE_RULES % max_steps}, {"role": "user", "content": goal}]
    last_call, calls = None, 0
    while True:
        if cancel is not None and cancel.is_set():
            yield _stopped()
            return
        msg = client.chat_once(model, messages, temperature, tools=specs)
        client.tool_support[model] = True
        wanted = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            wanted.append((fn.get("name", ""), args))
        if not wanted:                                  # some models print the call as text instead
            wanted = _calls_from_text(msg.get("content", ""), names)
        if not wanted:
            yield {"type": "loop_final", "text": msg.get("content", "") or "(no answer)", "strategy": "native"}
            return
        if calls >= max_steps:
            yield {"type": "loop_final", "strategy": "native",
                   "text": "Reached the step limit. Last thought: " + (msg.get("content") or "")[:500]}
            return
        messages.append({"role": "assistant", "content": msg.get("content", ""), "tool_calls": msg.get("tool_calls") or []})
        for name, args in wanted[:3]:
            calls += 1
            yield {"type": "tool_call", "name": name, "args": args}
            result, last_call = _result_for(name, args, names, last_call, ctx)
            yield {"type": "tool_result", "name": name, "result": result}
            messages.append({"role": "tool", "tool_name": name, "content": result})


# ---------------------------------------------------------------- strategy 2: constrained JSON
def _run_schema(client, model, system, goal, tools, ctx, max_steps, temperature, cancel) -> Iterator[dict]:
    names = {t.name for t in tools}
    fmt = _schema_for(sorted(names))
    messages = [{"role": "system", "content": system + "\n\n" + SCHEMA_RULES % (tool_prompt(tools), max_steps)},
                {"role": "user", "content": goal}]
    last_call, calls, started = None, 0, False
    while True:
        if cancel is not None and cancel.is_set():
            yield _stopped()
            return
        try:
            raw = client.chat_once(model, messages, temperature, fmt=fmt).get("content", "")
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get("action") not in ("tool", "final"):
                raise ValueError("bad shape")
        except (ValueError, OllamaError) as exc:
            if not started:
                raise _Unusable() from exc
            yield {"type": "loop_final", "text": "(the model stopped answering in the expected format)", "strategy": "schema"}
            return
        if data["action"] == "final" or calls >= max_steps:
            text = str(data.get("report") or "(no report)")
            yield {"type": "loop_final", "strategy": "schema",
                   "text": text if data["action"] == "final" else "Reached the step limit. Last thought: " + text[:500]}
            return
        started = True
        name, args = str(data.get("tool", "")), data.get("args") if isinstance(data.get("args"), dict) else {}
        calls += 1
        yield {"type": "tool_call", "name": name, "args": args}
        result, last_call = _result_for(name, args, names, last_call, ctx)
        yield {"type": "tool_result", "name": name, "result": result}
        messages.append({"role": "assistant", "content": raw})
        messages.append({"role": "user", "content": f"TOOL RESULT ({name}):\n{result}"})


# ---------------------------------------------------------------- strategy 3: plain text protocol
def _run_text(client, model, system, goal, tools, ctx, max_steps, temperature, cancel) -> Iterator[dict]:
    names = {t.name for t in tools}
    messages = [{"role": "system", "content": system + "\n\n" + LOOP_RULES % (tool_prompt(tools), max_steps)},
                {"role": "user", "content": goal}]
    last_call: Optional[str] = None
    for step in range(max_steps + 1):
        if cancel is not None and cancel.is_set():
            yield _stopped()
            return
        reply = client.chat(model, messages, temperature, cancel)
        action = extract_action(reply)
        if action is None or step == max_steps:
            yield {"type": "loop_final", "strategy": "text",
                   "text": reply if action is None else "Reached the step limit. Last thought: " + reply[:500]}
            return
        name, args = action["tool"], action.get("args") or {}
        yield {"type": "tool_call", "name": name, "args": args}
        result, last_call = _result_for(name, args, names, last_call, ctx)
        yield {"type": "tool_result", "name": name, "result": result}
        messages.append({"role": "assistant", "content": reply})
        messages.append({"role": "user", "content": f"TOOL RESULT ({name}):\n{result}"})
