"""The review log: one JSON line per chat run in DATA_DIR/logs/runs-YYYYMM.jsonl, and a report that reads it back.

Why it exists: when it is time to upgrade Cerberus, the log shows what it was actually used for, how long each head and
step took, which tools failed and why, what the heads caught, and which runs went wrong. Read the report with
`python -m cerberus.telemetry` (or GET /api/telemetry/report, or the Chronicle on the Gates tab).

Privacy: everything stays on this PC. Secrets (API keys, tokens, passwords) are scrubbed before anything is written, and
requests/answers are kept only as ~200-character previews (switch off with settings telemetry.previews = false).
Logging can never break a chat: every write is wrapped and failures are swallowed.
"""
from __future__ import annotations

import json
import os
import re
import statistics
import threading
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from . import config, vault

PREVIEW_CHARS = 200
ROLL_BYTES = 10 * 1024 * 1024          # a month's file rolls to a new part past this size
MAX_TOOLS_PER_RUN = 40
MAX_ISSUES_PER_RUN = 6

_SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{16,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bdpapi:[A-Za-z0-9+/=]{12,}"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|token|secret|password|passwd|pwd)\b(\"?\s*[:=]\s*\"?)[^\s\"',;]{4,}"),
    re.compile(r"\b[A-Fa-f0-9]{40,}\b"),
    re.compile(r"\b[A-Za-z0-9_\-]{48,}\b"),
]
_lock = threading.Lock()


def log_dir() -> Path:
    return config.DATA_DIR / "logs"


def settings() -> dict:
    base = {"enabled": True, "previews": True, "keep_months": 6}
    try:
        cfg = config.load_settings().get("telemetry") or {}
        base.update({k: cfg[k] for k in base if k in cfg})
    except Exception:
        pass
    return base


def redact(text: Any, limit: Optional[int] = None) -> str:
    """Scrub anything that looks like a secret (and any secret actually stored in settings), then shorten."""
    s = "" if text is None else str(text)
    try:
        for secret in vault.secret_values(config.load_settings()):
            s = s.replace(secret, "[secret]")
    except Exception:
        pass
    for pat in _SECRET_PATTERNS:
        if pat.groups:
            s = pat.sub(lambda m: f"{m.group(1)}{m.group(2)}[secret]", s)
        else:
            s = pat.sub("[secret]", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit] + "..." if limit and len(s) > limit else s


def _signature(text: str) -> str:
    """A short fingerprint of an error so the same problem groups together (numbers, paths and ids stripped)."""
    s = re.sub(r"[A-Za-z]:\\[^\s'\"]+|/[\w./-]{6,}", "<path>", text or "")
    s = re.sub(r"0x[0-9a-fA-F]+|\d+", "#", s)
    return re.sub(r"\s+", " ", s).strip().lower()[:90]


