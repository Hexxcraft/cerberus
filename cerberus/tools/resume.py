"""Resume Studio: the user's Resume project (job feed -> tailored resume -> application form staged for review), run from Hunt.

The Resume code is never imported into Cerberus. Each step runs in a separate Python process (cerberus/resume_worker.py) that
puts the project's src folder on its path, inside the same memory and process limits as other programs Cerberus runs.

Safety rules that are part of the design, not settings:
  - Nothing is ever submitted and no file is uploaded: staging opens the page, fills the contact fields the user saved, and waits.
  - A job to stage or tailor is picked by its NUMBER in the list stored by the last search. The model cannot supply a URL.
  - Staging needs the user's approval and a yes from all three heads, and refuses private/internal addresses.
  - Every tool here is local-only: a phone or other device cannot start any of it.
  - The base resume is the only source of facts; tailored bullets are checked for names and numbers that are not in it.
"""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from .. import config, guard
from ..safety import Denied
from . import jobs as jobboards
from .base import ToolContext, tool
from .web import check_public_url

WORKER = Path(__file__).resolve().parent.parent / "resume_worker.py"
MARK = "@@RESULT@@"
JOBS_KEY = "resume_jobs"
BELOW_NORMAL, NO_WINDOW = 0x00004000, 0x08000000
GUESSES = (("OneDrive", "Documents", "Projects", "Resume"), ("Documents", "Projects", "Resume"))


# ---------------------------------------------------------------- settings and folders
def settings() -> dict:
    s = config.load_settings().get("resume") or {}
    app = s.get("applicant") or {}
    return {"path": str(s.get("path") or ""), "feed_urls": [u for u in (s.get("feed_urls") or []) if isinstance(u, str)],
            "base_resume": str(s.get("base_resume") or ""),
            "applicant": {k: str(app.get(k) or "") for k in ("name", "email", "phone", "linkedin")}}


def _looks_right(p: Path) -> bool:
    return (p / "src" / "job_agent" / "scraper.py").is_file()


def folder() -> Path:
    raw = settings()["path"].strip()
    if raw:
        p = Path(raw).expanduser()
        if not _looks_right(p):
            raise Denied(f"'{p}' does not look like the Resume project (src/job_agent/scraper.py is missing). "
                         "Set the right folder in Gates > Resume Studio.")
        return p
    for guess in GUESSES:
        p = Path.home().joinpath(*guess)
        if _looks_right(p):
            return p
    raise Denied("The Resume project folder is not set. Choose it in Gates > Resume Studio.")


def python_exe() -> str:
    exe = sys.executable
    if getattr(sys, "frozen", False) or not exe:
        exe = shutil.which("python") or ""
    if not exe:
        raise Denied("No Python interpreter was found to run the Resume project.")
    return exe


_MODULE_PROBE: dict[str, bool] = {}


def has_module(name: str) -> bool:
    """Is `name` importable by the interpreter that runs the worker (the bundled .exe has its own, different, packages)?"""
    if not getattr(sys, "frozen", False):
        return importlib.util.find_spec(name) is not None
    if name not in _MODULE_PROBE:
        try:
            r = subprocess.run([python_exe(), "-c", f"import {name}"], capture_output=True, timeout=30, creationflags=NO_WINDOW)
            _MODULE_PROBE[name] = r.returncode == 0
        except (Denied, OSError, subprocess.SubprocessError):
            _MODULE_PROBE[name] = False
    return _MODULE_PROBE[name]


def _data_dir() -> Path:
    d = config.DATA_DIR / "resume"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cool() -> None:
    st = guard.status()
    if st.get("blocked"):
        hot = st.get("hottest") or {}
        raise Denied(f"The {hot.get('label', 'computer')} is too hot ({hot.get('temp_c', '?')} C), so nothing heavy is started. "
                     "Try again in a few minutes.")


def _call(action: str, args: dict, timeout: int) -> dict:
    """Run one worker step inside the guard's limits and return its result dict (never raises for a failed step)."""
    work = folder()
    args = {**args, "folder": str(work)}
    job = _data_dir() / f"{action}-{int(time.time() * 1000)}.json"
    job.write_text(json.dumps(args), encoding="utf-8")
    try:
        res = guard.child_run([python_exe(), str(WORKER), action, str(job)], str(work), timeout)
    finally:
        job.unlink(missing_ok=True)
    if res["timed_out"]:
        return {"ok": False, "error": f"It was still running after {timeout} seconds and was stopped."}
    for ln in reversed(res["out"].splitlines()):
        if ln.startswith(MARK):
            try:
                return json.loads(ln[len(MARK):])
            except ValueError:
                break
    tail = (res["err"] or res["out"]).strip().splitlines()[-1:] or [f"exit code {res['code']}"]
    return {"ok": False, "error": tail[0][:300]}


# ---------------------------------------------------------------- stored job list, base resume, claim check
def stored(db) -> dict:
    return db.kv_get(JOBS_KEY) or {}


