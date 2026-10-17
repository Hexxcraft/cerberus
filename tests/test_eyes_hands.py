import http.server
import importlib.util
import os
import shutil
import threading
import unittest
from unittest import mock

from PIL import Image

from cerberus.db import Database
from cerberus.safety import SafetyGate
from cerberus.tools import ToolContext, run_tool
from cerberus.tools import browser, vision

from .helpers import FakeOllama, Isolated

PAGE = b"""<html><head><title>Gate Test</title></head><body>
<h1>Welcome, traveller</h1>
<input id="who" placeholder="Your name">
<button onclick="document.getElementById('out').innerText='Hello ' + document.getElementById('who').value">Greet</button>
<a href="/second">Second page</a>
<p id="out"></p></body></html>"""
SECOND = b"<html><head><title>Second</title></head><body><p>You arrived.</p></body></html>"


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = SECOND if self.path.startswith("/second") else PAGE
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class VisionTests(unittest.TestCase):
    def setUp(self):
        iso = Isolated()
        iso.__enter__()
        self.addCleanup(iso.__exit__, None, None, None)
        self.fake = FakeOllama("A terminal with a red error.")
        self.fake.installed.append("moondream:latest")

    def test_encode_shrinks_big_images(self):
        import base64
        import io
        b64 = vision.encode_png(Image.new("RGB", (3200, 1800), "black"))
        img = Image.open(io.BytesIO(base64.b64decode(b64)))
        self.assertEqual(max(img.size), vision.MAX_SIDE)

    def test_describe_sends_the_image_to_a_vision_model(self):
        out = vision.describe_image("AAAA", "what is wrong?", client=self.fake)
        model, messages = self.fake.calls[0]
        self.assertEqual(model, "moondream:latest")           # qwen2.5vl absent, so any installed vision model is used
        self.assertEqual(messages[0]["images"], ["AAAA"])
        self.assertIn("red error", out)

    def test_empty_answer_is_retried_with_a_simple_question(self):
        asked = []

        def chat_once(model, messages, **kw):
            asked.append(messages[0]["content"])
            return {"content": "" if len(asked) == 1 else "A red error banner."}
        self.fake.chat_once = chat_once
        out = vision.describe_image("AAAA", "Read the long error text and list every button please.", client=self.fake)
        self.assertEqual(asked[1], vision.SIMPLE_QUESTION)
        self.assertIn("red error banner", out)

    def test_no_vision_model_says_how_to_get_one(self):
        self.fake.installed.remove("moondream:latest")
        out = vision.describe_image("AAAA", "", client=self.fake)
        self.assertIn("No vision model", out)
        self.assertEqual(self.fake.calls, [])

    def test_screen_read_is_local_only(self):
        from cerberus import config
        db = Database(config.DATA_DIR.parent / "v.db")
        self.addCleanup(db.close)
        r = run_tool("screen_read", {}, ToolContext(gate=SafetyGate(), db=db, origin="remote"))
        self.assertIn("blocked", r)

    def test_screen_read_takes_a_screenshot_and_asks_the_model(self):
        shot = Image.new("RGB", (100, 60), "white")
        fake_pg = mock.Mock()
        fake_pg.screenshot.return_value = shot
        with mock.patch("cerberus.tools.desktop.pg", return_value=fake_pg), \
                mock.patch.object(vision, "_client", return_value=self.fake):
            out = vision.screen_read(None, "what is open?")
        self.assertIn("red error", out)
        self.assertTrue(self.fake.calls[0][1][0]["images"][0])


def _edge_available() -> bool:
    if importlib.util.find_spec("playwright") is None:
        return False
    roots = [os.environ.get("ProgramFiles(x86)", ""), os.environ.get("ProgramFiles", ""),
             r"C:\Program Files (x86)", r"C:\Program Files"]
    return any(os.path.exists(os.path.join(r, "Microsoft", "Edge", "Application", "msedge.exe")) for r in roots if r) \
        or shutil.which("msedge") is not None


@unittest.skipUnless(_edge_available(), "Playwright or Microsoft Edge is not installed")
class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        iso = Isolated()
        self.root = iso.__enter__()
        self.addCleanup(iso.__exit__, None, None, None)
        from cerberus import config
        config.update_settings({"browser_headless": True})
        self.db = Database(self.root / "b.db")
        self.addCleanup(self.db.close)
        self.gate = SafetyGate()
        self.gate.set_autonomous(True)
        self.ctx = ToolContext(gate=self.gate, db=self.db)
        self.addCleanup(browser.shutdown)

    def test_public_guard_blocks_loopback(self):
        out = run_tool("browser_open", {"url": self.url}, self.ctx)
        self.assertIn("private network", out)

    def test_open_read_fill_click_follow(self):
        with mock.patch.object(browser, "check_public_url", lambda url: None):
            page = run_tool("browser_open", {"url": self.url}, self.ctx)
        self.assertIn("Welcome, traveller", page)
        self.assertIn("Gate Test", page)
        field = next(l for l in page.splitlines() if "Your name" in l)
        button = next(l for l in page.splitlines() if '"Greet"' in l)
        self.assertTrue(any("Second page" in l for l in page.splitlines()))
        num = lambda l: int(l[1:l.index("]")])
        self.assertIn("Filled", run_tool("browser_fill", {"index": num(field), "text": "Alex"}, self.ctx))
        after = run_tool("browser_click", {"index": num(button)}, self.ctx)
        self.assertIn("Hello Alex", after)
        moved = run_tool("browser_click", {"text": "Second page"}, self.ctx)
        self.assertIn("You arrived", moved)
        self.assertIn("/second", moved)
        bad = run_tool("browser_click", {"index": 99}, self.ctx)
        self.assertIn("No element [99]", bad)
        shot = run_tool("browser_screenshot", {}, self.ctx)
        self.assertIn("Saved", shot)

    def test_tools_need_permission_and_stay_local(self):
        remote = ToolContext(gate=SafetyGate(), db=self.db, origin="remote")
        self.assertIn("blocked", run_tool("browser_read", {}, remote))


if __name__ == "__main__":
    unittest.main()