class Recorder:
    """Watches the event stream of one run and writes a single summary line when it ends."""

    def __init__(self, request: str, mode_key: str, options: Optional[dict] = None, origin: str = "local") -> None:
        self.cfg = settings()
        self.t0 = time.perf_counter()
        self.rec: dict = {"v": 1, "id": uuid.uuid4().hex[:10], "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                          "epoch": round(time.time(), 1), "mode": mode_key, "origin": origin,
                          "options": sorted(k for k, v in (options or {}).items() if v and k in
                                            ("allow_actions", "save_files", "cloud", "deep", "verify", "thinking", "context_files")),
                          "request_chars": len(request or ""), "stages": [], "tools": [], "council": {"reviews": 0, "found": 0, "clean": 0, "skipped": 0, "issues": []},
                          "errors": []}
        if self.cfg["previews"]:
            self.rec["request_preview"] = redact(request, PREVIEW_CHARS)
        self._stage: Optional[dict] = None
        self._stage_t0 = 0.0
        self._tool_t0: dict[str, float] = {}
        self._first_content: Optional[float] = None
        self._outcome: Optional[str] = None
        self._closed = False
        self._snapshot_hardware()

    def _snapshot_hardware(self) -> None:
        try:
            from . import guard
            st = guard.status()
            hot = st.get("hottest") or {}
            self.rec["hardware"] = {"level": st.get("level"), "temp_c": hot.get("temp_c"), "part": hot.get("label"),
                                    "memory_low": bool(st.get("memory_low"))}
        except Exception:
            pass

    def _now(self) -> float:
        return round(time.perf_counter() - self.t0, 2)

    def _end_stage(self) -> None:
        if self._stage is not None:
            self._stage["s"] = round(time.perf_counter() - self._stage_t0, 2)
            self._stage = None

    def error(self, kind: str, text: str, where: str = "", trace: str = "") -> None:
        if len(self.rec["errors"]) < 10:
            entry = {"kind": kind, "where": where, "text": redact(text, 300)}
            if trace:
                entry["trace"] = redact(trace, 1800)
            self.rec["errors"].append(entry)

    def feed(self, ev: dict, trace: str = "") -> None:
        try:
            self._feed(ev, trace)
        except Exception:
            pass

    def _feed(self, ev: dict, trace: str) -> None:
        kind = ev.get("type")
        rec = self.rec
        if kind == "start":
            rec["thinking"] = ev.get("thinking")
            rec["models"] = ev.get("models") or {}
            rec["missing_models"] = ev.get("missing_models") or []
        elif kind == "stage_start":
            self._end_stage()
            self._stage = {"key": ev.get("key"), "title": ev.get("title"), "role": ev.get("role"), "model": ev.get("model"),
                           "content_chars": 0, "think_chars": 0}
            self._stage_t0 = time.perf_counter()
            rec["stages"].append(self._stage)
        elif kind == "token" and self._stage is not None:
            n = len(ev.get("text") or "")
            if ev.get("kind") == "thinking":
                self._stage["think_chars"] += n
            else:
                self._stage["content_chars"] += n
            if "ttft_s" not in self._stage:
                self._stage["ttft_s"] = round(time.perf_counter() - self._stage_t0, 2)
            if self._first_content is None and ev.get("kind") != "thinking":
                self._first_content = self._now()
                rec["first_word_s"] = self._first_content
        elif kind == "stage_end":
            self._end_stage()
        elif kind == "tool_call":
            self._tool_t0[ev.get("name", "?")] = time.perf_counter()
        elif kind == "tool_result" and len(rec["tools"]) < MAX_TOOLS_PER_RUN:
            name = ev.get("name", "?")
            result = str(ev.get("result") or "")
            started = self._tool_t0.pop(name, None)
            entry: dict = {"name": name, "ok": not result.startswith(("ERROR", "DENIED"))}
            if started is not None:
                entry["s"] = round(time.perf_counter() - started, 2)
            if not entry["ok"]:
                entry["error"] = redact(result, 240)
            rec["tools"].append(entry)
        elif kind == "council":
            c, phase = rec["council"], ev.get("phase")
            if phase == "review":
                c["reviews"] += 1
            elif phase == "found":
                c["found"] += len(ev.get("issues") or [])
                for it in ev.get("issues") or []:
                    if len(c["issues"]) < MAX_ISSUES_PER_RUN:
                        c["issues"].append({"by": it.get("by"), "problem": redact(it.get("problem"), 160)})
            elif phase == "clean":
                c["clean"] += 1
            elif phase == "skipped":
                c["skipped"] += 1
        elif kind in ("cloud_start", "cloud_end", "cloud_error"):
            cloud = rec.setdefault("cloud", {})
            if kind == "cloud_start":
                cloud.update(provider=ev.get("provider"), model=ev.get("model"), sent_tokens=ev.get("sent_tokens"))
                cloud["_t0"] = time.perf_counter()
            elif kind == "cloud_end":
                cloud["ok"] = bool(ev.get("ok"))
                if "_t0" in cloud:
                    cloud["s"] = round(time.perf_counter() - cloud.pop("_t0"), 2)
            else:
                cloud["ok"] = False
                cloud["error"] = redact(ev.get("message"), 200)
                self.error("cloud", ev.get("message", ""), "cloud")
        elif kind == "final":
            rec["answer_chars"] = len(ev.get("text") or "")
            rec["source"] = ev.get("source")
            rec["doubts_shown"] = "still doubt" in (ev.get("text") or "")
            if self.cfg["previews"]:
                rec["answer_preview"] = redact(ev.get("text"), PREVIEW_CHARS)
            self._outcome = self._outcome or "ok"
        elif kind == "error":
            self._outcome = "error"
            self.error(ev.get("kind", "error"), ev.get("detail") or ev.get("message", ""), "run", trace)
            rec["error_message"] = redact(ev.get("message"), 300)
        elif kind == "cancelled":
            self._outcome = "cancelled"
        elif kind == "done":
            self._outcome = self._outcome or "ok"
            self._finish()

    def _finish(self) -> None:
        self._end_stage()
        rec = self.rec
        rec["total_s"] = self._now()
        rec["outcome"] = self._outcome or "abandoned"
        rec["tool_errors"] = sum(1 for t in rec["tools"] if not t["ok"])
        cloud = rec.get("cloud")
        if cloud:
            cloud.pop("_t0", None)

    def close(self) -> None:
        """Always call this (in a finally). Writes the line once; a run closed early is logged as 'abandoned'."""
        if self._closed:
            return
        self._closed = True
        try:
            if "outcome" not in self.rec:
                self._finish()
            if self.cfg["enabled"]:
                write(self.rec)
        except Exception:
            pass


