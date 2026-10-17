"""The Council: the three heads check each other.

Two jobs
  1. ANSWERS. After a head writes an answer, the other two read it as fact-checkers. Each replies `VERDICT: OK` or lists
     concrete problems. A problem counts only if both reviewers raise it, or the second reviewer confirms it when asked
     (so one head's false alarm is overruled by another). The writer is then cut off and has to redo the answer with the
     confirmed problems fixed, and the reviewers look again, for a bounded number of rounds. Whatever is still doubtful at the
     end is said out loud instead of hidden.
  2. CHANGES. Before the computer is changed (a file written, moved, deleted...) all three heads must approve the exact
     change. One "no" stops it, and the reason goes back to whoever proposed it so they can try again.

Nothing here can loop forever or crash a run: every model call is wrapped, and an unclear reply never counts as an objection
to an answer (but always counts as a "no" to a change - when unsure about a change, the answer is no).
"""
from __future__ import annotations

import re
import threading
import time
from typing import Callable, Iterator, Optional

from .ollama_client import Cancelled, OllamaError, strip_think

ROLES = ("reasoner", "editor", "generalist")          # fixed order = fixed pecking order when a role is missing
HEAD_NAME = {"reasoner": "the Reasoner", "editor": "the Editor", "generalist": "the Generalist"}

LEVELS = {
    "quick": {"label": "Quick", "review": False, "rounds": 0, "stage_checks": False, "one_head_chat": True,
              "blurb": "One head answers small talk, no double-checking. Fastest."},
    "careful": {"label": "Careful", "review": True, "rounds": 2, "stage_checks": False, "one_head_chat": False,
                "blurb": "All three heads work, and the two that did not write the answer check it. Recommended."},
    "meticulous": {"label": "Meticulous", "review": True, "rounds": 3, "stage_checks": True, "one_head_chat": False,
                   "blurb": "Like Careful, plus every step is checked before the next head builds on it. Slowest, most reliable."},
}
DEFAULT_LEVEL = "careful"
MAX_ISSUES = 5
LESSON_CAP = 60
TASK_LIMIT = 6000
DRAFT_LIMIT = 9000


def level_info(name: Optional[str]) -> dict:
    key = name if name in LEVELS else DEFAULT_LEVEL
    return {"key": key, **LEVELS[key]}


# ---------------------------------------------------------------- parsing
_VERDICT = re.compile(r"VERDICT\s*[:=]\s*\**\s*(OK|FIX)\b", re.I)
_ISSUE = re.compile(r"^\s*[-*\d.)]*\s*ISSUE\s*\d*\s*[:=]\s*(.+?)\s*$", re.I | re.M)
_RULING = re.compile(r"^\s*[-*]?\s*(?:ISSUE\s*)?(\d+)\s*[:.)=-]\s*\**\s*(TRUE|FALSE|YES|NO)\b", re.I | re.M)
_VOTE = re.compile(r"VOTE\s*[:=]\s*\**\s*(APPROVE|REJECT)\b", re.I)
_REASON = re.compile(r"REASON\s*[:=]\s*(.+)", re.I | re.S)


def parse_verdict(text: str) -> Optional[dict]:
    """{'ok': True} | {'ok': False, 'issues': [{'where','problem','fix'}]} | None when the reply cannot be understood."""
    m = _VERDICT.search(text or "")
    if not m:
        return None
    if m.group(1).upper() == "OK":
        return {"ok": True, "issues": []}
    issues = []
    for raw in _ISSUE.findall(text[m.end():]):
        parts = [p.strip(" \t\"'*") for p in raw.split("=>")]
        if len(parts) == 1:
            parts = ["", parts[0], ""]
        elif len(parts) == 2:
            parts = ["", parts[0], parts[1]]
        where, problem, fix = parts[0], parts[1], " ".join(parts[2:])
        if len(problem) >= 8:
            issues.append({"where": where[:200], "problem": problem[:400], "fix": fix[:400]})
    return {"ok": False, "issues": issues[:MAX_ISSUES]} if issues else None


def parse_rulings(text: str, count: int) -> list[bool]:
    ruled = {int(n): v.upper() in ("TRUE", "YES") for n, v in _RULING.findall(text or "")}
    return [ruled.get(i + 1, False) for i in range(count)]


def parse_vote(text: str) -> tuple[Optional[bool], str]:
    m = _VOTE.search(text or "")
    if not m:
        return None, ""
    reason = _REASON.search(text[m.end():])
    return m.group(1).upper() == "APPROVE", (reason.group(1).strip()[:400] if reason else "")


