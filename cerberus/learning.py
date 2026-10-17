"""Learning about YOU: Cerberus builds a private profile on your own machine.

Three sources:
  1. Habit statistics (message length, active hours, favourite modes, emoji/punctuation habits) - computed with plain code.
  2. Facts & preferences - the "editor" model (Mistral) reads what you say and extracts durable facts
     ("works night shifts", "dislikes long explanations"). You can review/edit/delete every fact in the Soul Ledger tab.
  3. Writing style - stats + a style guide made from your writing samples, used by Writing mode.

Everything is stored in data/cerberus.db. Nothing leaves your PC unless you turn on Cloud Handoff - and even then only
the compressed task text is sent, never your raw profile.
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import Counter
from typing import Callable, Optional

from .db import Database
from .tools.base import tool

STOPWORDS = set("""a about above after again all also am an and any are as at be because been before being below between both but
by can could did do does doing down during each few for from further had has have having he her here hers him his how i if in into
is it its just me more most my no nor not now of off on once only or other our out over own same she should so some such than that
the their them then there these they this those through to too under until up very was we were what when where which while who whom
why will with would you your yours im ive dont cant its thats""".split())

CUE_RE = re.compile(r"\b(i am|i'm|im|i like|i love|i hate|i prefer|i work|i live|i want|i need|my |i usually|i always|i never|"
                    r"i study|i play|i'm learning|my name)\b", re.I)

BOND_LEVELS = [(0, "Stranger"), (15, "Acquaintance"), (60, "Familiar"), (150, "Confidant"), (400, "Bound"), (1000, "Soulbound")]


# ---------------------------------------------------------------- facts
def upsert_fact(db: Database, key: str, value: str, category: str = "general", confidence: float = 0.6,
                source: str = "chat") -> None:
    key = re.sub(r"\s+", "_", key.strip().lower())[:60]
    value = value.strip()[:300]
    if not key or not value:
        return
    now = time.time()
    row = db.one("SELECT * FROM facts WHERE key=?", (key,))
    if row:
        conf = min(1.0, row["confidence"] + 0.1) if row["value"].lower() == value.lower() else confidence
        db.execute("UPDATE facts SET value=?, category=?, confidence=?, last_seen=?, seen_count=seen_count+1 WHERE key=?",
                   (value, category, conf, now, key))
    else:
        db.execute("INSERT INTO facts(key,value,category,confidence,source,first_seen,last_seen) VALUES(?,?,?,?,?,?,?)",
                   (key, value, category, confidence, source, now, now))


def list_facts(db: Database) -> list[dict]:
    return db.query("SELECT * FROM facts ORDER BY confidence DESC, last_seen DESC")


# ---------------------------------------------------------------- habit statistics
def observe(db: Database, text: str, mode: str) -> None:
    """Cheap, instant learning from one user message (no model needed)."""
    s = db.kv_get("stats", {}) or {}
    n = s.get("messages", 0)
    words = re.findall(r"[A-Za-z']+", text)
    s["messages"] = n + 1
    s["avg_words"] = round((s.get("avg_words", 0) * n + len(words)) / (n + 1), 1)
    hours = s.setdefault("hours", {})
    hour = str(time.localtime().tm_hour)
    hours[hour] = hours.get(hour, 0) + 1
    modes = s.setdefault("modes", {})
    modes[mode] = modes.get(mode, 0) + 1
    s["emoji"] = s.get("emoji", 0) + len(re.findall(r"[\U0001F300-\U0001FAFF☀-➿]", text))
    s["questions"] = s.get("questions", 0) + text.count("?")
    s["exclaims"] = s.get("exclaims", 0) + text.count("!")
    s["lowercase_starts"] = s.get("lowercase_starts", 0) + (1 if text[:1].islower() else 0)
    s["no_end_punct"] = s.get("no_end_punct", 0) + (1 if text.strip() and text.strip()[-1] not in ".!?" else 0)
    s["first_seen"] = s.get("first_seen", time.time())
    db.kv_set("stats", s)


def bond(db: Database) -> dict:
    n = (db.kv_get("stats", {}) or {}).get("messages", 0)
    level, next_at = BOND_LEVELS[0][1], None
    for i, (threshold, name) in enumerate(BOND_LEVELS):
        if n >= threshold:
            level = name
            next_at = BOND_LEVELS[i + 1][0] if i + 1 < len(BOND_LEVELS) else None
    return {"level": level, "interactions": n, "next_at": next_at}


def habit_lines(db: Database) -> list[str]:
    s = db.kv_get("stats", {}) or {}
    n = s.get("messages", 0)
    if n < 5:
        return []
    lines = []
    aw = s.get("avg_words", 0)
    lines.append("writes short, terse messages" if aw < 8 else "writes long, detailed messages" if aw > 35
                 else "writes medium-length messages")
    if s.get("lowercase_starts", 0) / n > 0.6:
        lines.append("often types in lowercase")
    if s.get("no_end_punct", 0) / n > 0.6:
        lines.append("rarely ends messages with punctuation")
    if s.get("emoji", 0) / n > 0.3:
        lines.append("uses emoji")
    if s.get("exclaims", 0) / n > 0.5:
        lines.append("expressive; uses exclamation marks")
    hours = {int(h): c for h, c in (s.get("hours") or {}).items()}
    if hours:
        night = sum(c for h, c in hours.items() if h >= 22 or h < 5)
        morning = sum(c for h, c in hours.items() if 5 <= h < 12)
        total = sum(hours.values())
        if night / total > 0.4:
            lines.append("is mostly active late at night")
        elif morning / total > 0.5:
            lines.append("is mostly active in the morning")
    modes = s.get("modes") or {}
    if modes:
        top = max(modes, key=modes.get)
        lines.append(f"uses {top} mode most")
    return lines


def profile_summary(db: Database, max_chars: int = 2600, name: str = "", facts: Optional[list[dict]] = None) -> str:
    """A compact paragraph injected into prompts so every head 'knows' the user. Pass `facts` to use a relevance-picked subset."""
    parts = []
    if name:
        parts.append(f"The user's name is {name}.")
    if facts is None:
        facts = [f for f in list_facts(db) if f["confidence"] >= 0.4][:40]
    if facts:
        parts.append("Known about the user: " + "; ".join(f"{f['key'].replace('_', ' ')}: {f['value']}" for f in facts) + ".")
    habits = habit_lines(db)
    if habits:
        parts.append("Observed habits: the user " + ", ".join(habits) + ".")
    return " ".join(parts)[:max_chars]


# ---------------------------------------------------------------- LLM-based fact extraction
EXTRACT_SYSTEM = (
    "You extract durable personal facts about a user from their message. Return ONLY JSON: "
    '{"facts":[{"key":"short_snake_case","value":"short value","category":"identity|preference|work|interest|goal|habit|dislike|skill"}]}. '
    "Only include things the user clearly stated about THEMSELVES that will still be true next week. "
    "Never invent. If there is nothing, return {\"facts\":[]}."
)


def parse_facts_json(raw: str) -> list[dict]:
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    out = []
    for f in (data.get("facts") or [])[:8]:
        if isinstance(f, dict) and f.get("key") and f.get("value"):
            out.append({"key": str(f["key"]), "value": str(f["value"]), "category": str(f.get("category", "general"))})
    return out


def extract_facts_async(db: Database, llm: Callable[[list[dict]], str], user_text: str, enabled: bool = True) -> None:
    """Fire-and-forget: ask the editor model for facts and store them. `llm(messages)->str`."""
    if not enabled or len(user_text) < 20 or not CUE_RE.search(user_text):
        return

    def work() -> None:
        try:
            raw = llm([{"role": "system", "content": EXTRACT_SYSTEM}, {"role": "user", "content": user_text[:1500]}])
            for f in parse_facts_json(raw):
                upsert_fact(db, f["key"], f["value"], f["category"], 0.6, "chat")
        except Exception:
            pass  # learning must never disturb a conversation

    threading.Thread(target=work, daemon=True, name="cerberus-learn").start()


# ---------------------------------------------------------------- writing style
def style_stats(texts: list[str]) -> dict:
    """Measurable fingerprints of how someone writes."""
    joined = "\n".join(texts)
    sentences = [s for s in re.split(r"(?<=[.!?])\s+|\n+", joined) if len(s.split()) >= 2]
    words = re.findall(r"[A-Za-z']+", joined.lower())
    if not words:
        return {}
    content = [w for w in words if w not in STOPWORDS and len(w) > 3]
    paras = [p for p in re.split(r"\n\s*\n", joined) if p.strip()]
    return {
        "words": len(words),
        "avg_sentence_words": round(len(words) / max(1, len(sentences)), 1),
        "avg_paragraph_sentences": round(len(sentences) / max(1, len(paras)), 1),
        "vocab_richness": round(len(set(words)) / len(words), 2),
        "contraction_rate": round(sum(1 for w in words if "'" in w) / len(words) * 100, 1),
        "comma_per_sentence": round(joined.count(",") / max(1, len(sentences)), 2),
        "semicolons": joined.count(";"), "dashes": joined.count("—") + joined.count(" - ") + joined.count("--"),
        "exclamations_per_100_sent": round(joined.count("!") / max(1, len(sentences)) * 100),
        "questions_per_100_sent": round(joined.count("?") / max(1, len(sentences)) * 100),
        "ellipses": joined.count("..."),
        "favourite_words": [w for w, _ in Counter(content).most_common(12)],
    }


def style_card_text(stats: dict, guide: str = "") -> str:
    if not stats:
        return ""
    lines = [
        f"Average sentence length: {stats['avg_sentence_words']} words; paragraphs ~{stats['avg_paragraph_sentences']} sentences.",
        f"Contractions: {stats['contraction_rate']} per 100 words; commas per sentence: {stats['comma_per_sentence']}.",
        f"Vocabulary richness (unique/total): {stats['vocab_richness']}.",
        f"Exclamations per 100 sentences: {stats['exclamations_per_100_sent']}; questions: {stats['questions_per_100_sent']}; "
        f"ellipses: {stats['ellipses']}; semicolons: {stats['semicolons']}; dashes: {stats['dashes']}.",
        "Words they favour: " + ", ".join(stats.get("favourite_words", [])) + ".",
    ]
    if guide:
        lines.append("Voice notes: " + guide)
    return "\n".join(lines)


STYLE_GUIDE_SYSTEM = (
    "You are a literary analyst. Read the writing samples and describe the author's VOICE in at most 120 words: tone, "
    "diction, rhythm, humour, how they open and close, quirks, what they avoid. Be concrete and specific. No preamble."
)


def rebuild_style_card(db: Database, llm: Optional[Callable[[list[dict]], str]]) -> dict:
    """Recompute the style card from writing samples (and, lightly, the user's own chat messages)."""
    samples = [s["content"] for s in db.list_samples("writing")]
    chat = [m["content"] for m in db.query("SELECT content FROM messages WHERE role='user' AND length(content)>120 "
                                           "ORDER BY id DESC LIMIT 30")]
    texts = samples + chat
    stats = style_stats(texts)
    guide = ""
    if llm and samples:
        excerpt = "\n\n---\n\n".join(s[:1500] for s in samples[:5])
        try:
            guide = llm([{"role": "system", "content": STYLE_GUIDE_SYSTEM}, {"role": "user", "content": excerpt}])
        except Exception:
            guide = ""
    card = {"stats": stats, "guide": guide, "text": style_card_text(stats, guide), "samples": len(samples),
            "updated": time.time()}
    db.kv_set("style_card", card)
    return card


def pick_excerpts(db: Database, query: str, limit: int = 2, max_chars: int = 900) -> list[str]:
    """Few-shot examples of the user's writing, preferring ones that share words with the request."""
    samples = db.list_samples("writing")
    if not samples:
        return []
    qwords = set(re.findall(r"[a-z]{4,}", query.lower()))

    def score(s: dict) -> int:
        return len(qwords & set(re.findall(r"[a-z]{4,}", s["content"].lower())))

    best = sorted(samples, key=score, reverse=True)[:limit]
    return [s["content"][:max_chars] for s in best]


# ---------------------------------------------------------------- tool: let agents remember things
@tool("remember", "Memory", "Save a durable fact about the user to Cerberus's memory.",
      {"key": "short_snake_case name", "value": "the fact"})
def remember(ctx, key: str, value: str) -> str:
    upsert_fact(ctx.db, key, value, "general", 0.7, f"agent:{ctx.actor}")
    return f"Remembered {key}."
