"""Relevance-based memory: instead of pasting everything known about the user into every prompt, retrieve only what matters.

  * Facts: a few CORE facts (name, job, goal...) are always included; the rest are ranked by similarity to what was just said.
  * Past messages: "echoes" of earlier conversations that are semantically close to the new message.

Similarity comes from an Ollama embedding model (settings -> embed_model, default nomic-embed-text, ~274 MB).
If that model is not installed (or Ollama hiccups) everything quietly falls back to keyword overlap, so nothing breaks.
Vectors live in the `embeddings` table of the normal database; they are rebuilt automatically when a fact changes.
"""
from __future__ import annotations

import math
import re
import time
from array import array
from typing import Optional

from . import dossier, learning
from .ollama_client import OllamaError

FACT_K = 8                  # how many non-core facts to include at most
FACT_FLOOR = 0.45           # minimum cosine for a fact to count (tuned on nomic-embed-text, where unrelated text sits near 0.4)
FACT_MARGIN = 0.12          # ...and it must be within this of the best match
ECHO_THRESHOLD = 0.5        # minimum cosine similarity for an old message to count as an echo
MESSAGE_BACKLOG = 300       # how many recent user messages are searchable
RETRY_AFTER = 60            # seconds to leave embeddings alone after a failure
WORD_RE = re.compile(r"[a-z0-9']{3,}")


def _pack(vec: list[float]) -> bytes:
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return array("f", [x / norm for x in vec]).tobytes()


def _unpack(blob: bytes) -> list[float]:
    a = array("f")
    a.frombytes(blob)
    return list(a)


def cosine(a: list[float], b: list[float]) -> float:
    """Both vectors are stored normalised, so the dot product is the cosine."""
    return sum(x * y for x, y in zip(a, b))


def keyword_score(query: str, text: str) -> float:
    q = set(WORD_RE.findall(query.lower())) - learning.STOPWORDS
    if not q:
        return 0.0
    return len(q & set(WORD_RE.findall(text.lower()))) / math.sqrt(len(q))


class Memory:
    def __init__(self, core) -> None:
        self.core = core
        self._down_until = 0.0
        self.last_mode = "keywords"      # what the last retrieval used: "embeddings" or "keywords"

    # -- embedding plumbing
    def _model(self) -> Optional[str]:
        if time.time() < self._down_until:
            return None
        wanted = self.core.settings().get("embed_model") or ""
        try:
            return self.core.ollama.resolve(wanted) if wanted else None
        except Exception:
            return None

    def _embed(self, model: str, texts: list[str]) -> Optional[list[list[float]]]:
        try:
            vecs = self.core.ollama.embed(model, [t[:1500] for t in texts])
            if len(vecs) == len(texts):
                return vecs
        except (OllamaError, AttributeError):
            pass
        self._down_until = time.time() + RETRY_AFTER
        return None

    def _sync(self, kind: str, items: dict[str, str], model: str) -> bool:
        """Make sure every {ref: text} has an up-to-date vector. Returns False if embedding failed."""
        db = self.core.db
        have = {r["ref"]: r for r in db.query("SELECT ref,text,model FROM embeddings WHERE kind=?", (kind,))}
        todo = [ref for ref, text in items.items() if ref not in have or have[ref]["text"] != text or have[ref]["model"] != model]
        for i in range(0, len(todo), 32):
            batch = todo[i:i + 32]
            vecs = self._embed(model, [items[r] for r in batch])
            if vecs is None:
                return False
            for ref, vec in zip(batch, vecs):
                db.execute("INSERT INTO embeddings(kind,ref,text,model,vec) VALUES(?,?,?,?,?) "
                           "ON CONFLICT(kind,ref) DO UPDATE SET text=excluded.text, model=excluded.model, vec=excluded.vec",
                           (kind, ref, items[ref], model, _pack(vec)))
        return True

    def _rank(self, kind: str, items: dict[str, str], query: str) -> Optional[dict[str, float]]:
        """ref -> cosine similarity to the query, or None when embeddings are unavailable."""
        model = self._model()
        if not model or not items:
            return None
        if not self._sync(kind, items, model):
            return None
        qv = self._embed(model, [query])
        if qv is None:
            return None
        q = _unpack(_pack(qv[0]))
        rows = self.core.db.query("SELECT ref,vec FROM embeddings WHERE kind=? AND model=?", (kind, model))
        return {r["ref"]: cosine(q, _unpack(r["vec"])) for r in rows if r["ref"] in items}

    # -- public API
    def relevant_facts(self, query: str, k: int = FACT_K) -> list[dict]:
        facts = [f for f in learning.list_facts(self.core.db) if f["confidence"] >= 0.4]
        core_keys = dossier.core_fact_keys()               # the profile's key facts are always part of the prompt
        core = [f for f in facts if f["key"] in core_keys]
        rest = [f for f in facts if f["key"] not in core_keys]
        if len(rest) <= k:
            self.last_mode = "all"
            return facts
        items = {f["key"]: f"{f['key'].replace('_', ' ')}: {f['value']}" for f in rest}
        scores = self._rank("fact", items, query)
        scores_are_embeddings = scores is not None
        self.last_mode = "embeddings" if scores_are_embeddings else "keywords"
        if scores is None:
            scores = {key: keyword_score(query, text) for key, text in items.items()}
        learned = [f["key"] for f in rest if f.get("source") != "dossier"][:3]     # recent things it picked up itself
        ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        if scores_are_embeddings:
            top = ordered[0][1] if ordered else 0.0
            floor = max(FACT_FLOOR, top - FACT_MARGIN)      # unrelated facts all score about the same; keep only clear winners
            ranked = [key for key, s in ordered if s >= floor][:k]
        else:
            ranked = [key for key, s in ordered if s > 0][:k]
        keep = set(ranked) | set(learned)
        return core + [f for f in rest if f["key"] in keep]

    def recall_messages(self, query: str, k: int = 3, exclude_id: Optional[int] = None) -> list[str]:
        """Earlier things the user said that relate to `query`."""
        db = self.core.db
        rows = db.query("SELECT id,content FROM messages WHERE role='user' AND id IS NOT ? AND length(content)>=25 "
                        "ORDER BY id DESC LIMIT ?", (exclude_id, MESSAGE_BACKLOG))
        scores = self._rank("message", {str(r["id"]): r["content"] for r in rows}, query) if rows else None
        if scores is not None:
            best = [rid for rid, s in sorted(scores.items(), key=lambda kv: kv[1], reverse=True) if s >= ECHO_THRESHOLD][:k]
            by_id = {str(r["id"]): r["content"] for r in rows}
            return [by_id[rid][:200] for rid in best]
        words = re.findall(r"[A-Za-z']{4,}", query)
        found = db.search_messages(words, limit=k + 1)
        return [e["content"][:200] for e in found if e["content"] != query][:k]

    def prune(self) -> None:
        """Drop vectors whose fact or message no longer exists."""
        db = self.core.db
        db.execute("DELETE FROM embeddings WHERE kind='fact' AND ref NOT IN (SELECT key FROM facts)")
        db.execute("DELETE FROM embeddings WHERE kind='message' AND ref NOT IN (SELECT CAST(id AS TEXT) FROM messages)")