def _job(db, number) -> dict:
    data = stored(db)
    try:
        n = int(number)
    except (TypeError, ValueError):
        raise Denied("Give the job's number from the list, for example 3.")
    jobs = data.get("jobs") or []
    if not jobs:
        raise Denied("There is no job list yet. Run resume_find_jobs first.")
    if not 1 <= n <= len(jobs):
        raise Denied(f"There is no job number {n}; the list has {len(jobs)} jobs.")
    return {**jobs[n - 1], "n": n}


def base_resume_text(db) -> str:
    """The facts tailoring may use: the saved base resume file, else the newest resume sample the user gave Cerberus."""
    raw = settings()["base_resume"].strip()
    if raw:
        from .. import filectx
        text, _total, _ = filectx.read_text(filectx.check(raw), 12_000)
        if text.strip():
            return text
        raise Denied("The base resume file is empty.")
    for s in (db.list_samples("resume") or [])[:1]:
        if (s.get("content") or "").strip():
            return s["content"][:12_000]
    raise Denied("There is no base resume. Choose a .docx or text file in Gates > Resume Studio, or teach Cerberus one resume sample.")


def unsupported(bullets: list[str], base: str) -> list[str]:
    """Names and numbers in tailored bullets that do not appear in the base resume (a cheap guard against invented claims)."""
    low = base.lower()
    out: list[str] = []
    for b in bullets:
        body = re.sub(r"^[\s\-*•]+", "", b)
        rest = body.split(None, 1)[1] if " " in body else ""
        for tok in re.findall(r"\d[\d,.]*%?|\b[A-Z][A-Za-z0-9+#.\-]{2,}\b", rest):
            t = tok.strip(".,-")
            if t and t.lower() not in low and t not in out:
                out.append(t)
    return out[:12]


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name or "").strip("_")[:40] or "Job"


def _numbered(jobs: list[dict]) -> str:
    return "\n".join(f"{i}. {j['title']} @ {j['company'] or '?'}" + (f"  ({j['url']})" if j.get("url") else "  (no application link)")
                     for i, j in enumerate(jobs, 1))


# ---------------------------------------------------------------- tools
@tool("resume_status", "Resume Studio", "Show whether Resume Studio (job list -> tailored resume -> staged application) is ready, "
      "and what the last job search found.", risk="safe", local_only=True)
def resume_status(ctx: ToolContext) -> str:
    s = settings()
    lines = []
    try:
        lines.append(f"Resume project: {folder()}")
    except Denied as exc:
        return f"Not ready: {exc}"
    work = folder()
    lines.append("Resume template: " + ("found" if (work / "data" / "resume" / "template.docx").is_file()
                                         else "none yet (a plain one is made automatically the first time)"))
    lines.append("Word documents (python-docx): " + ("installed" if has_module("docx") else "NOT installed (tailored resumes will be saved as .txt)"))
    lines.append("Form staging (playwright): " + ("installed" if has_module("playwright") else "NOT installed"))
    lines.append(f"Job feeds set: {len(s['feed_urls'])}" + ("" if s["feed_urls"] else " (none; the built-in Remotive/Arbeitnow boards are used)"))
    try:
        base_resume_text(ctx.db)
        lines.append("Base resume: ready")
    except Denied as exc:
        lines.append(f"Base resume: missing ({exc})")
    miss = [k for k, v in s["applicant"].items() if not v]
    lines.append("Contact details to fill in forms: " + ("all set" if not miss else "missing " + ", ".join(miss)))
    data = stored(ctx.db)
    if data.get("jobs"):
        lines.append(f"Last search: '{data.get('query')}' in '{data.get('location') or 'anywhere'}' found {len(data['jobs'])} jobs.")
    return "\n".join(lines)


@tool("resume_find_jobs", "Resume Studio", "Search for jobs and keep a numbered list the other Resume Studio steps use. "
      "Uses the user's own job feeds if set, else the built-in Remotive/Arbeitnow boards.",
      {"query": "job title or keywords", "location": "place or 'Remote' (optional)"}, risk="safe", local_only=True)
def resume_find_jobs(ctx: ToolContext, query: str, location: str = "") -> str:
    query = (query or "").strip()
    if not query:
        return "Say what kind of job to look for."
    s = settings()
    found: list[dict] = []
    if s["feed_urls"]:
        _cool()
        res = _call("find", {"query": query, "location": location or "Remote", "feed_urls": s["feed_urls"]}, 180)
        if not res.get("ok"):
            return f"The job search failed: {res.get('error')}"
        found = res["jobs"]
    else:
        for j in jobboards.search_jobs(query, location, 20):
            found.append({"title": j.get("title") or "", "company": j.get("company") or "",
                          "description": j.get("description") or j.get("snippet") or "", "url": j.get("url") or "",
                          "source": j.get("source") or ""})
    found = [j for j in found if j.get("title") and j.get("description")]
    ctx.db.kv_set(JOBS_KEY, {"query": query, "location": location, "ts": time.time(), "jobs": found})
    if not found:
        return "No usable jobs were found (a listing needs a title and a description). Try different words."
    return f"Found {len(found)} jobs; they are numbered for the next steps (resume_tailor, resume_stage):\n{_numbered(found)}"


