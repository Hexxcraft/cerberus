import threading
import unittest

from cerberus import cloud
from cerberus.agent_runtime import extract_action
from cerberus.agents import parse_schedule
from cerberus.db import Database
from cerberus.ollama_client import ThinkSplitter, strip_think
from cerberus.orchestrator import parse_files, validate_files
from cerberus.safety import Denied, SafetyGate, safe_path
from cerberus.tools import ToolContext, run_tool
from cerberus.tools.web import check_public_url

from .helpers import Isolated


class SandboxTests(unittest.TestCase):
    def test_paths_cannot_escape(self):
        with Isolated() as root:
            ok = safe_path("proj/a.txt")
            self.assertTrue(str(ok).startswith(str((root / "workspace").resolve())))
            for bad in ("../x", "..\\..\\windows\\win.ini", "C:\\Windows\\win.ini", "/etc/passwd"):
                with self.assertRaises(Denied):
                    safe_path(bad)

    def test_ssrf_guard(self):
        for url in ("http://127.0.0.1/", "http://localhost:11434", "http://192.168.1.1/", "http://169.254.169.254/", "file:///etc/passwd"):
            with self.assertRaises(Exception, msg=url):
                check_public_url(url)


class GateTests(unittest.TestCase):
    def make(self):
        iso = Isolated()
        root = iso.__enter__()
        self.addCleanup(iso.__exit__, None, None, None)
        db = Database(root / "t.db")
        self.addCleanup(db.close)
        return SafetyGate(), db

    def test_write_tool_needs_approval_and_denies_by_default_on_seal(self):
        gate, db = self.make()
        ctx = ToolContext(gate=gate, db=db)
        out = {}
        t = threading.Thread(target=lambda: out.update(r=run_tool("write_file", {"path": "a.txt", "content": "hi"}, ctx)))
        t.start()
        for _ in range(50):
            if gate.pending():
                break
            threading.Event().wait(0.05)
        self.assertEqual(len(gate.pending()), 1)
        gate.seal()
        t.join(5)
        self.assertTrue(out["r"].startswith("DENIED"))

    def test_approval_runs_the_tool(self):
        gate, db = self.make()
        ctx = ToolContext(gate=gate, db=db)
        out = {}
        t = threading.Thread(target=lambda: out.update(r=run_tool("write_file", {"path": "a.txt", "content": "hi"}, ctx)))
        t.start()
        for _ in range(50):
            if gate.pending():
                break
            threading.Event().wait(0.05)
        gate.decide(gate.pending()[0]["id"], True)
        t.join(5)
        self.assertNotIn("DENIED", out["r"])
        self.assertEqual(run_tool("read_file", {"path": "a.txt"}, ctx).strip(), "hi")

    def test_autonomous_skips_prompt_and_seal_turns_it_off(self):
        gate, db = self.make()
        gate.set_autonomous(True)
        ctx = ToolContext(gate=gate, db=db)
        self.assertNotIn("DENIED", run_tool("write_file", {"path": "b.txt", "content": "x"}, ctx))
        gate.seal()
        self.assertFalse(gate.autonomous)

    def test_remote_origin_cannot_use_desktop_tools(self):
        gate, db = self.make()
        gate.set_autonomous(True)
        r = run_tool("click", {"x": 1, "y": 1}, ToolContext(gate=gate, db=db, origin="remote"))
        self.assertIn("only works from the computer", r)

    def test_unknown_tool_and_bad_args_never_raise(self):
        gate, db = self.make()
        ctx = ToolContext(gate=gate, db=db)
        self.assertTrue(run_tool("nope", {}, ctx).startswith("ERROR"))
        self.assertTrue(run_tool("read_file", {"wrong": 1}, ctx).startswith("ERROR"))


