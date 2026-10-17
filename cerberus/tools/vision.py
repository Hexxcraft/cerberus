"""Eyes: let Cerberus LOOK at the screen (or any image) using a local Ollama vision model.

`screen_read` takes a screenshot of the desktop and asks the vision model a question about it. Nothing leaves your computer.
Which model: settings -> vision_model (default qwen2.5vl). If that one is not installed, any other installed vision model
(llava, llama3.2-vision, minicpm-v, moondream, ...) is used; if there is none, the tool says how to get one.
"""
from __future__ import annotations

import base64
import io
import re

from ..config import load_settings
from ..ollama_client import OllamaClient, OllamaError
from .base import tool

VISION_HINT = re.compile(r"llava|vision|[-:.]vl|vl[-:.]|minicpm-v|moondream|gemma3|granite3.2-vision|bakllava", re.I)
MAX_SIDE = 1600
DEFAULT_QUESTION = "Describe what is on this screen. Read any text and name any error or button."
SIMPLE_QUESTION = "Describe this image."       # tiny models (moondream) answer long prompts with nothing; this one they handle


def _client() -> OllamaClient:
    s = load_settings()
    return OllamaClient(s["ollama_url"], s["num_ctx"], s["keep_alive"])


def pick_vision_model(client: OllamaClient, wanted: str) -> str | None:
    found = client.resolve(wanted) if wanted else None
    if found:
        return found
    return next((n for n in client.installed_names() if VISION_HINT.search(n)), None)


def encode_png(image) -> str:
    """PIL image -> base64 PNG, shrunk so a big monitor does not swamp the model."""
    w, h = image.size
    scale = MAX_SIDE / max(w, h)
    if scale < 1:
        image = image.resize((int(w * scale), int(h * scale)))
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def describe_image(b64_png: str, question: str, client: OllamaClient | None = None) -> str:
    """Ask the vision model about one image. Returns the answer, or a plain sentence saying why it could not."""
    client = client or _client()
    if not client.is_up():
        return "Ollama is not running, so I cannot look at anything."
    model = pick_vision_model(client, load_settings().get("vision_model", ""))
    if not model:
        return ("No vision model is installed. Open the Gates tab -> Model upgrades and download one "
                "(qwen2.5vl:7b is the best fit), or run: ollama pull qwen2.5vl:7b")
    def ask(text: str) -> str:
        msg = client.chat_once(model, [{"role": "user", "content": text, "images": [b64_png]}], temperature=0.1, num_predict=900)
        return (msg.get("content") or "").strip()
    try:
        answer = ask(question or DEFAULT_QUESTION)
        if not answer:
            answer = ask(SIMPLE_QUESTION)
    except OllamaError as exc:
        return f"The vision model ({model}) failed: {exc}"
    return f"[seen by {model}] " + (answer or "(it saw nothing it could describe)")


@tool("screen_read", "Screen Vision",
      "Look at the screen right now and answer a question about it (reads text, finds buttons, spots errors).",
      {"question": "what to look for (optional)"}, risk="control", local_only=True)
def screen_read(ctx, question: str = "") -> str:
    from .desktop import pg
    shot = pg().screenshot()
    return describe_image(encode_png(shot), question)
