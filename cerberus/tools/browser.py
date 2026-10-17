"""Hands on the web: drive a real Microsoft Edge window with Playwright, so Cerberus can open pages, read them, click and fill forms.

  * Uses the Edge that is already installed on Windows (no browser download). It runs with its OWN profile in data/browser_profile,
    so it never touches your everyday Edge logins; log in once inside this window and it remembers.
  * Playwright must stay on one thread, so a single worker thread owns the browser and tools just queue jobs for it.
  * Reading a page gives the visible text plus a NUMBERED list of buttons, links and fields; the model clicks "[4]" instead of
    guessing pixel coordinates, which is far more reliable for small local models.
  * Every tool asks first (SafetyGate) and is refused for remote clients. Private/LAN addresses are blocked like fetch_url.
"""
from __future__ import annotations

import queue
import threading
from datetime import datetime
from typing import Any, Callable, Optional

from .. import config
from .base import tool
from .vision import describe_image
from .web import check_public_url

NAV_TIMEOUT = 25000
ACTION_TIMEOUT = 8000
CALL_TIMEOUT = 90

LIST_ELEMENTS_JS = """() => {
  document.querySelectorAll('[data-cerb]').forEach(e => e.removeAttribute('data-cerb'));
  const sel = 'a[href], button, input:not([type=hidden]), select, textarea, [role=button], [role=link], [role=tab], [onclick]';
  const out = []; let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none') continue;
    n++; el.setAttribute('data-cerb', String(n));
    const tag = el.tagName.toLowerCase();
    const label = (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') || el.title || el.name || el.alt || '')
        .trim().replace(/\\s+/g, ' ').slice(0, 70);
    out.push([n, tag, tag === 'input' ? el.type : '', label, tag === 'a' ? (el.href || '') : '']);
    if (n >= 80) break;
  }
  return out;
}"""


class BrowserWorker:
    """One thread that owns Playwright + Edge. `call(fn)` runs fn(page) on that thread and returns its result."""

    def __init__(self) -> None:
        self._jobs: queue.Queue = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="cerberus-browser", daemon=True)
                self._thread.start()

    def _run(self) -> None:
        pw = ctx = None
        page = None
        try:
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
        except Exception as exc:                       # playwright missing or broken: fail every job clearly
            pw_error = exc
        else:
            pw_error = None
        while True:
            job = self._jobs.get()
            if job is None:
                break
            fn, box = job
            try:
                if pw_error:
                    raise RuntimeError(f"Playwright is not available ({pw_error}). Install it with: pip install playwright")
                if ctx is None or page is None or page.is_closed():
                    if ctx is None or not _alive(ctx):
                        ctx = pw.chromium.launch_persistent_context(
                            str(config.DATA_DIR / "browser_profile"), channel="msedge",
                            headless=bool(config.load_settings().get("browser_headless", False)),
                            viewport={"width": 1280, "height": 860}, args=["--disable-blink-features=AutomationControlled"])
                        ctx.set_default_timeout(ACTION_TIMEOUT)
                        ctx.set_default_navigation_timeout(NAV_TIMEOUT)
                    page = ctx.pages[0] if ctx.pages else ctx.new_page()
                box["value"] = fn(page)
            except Exception as exc:
                box["error"] = exc
            finally:
                box["done"].set()
        try:
            if ctx:
                ctx.close()
            if pw:
                pw.stop()
        except Exception:
            pass

    def call(self, fn: Callable[[Any], Any], timeout: int = CALL_TIMEOUT) -> Any:
        self._ensure_thread()
        box: dict = {"done": threading.Event()}
        self._jobs.put((fn, box))
        if not box["done"].wait(timeout):
            raise TimeoutError(f"The browser did not answer within {timeout}s.")
        if "error" in box:
            raise box["error"]
        return box["value"]

    def shutdown(self) -> None:
        if self._thread and self._thread.is_alive():
            self._jobs.put(None)
            self._thread.join(timeout=10)
        self._thread = None


def _alive(ctx) -> bool:
    try:
        _ = ctx.pages
        return True
    except Exception:
        return False


BROWSER = BrowserWorker()


def shutdown() -> None:
    BROWSER.shutdown()