# ---------------------------------------------------------------- prompts
REVIEW_SYSTEM = (
    "You are {who} of CERBERUS, a private AI with three heads. Another head ({author}) wrote the DRAFT below. Your job is to be a "
    "strict, honest fact-checker. Flag ONLY real problems: invented facts, claims the material does not support, wrong numbers or "
    "arithmetic, contradictions, code that would not run, or something the user asked for that is missing or ignored. Do NOT flag "
    "style, tone, taste or length. If you are not sure something is wrong, it is OK. Reply in exactly this format and nothing else:\n"
    "VERDICT: OK\n"
    "or\n"
    "VERDICT: FIX\n"
    "ISSUE: <quote the bad part> => <what is wrong with it> => <what is correct instead>\n"
    "(one ISSUE line per problem, at most %d)" % MAX_ISSUES)

RULING_SYSTEM = (
    "You are {who} of CERBERUS. A fellow head claims the problems listed below exist in a draft. Check each claim against the "
    "draft text itself. Say TRUE only if the draft really contains that problem (an invented or wrong fact, a wrong number, a "
    "contradiction, something the task asked for that is missing). Say FALSE if the draft already handles it correctly, if the "
    "claim misreads the draft, or if it is only taste. Be honest; do not agree just to be agreeable. "
    "Reply with one line per claim, exactly like:\n1: TRUE\n2: FALSE")

VOTE_SYSTEM = (
    "You are {who} of CERBERUS, a private AI that lives on the user's computer. You are one of three heads who must ALL approve "
    "before the computer is changed. Be a careful guardian. APPROVE only if the change clearly serves what the user asked for, "
    "targets the right place, and cannot damage anything the user did not mean to touch. REJECT if the path looks guessed or "
    "invented, the content is broken, truncated, empty by accident, contradicts the request, overwrites or removes something "
    "valuable without need, or if you are unsure. Reply in exactly this format:\n"
    "VOTE: APPROVE\nREASON: <one short sentence>\n"
    "or\nVOTE: REJECT\nREASON: <one short sentence saying what is wrong and how to fix it>")


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit // 2] + "\n...[middle cut]...\n" + text[-limit // 2:]