# ---------------------------------------------------------------- storage
def _month_file(now: Optional[datetime] = None) -> Path:
    d = log_dir()
    stamp = (now or datetime.now()).strftime("%Y%m")
    path = d / f"runs-{stamp}.jsonl"
    part = 1
    while path.exists() and path.stat().st_size > ROLL_BYTES:
        part += 1
        path = d / f"runs-{stamp}-{part}.jsonl"
    return path


def write(record: dict) -> bool:
    if os.environ.get("CERBERUS_NO_TELEMETRY"):          # the test suite sets this so tests never write to a real log
        return False
    try:
        line = json.dumps(record, ensure_ascii=False, default=str)
        with _lock:
            log_dir().mkdir(parents=True, exist_ok=True)
            with open(_month_file(), "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            _prune()
        return True
    except Exception:
        return False


_last_prune = 0.0


def _prune() -> None:
    """Delete month files older than keep_months (checked at most once an hour)."""
    global _last_prune
    if time.time() - _last_prune < 3600:
        return
    _last_prune = time.time()
    keep = max(1, int(settings().get("keep_months") or 6))
    cutoff = (datetime.now().replace(day=1) - timedelta(days=30 * (keep - 1))).strftime("%Y%m")
    for f in log_dir().glob("runs-*.jsonl"):
        m = re.match(r"runs-(\d{6})", f.name)
        if m and m.group(1) < cutoff:
            try:
                f.unlink()
            except OSError:
                pass


def read_runs(days: Optional[int] = 30) -> list[dict]:
    since = time.time() - days * 86400 if days else 0.0
    out: list[dict] = []
    for f in sorted(log_dir().glob("runs-*.jsonl")):
        try:
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(r, dict) and r.get("epoch", 0) >= since:
                        out.append(r)
        except OSError:
            continue
    return out


# ---------------------------------------------------------------- the report
def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    v = sorted(values)
    return round(v[min(len(v) - 1, int(round(p * (len(v) - 1))))], 1)


def _lat(values: list[float]) -> dict:
    return {"n": len(values), "median_s": round(statistics.median(values), 1) if values else 0.0,
            "p90_s": _pct(values, 0.9), "max_s": round(max(values), 1) if values else 0.0}


def report(days: int = 30, runs: Optional[list[dict]] = None) -> dict:
    runs = read_runs(days) if runs is None else runs
    total = len(runs)
    out: dict = {"days": days, "runs": total, "generated": datetime.now().astimezone().isoformat(timespec="seconds"),
                 "log_folder": str(log_dir())}
    if not total:
        out["suggestions"] = ["No runs logged yet. Use Cerberus for a while, then read this again."]
        return out

    outcomes = Counter(r.get("outcome", "?") for r in runs)
    out["outcomes"] = dict(outcomes)
    out["success_rate"] = round(outcomes.get("ok", 0) / total, 3)

    done = [r for r in runs if r.get("outcome") == "ok" and r.get("total_s") is not None]
    out["latency"] = {"overall": _lat([r["total_s"] for r in done]),
                      "first_word": _lat([r["first_word_s"] for r in done if r.get("first_word_s") is not None]),
                      "by_mode": {}, "by_thinking": {}}
    for key, field in (("by_mode", "mode"), ("by_thinking", "thinking")):
        groups: dict[str, list[float]] = defaultdict(list)
        for r in done:
            groups[str(r.get(field))].append(r["total_s"])
        out["latency"][key] = {k: _lat(v) for k, v in sorted(groups.items())}

    stage_s: dict[str, list[float]] = defaultdict(list)
    speed: dict[str, list[float]] = defaultdict(list)
    for r in runs:
        for s in r.get("stages") or []:
            if s.get("s") is None:
                continue
            title = re.sub(r" \(correction \d+\)", " (correction)", str(s.get("title") or s.get("key")))
            label = f"{s.get('model') or '?'} / {title}"
            stage_s[label].append(s["s"])
            if s.get("content_chars") and s["s"] > 0:
                speed[str(s.get("model"))].append(s["content_chars"] / s["s"])
    out["stages"] = {k: _lat(v) for k, v in sorted(stage_s.items(), key=lambda kv: -sum(kv[1]))[:14]}
    out["model_speed_chars_per_s"] = {m: round(statistics.median(v), 1) for m, v in speed.items()}

    tools: dict[str, dict] = defaultdict(lambda: {"calls": 0, "errors": 0, "s": [], "last_error": ""})
    for r in runs:
        for t in r.get("tools") or []:
            e = tools[t["name"]]
            e["calls"] += 1
            if t.get("s") is not None:
                e["s"].append(t["s"])
            if not t.get("ok"):
                e["errors"] += 1
                e["last_error"] = t.get("error", "")
    out["tools"] = {n: {"calls": e["calls"], "errors": e["errors"], "median_s": round(statistics.median(e["s"]), 1) if e["s"] else None,
                        "last_error": e["last_error"]} for n, e in sorted(tools.items(), key=lambda kv: -kv[1]["calls"])}

    errs: dict[str, dict] = {}
    for r in runs:
        for e in r.get("errors") or []:
            sig = f"{e.get('kind')}: {_signature(e.get('text', ''))}"
            slot = errs.setdefault(sig, {"kind": e.get("kind"), "count": 0, "example": e.get("text"), "last": r.get("ts"),
                                         "mode": r.get("mode"), "run": r.get("id"), "trace": e.get("trace")})
            slot["count"] += 1
            slot["last"], slot["run"] = r.get("ts"), r.get("id")
    out["errors"] = sorted(errs.values(), key=lambda e: -e["count"])[:12]

    reviews = sum(r.get("council", {}).get("reviews", 0) for r in runs)
    found = sum(r.get("council", {}).get("found", 0) for r in runs)
    clean = sum(r.get("council", {}).get("clean", 0) for r in runs)
    heads: Counter = Counter()
    issues_by_mode: dict[str, list[str]] = defaultdict(list)
    for r in runs:
        for it in r.get("council", {}).get("issues") or []:
            heads[str(it.get("by"))] += 1
            if len(issues_by_mode[r.get("mode", "?")]) < 5:
                issues_by_mode[r.get("mode", "?")].append(it.get("problem", ""))
    out["council"] = {"reviews": reviews, "problems_confirmed": found, "clean_reviews": clean,
                      "who_caught_most": dict(heads), "doubts_shown_to_user": sum(1 for r in runs if r.get("doubts_shown")),
                      "examples_by_mode": dict(issues_by_mode)}

    hours = Counter()
    for r in runs:
        try:
            hours[datetime.fromisoformat(r["ts"]).hour] += 1
        except (KeyError, ValueError):
            pass
    out["habits"] = {
        "by_mode": dict(Counter(r.get("mode") for r in runs)),
        "by_thinking": dict(Counter(r.get("thinking") for r in runs)),
        "by_origin": dict(Counter(r.get("origin") for r in runs)),
        "busiest_hours": [{"hour": h, "runs": n} for h, n in hours.most_common(3)],
        "avg_request_chars": round(statistics.mean(r.get("request_chars", 0) for r in runs)),
        "let_it_act": sum(1 for r in runs if "allow_actions" in (r.get("options") or [])),
        "cloud_runs": sum(1 for r in runs if r.get("cloud")),
        "files_attached": sum(1 for r in runs if "context_files" in (r.get("options") or [])),
    }
    slowest = sorted(done, key=lambda r: -r["total_s"])[:5]
    out["slowest"] = [{"id": r["id"], "ts": r["ts"], "mode": r.get("mode"), "thinking": r.get("thinking"), "total_s": r["total_s"],
                       "request": r.get("request_preview", ""),
                       "slowest_step": max(((s.get("s") or 0, s.get("title")) for s in r.get("stages", [])), default=(0, None))[1]}
                      for r in slowest]
    out["failed_runs"] = [{"id": r["id"], "ts": r["ts"], "mode": r.get("mode"), "outcome": r.get("outcome"),
                           "message": r.get("error_message", ""), "request": r.get("request_preview", "")}
                          for r in runs if r.get("outcome") in ("error", "abandoned")][-8:]
    out["suggestions"] = _suggest(out, runs)
    return out


def _suggest(rep: dict, runs: list[dict]) -> list[str]:
    tips: list[str] = []
    total = rep["runs"]
    out = rep["outcomes"]
    if total >= 5 and out.get("abandoned", 0) / total > 0.15:
        tips.append(f"{out['abandoned']} of {total} runs were abandoned (window closed or stopped mid-answer). They are probably too slow: "
                    "look at the slowest steps below and consider a lighter Thinking level or a smaller model for that rite.")
    if out.get("error", 0):
        tips.append(f"{out['error']} run(s) ended in an error. Start with the most frequent one under 'errors'.")
    for sig in rep["errors"][:3]:
        if sig["count"] >= 3:
            tips.append(f"'{sig['kind']}' happened {sig['count']} times ({(sig['example'] or '')[:90]}). Worth a real fix.")
    for name, t in rep["tools"].items():
        if t["calls"] >= 4 and t["errors"] / t["calls"] >= 0.4:
            tips.append(f"Tool '{name}' failed {t['errors']}/{t['calls']} times (last: {t['last_error'][:90]}). Its description or "
                        "arguments are probably confusing the models, or its safety checks are too strict.")
    for label, st in rep["stages"].items():
        if st["n"] >= 3 and st["median_s"] > 90:
            tips.append(f"'{label}' takes {st['median_s']}s at the median. This step dominates the wait.")
            break
    slow_models = {m: v for m, v in rep["model_speed_chars_per_s"].items() if v < 8}
    for m, v in slow_models.items():
        tips.append(f"{m} writes only about {v} characters/second here. It may be spilling out of GPU memory; check num_ctx or the model size.")
    c = rep["council"]
    if c["reviews"] >= 5 and c["problems_confirmed"] / max(c["reviews"], 1) > 1.2:
        tips.append("The heads confirm many problems per review. Either the writer prompts are weak in some rite (see the examples) or the "
                    "reviewers are too eager; compare against the 'false alarms' count on the Gates tab.")
    if c["doubts_shown_to_user"] >= 3:
        tips.append(f"{c['doubts_shown_to_user']} answers carried an unresolved-doubt note. Those are the questions the local models cannot settle "
                    "alone: use Cloud Handoff or a source for that kind of question.")
    h = rep["habits"]
    if h["by_thinking"].get("quick", 0) > total * 0.6 and total >= 5:
        tips.append("Most runs used Quick, which skips cross-checking. If accuracy matters more than speed, Careful should be the default.")
    if not rep["tools"] and total >= 5 and h["let_it_act"] == 0:
        tips.append("'Let Cerberus act' was never ticked, so no tool was ever exercised in this period.")
    return tips or ["Nothing stands out. No action needed."]


def render_text(rep: dict) -> str:
    """The report as plain readable text (CLI and the Chronicle viewer)."""
    L: list[str] = []
    add = L.append
    add(f"CERBERUS REVIEW - last {rep['days']} days, {rep['runs']} run(s)   [{rep.get('log_folder', '')}]")
    if not rep["runs"]:
        L += ["", *rep.get("suggestions", [])]
        return "\n".join(L)
    oc = rep["outcomes"]
    add("Outcomes: " + ", ".join(f"{k} {v}" for k, v in oc.items()) + f"   (success {rep['success_rate'] * 100:.0f}%)")
    lat = rep["latency"]
    add("")
    add("HOW LONG IT TAKES (finished runs)")
    add(f"  overall   median {lat['overall']['median_s']}s, slowest 1 in 10: {lat['overall']['p90_s']}s")
    add(f"  to the first word of an answer: median {lat['first_word']['median_s']}s")
    for k, v in lat["by_mode"].items():
        add(f"  rite {k:<13} n={v['n']:<3} median {v['median_s']}s  p90 {v['p90_s']}s")
    for k, v in lat["by_thinking"].items():
        add(f"  thinking {k:<10} n={v['n']:<3} median {v['median_s']}s  p90 {v['p90_s']}s")
    add("")
    add("WHERE THE TIME GOES (model / step)")
    for k, v in rep["stages"].items():
        add(f"  {k[:58]:<58} n={v['n']:<3} median {v['median_s']}s  max {v['max_s']}s")
    if rep["model_speed_chars_per_s"]:
        add("  speed: " + ", ".join(f"{m} {v} chars/s" for m, v in rep["model_speed_chars_per_s"].items()))
    add("")
    add("TOOLS")
    for n, t in rep["tools"].items():
        add(f"  {n:<20} calls {t['calls']:<3} errors {t['errors']:<3}" + (f" last: {t['last_error'][:70]}" if t["errors"] else ""))
    if not rep["tools"]:
        add("  (none used)")
    add("")
    add("ERRORS (most frequent first)")
    for e in rep["errors"]:
        add(f"  x{e['count']} [{e['kind']}] {(e['example'] or '')[:100]}   last {e['last']} (run {e['run']})")
    if not rep["errors"]:
        add("  (none)")
    c = rep["council"]
    add("")
    add("THE HEADS CHECKING EACH OTHER")
    add(f"  {c['reviews']} review pass(es): {c['problems_confirmed']} problem(s) confirmed, {c['clean_reviews']} clean, "
        f"{c['doubts_shown_to_user']} answer(s) left with a doubt note")
    if c["who_caught_most"]:
        add("  caught by: " + ", ".join(f"{k} {v}" for k, v in c["who_caught_most"].items()))
    for mode, ex in c["examples_by_mode"].items():
        for p in ex[:2]:
            add(f"  [{mode}] {p[:110]}")
    h = rep["habits"]
    add("")
    add("HABITS")
    add("  rites: " + ", ".join(f"{k} {v}" for k, v in h["by_mode"].items()))
    add("  thinking: " + ", ".join(f"{k} {v}" for k, v in h["by_thinking"].items()) + "   origin: " + ", ".join(f"{k} {v}" for k, v in h["by_origin"].items()))
    add("  busiest hours: " + ", ".join(f"{x['hour']:02d}:00 ({x['runs']})" for x in h["busiest_hours"]) +
        f"   avg request {h['avg_request_chars']} chars")
    add(f"  let-it-act runs {h['let_it_act']}, cloud runs {h['cloud_runs']}, runs with attached files {h['files_attached']}")
    if rep["slowest"]:
        add("")
        add("SLOWEST RUNS")
        for s in rep["slowest"]:
            add(f"  {s['total_s']}s  {s['ts'][:16]}  {s['mode']}/{s['thinking']}  slowest step: {s['slowest_step']}  \"{s['request'][:50]}\"  (run {s['id']})")
    if rep["failed_runs"]:
        add("")
        add("FAILED OR ABANDONED RUNS")
        for s in rep["failed_runs"]:
            add(f"  {s['ts'][:16]}  {s['mode']}  {s['outcome']}  {s['message'][:80]}  \"{s['request'][:40]}\"  (run {s['id']})")
    add("")
    add("WHAT TO TWEAK")
    for t in rep["suggestions"]:
        add("  - " + t)
    return "\n".join(L)


def main(argv: Optional[list[str]] = None) -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Read the Cerberus review log.")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--json", action="store_true", help="print the raw report as JSON")
    ap.add_argument("--run", help="show every logged detail of one run id")
    args = ap.parse_args(argv)
    if args.run:
        for r in read_runs(None):
            if r.get("id") == args.run:
                print(json.dumps(r, indent=2, ensure_ascii=False))
                return
        print("No such run.")
        return
    rep = report(args.days)
    print(json.dumps(rep, indent=2, ensure_ascii=False) if args.json else render_text(rep))


if __name__ == "__main__":
    main()
