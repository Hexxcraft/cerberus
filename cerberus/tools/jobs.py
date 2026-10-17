"""Job-search tools using free public job-board APIs (no keys): Remotive and Arbeitnow."""
from __future__ import annotations

import re

import requests
from bs4 import BeautifulSoup

from .base import tool

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; CerberusLocalAssistant/1.0)"}


def _plain(html: str, limit: int = 220) -> str:
    text = BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text)[:limit]


def search_jobs(query: str, location: str = "", limit: int = 8) -> list[dict]:
    jobs: list[dict] = []
    try:
        r = requests.get("https://remotive.com/api/remote-jobs", params={"search": query, "limit": 30},
                         headers=HEADERS, timeout=20)
        for j in r.json().get("jobs", []):
            jobs.append({"title": j.get("title"), "company": j.get("company_name"),
                         "location": j.get("candidate_required_location") or "Remote",
                         "salary": j.get("salary") or "", "url": j.get("url"), "source": "Remotive",
                         "posted": (j.get("publication_date") or "")[:10], "snippet": _plain(j.get("description")),
                         "description": _plain(j.get("description"), 6000)})
    except Exception:
        pass
    try:
        r = requests.get("https://www.arbeitnow.com/api/job-board-api", headers=HEADERS, timeout=20)
        words = [w for w in re.split(r"\W+", query.lower()) if w]
        for j in r.json().get("data", []):
            hay = f"{j.get('title', '')} {' '.join(j.get('tags', []))}".lower()
            if words and not any(w in hay for w in words):
                continue
            jobs.append({"title": j.get("title"), "company": j.get("company_name"),
                         "location": j.get("location") or ("Remote" if j.get("remote") else ""),
                         "salary": "", "url": j.get("url"), "source": "Arbeitnow", "posted": "",
                         "snippet": _plain(j.get("description")), "description": _plain(j.get("description"), 6000)})
    except Exception:
        pass
    if location:
        loc = location.lower()
        matched = [j for j in jobs if loc in (j["location"] or "").lower() or "remote" in (j["location"] or "").lower()
                   or "worldwide" in (j["location"] or "").lower()]
        jobs = matched or jobs
    seen, unique = set(), []
    for j in jobs:
        if j["url"] and j["url"] not in seen:
            seen.add(j["url"])
            unique.append(j)
    return unique[:limit]


def format_jobs(jobs: list[dict]) -> str:
    if not jobs:
        return "No matching jobs found on Remotive/Arbeitnow. Try web_search for company career pages."
    return "\n".join(f"{i}. {j['title']} @ {j['company']} | {j['location']} | {j['salary'] or 'salary n/a'} | "
                     f"{j['source']} {j['posted']}\n   {j['url']}\n   {j['snippet']}" for i, j in enumerate(jobs, 1))


@tool("job_search", "Job Searching", "Search remote/online job listings (Remotive, Arbeitnow).",
      {"query": "job title or keywords", "location": "preferred location (optional)",
       "limit": "max results, default 8 (optional)"})
def job_search(ctx, query: str, location: str = "", limit: int = 8) -> str:
    return format_jobs(search_jobs(query, location, max(1, min(int(limit), 20))))
