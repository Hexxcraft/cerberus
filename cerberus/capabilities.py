"""What Cerberus can and cannot do, generated from the real tool registry so it never drifts out of date.

`short_block()` goes into every head's instructions so the models know their own reach (and stop pretending to have done
things). `full_text()` answers "what can you do?" and backs GET /api/capabilities.
"""
from __future__ import annotations

import re

from . import config, plugins
from .modes import MODES
from .tools import REGISTRY, skills_catalog, tools_for_skills

ASKING = re.compile(r"\b(what|which)\b[^.?!]{0,40}\b(can|could|are able|do) (you|cerberus|i|we)\b[^.?!]{0,40}\b(do|help|use|access|reach|open|read|control)|"
                    r"\b(what are|list|show|tell me)( me)?( all)?( of)? (your|the) (capabilit|abilit|skills|tools|features|powers)|"
                    r"\b(what|which) (tools|skills|abilities|capabilities|features|powers)\b|"
                    r"\bwhat (is|are) (your|cerberus'?s?) (capabilit|abilit|limits?|skills|tools|features)|"
                    r"\b(can|could) you (access|open|read|edit|control|browse|search|see|look at)\b|"
                    r"\bhow do (i|you) (use|work)\b|\bwhat do you know how to do\b|\bwhat are you (able|capable)", re.I)

LIMITS = [
    "Nothing is done to files, the web or the computer unless 'Let Cerberus act' is ticked for that message.",
    "Every change to this computer needs all three heads to agree AND the user to approve (unless Autonomous Agent Mode is on, "
    "which skips only the user prompt).",
    "Never changed: the Windows and Program Files folders and Cerberus's own program files. Never read or changed: secrets "
    "(SSH/cloud keys, browser profiles, .env, password stores) and Cerberus's own private data.",
    "Phones and other devices can chat, but cannot use any computer, project or desktop tool.",
    "The three heads are small local models. They check each other and catch many mistakes, not all. For facts that matter, "
    "Cloud Handoff or a real source is safer; any doubt that survives checking is shown under the answer.",
    "Mouse/keyboard control is experimental. Reading the screen needs a vision model installed in Ollama.",
]


def is_asking(request: str) -> bool:
    return bool(ASKING.search(request or ""))


def _vision_ready(ollama) -> bool:
    try:
        from .tools.vision import pick_vision_model
        return bool(pick_vision_model(ollama, config.load_settings().get("vision_model", "")))
    except Exception:
        return False


def _state(core, origin: str) -> dict:
    s = config.load_settings()
    cloud = s.get("cloud") or {}
    key = (cloud.get("providers") or {}).get(cloud.get("provider", ""), {}).get("api_key") if cloud.get("enabled") else ""
    return {"remote": origin != "local", "full_access": bool(s.get("full_access", True)), "cloud": bool(cloud.get("enabled") and key),
            "vision": _vision_ready(core.ollama) if core is not None else False,
            "embed": bool(core.ollama.resolve(s.get("embed_model", ""))) if core is not None and s.get("embed_model") else False}


def short_block(core, mode, allow_actions: bool, origin: str = "local") -> str:
    """A few lines for the system prompt: this chat's real reach. Truthful about whether actions are on."""
    st = _state(core, origin)
    lines = ["WHAT CERBERUS CAN AND CANNOT DO RIGHT NOW (facts; never claim more than this):"]
    if allow_actions and mode.hand_skills:
        skills = list(mode.hand_skills) + plugins.skills()
        if st["remote"]:
            skills = [sk for sk in skills if any(not t.local_only for t in tools_for_skills([sk]))]
        lines.append("- Actions are ON for this message: a tool-using Hand can use these skills: " + ", ".join(skills) +
                     ". Changes need the heads' unanimous vote and the user's approval.")
    else:
        lines.append("- Actions are OFF for this message: you cannot read or change files, browse the web or touch the computer. "
                     "If the user wants that, tell them to tick 'Let Cerberus act' (not available from a phone for PC tools).")
    if st["remote"]:
        lines.append("- This chat comes from another device: no computer, project or desktop tools.")
    lines.append("- Cloud Handoff to a big model is " + ("ON." if st["cloud"] else "OFF (everything stays local)."))
    lines.append("- Never say you did, saved, sent or opened something unless a tool result in this conversation shows it. "
                 "If you cannot do something, say so plainly and name what would make it possible.")
    return "\n".join(lines)


def describe(core=None, origin: str = "local") -> dict:
    """Structured capabilities (GET /api/capabilities)."""
    st = _state(core, origin)
    plug = plugins.skills()
    groups = []
    for skill, names in skills_catalog().items():
        tools = [REGISTRY[n] for n in names]
        groups.append({"skill": skill, "plugin": skill in plug,
                       "tools": [t.public() for t in tools if not (st["remote"] and t.local_only)]})
    return {"rites": [{"key": m.key, "name": m.label, "about": m.subtitle, "skills": m.hand_skills} for m in MODES.values()],
            "skills": [g for g in groups if g["tools"]], "state": st, "limits": LIMITS,
            "tool_count": sum(len(g["tools"]) for g in groups)}


def full_text(core=None, origin: str = "local") -> str:
    d = describe(core, origin)
    st = d["state"]
    out = ["CERBERUS CAPABILITIES (generated from the live tool list):", "",
           "Rites: " + "; ".join(f"{r['name']} ({r['about']})" for r in d["rites"]) + "."]
    out.append("Skills (each needs 'Let Cerberus act' ticked; the rite decides which skills its Hand gets):")
    for g in d["skills"]:
        names = ", ".join(t["name"] for t in g["tools"])
        asks = [t for t in g["tools"] if t["council"]]
        note = " [every change: heads vote + your approval]" if asks else ""
        tag = " (plugin)" if g["plugin"] else ""
        out.append(f"- {g['skill']}{tag}: {g['tools'][0]['description'][:90].rstrip('. ')} ... tools: {names}{note}")
    out.append("")
    out.append("State now: full computer access " + ("on" if st["full_access"] else "off (workspace only)") +
               "; cloud handoff " + ("on" if st["cloud"] else "off") + "; screen reading " + ("ready" if st["vision"] else "not available (no vision model)") +
               "; recall by meaning " + ("ready" if st["embed"] else "keyword only") + ".")
    out.append("Limits:")
    out += [f"- {x}" for x in LIMITS]
    return "\n".join(out)
