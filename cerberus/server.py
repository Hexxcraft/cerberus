"""The Cerberus HTTP API (FastAPI). The desktop app talks to this, and so can your phone/laptop on the home network.

Run it by itself with:   python -m cerberus.server            (this PC only)
                         python -m cerberus.server --lan      (whole home network; see scripts/start_lan_server.py)

Security model
  * Requests from this PC (loopback) are trusted, but we still reject browser tricks (foreign Origin / DNS rebinding).
  * Requests from other devices need the API token (data/settings.json -> api_token) and are tagged origin="remote":
    desktop-control tools are refused for them, and settings/agent/autonomy changes are blocked.
"""
from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import re
import socket
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, backup, capabilities, config, filectx, guard, hardware, learning, mistakes, plugins, telemetry
from .agents import parse_schedule
from .cloud import PROVIDERS
from .council import LEVELS
from .config import ASSETS_DIR, DEFAULT_PORT, get_workspace, load_settings
from .core import Cerberus
from .modes import MODE_ORDER, MODES
from .ollama_client import detect_ollama, start_ollama_server
from .rituals import BY_ID as RITUAL_IDS
from .safety import Denied, SafetyGate
from .tools import REGISTRY, computer, run_tool, skills_catalog
from .tools.finance import budget_summary_text, fetch_quote

WEB_DIR = Path(__file__).parent / "web"
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
CHAT_OPTION_KEYS = {"allow_actions", "submode", "save_files", "project", "cloud", "deep", "verify", "thinking", "context_files"}
LOCAL_ONLY_OPTIONS = {"context_files"}   # reading the PC's files is for the PC itself, never a phone or other device

# Suggested downloads (nothing is pulled automatically; sizes are approximate).
RECOMMENDED_MODELS = [
    {"name": "qwen2.5-coder:7b", "size": "4.7 GB", "why": "Much better code than a general model. Use for Coding's Builder (mode override)."},
    {"name": "nomic-embed-text", "size": "274 MB", "why": "Semantic memory: Cerberus recalls only what is relevant to what you say."},
    {"name": "qwen2.5vl:7b", "size": "6.0 GB", "why": "Vision: lets Cerberus read screenshots (screen_read, browser_screenshot)."},
    {"name": "qwen2.5:7b", "size": "4.7 GB", "why": "Strong, reliable native tool calling for agents."},
    {"name": "deepseek-r1:14b", "size": "9.0 GB", "why": "A much stronger Reasoner head. Fits your graphics card; set it in Settings > Models."},
    {"name": "qwen2.5:14b", "size": "9.0 GB", "why": "A much smarter Generalist/Editor head and a better fact-checker."},
]


# ---------------------------------------------------------------- request bodies
class ChatBody(BaseModel):
    message: str
    mode: str = "conversation"
    options: dict[str, Any] = Field(default_factory=dict)


class AgentBody(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    backstory: Optional[str] = None
    skills: Optional[list[str]] = None
    task: Optional[str] = None
    schedule: Optional[str] = None
    model_role: Optional[str] = None
    active: Optional[bool] = None


class ToolBody(BaseModel):
    args: dict[str, Any] = Field(default_factory=dict)


class FactBody(BaseModel):
    key: str
    value: str


class SampleBody(BaseModel):
    kind: str = "writing"
    title: str = ""
    content: str


class SymbolBody(BaseModel):
    symbol: str


class BudgetBody(BaseModel):
    kind: str
    amount: float
    category: str = "other"
    note: str = ""


class FlagBody(BaseModel):
    enabled: bool


class ApproveBody(BaseModel):
    approve: bool


class PullBody(BaseModel):
    name: str


class BackupBody(BaseModel):
    path: Optional[str] = None


class ReadBody(BaseModel):
    ids: Optional[list[int]] = None      # None = mark everything read


class WardenAnswer(BaseModel):
    id: int
    text: str = Field(max_length=1000)


class WardenObey(BaseModel):
    id: int


# ---------------------------------------------------------------- helpers
def lan_ips() -> list[str]:
    """Addresses other devices on the home network can use to reach this PC."""
    ips: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))   # no packet is sent; this just picks the right network card
            ips.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return ips


