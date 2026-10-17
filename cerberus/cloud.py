"""Cloud handoff: the local trio finishes its work, we squeeze it into ONE small prompt, and a frontier model polishes it.

Why? Cloud tokens cost money. The local heads do the heavy thinking for free; the cloud model only sees a dense brief.

Steps in `build_handoff`:
  1. Clean every stage's output (remove markdown fluff, filler phrases, duplicate lines). Code blocks are kept intact.
  2. Spend a token budget wisely: the request and the final local draft get the most room, earlier notes get less.
  3. If something is still too long, an optional local model summarises it (never touching code blocks).
Then `call_cloud` sends it to OpenAI, Anthropic, Gemini or any OpenAI-compatible service.
"""
from __future__ import annotations

import re
from typing import Callable, Optional

import requests

PROVIDERS: dict[str, dict] = {
    "openai": {"label": "OpenAI", "kind": "openai", "base_url": "https://api.openai.com/v1", "model": "gpt-4o"},
    "anthropic": {"label": "Anthropic Claude", "kind": "anthropic", "base_url": "https://api.anthropic.com/v1",
                  "model": "claude-sonnet-5"},
    "gemini": {"label": "Google Gemini", "kind": "gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta",
               "model": "gemini-2.5-flash"},
    "xai": {"label": "xAI Grok", "kind": "openai", "base_url": "https://api.x.ai/v1", "model": "grok-3"},
    "deepseek": {"label": "DeepSeek API", "kind": "openai", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    "mistral": {"label": "Mistral API", "kind": "openai", "base_url": "https://api.mistral.ai/v1", "model": "mistral-large-latest"},
    "openrouter": {"label": "OpenRouter", "kind": "openai", "base_url": "https://openrouter.ai/api/v1", "model": "openai/gpt-4o"},
}


class CloudError(RuntimeError):
    pass


def estimate_tokens(text: str) -> int:
    """Rough token count (~4 characters per token for English). Good enough for budgeting."""
    return max(1, (len(text) + 3) // 4)


# ---------------------------------------------------------------- compression
_FENCE = re.compile(r"(```.*?```)", re.DOTALL)
_FILLER = re.compile(
    r"^(sure|certainly|of course|absolutely|great question|here(?:'s| is) (?:the|a|your)[^\n:]{0,60}:?|i hope this helps[^\n]*|"
    r"let me know if[^\n]*|feel free to[^\n]*|as an ai[^\n]*|in conclusion,?|overall,?)\s*[.!:]?\s*$",
    re.IGNORECASE)


def compress_text(text: str) -> str:
    """Lossless-ish cleanup: drop filler, markdown decoration and repeats; leave code alone."""
    out: list[str] = []
    for part in _FENCE.split(text):
        if part.startswith("```"):
            code = "\n".join(line.rstrip() for line in part.splitlines())
            out.append(re.sub(r"\n{3,}", "\n\n", code))
            continue
        lines, seen = [], set()
        for line in part.splitlines():
            line = re.sub(r"(\*\*|__)(.+?)\1", r"\2", line)           # bold
            line = re.sub(r"^#{1,6}\s*", "", line)                      # heading marks
            line = re.sub(r"^\s*[-*•]\s+", "- ", line)                  # bullet styles
            line = re.sub(r"[ \t]+", " ", line).strip()
            if not line:
                if lines and lines[-1] != "":
                    lines.append("")
                continue
            if _FILLER.match(line):
                continue
            key = line.lower()
            if key in seen and len(key) > 20:
                continue
            seen.add(key)
            lines.append(line)
        out.append("\n".join(lines))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def trim_to_tokens(text: str, budget: int, summarize: Optional[Callable[[str, int], str]] = None) -> str:
    """Make `text` fit in `budget` tokens. Prefers an LLM summary of prose; falls back to keeping head and tail."""
    if estimate_tokens(text) <= budget:
        return text
    parts = _FENCE.split(text)
    code_tokens = sum(estimate_tokens(p) for p in parts if p.startswith("```"))
    if summarize and code_tokens < budget * 0.8:  # summarise only the prose, keep code verbatim
        prose_budget = max(80, budget - code_tokens)
        rebuilt = []
        for p in parts:
            if p.startswith("```") or estimate_tokens(p) <= 60:
                rebuilt.append(p)
            else:
                try:
                    rebuilt.append(summarize(p, max(60, prose_budget // max(1, sum(1 for q in parts if not q.startswith('```'))))))
                except Exception:
                    rebuilt.append(p)
        text = "".join(rebuilt)
        if estimate_tokens(text) <= budget:
            return text
    chars = budget * 4
    head, tail = int(chars * 0.7), int(chars * 0.25)
    return text[:head].rstrip() + "\n[...trimmed...]\n" + text[-tail:].lstrip()


def build_handoff(request: str, mode_label: str, mode_brief: str, stages: list[tuple[str, str]], budget: int,
                  summarize: Optional[Callable[[str, int], str]] = None) -> dict:
    """Bundle local work into one prompt. `stages` = [(title, output), ...]; the LAST one is the local draft."""
    original = estimate_tokens(request) + sum(estimate_tokens(t) for _, t in stages)
    req = trim_to_tokens(compress_text(request), max(150, budget // 4), summarize)
    notes, draft = stages[:-1], stages[-1] if stages else ("", "")
    draft_budget = int(budget * 0.55) if notes else int(budget * 0.8)
    note_budget = max(0, budget - draft_budget - estimate_tokens(req))
    sections = [f"TASK ({mode_label}):\n{req}"]
    for title, txt in notes:
        sections.append(f"{title.upper()} NOTES:\n" + trim_to_tokens(compress_text(txt), max(60, note_budget // max(1, len(notes))), summarize))
    if draft[1]:
        sections.append("LOCAL DRAFT:\n" + trim_to_tokens(compress_text(draft[1]), draft_budget, summarize))
    sections.append("INSTRUCTION: Improve and finalize the LOCAL DRAFT for the TASK using the notes. Fix errors, fill gaps, "
                    "keep what is already good. Reply with the final deliverable only.")
    prompt = "\n\n".join(sections)
    system = (f"You are the final-polish stage of a local-first AI pipeline. Mode: {mode_label}. {mode_brief} "
              "Local models already did the thinking; be accurate, concise and complete.")
    return {"system": system, "prompt": prompt, "original_tokens": original,
            "sent_tokens": estimate_tokens(prompt) + estimate_tokens(system)}


# ---------------------------------------------------------------- provider calls
def _post(url: str, headers: dict, body: dict, timeout: int = 180) -> dict:
    try:
        r = requests.post(url, headers=headers, json=body, timeout=timeout)
    except requests.RequestException as exc:
        raise CloudError(f"Could not reach the cloud service: {exc}") from exc
    if r.status_code >= 400:
        try:
            msg = r.json()
            msg = msg.get("error", msg)
            msg = msg.get("message", msg) if isinstance(msg, dict) else msg
        except ValueError:
            msg = r.text[:300]
        hint = " (check your API key)" if r.status_code in (401, 403) else ""
        raise CloudError(f"Cloud error {r.status_code}: {msg}{hint}")
    return r.json()


def call_cloud(provider: str, api_key: str, model: str, system: str, prompt: str, max_tokens: int = 4096) -> str:
    spec = PROVIDERS.get(provider)
    if not spec:
        raise CloudError(f"Unknown provider '{provider}'.")
    if not api_key:
        raise CloudError(f"No API key saved for {spec['label']}. Add one in the Grimoire tab.")
    model = model or spec["model"]
    base = spec["base_url"].rstrip("/")
    kind = spec["kind"]
    if kind == "openai":
        body = {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
        body["max_completion_tokens" if provider == "openai" else "max_tokens"] = max_tokens
        data = _post(f"{base}/chat/completions", {"Authorization": f"Bearer {api_key}"}, body)
        return (data["choices"][0]["message"].get("content") or "").strip()
    if kind == "anthropic":
        data = _post(f"{base}/messages", {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
                     {"model": model, "max_tokens": max_tokens, "system": system,
                      "messages": [{"role": "user", "content": prompt}]})
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
    if kind == "gemini":
        data = _post(f"{base}/models/{model}:generateContent", {"x-goog-api-key": api_key},
                     {"systemInstruction": {"parts": [{"text": system}]},
                      "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                      "generationConfig": {"maxOutputTokens": max_tokens}})
        try:
            return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"]).strip()
        except (KeyError, IndexError) as exc:
            raise CloudError(f"Gemini returned no text: {str(data)[:200]}") from exc
    raise CloudError(f"Unsupported provider kind '{kind}'.")
