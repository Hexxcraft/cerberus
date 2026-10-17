"""Runs one step of the user's Resume project in its own process (so Cerberus never imports that code).

    python resume_worker.py <find|tailor|build|stage> <input.json>

Reads its arguments from the JSON file, writes one line starting with @@RESULT@@ to stdout, and uses only the standard library
plus whatever the Resume project itself needs (httpx, bs4, python-docx, playwright). It never submits an application and never
uploads a file: `stage` only opens the page, fills the contact fields it recognises and then waits for the person.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

MARK = "@@RESULT@@"


def _out(payload: dict) -> None:
    print(MARK + json.dumps(payload, ensure_ascii=False), flush=True)


def _find(a: dict) -> dict:
    from job_agent.scraper import scrape_jobs
    jobs = scrape_jobs(a["query"], a["location"], feed_urls=list(a["feed_urls"]))
    keep = []
    for j in jobs[: int(a.get("limit", 25))]:
        keep.append({"title": str(j.get("title") or "")[:200], "company": str(j.get("company") or "")[:200],
                     "description": str(j.get("description") or j.get("snippet") or "")[:6000],
                     "url": str(j.get("application_url") or "")[:1000], "source": str(j.get("source_url") or "")[:1000]})
    return {"jobs": keep}


def _tailor(a: dict) -> dict:
    from job_agent import tailoring
    tailoring.OLLAMA_CHAT_URL = a["ollama_url"].rstrip("/") + "/api/chat"
    tailoring.OLLAMA_MODEL = a["model"]
    return {"bullets": tailoring.tailor_resume(a["description"], a["base"])["tailored_bullets"]}


def _ensure_template(folder: Path, fallback: Path, name: str, contact: str) -> str:
    from job_agent.documents import DEFAULT_TEMPLATE_PATH
    if Path(DEFAULT_TEMPLATE_PATH).is_file():
        return str(DEFAULT_TEMPLATE_PATH)
    if not fallback.is_file():
        from docx import Document
        fallback.parent.mkdir(parents=True, exist_ok=True)
        doc = Document()
        doc.add_heading(name or "Your Name", 0)
        if contact:
            doc.add_paragraph(contact)
        doc.add_heading("Experience", 1)
        doc.add_paragraph("{{EXPERIENCE_BULLETS}}")
        doc.save(str(fallback))
    return str(fallback)


def _build(a: dict) -> dict:
    from job_agent.documents import build_resume
    folder = Path(a["folder"])
    template = _ensure_template(folder, Path(a["fallback_template"]), a.get("name", ""), a.get("contact", ""))
    path = build_resume(a["bullets"], template, a["output"])
    return {"path": path, "template": template}


def _stage(a: dict) -> dict:
    from job_agent.submitter import stage_application
    stage_application(a["url"], a["profile"], a.get("resume_path", ""))
    return {"done": True}


ACTIONS = {"find": _find, "tailor": _tailor, "build": _build, "stage": _stage}


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] not in ACTIONS:
        _out({"ok": False, "error": "usage: resume_worker.py <find|tailor|build|stage> <input.json>"})
        return 2
    try:
        args = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        sys.path.insert(0, str(Path(args["folder"]) / "src"))
        result = ACTIONS[sys.argv[1]](args)
        _out({"ok": True, **result})
        return 0
    except Exception as exc:                                    # the caller shows one plain sentence
        _out({"ok": False, "error": f"{type(exc).__name__}: {exc}"[:500]})
        return 1


if __name__ == "__main__":
    sys.exit(main())
