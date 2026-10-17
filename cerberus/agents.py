"""Custom agents: you define a name, role, backstory, skills and a task/schedule; Cerberus runs them in the background.

Schedules are typed in plain English in the UI. Examples:
    manual                       (only runs when you press Run)
    every 30 minutes
    hourly
    daily at 9am                 daily 21:30
    weekdays at 08:30
    weekly mon,fri 17:00         every monday at 10am
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import dossier, guard, learning
from .agent_runtime import tool_loop
from .tools import ToolContext, skills_catalog, tools_for_skills

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_NAMES = {"monday": "mon", "tuesday": "tue", "wednesday": "wed", "thursday": "thu", "friday": "fri",
             "saturday": "sat", "sunday": "sun"}


def _parse_time(text: str) -> tuple[int, int]:
    m = (re.search(r"\b(\d{1,2}):(\d{2})\s*(am|pm)?\b", text, re.I) or re.search(r"\b(\d{1,2})()\s*(am|pm)\b", text, re.I)
         or re.search(r"\bat\s+(\d{1,2})()()(?!\d)", text, re.I))
    if not m:
        return 9, 0
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    suffix = (m.group(3) or "").lower()
    if suffix == "pm" and hour < 12:
        hour += 12
    if suffix == "am" and hour == 12:
        hour = 0
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f"'{m.group(0)}' is not a valid time.")
    return hour, minute


def parse_schedule(text: str) -> dict:
    """Plain-English schedule -> {'type': 'manual'|'interval'|'cron', ...}. Raises ValueError if unclear."""
    t = (text or "").strip().lower()
    if t in ("", "manual", "none", "never", "on demand", "when i say"):
        return {"type": "manual"}
    m = re.search(r"every\s+(\d+)\s*(minutes?|mins?|m|hours?|hrs?|h|days?|d)\b", t)
    if m:
        n, unit = int(m.group(1)), m.group(2)[0]
        minutes = n * (1 if unit == "m" else 60 if unit == "h" else 1440)
        if minutes < 1:
            raise ValueError("Interval must be at least 1 minute.")
        return {"type": "interval", "minutes": minutes}
    if re.search(r"\b(hourly|every hour)\b", t):
        return {"type": "interval", "minutes": 60}
    hour, minute = _parse_time(t)
    days = [d for name, d in DAY_NAMES.items() if name in t] + [d for d in DAYS if re.search(rf"\b{d}\b", t)]
    days = list(dict.fromkeys(days))
    if "weekday" in t:
        return {"type": "cron", "days": "mon-fri", "hour": hour, "minute": minute}
    if "weekend" in t:
        return {"type": "cron", "days": "sat,sun", "hour": hour, "minute": minute}
    if days:
        return {"type": "cron", "days": ",".join(days), "hour": hour, "minute": minute}
    if re.search(r"\b(daily|every day|each day|everyday)\b", t):
        return {"type": "cron", "days": None, "hour": hour, "minute": minute}
    if "weekly" in t:
        return {"type": "cron", "days": "mon", "hour": hour, "minute": minute}
    raise ValueError("Could not understand the schedule. Try: 'daily at 9am', 'every 30 minutes', 'weekdays at 08:30', 'manual'.")


def describe_schedule(spec: dict) -> str:
    if spec["type"] == "manual":
        return "manual only"
    if spec["type"] == "interval":
        m = spec["minutes"]
        return f"every {m // 60} h" if m % 60 == 0 and m >= 60 else f"every {m} min"
    days = spec.get("days") or "every day"
    return f"{days} at {spec['hour']:02d}:{spec['minute']:02d}"


def to_trigger(spec: dict):
    if spec["type"] == "interval":
        return IntervalTrigger(minutes=spec["minutes"])
    if spec["type"] == "cron":
        return CronTrigger(day_of_week=spec["days"], hour=spec["hour"], minute=spec["minute"])
    return None


SEED_AGENTS = [
    ("Market Watcher", "Financial watchdog", "A calm, numbers-first sentinel who never panics and never hypes.",
     ["Stock Watching", "Web Searching"],
     "Show the watchlist prices. Summarise notable moves in at most 5 bullet points and flag anything beyond +/-3% today.",
     "daily at 9am"),
    ("Job Scout", "Tireless job hunter", "Relentless but polite; sniffs out openings that fit the user's skills.",
     ["Job Searching", "Web Searching", "Memory"],
     "Using what you know about the user, search for fresh job listings and list the 5 best matches with links.",
     "daily at 8am"),
    ("Repo Keeper", "Guardian of the code", "Meticulous, a little fussy about uncommitted changes.",
     ["Git", "File Reading"],
     "List the workspace, then check git status of each project folder and report any uncommitted changes.",
     "weekdays at 5pm"),
]


class AgentManager:
    def __init__(self, core) -> None:
        self.core = core
        self.scheduler = BackgroundScheduler()
        self.running: set[int] = set()
        self._run_lock = threading.Lock()
        self._gpu = threading.Semaphore(1)  # run one agent at a time so they don't fight over the local models

    # -- lifecycle
    def start(self) -> None:
        if not self.core.db.query("SELECT id FROM agents LIMIT 1"):
            for name, role, story, skills, task, sched in SEED_AGENTS:
                self.create(name, role, story, skills, task, sched, active=False)
        if not self.scheduler.running:
            self.scheduler.start()
        self.sync_jobs()

    def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        deadline = time.time() + 5                  # give a run that is mid-write a moment before the database closes
        while self.running and time.time() < deadline:
            time.sleep(0.1)

    def sync_jobs(self) -> None:
        for job in self.scheduler.get_jobs():
            if job.id.startswith("agent-"):
                job.remove()
        for a in self.core.db.query("SELECT * FROM agents WHERE active=1"):
            try:
                trig = to_trigger(parse_schedule(a["schedule"]))
            except ValueError:
                continue
            if trig:
                self.scheduler.add_job(self.run, trig, args=[a["id"], "schedule"], id=f"agent-{a['id']}",
                                       replace_existing=True, coalesce=True, max_instances=1, misfire_grace_time=600)

    # -- CRUD
    def _public(self, row: dict) -> dict:
        try:
            spec = parse_schedule(row["schedule"])
            sched_text = describe_schedule(spec)
        except ValueError:
            sched_text = "invalid schedule"
        job = self.scheduler.get_job(f"agent-{row['id']}") if self.scheduler.running else None
        nxt = job.next_run_time.timestamp() if job and job.next_run_time else None
        return {**row, "skills": json.loads(row["skills"] or "[]"), "active": bool(row["active"]),
                "schedule_text": sched_text, "running": row["id"] in self.running, "next_run": nxt}

    def list(self) -> list[dict]:
        return [self._public(r) for r in self.core.db.query("SELECT * FROM agents ORDER BY active DESC, name")]

    def get(self, agent_id: int) -> Optional[dict]:
        row = self.core.db.one("SELECT * FROM agents WHERE id=?", (agent_id,))
        return self._public(row) if row else None

    def create(self, name: str, role: str, backstory: str, skills: list[str], task: str, schedule: str,
               model_role: str = "generalist", active: bool = False) -> dict:
        name = name.strip()
        if not name:
            raise ValueError("An agent needs a name.")
        parse_schedule(schedule)  # validate early
        known = set(skills_catalog())
        skills = [s for s in skills if s in known]
        if self.core.db.one("SELECT id FROM agents WHERE name=?", (name,)):
            raise ValueError(f"An agent called '{name}' already exists.")
        agent_id = self.core.db.execute(
            "INSERT INTO agents(name,role,backstory,skills,task,schedule,model_role,active,created) VALUES(?,?,?,?,?,?,?,?,?)",
            (name, role.strip(), backstory.strip(), json.dumps(skills), task.strip(), schedule.strip() or "manual",
             model_role if model_role in ("reasoner", "generalist", "editor") else "generalist", int(active), time.time()))
        self.sync_jobs()
        return self.get(agent_id)  # type: ignore[return-value]

    def update(self, agent_id: int, **fields) -> Optional[dict]:
        if "schedule" in fields:
            parse_schedule(fields["schedule"])
        allowed = {"name", "role", "backstory", "task", "schedule", "model_role", "active", "skills"}
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed or v is None:
                continue
            if k == "skills":
                v = json.dumps([s for s in v if s in skills_catalog()])
            if k == "active":
                v = int(bool(v))
            sets.append(f"{k}=?")
            vals.append(v)
        if sets:
            self.core.db.execute(f"UPDATE agents SET {', '.join(sets)} WHERE id=?", (*vals, agent_id))
            self.sync_jobs()
        return self.get(agent_id)

    def delete(self, agent_id: int) -> None:
        self.core.db.execute("DELETE FROM agents WHERE id=?", (agent_id,))
        self.core.db.execute("DELETE FROM agent_runs WHERE agent_id=?", (agent_id,))
        self.sync_jobs()

    def runs(self, agent_id: int, limit: int = 10) -> list[dict]:
        return self.core.db.query("SELECT * FROM agent_runs WHERE agent_id=? ORDER BY id DESC LIMIT ?", (agent_id, limit))

    # -- running
    def run(self, agent_id: int, trigger: str = "manual", wait: bool = False) -> bool:
        """Start the agent (in a background thread unless wait=True). Returns False if it is already running."""
        with self._run_lock:                        # check-and-add must be one step, or two triggers start the same agent twice
            if agent_id in self.running:
                return False
            agent = self.core.db.one("SELECT * FROM agents WHERE id=?", (agent_id,))
            if not agent:
                return False
            self.running.add(agent_id)
        t = threading.Thread(target=self._work, args=(agent, trigger), daemon=True, name=f"agent-{agent['name']}")
        t.start()
        if wait:
            t.join()
        return True

    def _work(self, agent: dict, trigger: str) -> None:
        status, lines = "ok", []
        try:
            if not self._gpu.acquire(timeout=1800):
                raise RuntimeError("Another agent held the models for over 30 minutes; this run was skipped.")
            try:
                orch = self.core.orchestrator
                if not self.core.ollama.is_up():
                    raise RuntimeError("Ollama is not running.")
                if trigger == "schedule":
                    ok, why = guard.ensure_safe(max_wait=60)
                    if not ok:
                        raise RuntimeError(why)
                model = orch._model_for(agent["model_role"], orch.model_map())
                skills = json.loads(agent["skills"] or "[]")
                profile = learning.profile_summary(self.core.db, name=self.core.settings().get("user_name", ""))
                system = (f"You are {agent['name']}, {agent['role']}. Backstory: {agent['backstory']}\n"
                          f"You work for one user, privately, on their computer. {('About the user: ' + profile) if profile else ''}\n"
                          f"When you write your final REPORT to them:\n{dossier.voice_block()}")
                ctx = ToolContext(gate=self.core.gate, db=self.core.db, origin="local", actor=f"agent:{agent['name']}",
                              intent=f"Background agent task: {agent['task']}")
                for ev in tool_loop(self.core.ollama, model, system, agent["task"], tools_for_skills(skills), ctx,
                                    max_steps=8, temperature=0.3):
                    if ev["type"] == "tool_call":
                        lines.append(f"> {ev['name']} {json.dumps(ev['args'], ensure_ascii=False)[:160]}")
                    elif ev["type"] == "tool_result":
                        lines.append(f"  = {ev['result'][:200].replace(chr(10), ' ')}")
                        if ev["result"].startswith(("DENIED", "ERROR")):
                            status = "partial"
                    elif ev["type"] == "loop_final":
                        lines.append("\nREPORT:\n" + ev["text"])
            finally:
                self._gpu.release()
        except Exception as exc:
            status = "error"
            lines.append(f"{type(exc).__name__}: {exc}")
        finally:
            output = "\n".join(lines)
            now = time.time()
            self.core.db.execute("INSERT INTO agent_runs(agent_id,ts,trigger,status,output) VALUES(?,?,?,?,?)",
                                 (agent["id"], now, trigger, status, output))
            self.core.db.execute("DELETE FROM agent_runs WHERE agent_id=? AND id NOT IN "
                                 "(SELECT id FROM agent_runs WHERE agent_id=? ORDER BY id DESC LIMIT 200)", (agent["id"], agent["id"]))
            self.core.db.execute("UPDATE agents SET last_run=?, last_status=?, last_output=? WHERE id=?",
                                 (now, status, output[-4000:], agent["id"]))
            self.running.discard(agent["id"])
