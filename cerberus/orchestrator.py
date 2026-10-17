"""The Orchestrator: runs the "Three Wise Men" pipeline for a chat request and streams progress events.

Flow of one request
  0. save the user's message, learn from it (habit stats)
  1. gather CONTEXT with tools (stock quotes, job listings, project files, your writing style...)
  2. optional "Hand" step: a tool-using model carries out actions you asked for (only if "Let Cerberus act" is on)
  3. each head (stage) of the mode speaks in order, seeing what the previous heads produced
  4. optional CLOUD HANDOFF: the local work is compressed into one small prompt for a frontier model
  5. Coding mode: parse the files, check syntax, auto-repair, optionally save to the workspace
  6. save the answer, learn facts in the background

`run()` is a generator that yields event dicts (see the README "API" section); the server streams them as NDJSON.
"""
from __future__ import annotations

import ast
import json
import re
import threading
import traceback
from typing import Iterator, Optional

from . import capabilities
from . import cloud as cloudmod
from . import dossier, filectx, guard, hardware, learning, mistakes, plugins, telemetry, verify
from .agent_runtime import tool_loop
from .config import get_workspace
from .modes import MODES, Mode, Stage, render
from .ollama_client import Cancelled, OllamaError, strip_think
from .safety import Denied, safe_path
from .tools import ToolContext, run_tool, tools_for_skills
from .tools.finance import budget_summary_text, fetch_quote, format_quotes
from .tools.jobs import format_jobs, search_jobs

MAX_REPAIRS = 2             # repair rounds after the first check
MIN_CHECK_CHARS = 60        # answers shorter than this are not worth cross-checking

CONTEXT_LIMIT = 6000        # max characters of tool-gathered context given to a head
PRIOR_LIMIT = 7000          # max characters of earlier heads' output given to a head
HISTORY_CHAR_LIMIT = 1500   # per old chat message

TICKER_NAMES = {"apple": "AAPL", "tesla": "TSLA", "microsoft": "MSFT", "google": "GOOGL", "alphabet": "GOOGL",
                "amazon": "AMZN", "nvidia": "NVDA", "meta": "META", "netflix": "NFLX", "bitcoin": "BTC-USD",
                "ethereum": "ETH-USD", "s&p": "SPY", "sp500": "SPY", "s&p 500": "SPY", "nasdaq": "QQQ", "vanguard": "VOO"}
TICKER_STOP = set("""A I AM PM THE AND FOR ETF IRA USD USA CEO IPO ROTH HELP MY IS IT ON OR TO OF IN AN AS AT BE DO IF NO SO UP US WE
OK ALL ANY CAN HOW WHY WHO NOW NEW BUY SELL HOLD TAX TAXES PLAN LOAN DEBT RATE SAVE GOAL BEST FUND FUNDS STOCK STOCKS TODAY WHAT WHEN
WITH FROM THAT THIS WILL YOUR AFTER 401K HSA APR APY ROI GDP CPI FED SEC IRS""".split())


# ---------------------------------------------------------------- coding helpers
FENCE_LINE = re.compile(r"^\s*(`{3,})")
FILE_HEADER = re.compile(r"^[ \t]*(?:#{1,4}[ \t]*)?\*{0,2}[ \t]*FILE:[ \t]*(.+?)[ \t]*\*{0,2}[ \t]*$", re.IGNORECASE | re.MULTILINE)
PLACEHOLDER = re.compile(r"(TODO|FIXME|implement me|your code here|rest of (the )?code|\.\.\. ?existing)", re.IGNORECASE)


