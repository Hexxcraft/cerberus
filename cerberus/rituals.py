"""Proactive Cerberus: scheduled rituals that speak first, instead of waiting to be asked.

Built-in rituals (all local time; the daily rhythm of the operator profile):
    06:00  Morning Forge      workout, meal prep, weight check, plus markets and anything the agents found overnight
    17:00  Mind & Soul Pass   decompress, reflect, study, then create
    20:00  Reading Hour
    21:45  Blackout warning   Sunday-Thursday nights
    22:00  Blackout           Sunday-Thursday nights: put the phone down

A ritual writes a notification into the database. The desktop app shows it as a toast and in the chat, and the phone page
(the PWA) shows it as a real notification. The three big rituals are worded by the local model when Ollama is up; if it is
not, a fixed template is used, so a ritual never silently fails. Each fires at most once a day, and a PC that was asleep at
06:00 still delivers the briefing when it wakes (within an hour).
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Optional

from apscheduler.triggers.cron import CronTrigger

from . import dossier

MISFIRE_GRACE = 3600      # seconds: deliver a late ritual if the PC wakes within this long after the time
MAX_QUOTES = 6


@dataclass(frozen=True)
class Ritual:
    id: str
    title: str
    hour: int
    minute: int
    days: Optional[str]           # APScheduler day_of_week, None = every day
    use_llm: bool
    brief: str                    # instruction for the model
    plain_name: bool = False      # address the user by their plain name instead of a title (blunt moments)


RITUALS: list[Ritual] = [
    Ritual("morning_forge", "Morning Forge", 6, 0, None, True,
           "It is 06:00, the start of their Morning Forge: a short workout, meal prep, and a weight check. "
           "Greet them, push them into the forge, and mention today's markets and agent findings if any are listed below."),
    Ritual("mind_soul", "Mind & Soul Pass", 17, 0, None, True,
           "It is 17:00. The workday is over. Call them to their Mind & Soul Pass: decompress, reflect, a little philosophy "
           "or study, and then make something of their own (their projects, their writing, their hobbies)."),
    Ritual("reading_hour", "Reading Hour", 20, 0, None, True,
           "It is 20:00: Reading Hour. Tell them to put the work down and pick up a book."),
    Ritual("blackout_warning", "Blackout in 15", 21, 45, "sun,mon,tue,wed,thu", False,
           "Fifteen minutes to blackout."),
    Ritual("blackout_begin", "Blackout", 22, 0, "sun,mon,tue,wed,thu", False,
           "Blackout has begun.", plain_name=True),
]
BY_ID = {r.id: r for r in RITUALS}


def template_text(r: Ritual, material: dict, h: str) -> str:
    """The fixed wording used when the local model is unavailable (and always for the blackout rituals)."""
    if r.id == "morning_forge":
        weekday = time.localtime().tm_wday < 5
        lines = [f"{h}, it is 06:00. Morning Forge: move your body, prep the meals, log the weight. "
                 + ("The workday starts soon; do not arrive soft." if weekday else "No workday today, which is no excuse to rot.")]
        if material.get("markets"):
            lines.append("Markets: " + "; ".join(material["markets"]))
        for rep in material.get("reports", []):
            lines.append(f"{rep['agent']} reports: {rep['text']}")
        return "\n".join(lines)
    if r.id == "mind_soul":
        return (f"{h}, it is 17:00. The workday is done. Mind & Soul Pass: decompress, reflect, a chapter of something worth reading. "
                "Then build something of your own before the night is gone.")
    if r.id == "reading_hour":
        return f"{h}, 20:00. Reading Hour. Work down, phone face-down, book up."
    if r.id == "blackout_warning":
        return f"{h}, 21:45. Fifteen minutes to blackout. Save your work, close the machine, finish your business."
    return f"{h}, 22:00. Blackout. Put the damn phone down and go to sleep."


class Rituals:
    def __init__(self, core) -> None:
        self.core = core

    # -- configuration
    def _cfg(self) -> dict:
        return self.core.settings().get("rituals") or {}

    def enabled(self, rid: str) -> bool:
        cfg = self._cfg()
        return bool(cfg.get("enabled", True)) and rid not in (cfg.get("off") or [])

    # -- scheduling (shares the agents' scheduler)
    def start(self) -> None:
        sched = self.core.agents.scheduler
        if not sched.running:
            sched.start()
        self.sync()

    def sync(self) -> None:
        sched = self.core.agents.scheduler
        for r in RITUALS:
            job_id = f"ritual-{r.id}"
            if self.enabled(r.id):
                sched.add_job(self.fire, CronTrigger(day_of_week=r.days, hour=r.hour, minute=r.minute), args=[r.id], id=job_id,
                              replace_existing=True, coalesce=True, max_instances=1, misfire_grace_time=MISFIRE_GRACE)
            elif sched.get_job(job_id):
                sched.remove_job(job_id)

    def list(self) -> list[dict]:
        sched = self.core.agents.scheduler
        out = []
        for r in RITUALS:
            job = sched.get_job(f"ritual-{r.id}") if sched.running else None
            nxt = job.next_run_time.timestamp() if job and job.next_run_time else None
            out.append({"id": r.id, "title": r.title, "time": f"{r.hour:02d}:{r.minute:02d}",
                        "days": r.days or "every day", "enabled": self.enabled(r.id), "next_run": nxt})
        return out

    # -- content
    def _material(self, r: Ritual) -> dict:
        material: dict = {}
        if r.id != "morning_forge":
            return material
        db = self.core.db
        symbols = [row["symbol"] for row in db.query("SELECT symbol FROM watchlist ORDER BY symbol LIMIT ?", (MAX_QUOTES,))]
        if symbols:
            from .tools.finance import fetch_quote
            markets = []
            for sym in symbols:
                q = fetch_quote(sym)
                if "error" not in q and q.get("price") is not None:
                    chg = q.get("day_change_pct")
                    markets.append(f"{sym} {q['price']}" + (f" ({chg:+.1f}%)" if chg is not None else ""))
            if markets:
                material["markets"] = markets
        cutoff = time.time() - 24 * 3600
        reports = []
        for a in db.query("SELECT name,last_output FROM agents WHERE last_run>? AND last_status='ok' ORDER BY last_run DESC LIMIT 3",
                          (cutoff,)):
            text = (a["last_output"] or "").split("REPORT:", 1)[-1].strip().replace("\n", " ")
            if text:
                reports.append({"agent": a["name"], "text": text[:280]})
        if reports:
            material["reports"] = reports
        return material

    def _llm_text(self, r: Ritual, material: dict) -> Optional[str]:
        if not self._cfg().get("use_model", True):
            return None
        core = self.core
        try:
            if not core.ollama.is_up():
                return None
            orch = core.orchestrator
            model = orch._model_for("generalist", orch.model_map())
            facts = ""
            if material.get("markets"):
                facts += "Markets today: " + "; ".join(material["markets"]) + "\n"
            for rep in material.get("reports", []):
                facts += f"Agent {rep['agent']} found: {rep['text']}\n"
            system = ("You are Cerberus, speaking to your user. This is a scheduled message that you send on your own, unprompted.\n"
                      + dossier.voice_block() + "\nWrite AT MOST 4 short lines of plain text: no headings, no lists of rules, no emoji.")
            user = r.brief + ("\n\nMaterial:\n" + facts if facts else "")
            text = core.ollama.chat(model, [{"role": "system", "content": system}, {"role": "user", "content": user}],
                                    0.8, None, False, 220).strip()
            return text or None
        except Exception:                 # a ritual must never fail because the model did
            return None

    def compose(self, r: Ritual) -> str:
        try:
            material = self._material(r)
        except Exception:                   # a broken data source must not cancel the greeting
            material = {}
        h = dossier.plain_name() if r.plain_name else random.choice(dossier.honorifics())
        if r.use_llm:
            text = self._llm_text(r, material)
            if text:
                return text
        return template_text(r, material, h)

    # -- delivery
    def fire(self, rid: str, force: bool = False) -> Optional[dict]:
        """Create today's notification for a ritual. Returns it, or None if it already fired today (unless force)."""
        r = BY_ID.get(rid)
        if r is None:
            return None
        db = self.core.db
        today = time.strftime("%Y-%m-%d")
        last = db.kv_get("ritual_last", {}) or {}
        if not force and last.get(rid) == today:
            return None
        body = self.compose(r)
        nid = db.add_notification(r.id, r.title, body)
        last = db.kv_get("ritual_last", {}) or {}               # marked only once the message exists, so a failure retries
        last[rid] = today
        db.kv_set("ritual_last", last)
        self.core.gate.audit("ritual", "fired", {"ritual": rid, "forced": force})
        return {"id": nid, "kind": r.id, "title": r.title, "body": body}