def _is_loopback(host: str) -> bool:
    return host in LOOPBACK


def _host_ok(host_header: str) -> bool:
    """Reject DNS-rebinding: a web page can't make our server answer to a hostname that is not localhost / an IP."""
    name = host_header[1:].split("]")[0] if host_header.startswith("[") else host_header.split(":")[0]
    if name in LOOPBACK:
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def line(event: dict) -> bytes:
    return (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")


# ---------------------------------------------------------------- app factory
def create_app(core: Optional[Cerberus] = None) -> FastAPI:
    core = core or Cerberus()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        core.agents.start()
        core.rituals.start()
        core.warden.start()
        try:
            backup.auto_backup(core.db)
        except Exception as exc:                          # a failed backup must never stop the app from starting
            core.gate.audit("backup", "auto-failed", {"error": str(exc)[:200]})
        yield
        core.warden.stop()
        core.agents.stop()

    app = FastAPI(title="Cerberus", version=__version__, lifespan=lifespan)
    app.state.core = core
    if (ASSETS_DIR / "fonts").exists():
        app.mount("/fonts", StaticFiles(directory=ASSETS_DIR / "fonts"), name="fonts")

    # ---- auth dependencies
    def auth(request: Request) -> str:
        """Returns the request's origin: 'local' or 'remote'. Raises 401/403 when not allowed."""
        client = request.client.host if request.client else ""
        if _is_loopback(client):
            if not _host_ok(request.headers.get("host", "")):
                raise HTTPException(403, "Blocked: unexpected Host header.")
            origin_hdr = request.headers.get("origin")
            if origin_hdr and urlparse(origin_hdr).netloc != request.headers.get("host", ""):
                raise HTTPException(403, "Blocked: cross-site request.")
            return "local"
        supplied = request.headers.get("x-cerberus-token", "")
        bearer = request.headers.get("authorization", "")
        if bearer.lower().startswith("bearer "):
            supplied = bearer[7:].strip()
        if not supplied or not hmac.compare_digest(supplied.encode("utf-8"), str(load_settings()["api_token"]).encode("utf-8")):
            raise HTTPException(401, "Missing or wrong API token.")
        return "remote"

    def local_only(origin: str = Depends(auth)) -> str:
        if origin != "local":
            raise HTTPException(403, "This can only be done from the PC that runs Cerberus.")
        return origin

    api = APIRouter(prefix="/api")

    # ---- public
    @app.get("/", response_class=HTMLResponse)
    def web_page() -> str:
        page = WEB_DIR / "index.html"
        return page.read_text(encoding="utf-8") if page.exists() else "<h1>Cerberus</h1><p>Web page missing.</p>"

    # PWA shell: public on purpose (the browser fetches these without the token); they contain no data.
    @app.get("/manifest.webmanifest")
    def manifest() -> FileResponse:
        return FileResponse(WEB_DIR / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/sw.js")
    def service_worker() -> FileResponse:
        return FileResponse(WEB_DIR / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})

    @app.get("/icon-{size}.png")
    def icon(size: int) -> FileResponse:
        path = WEB_DIR / f"icon-{size}.png"
        if size not in (192, 512) or not path.exists():
            raise HTTPException(404, "No such icon.")
        return FileResponse(path, media_type="image/png")

    @api.get("/ping")
    def ping() -> dict:
        return {"app": "cerberus", "version": __version__}

    protected = APIRouter(prefix="/api", dependencies=[Depends(auth)])

    # ---- status / modes / settings
    @protected.get("/status")
    def status() -> dict:
        return core.status()

    @protected.get("/modes")
    def modes() -> list[dict]:
        out = []
        for key in MODE_ORDER:
            m = MODES[key]
            out.append({"key": m.key, "label": m.label, "subtitle": m.subtitle, "description": m.description,
                        "options_help": m.options_help,
                        "stages": [{"key": s.key, "title": s.title, "role": s.role} for s in m.stage_sets["default"]],
                        "has_hand": bool(m.hand_skills)})
        return out

    @protected.get("/settings")
    def get_settings(origin: str = Depends(auth)) -> dict:
        return core.public_settings(origin)

    @protected.put("/settings")
    def put_settings(patch: dict[str, Any], _o: str = Depends(local_only)) -> dict:
        allowed = {"ollama_url", "models", "num_ctx", "keep_alive", "cloud", "github", "vscode_command", "workspace",
                   "learning_enabled", "user_name", "mode", "mode_models", "thinking", "full_access", "embed_model",
                   "vision_model", "rituals", "browser_headless", "telemetry", "show_workings"}
        clean: dict[str, Any] = {k: v for k, v in patch.items() if k in allowed}
        if "show_workings" in clean:
            clean["show_workings"] = bool(clean["show_workings"])
        if "telemetry" in clean:
            tel = clean["telemetry"]
            if not isinstance(tel, dict):
                raise HTTPException(400, "telemetry must be an object")
            clean["telemetry"] = {"enabled": bool(tel.get("enabled", True)), "previews": bool(tel.get("previews", True)),
                                  "keep_months": max(1, min(24, int(tel.get("keep_months", 6))))}
        if "rituals" in clean:
            rit = clean["rituals"]
            if not isinstance(rit, dict):
                raise HTTPException(400, "rituals must be an object")
            clean["rituals"] = {"enabled": bool(rit.get("enabled", True)), "use_model": bool(rit.get("use_model", True)),
                                "off": [i for i in (rit.get("off") or []) if i in RITUAL_IDS]}
        if "thinking" in clean and clean["thinking"] not in LEVELS:
            raise HTTPException(400, "thinking must be quick, careful or meticulous")
        if "full_access" in clean and not isinstance(clean["full_access"], bool):
            raise HTTPException(400, "full_access must be true or false")
        if "num_ctx" in clean:
            clean["num_ctx"] = max(1024, min(131072, int(clean["num_ctx"])))
        if "models" in clean and not isinstance(clean["models"], dict):
            raise HTTPException(400, "models must be an object")
        if "mode_models" in clean:
            mm = clean["mode_models"]
            if not isinstance(mm, dict) or any(k not in MODES or not isinstance(v, dict) for k, v in mm.items()):
                raise HTTPException(400, "mode_models must look like {mode: {role: model}}")
            # replaced wholesale: a deep-merge could never remove an override
            clean["mode_models"] = {k: {r: str(n) for r, n in v.items() if n} for k, v in mm.items()}
        if "mode" in clean and clean["mode"] not in MODES:
            raise HTTPException(400, "unknown mode")
        if "workspace" in clean:
            if not isinstance(clean["workspace"], str) or not clean["workspace"].strip():
                raise HTTPException(400, "workspace must be a folder path")
            ws = Path(clean["workspace"]).expanduser()
            try:
                computer.check_workspace(ws)
                ws.mkdir(parents=True, exist_ok=True)
            except Denied as exc:
                raise HTTPException(400, str(exc))
            except OSError as exc:
                raise HTTPException(400, f"Cannot use that workspace folder: {exc}")
            clean["workspace"] = str(ws.resolve())
        if "cloud" in clean and not isinstance(clean["cloud"], dict):
            raise HTTPException(400, "cloud must be an object")
        if "github" in clean and not isinstance(clean["github"], dict):
            raise HTTPException(400, "github must be an object")
        cloud = clean.get("cloud")
        if isinstance(cloud, dict):
            if "provider" in cloud and cloud["provider"] not in PROVIDERS:
                raise HTTPException(400, "unknown cloud provider")
            if "max_handoff_tokens" in cloud:
                cloud["max_handoff_tokens"] = max(200, min(20000, int(cloud["max_handoff_tokens"])))
            for pid in list((cloud.get("providers") or {}).keys()):
                if pid not in PROVIDERS:
                    del cloud["providers"][pid]
        core.update_settings(clean, replace=tuple(k for k in ("mode_models", "rituals") if k in clean))
        if "rituals" in clean:
            core.rituals.sync()
        return core.public_settings()

    @protected.post("/ollama/start")
    def ollama_start(_o: str = Depends(local_only)) -> dict:
        msg = "Ollama is already running." if detect_ollama(core.ollama.base_url) else start_ollama_server()
        core.ollama.refresh_url()
        return {"message": msg, "status": core.status()}

    @protected.get("/models/recommended")
    def models_recommended() -> list[dict]:
        have = core.ollama.installed_names()
        return [m | {"installed": any(n.lower().startswith(m["name"].split(":")[0].lower()) for n in have)}
                for m in RECOMMENDED_MODELS]

    @protected.post("/models/pull")
    def models_pull(body: PullBody, _o: str = Depends(local_only)) -> StreamingResponse:
        """Download a model through Ollama, streaming progress as NDJSON. Local-only: it uses bandwidth and disk."""
        name = body.name.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,100}", name):
            raise HTTPException(400, "That does not look like a model name.")
        core.gate.audit("models_pull", "run", {"name": name})

        def stream():
            try:
                for chunk in core.ollama.pull_stream(name):
                    yield line(chunk)
                yield line({"status": "success", "done": True})
            except Exception as exc:
                yield line({"error": str(exc), "done": True})

        return StreamingResponse(stream(), media_type="application/x-ndjson")

    @protected.get("/network")
    def network(origin: str = Depends(auth)) -> dict:
        s = load_settings()
        port = s["server"]["port"]
        info = {"ips": lan_ips(), "port": port, "lan": s["server"]["host"] != "127.0.0.1"}
        if origin == "local":
            info["token"] = s["api_token"]
        return info

    # ---- chat
    @protected.post("/chat")
    def chat(body: ChatBody, origin: str = Depends(auth)) -> StreamingResponse:
        options = {k: v for k, v in body.options.items()
                   if k in CHAT_OPTION_KEYS and (origin == "local" or k not in LOCAL_ONLY_OPTIONS)}
        cancel = threading.Event()
        run_id = uuid.uuid4().hex[:8]
        core.runs[run_id] = cancel

        def stream():
            gen = core.orchestrator.run(body.message, body.mode, options, origin, cancel)
            try:
                yield line({"type": "run", "id": run_id})
                for ev in gen:
                    if origin != "local" and ev.get("type") == "error" and "detail" in ev:
                        ev = {k: v for k, v in ev.items() if k != "detail"}   # technical detail stays on the PC
                    yield line(ev)
            finally:
                cancel.set()  # client went away or run finished: stop any model still generating
                gen.close()
                core.runs.pop(run_id, None)

        return StreamingResponse(stream(), media_type="application/x-ndjson",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @protected.post("/chat/{run_id}/cancel")
    def chat_cancel(run_id: str) -> dict:
        ev = core.runs.get(run_id)
        if ev:
            ev.set()
        return {"cancelled": bool(ev)}

    @protected.get("/history")
    def history(mode: str = "conversation", limit: int = 40) -> list[dict]:
        return core.db.recent_messages(mode, max(1, min(limit, 200)))

    @protected.delete("/history")
    def clear_history(mode: Optional[str] = None, _o: str = Depends(local_only)) -> dict:
        core.db.clear_messages(mode)
        core.memory.prune()
        return {"ok": True}

    # ---- profile / learning
    @protected.get("/profile")
    def profile() -> dict:
        card = core.db.kv_get("style_card", {}) or {}
        return {"bond": learning.bond(core.db), "facts": learning.list_facts(core.db),
                "habits": learning.habit_lines(core.db), "style": {"text": card.get("text", ""),
                                                                   "samples": card.get("samples", 0),
                                                                   "updated": card.get("updated")},
                "samples": [{k: s[k] for k in ("id", "kind", "title", "ts")} | {"chars": len(s["content"])}
                            for s in core.db.list_samples()],
                "learning_enabled": core.settings()["learning_enabled"]}

    @protected.post("/profile/facts")
    def add_fact(body: FactBody) -> dict:
        learning.upsert_fact(core.db, body.key, body.value, "told", 1.0, "user")
        return {"ok": True}

    @protected.delete("/profile/facts/{key}")
    def delete_fact(key: str) -> dict:
        core.db.execute("DELETE FROM facts WHERE key=?", (key,))
        core.memory.prune()
        return {"ok": True}

    @protected.post("/profile/forget")
    def forget_everything(_o: str = Depends(local_only)) -> dict:
        core.db.execute("DELETE FROM facts WHERE source IS NOT 'dossier'")  # the dossier is the root; it stays
        for table in ("samples", "messages"):
            core.db.execute(f"DELETE FROM {table}")
        for key in ("stats", "style_card"):
            core.db.execute("DELETE FROM kv WHERE key=?", (key,))
        core.memory.prune()
        return {"ok": True}

    @protected.post("/samples")
    def add_sample(body: SampleBody) -> dict:
        if body.kind not in ("writing", "resume", "notes"):
            raise HTTPException(400, "kind must be writing, resume or notes")
        if len(body.content.strip()) < 20:
            raise HTTPException(400, "That sample is too short to learn from.")
        sid = core.db.add_sample(body.kind, body.title.strip() or body.content.strip()[:40], body.content.strip()[:20000])
        return {"id": sid}

    @protected.delete("/samples/{sample_id}")
    def delete_sample(sample_id: int) -> dict:
        core.db.delete_sample(sample_id)
        return {"ok": True}

    @protected.post("/profile/style/rebuild")
    def rebuild_style() -> dict:
        llm = core.llm_fn("generalist") if core.ollama.is_up() else None
        card = learning.rebuild_style_card(core.db, llm)
        return {"text": card["text"], "samples": card["samples"]}

    # ---- agents (managing agents is local-only: an agent can use tools with the full trust of this PC)
    @protected.get("/skills")
    def skills() -> dict:
        return skills_catalog()

    @protected.get("/agents")
    def agents_list() -> list[dict]:
        return core.agents.list()

    @protected.post("/agents")
    def agents_create(body: AgentBody, _o: str = Depends(local_only)) -> dict:
        try:
            return core.agents.create(body.name or "", body.role or "", body.backstory or "", body.skills or [],
                                      body.task or "", body.schedule or "manual", body.model_role or "generalist",
                                      bool(body.active))
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @protected.put("/agents/{agent_id}")
    def agents_update(agent_id: int, body: AgentBody, _o: str = Depends(local_only)) -> dict:
        try:
            result = core.agents.update(agent_id, **body.model_dump(exclude_none=True))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        if result is None:
            raise HTTPException(404, "No such agent.")
        return result

    @protected.delete("/agents/{agent_id}")
    def agents_delete(agent_id: int, _o: str = Depends(local_only)) -> dict:
        core.agents.delete(agent_id)
        return {"ok": True}

    @protected.post("/agents/{agent_id}/run")
    def agents_run(agent_id: int, _o: str = Depends(local_only)) -> dict:
        return {"started": core.agents.run(agent_id, "manual")}

    @protected.get("/agents/{agent_id}/runs")
    def agents_runs(agent_id: int) -> list[dict]:
        return core.agents.runs(agent_id)

    @protected.get("/schedule/check")
    def schedule_check(text: str) -> dict:
        try:
            return {"ok": True, "spec": parse_schedule(text)}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    # ---- proactive rituals and notifications
    @protected.get("/notifications")
    def notifications(unread: bool = False, limit: int = 30, after: Optional[int] = None) -> list[dict]:
        return core.db.notifications(unread, max(1, min(limit, 100)), after)

    @protected.post("/notifications/read")
    def notifications_read(body: ReadBody) -> dict:
        core.db.mark_notifications_read(body.ids)
        return {"ok": True}

    @protected.get("/rituals")
    def rituals_list() -> list[dict]:
        return core.rituals.list()

    @protected.post("/rituals/{ritual_id}/fire")
    def rituals_fire(ritual_id: str, _o: str = Depends(local_only)) -> dict:
        if ritual_id not in RITUAL_IDS:
            raise HTTPException(404, "No such ritual.")
        return core.rituals.fire(ritual_id, force=True) or {}

    # ---- backup / restore (local-only: the zip holds your whole history)
    @protected.post("/backup")
    def backup_create(body: BackupBody, _o: str = Depends(local_only)) -> dict:
        dest = Path(body.path).expanduser() if body.path else None
        if dest and dest.suffix.lower() != ".zip" and not dest.is_dir():
            raise HTTPException(400, "Choose a .zip file name (or an existing folder).")
        try:
            out = backup.create_backup(core.db, dest)
        except OSError as exc:
            raise HTTPException(400, f"Could not write the backup: {exc}")
        core.gate.audit("backup", "created", {"file": out.name})
        return {"path": str(out), "size": out.stat().st_size}

    @protected.post("/backup/restore")
    def backup_restore(body: BackupBody, _o: str = Depends(local_only)) -> dict:
        if not body.path:
            raise HTTPException(400, "Say which backup file to restore.")
        try:
            msg = backup.stage_restore(core.db, Path(body.path).expanduser())
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc))
        core.gate.audit("backup", "restore-staged", {"file": Path(body.path).name})
        return {"message": msg}

    @protected.get("/backup/list")
    def backup_list(_o: str = Depends(local_only)) -> dict:
        d = backup.backups_dir()
        files = sorted(d.glob("cerberus-*.zip"), reverse=True)[:10]
        return {"folder": str(d), "data_dir": str(config.DATA_DIR), "files": [f.name for f in files]}

    # ---- tools
    @protected.get("/tools")
    def tools_list() -> list[dict]:
        return [t.public() for t in REGISTRY.values()]

    @protected.post("/tools/{name}")
    def tools_run(name: str, body: ToolBody, origin: str = Depends(auth)) -> dict:
        tool_def = REGISTRY.get(name)
        if origin != "local" and tool_def is not None and tool_def.risk != "safe":
            raise HTTPException(403, "Only read-only tools can be run from another device. Do this on the PC.")
        ctx = core.tool_ctx(origin, f"user:{origin}")
        ctx.intent = f"The user ran the {name} tool directly from the app."
        return {"result": run_tool(name, body.args, ctx)}

    # ---- safety
    @protected.get("/safety")
    def safety() -> dict:
        return {"autonomous": core.gate.autonomous, "pending": core.gate.pending()}

    @protected.post("/safety/autonomous")
    def set_autonomous(body: FlagBody, _o: str = Depends(local_only)) -> dict:
        core.gate.set_autonomous(body.enabled)
        return {"autonomous": core.gate.autonomous}

    @protected.post("/safety/confirm/{pending_id}")
    def confirm(pending_id: str, body: ApproveBody) -> dict:
        return {"ok": core.gate.decide(pending_id, body.approve)}

    @protected.post("/safety/seal")
    def seal() -> dict:
        return {"denied": core.gate.seal(), "autonomous": core.gate.autonomous}

    @protected.get("/safety/audit")
    def audit(limit: int = 100, _o: str = Depends(local_only)) -> list[str]:
        return SafetyGate.read_audit(max(1, min(limit, 500)))

    # ---- wealth
    @protected.get("/watchlist")
    def watchlist() -> list[dict]:
        return [fetch_quote(r["symbol"]) for r in core.db.query("SELECT symbol FROM watchlist ORDER BY symbol")]

    @protected.post("/watchlist")
    def watchlist_add(body: SymbolBody) -> dict:
        from .tools.finance import watchlist_add as add
        try:
            return {"message": add(core.tool_ctx(), body.symbol)}
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @protected.delete("/watchlist/{symbol}")
    def watchlist_remove(symbol: str) -> dict:
        core.db.execute("DELETE FROM watchlist WHERE symbol=?", (symbol.strip().upper(),))
        return {"ok": True}

    @protected.get("/budget")
    def budget(days: int = 30) -> dict:
        rows = core.db.query("SELECT * FROM budget ORDER BY id DESC LIMIT 50")
        return {"summary": budget_summary_text(core.db, days), "entries": rows}

    @protected.post("/budget")
    def budget_add(body: BudgetBody) -> dict:
        from .tools.finance import budget_add as add
        try:
            return {"message": add(core.tool_ctx(), body.kind, body.amount, body.category, body.note)}
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    # ---- this computer, the council, plugins, projects
    @protected.get("/system")
    def system() -> dict:
        hw = core.hardware()
        rec = hardware.recommend(hw)
        s = load_settings()
        return {"hardware": hw, "summary": hardware.describe(hw), "recommended": rec, "pressure": hardware.pressure(),
                "settings": {"num_ctx": s["num_ctx"], "keep_alive": s["keep_alive"]},
                "thinking": {"current": core.council.level()["key"],
                             "levels": [{"key": k, "label": v["label"], "blurb": v["blurb"]} for k, v in LEVELS.items()]},
                "council": core.council.stats(), "lessons": len(core.council.lessons()),
                "full_access": bool(s.get("full_access", True)), "guard": guard.status()}

    @protected.get("/guard")
    def guard_status() -> dict:
        return guard.status(force=True)

    # ---- the Warden (screen-time watcher). Anything that changes it, answers it or closes something is local-only.
    @protected.get("/warden")
    def warden_status() -> dict:
        return core.warden.status()

    @protected.get("/warden/pending")
    def warden_pending(_o: str = Depends(local_only)) -> dict:
        return {"alert": core.warden.pending()}

    @protected.post("/warden/answer")
    def warden_answer(body: WardenAnswer, _o: str = Depends(local_only)) -> dict:
        try:
            return core.warden.answer(body.id, body.text)
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    @protected.post("/warden/obey")
    def warden_obey(body: WardenObey, _o: str = Depends(local_only)) -> dict:
        try:
            return core.warden.obey(body.id)
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    @protected.put("/warden/settings")
    def warden_settings(patch: dict[str, Any], _o: str = Depends(local_only)) -> dict:
        try:
            return core.warden.update_settings(patch)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @protected.post("/system/tune")
    def system_tune(_o: str = Depends(local_only)) -> dict:
        rec = hardware.recommend(core.hardware())
        core.update_settings({"num_ctx": rec["num_ctx"], "keep_alive": rec["keep_alive"], "auto_tuned": True})
        return {"applied": {"num_ctx": rec["num_ctx"], "keep_alive": rec["keep_alive"]}, "note": rec["note"]}

    @protected.get("/council/lessons")
    def council_lessons() -> list[dict]:
        return core.council.lessons()

    @protected.delete("/council/lessons")
    def council_forget(_o: str = Depends(local_only)) -> dict:
        core.council.forget_lessons()
        return {"ok": True}

    @protected.get("/plugins")
    def plugins_status() -> dict:
        return plugins.status()

    @protected.get("/projects")
    def projects_list(_o: str = Depends(local_only)) -> dict:
        return load_settings().get("projects") or {}

    @protected.put("/projects")
    def projects_put(body: dict[str, Any], _o: str = Depends(local_only)) -> dict:
        clean: dict[str, dict] = {}
        for name, spec in body.items():
            if not isinstance(spec, dict) or not re.fullmatch(r"[\w .-]{1,40}", str(name)):
                raise HTTPException(400, "Each project needs a simple name and {path, run, about}.")
            path, run = str(spec.get("path", "")).strip(), str(spec.get("run", "")).strip()
            if not path or not run or not Path(path).expanduser().is_dir():
                raise HTTPException(400, f"Project {name}: the folder must exist and a run command is needed.")
            clean[str(name)] = {"path": path, "run": run, "about": str(spec.get("about", ""))[:300]}
        core.update_settings({"projects": clean}, replace=("projects",))
        core.gate.audit("projects", "updated", {"names": list(clean)})
        return clean

    # ---- workspace
    @protected.get("/workspace")
    def workspace(_o: str = Depends(local_only)) -> dict:
        root = get_workspace()
        items = []
        for p in root.rglob("*"):
            rel = p.relative_to(root)
            if any(part.startswith(".git") or part == "__pycache__" or part == "node_modules" for part in rel.parts):
                continue
            try:
                items.append({"path": rel.as_posix(), "dir": p.is_dir(), "size": p.stat().st_size if p.is_file() else 0})
            except OSError:
                continue
            if len(items) >= 400:
                break
        items.sort(key=lambda i: i["path"].lower())
        return {"root": str(root), "items": items}

    # ---- review log, self-knowledge, error memory
    @protected.get("/telemetry/report")
    def telemetry_report(days: int = 30, text: bool = False, _o: str = Depends(local_only)) -> Any:
        rep = telemetry.report(max(1, min(days, 365)))
        if text:
            return HTMLResponse(telemetry.render_text(rep), media_type="text/plain; charset=utf-8")
        return rep

    @protected.get("/capabilities")
    def capabilities_get(text: bool = False, origin: str = Depends(auth)) -> Any:
        if text:
            return HTMLResponse(capabilities.full_text(core, origin), media_type="text/plain; charset=utf-8")
        return capabilities.describe(core, origin)

    @protected.get("/mistakes")
    def mistakes_list(_o: str = Depends(local_only)) -> dict:
        return mistakes.summary(core.db)

    @protected.delete("/mistakes")
    def mistakes_forget(_o: str = Depends(local_only)) -> dict:
        mistakes.forget(core.db)
        core.gate.audit("mistakes", "forgotten", {})
        return {"ok": True}

    # ---- file browser (this computer only; the same read rules as the Computer tools)
    @protected.get("/files/browse")
    def files_browse(path: str = "", _o: str = Depends(local_only)) -> dict:
        try:
            return filectx.browse(path)
        except Denied as exc:
            raise HTTPException(403, str(exc))

    @protected.get("/files/preview")
    def files_preview(path: str, _o: str = Depends(local_only)) -> dict:
        try:
            return filectx.preview(path)
        except Denied as exc:
            raise HTTPException(403, str(exc))

    # ---- Resume Studio (the Resume project wired into Hunt)
    @protected.get("/resume")
    def resume_get(_o: str = Depends(local_only)) -> dict:
        from .tools import resume as studio
        cfg = studio.settings()
        try:
            found, problem = str(studio.folder()), ""
        except Denied as exc:
            found, problem = "", str(exc)
        stored = studio.stored(core.db)
        return {**cfg, "folder": found, "problem": problem, "jobs": len(stored.get("jobs") or []),
                "last_query": stored.get("query", "")}

    @protected.put("/resume")
    def resume_put(body: dict[str, Any], _o: str = Depends(local_only)) -> dict:
        from .tools import resume as studio
        cur = studio.settings()
        path = str(body.get("path", cur["path"])).strip()
        if path and not studio._looks_right(Path(path).expanduser()):
            raise HTTPException(400, "That folder is not the Resume project (src/job_agent/scraper.py is missing).")
        feeds = body.get("feed_urls", cur["feed_urls"])
        if isinstance(feeds, str):
            feeds = [x.strip() for x in re.split(r"[\n,]", feeds)]
        feeds = [str(x).strip() for x in (feeds or []) if str(x).strip()]
        if len(feeds) > 10 or any(urlparse(x).scheme not in ("http", "https") or not urlparse(x).hostname for x in feeds):
            raise HTTPException(400, "Job feeds must be web addresses (http or https), at most 10, using {query} and {location}.")
        base = str(body.get("base_resume", cur["base_resume"])).strip()
        if base:
            try:
                filectx.read_text(filectx.check(base), 2000)
            except Denied as exc:
                raise HTTPException(400, f"The base resume cannot be used: {exc}")
        app_in = body.get("applicant", cur["applicant"])
        if not isinstance(app_in, dict):
            raise HTTPException(400, "applicant must be an object.")
        applicant = {k: str(app_in.get(k, "")).strip()[:200] for k in ("name", "email", "phone", "linkedin")}
        core.update_settings({"resume": {"path": path, "feed_urls": feeds, "base_resume": base, "applicant": applicant}},
                             replace=("resume",))
        core.gate.audit("resume", "settings updated", {"feeds": len(feeds), "path": path, "base_resume": base})
        return resume_get(_o)

    app.include_router(api)
    app.include_router(protected)
    return app


# ---------------------------------------------------------------- command line
def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Run the Cerberus API server.")
    parser.add_argument("--host", default=None, help="Address to listen on (default 127.0.0.1; 0.0.0.0 = whole network)")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--lan", action="store_true", help="Shortcut for --host 0.0.0.0")
    args = parser.parse_args()
    from .config import update_settings
    settings = load_settings()
    host = "0.0.0.0" if args.lan else (args.host or settings["server"]["host"])
    port = args.port or settings["server"]["port"] or DEFAULT_PORT
    update_settings({"server": {"host": host, "port": port, "lan": host != "127.0.0.1"}})
    if host != "127.0.0.1":
        print(f"\n  Cerberus is listening on the network: port {port}")
        for ip in lan_ips():
            print(f"    http://{ip}:{port}")
        print(f"  API token (needed from other devices): {settings['api_token']}\n")
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