def parse_files(text: str) -> tuple[list[dict], str]:
    """Extract [{'path','content'}] from '### FILE: path' + fenced block sections, and any trailing '## Notes' text."""
    headers = list(FILE_HEADER.finditer(text))
    files: list[dict] = []
    notes = ""
    for idx, h in enumerate(headers):
        end = headers[idx + 1].start() if idx + 1 < len(headers) else len(text)
        segment = text[h.end():end]
        lines = segment.splitlines()
        open_i = next((i for i, ln in enumerate(lines) if FENCE_LINE.match(ln)), None)
        if open_i is None:
            continue
        ticks = len(FENCE_LINE.match(lines[open_i]).group(1))
        close_i = None
        for i in range(len(lines) - 1, open_i, -1):
            if lines[i].strip() == "`" * ticks or (set(lines[i].strip()) == {"`"} and len(lines[i].strip()) >= ticks):
                close_i = i
                break
        body = lines[open_i + 1: close_i if close_i is not None else len(lines)]
        path = re.sub(r"^(\./)+", "", h.group(1).strip().strip("`*\"' ").replace("\\", "/")).lstrip("/")
        if path:
            files.append({"path": path, "content": "\n".join(body).rstrip() + "\n"})
        if idx + 1 == len(headers) and close_i is not None:
            tail = "\n".join(lines[close_i + 1:]).strip()
            if tail:
                notes = tail
    if not notes:
        m = re.search(r"^\s*#{1,3}\s*Notes.*", text, re.IGNORECASE | re.MULTILINE | re.DOTALL)
        notes = m.group(0).strip() if m else ""
    return files, notes


def render_files(files: list[dict], notes: str = "") -> str:
    out = []
    for f in files:
        ext = f["path"].rsplit(".", 1)[-1] if "." in f["path"] else ""
        out.append(f"### FILE: {f['path']}\n```{ext}\n{f['content'].rstrip()}\n```")
    if notes:
        out.append(notes)
    return "\n\n".join(out)


def validate_files(files: list[dict]) -> list[str]:
    """Cheap, local correctness checks: Python/JSON syntax and leftover placeholders."""
    problems = []
    for f in files:
        path, src = f["path"], f["content"]
        if path.endswith(".py"):
            try:
                ast.parse(src, filename=path)
            except SyntaxError as exc:
                problems.append(f"{path}: SyntaxError line {exc.lineno}: {exc.msg}")
        elif path.endswith(".json"):
            try:
                json.loads(src)
            except json.JSONDecodeError as exc:
                problems.append(f"{path}: invalid JSON ({exc})")
        if not path.lower().endswith((".md", ".txt")) and PLACEHOLDER.search(src):
            problems.append(f"{path}: still contains a placeholder (TODO / 'rest of code'). Implement it fully.")
    return problems