class Council:
    def __init__(self, core) -> None:
        self.core = core
        self._lock = threading.Lock()

    # ------------------------------------------------------------ plumbing
    @property
    def orch(self):
        return self.core.orchestrator

    def level(self, options: Optional[dict] = None) -> dict:
        wanted = (options or {}).get("thinking") or self.core.settings().get("thinking")
        return level_info(wanted)

    def _ask(self, role: str, mapping: dict, system: str, user: str, cancel, num_predict: int = 4096,
             temperature: float = 0.1) -> str:
        model = self.orch._model_for(role, mapping)
        text = self.core.ollama.chat(model, [{"role": "system", "content": system}, {"role": "user", "content": user}],
                                     temperature, cancel, False, num_predict)
        return strip_think(text or "")

    def _reviewers(self, author_role: str) -> list[str]:
        return [r for r in ROLES if r != author_role]

    # ------------------------------------------------------------ stats and lessons (how it learns)
    def stats(self) -> dict:
        base = {"checked": 0, "clean": 0, "caught": 0, "fixed": 0, "unresolved": 0, "false_alarms": 0,
                "changes_approved": 0, "changes_blocked": 0, "by_head": {}}
        saved = self.core.db.kv_get("council_stats") or {}
        base.update({k: v for k, v in saved.items() if k in base})
        return base

    def _bump(self, **amounts) -> None:
        with self._lock:
            s = self.stats()
            for k, v in amounts.items():
                if k == "by_head":
                    for role, n in v.items():
                        s["by_head"][role] = s["by_head"].get(role, 0) + n
                else:
                    s[k] = s.get(k, 0) + v
            self.core.db.kv_set("council_stats", s)

    def lessons(self) -> list[dict]:
        return self.core.db.kv_get("council_lessons") or []

    def add_lessons(self, mode_key: str, issues: list[dict]) -> None:
        with self._lock:
            have = self.lessons()
            seen = {x["text"].lower() for x in have}
            for it in issues:
                text = it["problem"] if not it.get("fix") else f"{it['problem']} (correct: {it['fix']})"
                text = text.strip()[:240]
                if len(text) >= 20 and text.lower() not in seen:
                    have.append({"mode": mode_key, "text": text, "ts": time.time()})
                    seen.add(text.lower())
            self.core.db.kv_set("council_lessons", have[-LESSON_CAP:])

    def lessons_block(self, mode_key: str, request: str, k: int = 3) -> str:
        from .memory import keyword_score
        pool = [x for x in self.lessons() if x.get("mode") == mode_key]
        if not pool:
            return ""
        ranked = sorted(pool, key=lambda x: (keyword_score(request, x["text"]), x["ts"]), reverse=True)[:k]
        return "Mistakes made before in this kind of task (do not repeat them):\n" + "\n".join("- " + x["text"] for x in ranked)

    def forget_lessons(self) -> None:
        self.core.db.kv_set("council_lessons", [])

    # ------------------------------------------------------------ 1. checking answers
    def _review_once(self, author_role: str, mapping: dict, task: str, draft: str, cancel) -> tuple[dict, list[dict]]:
        """Both other heads read the draft. Returns ({role: parsed-or-None}, confirmed issues after adjudication)."""
        author = HEAD_NAME[author_role]
        verdicts: dict[str, Optional[dict]] = {}
        for role in self._reviewers(author_role):
            system = REVIEW_SYSTEM.format(who=HEAD_NAME[role], author=author)
            user = f"MATERIAL AND TASK GIVEN TO {author.upper()}:\n{_clip(task, TASK_LIMIT)}\n\nDRAFT:\n{_clip(draft, DRAFT_LIMIT)}"
            try:
                verdicts[role] = parse_verdict(self._ask(role, mapping, system, user, cancel))
            except OllamaError:
                verdicts[role] = None
        return verdicts, self._adjudicate(verdicts, mapping, task, draft, cancel)

    def _adjudicate(self, verdicts: dict, mapping: dict, task: str, draft: str, cancel) -> list[dict]:
        flagged = {r: v["issues"] for r, v in verdicts.items() if v and not v["ok"]}
        if not flagged:
            return []
        confirmed: list[dict] = []
        false_alarms = 0
        for accuser, issues in flagged.items():                         # every claim is judged by a head that did not make it
            judge = next((r for r in verdicts if r != accuser), None)
            if judge is None:                                           # only one reviewer exists (e.g. one model): take its word
                confirmed += [dict(i, by=accuser) for i in issues]
                continue
            lines = "\n".join(f"{n}: {i['problem']} (in: {i['where'] or 'the draft'})" for n, i in enumerate(issues, 1))
            user = (f"TASK:\n{_clip(task, TASK_LIMIT // 2)}\n\nDRAFT:\n{_clip(draft, DRAFT_LIMIT)}\n\nCLAIMED PROBLEMS:\n{lines}")
            try:
                reply = self._ask(judge, mapping, RULING_SYSTEM.format(who=HEAD_NAME[judge]), user, cancel, 1500)
                rulings = parse_rulings(reply, len(issues))
            except OllamaError:
                rulings = [False] * len(issues)
            for issue, real in zip(issues, rulings):
                if real:
                    confirmed.append(dict(issue, by=accuser))
                else:
                    false_alarms += 1
        if false_alarms:
            self._bump(false_alarms=false_alarms)
        return confirmed[:MAX_ISSUES + 2]

    def refine(self, *, mode, stage, task: str, draft: str, mapping: dict, cancel, rounds: int, system: str,
               accept: Optional[Callable[[str, str], bool]] = None, label: str = "answer") -> Iterator[dict]:
        """Generator. Yields council events; its return value is {'text', 'fixed', 'unresolved', 'rounds'}."""
        author_role = stage.role
        text, fixed_all, caught_by = draft, [], {}
        unresolved: list[dict] = []
        rnd = 0
        reviewers = self._reviewers(author_role)
        names = ", ".join(HEAD_NAME[r].replace("the ", "") for r in reviewers)
        for rnd in range(1, rounds + 2):
            yield from self.orch._cool_down(cancel)
            yield {"type": "council", "phase": "review", "round": rnd,
                   "message": f"{names} {'is' if len(reviewers) == 1 else 'are'} checking the {label}..."}
            try:
                verdicts, confirmed = self._review_once(author_role, mapping, task, text, cancel)
            except OllamaError as exc:
                yield {"type": "council", "phase": "skipped", "message": f"The check could not run ({exc})."}
                break
            answered = sum(1 for v in verdicts.values() if v is not None)
            self._bump(checked=1 if rnd == 1 else 0)
            if not confirmed:
                if rnd == 1:
                    self._bump(clean=1)
                msg = ("Both heads found nothing wrong." if answered else "The checking heads gave no usable verdict.") \
                    if rnd == 1 else "The correction passed the second look."
                yield {"type": "council", "phase": "clean", "round": rnd, "message": msg}
                unresolved = []
                break
            for it in confirmed:
                caught_by[it["by"]] = caught_by.get(it["by"], 0) + 1
            yield {"type": "council", "phase": "found", "round": rnd, "issues": confirmed,
                   "message": f"{len(confirmed)} problem(s) confirmed: " + "; ".join(i["problem"][:120] for i in confirmed)}
            if rnd > rounds:                                           # out of retries: say so, do not pretend
                unresolved = confirmed
                break
            title = f"{stage.title} (correction {rnd})"
            model = self.orch._model_for(author_role, mapping)
            yield {"type": "stage_start", "key": f"fix{rnd}-{stage.key}", "title": title, "role": author_role, "model": model}
            bullets = "\n".join(f"{n}. {i['problem']}" + (f" -> correct: {i['fix']}" if i.get("fix") else "")
                                for n, i in enumerate(confirmed, 1))
            user = (f"{task}\n\nYOUR PREVIOUS DRAFT:\n{_clip(text, DRAFT_LIMIT)}\n\n"
                    f"THE OTHER HEADS CHECKED YOUR DRAFT AND CONFIRMED THESE PROBLEMS:\n{bullets}\n\n"
                    "Redo the draft with exactly these problems fixed. Keep everything that was right, keep the same format and "
                    "voice, and do not add commentary about the fixes. If a correction would require a fact you do not have, "
                    "remove the claim instead of guessing. Output only the corrected draft.")
            parts: list[str] = []
            try:
                for kind, piece in self.core.ollama.chat_stream(model, [{"role": "system", "content": system},
                                                                        {"role": "user", "content": user}], 0.2, cancel):
                    if kind == "content":
                        parts.append(piece)
                    yield {"type": "token", "key": f"fix{rnd}-{stage.key}", "kind": kind, "text": piece}
            except OllamaError as exc:
                yield {"type": "stage_end", "key": f"fix{rnd}-{stage.key}", "text": f"(correction failed: {exc})"}
                unresolved = confirmed
                break
            revised = strip_think("".join(parts))
            usable = bool(revised) and not (len(text) >= 100 and len(revised) < 0.4 * len(text)) \
                and (accept is None or accept(text, revised))
            yield {"type": "stage_end", "key": f"fix{rnd}-{stage.key}",
                   "text": "(correction kept)" if usable else "(correction rejected: it lost too much of the answer)"}
            if not usable:
                unresolved = confirmed
                break
            fixed_all += confirmed
            text = revised
        if fixed_all and not unresolved:                               # only lessons that a second look verified are kept
            self.add_lessons(mode.key, fixed_all)
        self._bump(caught=len(fixed_all) + len(unresolved), fixed=0 if unresolved else len(fixed_all),
                   unresolved=len(unresolved), by_head=caught_by)
        return {"text": text, "fixed": fixed_all, "unresolved": unresolved, "rounds": rnd}

    # ------------------------------------------------------------ 2. approving changes
    def vote_change(self, tool_name: str, detail: str, goal: str, cancel=None) -> tuple[bool, str]:
        """All three heads must approve. Returns (approved, explanation). Fails closed on any doubt or error."""
        try:
            if not self.core.ollama.is_up():
                return False, "the heads cannot be consulted because Ollama is not running"
            mapping = self.orch.model_map()
            user = (f"WHAT THE USER ASKED FOR:\n{goal or '(not stated)'}\n\n"
                    f"THE PROPOSED CHANGE ({tool_name}):\n{_clip(detail, 7000)}")
            roster: list[tuple[str, str]] = []
            for role in ("editor", "generalist", "reasoner"):              # quickest first: a "no" ends the vote early
                roster.append((role, self.orch._model_for(role, mapping)))
            lines = []
            for role, _model in roster:
                approved, reason = None, ""
                for attempt in range(2):
                    try:
                        reply = self._ask(role, mapping, VOTE_SYSTEM.format(who=HEAD_NAME[role]),
                                          user if attempt == 0 else user + "\n\nReply ONLY with the two lines VOTE: and REASON:.",
                                          cancel, 3000)
                    except OllamaError as exc:
                        approved, reason = False, f"could not be reached ({exc})"
                        break
                    approved, reason = parse_vote(reply)
                    if approved is not None:
                        break
                if approved is None:
                    approved, reason = False, "gave no clear answer, so it counts as a no"
                lines.append(f"{HEAD_NAME[role]}: {'approves' if approved else 'REFUSES'}" + (f" ({reason})" if reason else ""))
                if not approved:
                    self._bump(changes_blocked=1, by_head={role: 1})
                    return False, "; ".join(lines)
            self._bump(changes_approved=1)
            return True, "; ".join(lines)
        except Cancelled:
            return False, "cancelled"
        except Exception as exc:                                           # never let a vote crash a tool call
            return False, f"the vote failed ({type(exc).__name__}: {exc})"