def _snapshot(page, max_chars: int = 2500) -> str:
    text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())[:max_chars]
    rows = page.evaluate(LIST_ELEMENTS_JS)
    lines = []
    for n, tag, kind, label, href in rows:
        what = {"a": "link", "button": "button"}.get(tag, f"{tag}({kind})" if kind else tag)
        lines.append(f'[{n}] {what} "{label}"' + (f" -> {href[:90]}" if href else ""))
    return (f"URL: {page.url}\nTITLE: {page.title()}\n\nPAGE TEXT:\n{text or '(empty)'}\n\n"
            f"CLICKABLE / FILLABLE ELEMENTS (use the number):\n" + ("\n".join(lines) or "(none found)"))


def _settle(page) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=5000)
        page.wait_for_timeout(400)
    except Exception:
        pass


def _element(page, index: Optional[int], text: str):
    if index not in (None, ""):
        loc = page.locator(f'[data-cerb="{int(index)}"]')
        if loc.count() == 0:
            raise ValueError(f"No element [{index}] on this page. Call browser_read again (numbers change when the page changes).")
        return loc.first
    if text:
        return page.get_by_text(text, exact=False).first
    raise ValueError("Give the element's number (from browser_read) or its visible text.")


@tool("browser_open", "Browser Automation", "Open a web page in the Edge automation window and read it.",
      {"url": "full https:// address"}, risk="control", local_only=True)
def browser_open(ctx, url: str) -> str:
    check_public_url(url)

    def job(page):
        page.goto(url, wait_until="domcontentloaded")
        _settle(page)
        return _snapshot(page)
    return BROWSER.call(job)


@tool("browser_read", "Browser Automation", "Read the current page again: text plus numbered buttons, links and fields.",
      {"max_chars": "text limit, default 2500 (optional)"}, risk="control", local_only=True)
def browser_read(ctx, max_chars: int = 2500) -> str:
    return BROWSER.call(lambda page: _snapshot(page, max(500, min(int(max_chars), 8000))))


@tool("browser_click", "Browser Automation", "Click a button or link on the current page, then read the result.",
      {"index": "number 1 or higher: element number from browser_read (optional)", "text": "visible text instead of a number (optional)"},
      risk="control", local_only=True)
def browser_click(ctx, index: Optional[int] = None, text: str = "") -> str:
    def job(page):
        _element(page, index, text).click()
        _settle(page)
        return _snapshot(page)
    return BROWSER.call(job)


@tool("browser_fill", "Browser Automation", "Type into a field (or choose a dropdown option) on the current page.",
      {"index": "number 1 or higher: element number from browser_read", "text": "the text to enter",
       "submit": "true to press Enter afterwards (optional)"}, risk="control", local_only=True)
def browser_fill(ctx, index: int, text: str, submit: bool = False) -> str:
    def job(page):
        el = _element(page, index, "")
        if el.evaluate("e => e.tagName.toLowerCase()") == "select":
            el.select_option(label=text)
        else:
            el.fill(str(text))
        if str(submit).lower() in ("true", "1", "yes"):
            el.press("Enter")
            _settle(page)
        return f"Filled [{index}] with {len(str(text))} characters." + (" Submitted." if str(submit).lower() in ("true", "1", "yes") else "")
    return BROWSER.call(job)


@tool("browser_press", "Browser Automation", "Press a key on the current page, e.g. Enter, Tab, PageDown.",
      {"key": "key name"}, risk="control", local_only=True)
def browser_press(ctx, key: str) -> str:
    def job(page):
        page.keyboard.press(key)
        _settle(page)
        return f"Pressed {key}. Now at {page.url}"
    return BROWSER.call(job)


@tool("browser_screenshot", "Browser Automation",
      "Screenshot the current page into workspace/screenshots; give a question to have the vision model describe it.",
      {"question": "what to look for (optional)"}, risk="control", local_only=True)
def browser_screenshot(ctx, question: str = "") -> str:
    def job(page):
        out = config.get_workspace() / "screenshots"
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"browser_{datetime.now():%Y%m%d_%H%M%S}.png"
        png = page.screenshot(path=str(path))
        return path, png
    path, png = BROWSER.call(job)
    result = f"Saved {path}"
    if question:
        import base64
        result += "\n" + describe_image(base64.b64encode(png).decode("ascii"), question)
    return result


@tool("browser_close", "Browser Automation", "Close the automation browser window.", risk="control", local_only=True)
def browser_close(ctx) -> str:
    BROWSER.shutdown()
    return "Browser closed."