def save_project(files: list[dict], project: str, ctx: ToolContext) -> list[str]:
    """Write files to workspace/<project>/ after ONE approval. Returns the saved relative paths."""
    project = re.sub(r"[^A-Za-z0-9_.-]+", "_", project.strip()).strip("._") or "project"
    listing = ", ".join(f["path"] for f in files[:8]) + (" ..." if len(files) > 8 else "")
    if not ctx.gate.request("save_project", "write", f"Write {len(files)} file(s) to workspace/{project}/: {listing}"):
        raise Denied("You declined to save the files.")
    saved = []
    for f in files:
        target = safe_path(f"{project}/{f['path']}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f["content"], encoding="utf-8")
        saved.append(target.relative_to(get_workspace()).as_posix())
    ctx.gate.audit("save_project", "wrote", {"project": project, "files": saved})
    return saved


# ---------------------------------------------------------------- orchestrator
class Orchestrator:
    def __init__(self, core) -> None:
        self.core = core

    # -- model plumbing
    @property
    def settings(self) -> dict:
        return self.core.settings()

    def model_map(self, mode_key: Optional[str] = None) -> dict[str, Optional[str]]:
        """role -> installed model. A per-mode override (settings 'mode_models') wins when that model is installed."""
        ollama = self.core.ollama
        mapping = {role: ollama.resolve(name) for role, name in self.settings["models"].items()}
        for role, name in ((self.settings.get("mode_models") or {}).get(mode_key or "", {}) or {}).items():
            found = ollama.resolve(name) if name and role in mapping else None
            if found:
                mapping[role] = found
        return mapping

    def _model_for(self, role: str, mapping: dict[str, Optional[str]]) -> str:
        if mapping.get(role):
            return mapping[role]  # type: ignore[return-value]
        for m in mapping.values():          # fall back to any head that IS installed
            if m:
                return m
        names = self.core.ollama.installed_names()
        if names:
            return names[0]
        raise OllamaError("No models are installed in Ollama. Run: ollama pull llama3")

    def llm(self, role: str, messages: list[dict], temperature: float = 0.3, json_mode: bool = False,
            cancel: Optional[threading.Event] = None) -> str:
        return self.core.ollama.chat(self._model_for(role, self.model_map()), messages, temperature, cancel, json_mode)

    @staticmethod
    def _low_memory() -> bool:
        """True when the computer is short on memory or the graphics card is warm: do the lighter kind of work."""
        try:
            return bool(hardware.pressure()["low"]) or guard.should_throttle()
        except Exception:
            return False

    def _cool_down(self, cancel) -> Iterator[dict]:
        """If the computer is hot, say so and wait for it to cool; stop the run if it will not."""
        st = guard.status()
        if not st["blocked"]:
            return
        hot = st["hottest"]
        yield {"type": "council", "phase": "skipped", "message": f"The {hot['label']} is hot ({hot['temp_c']:g} C). Pausing so it can cool down..."}
        ok, why = guard.ensure_safe(cancel)
        if not ok:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            raise Denied(why)

    def _history(self, mode_key: str, turns: int) -> list[dict]:
        if turns <= 0:
            return []
        rows = self.core.db.recent_messages(mode_key, turns)
        return [{"role": r["role"], "content": r["content"][:HISTORY_CHAR_LIMIT]} for r in rows
                if r["role"] in ("user", "assistant")]

    # -- context gathering (tools used by code, not by the model)
    def _gather_context(self, mode: Mode, request: str, ctx: ToolContext, options: dict,
                        exclude_id: Optional[int] = None) -> Iterator[dict]:
        """Yields tool events; the last event is {'type':'_context','text':...}."""
        parts: list[str] = []

        def call(name: str, **args) -> str:
            yield_events.append({"type": "tool_call", "name": name, "args": args})
            res = run_tool(name, args, ctx)
            yield_events.append({"type": "tool_result", "name": name, "result": res[:600]})
            return res

        yield_events: list[dict] = []
        db = self.core.db

        if mode.key == "coding":
            listing = call("list_files", path=".")
            parts.append("Workspace files:\n" + listing[:1500])
            for rel in list(dict.fromkeys(re.findall(r"[\w./\\-]+\.(?:py|js|ts|html|css|json|md|txt|cs|java|go|rs)", request)))[:3]:
                body = call("read_file", path=rel.replace("\\", "/"), max_chars=4000)
                if not body.startswith(("File '", "DENIED", "ERROR")):
                    parts.append(f"--- existing file {rel} ---\n{body}")
        elif mode.key == "writing":
            card = db.kv_get("style_card") or {}
            if card.get("text"):
                parts.append(card["text"])
                for i, ex in enumerate(learning.pick_excerpts(db, request), 1):
                    parts.append(f"Example of the author's own writing #{i}:\n\"\"\"\n{ex}\n\"\"\"")
            else:
                parts.append("No writing samples yet - write in a natural, plain, human voice and suggest the user add samples "
                             "in the Soul Ledger tab so the style can be learned.")
        elif mode.key == "wealth":
            tickers = self._extract_tickers(request, [r["symbol"] for r in db.query("SELECT symbol FROM watchlist")])
            if tickers:
                quotes = [fetch_quote(t) for t in tickers[:6]]
                good = [q for q in quotes if "error" not in q]  # ALL-CAPS words that aren't tickers just fail; drop them
                parts.append("Live quotes:\n" + (format_quotes(good) if good else
                                                 "Could not fetch quotes for: " + ", ".join(tickers[:6])))
                yield_events.append({"type": "tool_call", "name": "stock_quote", "args": {"symbols": tickers[:6]}})
            if re.search(r"\b(watch ?list|portfolio|watching)\b", request, re.I):
                parts.append("Watchlist:\n" + call("watchlist_show"))
            if re.search(r"\b(budget|spend|spent|expenses?|income|saving|savings|afford|paycheck)\b", request, re.I):
                parts.append("Budget ledger:\n" + budget_summary_text(db, 30))
            if re.search(r"\b(news|latest|headline|why is|what happened)\b", request, re.I):
                q = (tickers[0] + " stock news") if tickers else request[:120]
                parts.append("Web results:\n" + call("web_search", query=q, max_results=4))
        elif mode.key == "jobs":
            resume = "\n\n".join(s["content"][:3500] for s in db.list_samples("resume")[:2])
            if not resume:
                try:
                    from .tools import resume as resume_studio
                    resume = resume_studio.base_resume_text(db)[:3500]
                except Exception:
                    resume = ""
            parts.append("RESUME:\n" + resume if resume else
                         "No resume on file - ask the user to paste one in Soul Ledger -> Offerings, and use [FILL IN] for unknowns.")
            for url in re.findall(r"https?://\S+", request)[:2]:
                page = call("fetch_url", url=url.rstrip(").,"), max_chars=3500)
                parts.append(f"--- job posting {url} ---\n{page}")
            if re.search(r"\b(find|search|look(?:ing)? for|show me|openings?|hiring|jobs?|positions?|roles?)\b", request, re.I) \
                    and re.search(r"\b(jobs?|openings?|positions?|roles?|hiring|work)\b", request, re.I):
                spec = self._job_query(request)
                yield_events.append({"type": "tool_call", "name": "job_search", "args": spec})
                found = format_jobs(search_jobs(spec["query"], spec.get("location", ""), 8))
                yield_events.append({"type": "tool_result", "name": "job_search", "result": found[:600]})
                parts.append("JOB LISTINGS FOUND:\n" + found)
        elif mode.key == "conversation":
            echoes = self.core.memory.recall_messages(request, k=3, exclude_id=exclude_id)
            if echoes:
                parts.append("Echoes of earlier talks: " + " | ".join(e[:160] for e in echoes))

        for ev in yield_events:
            yield ev
        yield {"type": "_context", "text": "\n\n".join(parts)[:CONTEXT_LIMIT]}

    @staticmethod
    def _extract_tickers(text: str, watch: list[str]) -> list[str]:
        found: list[str] = []
        low = text.lower()
        found += [m.upper() for m in re.findall(r"\$([A-Za-z]{1,5}(?:-USD)?)\b", text)]
        found += [sym for name, sym in TICKER_NAMES.items() if name in low]
        found += [t for t in re.findall(r"\b[A-Z]{2,5}(?:-USD)?\b", text) if t not in TICKER_STOP]
        found += [w for w in watch if re.search(rf"\b{re.escape(w)}\b", text, re.I)]
        return list(dict.fromkeys(found))

    def _job_query(self, request: str) -> dict:
        try:
            raw = self.llm("editor", [
                {"role": "system", "content": 'Extract a job search from the message. Reply ONLY with JSON: {"query":"job title/keywords","location":"city/country or empty"}'},
                {"role": "user", "content": request[:600]}], 0.0, json_mode=True)
            data = json.loads(re.search(r"\{.*\}", raw, re.DOTALL).group(0))
            if data.get("query"):
                return {"query": str(data["query"])[:80], "location": str(data.get("location", ""))[:60]}
        except Exception:
            pass
        q = re.sub(r"\b(find|search|look(ing)?|for|me|show|jobs?|openings?|please|a|an|the|some)\b", " ", request, flags=re.I)
        return {"query": re.sub(r"\s+", " ", q).strip()[:80] or "remote", "location": ""}

    # -- cloud handoff
    def _cloud_settings(self, options: dict) -> Optional[tuple[str, str, str]]:
        cfg = self.settings["cloud"]
        override = options.get("cloud")
        enabled = cfg.get("enabled") if override is None else bool(override)
        if not enabled:
            return None
        pid = cfg.get("provider", "openai")
        prov = (cfg.get("providers") or {}).get(pid, {})
        return pid, prov.get("api_key", ""), prov.get("model") or cloudmod.PROVIDERS.get(pid, {}).get("model", "")

    # -- main entry
    def run(self, request: str, mode_key: str, options: Optional[dict] = None, origin: str = "local",
            cancel: Optional[threading.Event] = None) -> Iterator[dict]:
        """Runs a request and yields events. Every run is also written to the review log (telemetry) and its errors are
        filed in the error memory; neither can ever break the chat."""
        rec = telemetry.Recorder(request, mode_key, options, origin)
        watcher = mistakes.Watcher(self.core.db, mode_key)
        inner = self._run(request, mode_key, options, origin, cancel)
        try:
            for ev in inner:
                trace = ev.pop("_trace", "")
                rec.feed(ev, trace)
                watcher.feed(ev)
                yield ev
        finally:
            inner.close()
            rec.close()

    def _run(self, request: str, mode_key: str, options: Optional[dict] = None, origin: str = "local",
             cancel: Optional[threading.Event] = None) -> Iterator[dict]:
        options = options or {}
        mode = MODES.get(mode_key)
        if mode is None:
            yield {"type": "error", "message": f"Unknown mode '{mode_key}'.", "kind": "input"}
            yield {"type": "done"}
            return
        request = request.strip()
        if not request:
            yield {"type": "error", "message": "Say something first.", "kind": "input"}
            yield {"type": "done"}
            return
        cancel = cancel or threading.Event()
        db = self.core.db
        ctx = ToolContext(gate=self.core.gate, db=db, origin=origin, actor="cerberus", cancel=cancel, intent=request)
        council = self.core.council
        level = council.level(options)
        downgraded = False
        if level["key"] != "quick" and self._low_memory():
            level, downgraded = council.level({"thinking": "quick"}), True
        options = {"fast_chat": level["one_head_chat"], **options}
        stages = mode.stages_for(request, options)
        try:
            if not self.core.ollama.is_up():
                raise OllamaError("Ollama is not running. Start it (Ollama app, or `ollama serve`) and try again.")
            mapping = self.model_map(mode.key)
            missing = [r for r, m in mapping.items() if not m]
            yield {"type": "start", "mode": mode.key, "missing_models": missing, "thinking": level["key"],
                   "models": {r: self._model_for(r, mapping) for r in {s.role for s in stages}},
                   "stages": [{"key": s.key, "title": s.title, "role": s.role} for s in stages]}

            if downgraded:
                yield {"type": "council", "phase": "skipped",
                       "message": "This computer is short on free memory or running warm right now, so the heads are working one at a time without cross-checking."}

            # 0. remember the user's message + instant habit learning
            name =self.settings.get("user_name", "")
            history_cache = {s.history_turns: self._history(mode.key, s.history_turns) for s in stages}
            msg_id = db.add_message(mode.key, "user", request)
            if self.settings.get("learning_enabled", True):
                learning.observe(db, request, mode.key)
            memory = self.core.memory
            profile = learning.profile_summary(db, name=name, facts=memory.relevant_facts(request))

            # 1. context from tools
            context = ""
            for ev in self._gather_context(mode, request, ctx, options, msg_id):
                if ev["type"] == "_context":
                    context = ev["text"]
                else:
                    yield ev

            # 1b. things every head must see for this request: files the user attached, and (when asked) the capability list
            extras: list[str] = []
            if options.get("context_files") and origin == "local":
                attached, report = filectx.load(options["context_files"])
                yield {"type": "files_attached", "files": report}
                if attached:
                    extras.append("FILES THE USER ATTACHED (use them as the source for this request; quote them, do not invent "
                                  "what they do not say):\n" + attached)
            if capabilities.is_asking(request):
                extras.append(capabilities.full_text(self.core, origin))
            extra = "\n\n".join(extras)
            cap_short = capabilities.short_block(self.core, mode, bool(options.get("allow_actions")), origin)

            # 2. the Hand (optional tool-using step)
            if options.get("allow_actions") and mode.hand_skills:
                hand_tools = tools_for_skills(list(mode.hand_skills) + plugins.skills())
                yield {"type": "stage_start", "key": "hand", "title": "The Hand", "role": "editor",
                       "model": self._model_for("editor", mapping)}
                reach = ("The Computer tools (pc_*) reach files anywhere on this PC; every change needs all three heads and the "
                         "user to agree, and system, program and secret files are off limits. "
                         if self.settings.get("full_access", True) and origin == "local" else "")
                hand_system = ("You are the HAND of Cerberus: you carry out the user's practical requests with tools. "
                               f"The user's private workspace is a sandbox. {reach}{mode.persona}\n{cap_short}")
                pitfalls = mistakes.lessons_block(db, request)
                if pitfalls:
                    hand_system += "\n" + pitfalls
                if extra:
                    hand_system += "\n\n" + extra
                hand_report = ""
                for ev in tool_loop(self.core.ollama, self._model_for("editor", mapping), hand_system, request,
                                    hand_tools, ctx, max_steps=6, temperature=0.1, cancel=cancel):
                    if ev["type"] == "loop_final":
                        hand_report = ev["text"]
                    else:
                        yield ev
                yield {"type": "stage_end", "key": "hand", "text": hand_report}
                context = (context + "\n\nACTIONS CARRIED OUT BY CERBERUS' HAND:\n" + hand_report)[:CONTEXT_LIMIT + 2000]

            # 3. the heads, in order
            outputs: list[tuple[Stage, str]] = []
            task_for = {}
            for stage in stages:
                yield from self._cool_down(cancel)
                model = self._model_for(stage.role, mapping)
                yield {"type": "stage_start", "key": stage.key, "title": stage.title, "role": stage.role, "model": model}
                prior = "\n\n".join(f"[{s.title}]\n{t}" for s, t in outputs)[-PRIOR_LIMIT:]
                last = outputs[-1][1] if outputs else ""
                instruction = render(stage.instruction, request=request, context=context or "(none)",
                                     prior=prior or "(none)", last=last or "(none)", profile=profile or "(nothing yet)")
                if extra:
                    instruction += "\n\n" + extra
                system = (f"You are '{stage.title}', one of the three heads of CERBERUS, a private AI that runs on the user's own "
                          f"computer. {mode.persona}\n{stage.system}\n{cap_short}")
                if profile and "{profile}" not in stage.instruction:
                    system += f"\nWhat you know about the user (private): {profile}"
                if stage.final:
                    system += "\n" + dossier.voice_block()
                lessons = council.lessons_block(mode.key, request)
                if lessons:
                    system += "\n" + lessons
                messages = [{"role": "system", "content": system}, *history_cache.get(stage.history_turns, []),
                            {"role": "user", "content": instruction}]
                content = []
                for kind, piece in self.core.ollama.chat_stream(model, messages, stage.temperature, cancel):
                    if kind == "content":
                        content.append(piece)
                    yield {"type": "token", "key": stage.key, "kind": kind, "text": piece}
                text = strip_think("".join(content)) or "(this head had nothing to say)"
                outputs.append((stage, text))
                yield {"type": "stage_end", "key": stage.key, "text": text}
                task_for[stage.key] = (instruction, system)
                if level["stage_checks"] and stage.check and not stage.final and len(text) >= MIN_CHECK_CHARS:
                    done = yield from council.refine(mode=mode, stage=stage, task=instruction, draft=text, mapping=mapping,
                                                     cancel=cancel, rounds=level["rounds"], system=system, label="draft")
                    outputs[-1] = (stage, done["text"])

            final_text, source = outputs[-1][1], "local"

            # 4. cloud handoff
            cloud_cfg = self._cloud_settings(options)
            if cloud_cfg:
                pid, key, cmodel = cloud_cfg
                try:
                    budget = int(self.settings["cloud"].get("max_handoff_tokens", 1500)) * (4 if mode.key == "coding" else 1)
                    packet = cloudmod.build_handoff(request, mode.label, mode.cloud_brief,
                                                    [(s.title, t) for s, t in outputs], budget, self._summarizer(cancel))
                    voice = "\n" + dossier.voice_block()
                    packet["system"] += voice
                    packet["sent_tokens"] += cloudmod.estimate_tokens(voice)
                    yield {"type": "cloud_start", "provider": pid, "model": cmodel,
                           "original_tokens": packet["original_tokens"], "sent_tokens": packet["sent_tokens"]}
                    cloud_text = cloudmod.call_cloud(pid, key, cmodel, packet["system"], packet["prompt"],
                                                     max_tokens=8192 if mode.key == "coding" else 4096)
                    if cloud_text:
                        final_text, source = cloud_text, "cloud"
                    yield {"type": "cloud_end", "provider": pid, "ok": bool(cloud_text)}
                except cloudmod.CloudError as exc:
                    yield {"type": "cloud_error", "message": str(exc)}

            # 4b. the other two heads check the answer and make the writer fix what they confirm is wrong
            if level["review"] and source == "local":
                final_stage = outputs[-1][0]
                floor = MIN_CHECK_CHARS * 2 if mode.key == "conversation" else MIN_CHECK_CHARS
                if len(final_text) >= floor:
                    task, system = task_for[final_stage.key]
                    had_files = mode.key == "coding" and bool(parse_files(final_text)[0])
                    keeps_files = (lambda old, new: bool(parse_files(new)[0])) if had_files else None
                    done = yield from council.refine(mode=mode, stage=final_stage, task=task, draft=final_text, mapping=mapping,
                                                     cancel=cancel, rounds=level["rounds"], system=system, accept=keeps_files)
                    final_text = done["text"]
                    if done["unresolved"]:
                        final_text += ("\n\n> The other heads still doubt: " +
                                       "; ".join(i["problem"] for i in done["unresolved"]) + " Check this yourself before relying on it.")

            # 5. coding: files, validation, auto-repair, optional save
            if mode.key == "coding":
                final_text, files_event = yield from self._finish_coding(final_text, outputs, request, mapping, ctx, options, cancel)
                if files_event:
                    yield files_event

            db.add_message(mode.key, "assistant", final_text)
            yield {"type": "final", "text": final_text, "source": source}

            # 6. background learning
            if self.settings.get("learning_enabled", True):
                learning.extract_facts_async(db, lambda m: self.llm("editor", m, 0.0, json_mode=True), request, True)
        except Cancelled:
            yield {"type": "cancelled"}
        except Denied as exc:
            yield {"type": "error", "message": str(exc), "kind": "heat" if "too hot" in str(exc) else "denied"}
        except OllamaError as exc:
            yield {"type": "error", "message": str(exc), "kind": "ollama"}
        except Exception as exc:  # never let the stream die silently
            yield {"type": "error", "kind": "internal", "_trace": traceback.format_exc(),
                   "message": "Something went wrong on my side. It has been written to the review log so it can be fixed.",
                   "detail": f"{type(exc).__name__}: {exc}"}
        yield {"type": "done"}  # (not in `finally`: a closed generator must not yield)

    # -- helpers used by run()
    def _summarizer(self, cancel):
        def summarize(text: str, tokens: int) -> str:
            return self.llm("editor", [
                {"role": "system", "content": f"Compress the text to at most {tokens} tokens. Keep every fact, number, name, "
                                              "decision and constraint. No preamble, no commentary."},
                {"role": "user", "content": text}], 0.1, cancel=cancel)
        return summarize

    def _check_code(self, files: list[dict], ctx: ToolContext, want_tests: bool, state: dict) -> Iterator[dict]:
        """Static checks always; real test run when asked (after one approval). Returns (problems, test_failure_text)."""
        problems = validate_files(files)
        if problems or not want_tests or state.get("declined"):
            return problems, ""
        steps = verify.plan(files)
        if not steps:
            state["result"] = {"ran": False, "ok": True, "summary": "no tests to run"}
            return problems, ""
        names = ", ".join(label for label, _ in steps)
        if not state.get("approved"):
            if not ctx.gate.request("verify_code", "write", f"Run the generated code's checks in a temporary folder: {names}"):
                state["declined"] = True
                state["result"] = {"ran": False, "ok": False, "summary": "you declined to run the tests"}
                return problems, ""
            state["approved"] = True
        yield {"type": "tool_call", "name": "verify_code", "args": {"checks": names}}
        result = verify.run_checks(files)
        ctx.gate.audit("verify_code", "ran", {"checks": names, "ok": result["ok"]})
        yield {"type": "tool_result", "name": "verify_code", "result": result["summary"] + ("\n" + result["output"] if result["output"] else "")}
        state["result"] = {k: result[k] for k in ("ran", "ok", "summary")}
        return problems, ("" if result["ok"] else result["output"])

    def _finish_coding(self, text: str, outputs, request: str, mapping, ctx: ToolContext, options: dict,
                       cancel) -> Iterator[dict]:
        files, notes = parse_files(text)
        if not files:
            return text, None
        want_tests = bool(options.get("verify"))
        state: dict = {}
        problems, test_failure = yield from self._check_code(files, ctx, want_tests, state)
        for attempt in range(MAX_REPAIRS):
            if not problems and not test_failure:
                break
            title = f"The Repairer (round {attempt + 1})"
            model = self._model_for("generalist", mapping)
            yield {"type": "stage_start", "key": "repair", "title": title, "role": "generalist", "model": model}
            if test_failure:
                affected = files           # a failing test can point at any file
                found = "The generated tests were run and FAILED:\n" + test_failure
            else:
                affected = [f for f in files if any(f['path'] in p for p in problems)]
                found = "These problems were found by automatic checks:\n" + "\n".join(problems)
            msg = (found + "\n\nCurrent files:\n" + render_files(affected)[:14000] +
                   "\n\nReturn ONLY the corrected versions of the affected files, in the same strict format "
                   "(`### FILE: path` + one fenced block with the complete file). Fix the real cause; do not delete tests to make them pass.")
            fixed_text = []
            for kind, piece in self.core.ollama.chat_stream(
                    model, [{"role": "system", "content": "You fix code. Complete files only, no placeholders."},
                            {"role": "user", "content": msg}], 0.1, cancel):
                if kind == "content":
                    fixed_text.append(piece)
                yield {"type": "token", "key": "repair", "kind": kind, "text": piece}
            fixed, _ = parse_files(strip_think("".join(fixed_text)))
            yield {"type": "stage_end", "key": "repair", "text": f"{len(fixed)} file(s) rewritten"}
            by_path = {f["path"]: f for f in files}
            for f in fixed:
                by_path[f["path"]] = f
            files = list(by_path.values())
            problems, test_failure = yield from self._check_code(files, ctx, want_tests, state)
        final_text = render_files(files, notes)
        if problems:
            final_text += "\n\n> Automatic checks still report: " + "; ".join(problems)
        verdict = state.get("result")
        if verdict:
            if test_failure:
                final_text += "\n\n> Verification: the tests still FAIL after repair. Last output:\n> " + test_failure[-600:].replace("\n", "\n> ")
                problems = problems + ["tests still failing"]
            else:
                final_text += f"\n\n> Verification: {verdict['summary']}."
        project = options.get("project") or re.sub(r"\W+", "_", request.split("\n")[0])[:32].strip("_").lower() or "project"
        event = {"type": "files", "project": project, "files": files, "problems": problems, "saved": [], "verify": verdict}
        if options.get("save_files"):
            try:
                event["saved"] = save_project(files, project, ctx)
            except Denied as exc:
                event["error"] = str(exc)
            except OSError as exc:
                event["error"] = f"Could not write the files: {exc}"
        return final_text, event