class TextTests(unittest.TestCase):
    def test_think_splitter_across_chunks(self):
        sp = ThinkSplitter()
        events = []
        for chunk in ["<thi", "nk>pond", "ering</th", "ink>Hello ", "world"]:
            events += sp.feed(chunk)
        events += sp.flush()
        thinking = "".join(t for k, t in events if k == "thinking")
        content = "".join(t for k, t in events if k == "content")
        self.assertEqual(thinking, "pondering")
        self.assertEqual(content.strip(), "Hello world")

    def test_strip_think(self):
        self.assertEqual(strip_think("<think>x</think>Answer").strip(), "Answer")

    def test_extract_action(self):
        self.assertEqual(extract_action('Sure!\n```json\n{"tool":"web_search","args":{"query":"a {b}"}}\n```')["tool"], "web_search")
        self.assertIsNone(extract_action("plain text with {braces} only"))
        self.assertIsNone(extract_action('{"not_a_tool": 1}'))

    def test_schedule_parser(self):
        self.assertEqual(parse_schedule("manual")["type"], "manual")
        self.assertEqual(parse_schedule("every 30 minutes"), {"type": "interval", "minutes": 30})
        self.assertEqual(parse_schedule("every 2 hours")["minutes"], 120)
        self.assertEqual(parse_schedule("hourly")["minutes"], 60)
        d = parse_schedule("daily at 9pm")
        self.assertEqual((d["hour"], d["minute"], d["days"]), (21, 0, None))
        w = parse_schedule("weekdays at 08:30")
        self.assertEqual((w["days"], w["hour"], w["minute"]), ("mon-fri", 8, 30))
        self.assertEqual(parse_schedule("weekly mon,fri 17:00")["days"], "mon,fri")
        self.assertEqual(parse_schedule("every monday at 10am")["days"], "mon")
        self.assertEqual(parse_schedule("12am daily")["hour"], 0)
        for bad in ("whenever", "daily at 25:00"):
            with self.assertRaises(ValueError):
                parse_schedule(bad)


class CloudTests(unittest.TestCase):
    def test_compress_keeps_code_and_drops_filler(self):
        text = "Sure!\n**Bold** point\nI hope this helps!\n```py\nx = 1\n\n\n\ny = 2\n```"
        out = cloud.compress_text(text)
        self.assertNotIn("Sure", out)
        self.assertNotIn("hope this helps", out)
        self.assertIn("x = 1", out)
        self.assertNotIn("**", out)

    def test_handoff_fits_budget(self):
        stages = [("Analyst", "fact. " * 3000), ("Advisor", "advice. " * 3000), ("Skeptic", "draft. " * 3000)]
        packet = cloud.build_handoff("what should I do?" * 50, "Wealth", "brief", stages, 800)
        self.assertLess(packet["sent_tokens"], 1300)
        self.assertGreater(packet["original_tokens"], 5000)
        self.assertIn("LOCAL DRAFT", packet["prompt"])


class CodingTests(unittest.TestCase):
    SAMPLE = ("## Plan\nstuff\n\n### FILE: app/main.py\n```python\nprint('hi')\n```\n\n"
              "### FILE: `README.md`\n```markdown\n# Title\n```\n\n## Notes\nRun it.")

    def test_parse_files(self):
        files, notes = parse_files(self.SAMPLE)
        self.assertEqual([f["path"] for f in files], ["app/main.py", "README.md"])
        self.assertEqual(files[0]["content"], "print('hi')\n")
        self.assertIn("Run it", notes)

    def test_parse_files_tolerates_bold_headers(self):
        text = "**FILE: tipcalc.py**\n```python\nx = 1\n```\n\nFILE: `README.md`\n```markdown\n# T\n```\nDone."
        files, notes = parse_files(text)
        self.assertEqual([f["path"] for f in files], ["tipcalc.py", "README.md"])
        self.assertIn("Done", notes)

    def test_nested_fences_survive(self):
        text = "### FILE: README.md\n````markdown\n# T\n```bash\npip install x\n```\n````\n"
        files, _ = parse_files(text)
        self.assertIn("```bash", files[0]["content"])

    def test_validate_catches_syntax_and_placeholders(self):
        problems = validate_files([{"path": "a.py", "content": "def f(:\n"}, {"path": "b.py", "content": "# TODO implement me\n"},
                                   {"path": "c.json", "content": "{bad"}])
        joined = " ".join(problems)
        self.assertIn("a.py", joined)
        self.assertIn("b.py", joined)
        self.assertIn("c.json", joined)
        self.assertEqual(validate_files([{"path": "ok.py", "content": "x = 1\n"}]), [])


if __name__ == "__main__":
    unittest.main()