def _pre_tailor(ctx: ToolContext, job_number=None, **_) -> None:
    _job(ctx.db, job_number)
    base_resume_text(ctx.db)
    _cool()


@tool("resume_tailor", "Resume Studio", "Rewrite the user's own base resume bullets to fit one job from the list (nothing new is invented) "
      "and save it as a Word file in the Resume project's output folder.",
      {"job_number": "the job's number from resume_find_jobs"}, risk="write", local_only=True, preflight=_pre_tailor)
def resume_tailor(ctx: ToolContext, job_number: int) -> str:
    job, base = _job(ctx.db, job_number), base_resume_text(ctx.db)
    cfg = config.load_settings()
    res = _call("tailor", {"description": job["description"], "base": base, "ollama_url": cfg.get("ollama_url", "http://localhost:11434"),
                           "model": (cfg.get("models") or {}).get("generalist", "llama3")}, 300)
    if not res.get("ok"):
        return f"Tailoring failed: {res.get('error')}"
    bullets = [b.strip() for b in res["bullets"] if b.strip()]
    if not bullets:
        return "The model returned no bullets. Nothing was saved."
    flagged = unsupported(bullets, base)
    app = settings()["applicant"]
    name = f"{job['n']:03d}_{_safe(job['company'])}_Resume.docx"
    built = _call("build", {"bullets": bullets, "output": f"output/{name}", "name": app["name"],
                            "fallback_template": str(_data_dir() / "template.docx"),
                            "contact": " | ".join(v for v in (app["email"], app["phone"], app["linkedin"]) if v)}, 60)
    if built.get("ok"):
        where = built["path"]
    else:
        txt = folder() / "output" / (Path(name).stem + ".txt")
        guard.assert_space(txt, 20_000)
        txt.parent.mkdir(parents=True, exist_ok=True)
        txt.write_text("\n".join("- " + b for b in bullets) + "\n", encoding="utf-8")
        where = f"{txt} (a Word file could not be made: {built.get('error')})"
    out = [f"Tailored for {job['title']} @ {job['company']}. Saved: {where}", "", *("- " + b for b in bullets)]
    if flagged:
        out += ["", "CHECK BEFORE USING: these names/numbers are not in the base resume, so they may be invented: " + ", ".join(flagged)]
    return "\n".join(out)


def _pre_stage(ctx: ToolContext, job_number=None, **_) -> None:
    job = _job(ctx.db, job_number)
    url = job.get("url") or ""
    if urlparse(url).scheme not in ("http", "https"):
        raise Denied(f"Job {job['n']} has no web application link, so there is nothing to open.")
    try:
        check_public_url(url)
    except ValueError as exc:
        raise Denied(str(exc))
    if not has_module("playwright"):
        raise Denied("The browser helper (playwright) is not installed.")
    folder()
    _cool()


def _prev_stage(ctx: ToolContext, job_number=None, **_) -> str:
    job = _job(ctx.db, job_number)
    return (f"Open a visible browser at the application page for '{job['title']}' at {job['company']} ({job['url']}), fill in only "
            "your saved name, email, phone and LinkedIn, then stop. It will not upload your resume or submit anything.")


@tool("resume_stage", "Resume Studio", "Open the application page for one job from the list in a visible browser and fill in the user's "
      "contact details, then stop so the USER reviews, uploads the resume and submits. Never submits.",
      {"job_number": "the job's number from resume_find_jobs"}, risk="control", local_only=True, council=True,
      preflight=_pre_stage, preview=_prev_stage)
def resume_stage(ctx: ToolContext, job_number: int) -> str:
    job = _job(ctx.db, job_number)
    work = folder()
    app = settings()["applicant"]
    resume = next(iter(sorted((work / "output").glob(f"{job['n']:03d}_*"))), None)
    spec = _data_dir() / f"stage-{int(time.time() * 1000)}.json"
    spec.write_text(json.dumps({"folder": str(work), "url": job["url"], "profile": app,
                                "resume_path": str(resume or "")}), encoding="utf-8")
    log = _data_dir() / "stage.log"
    ctx.gate.audit("resume_stage", "started", {"job": job["n"], "company": job["company"], "url": job["url"]})
    with open(log, "ab") as fh:
        subprocess.Popen([python_exe(), str(WORKER), "stage", str(spec)], cwd=str(work), stdin=subprocess.DEVNULL, stdout=fh, stderr=fh,
                         creationflags=(BELOW_NORMAL | NO_WINDOW) if sys.platform == "win32" else 0)
    miss = [k for k, v in app.items() if not v]
    note = f" These contact fields are not saved, so they stay blank: {', '.join(miss)}." if miss else ""
    where = f" Your tailored resume is {resume}." if resume else " No tailored resume exists for this job yet (resume_tailor makes one)."
    return (f"A browser window is opening on the application for {job['title']} at {job['company']}.{note} Review the page, upload your "
            f"resume yourself, and press submit yourself; nothing has been sent.{where}")
