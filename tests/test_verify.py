import unittest

from cerberus import verify
from cerberus.core import Cerberus

from .helpers import FakeOllama, Isolated

GOOD = {"path": "calc.py", "content": "def add(a, b):\n    return a + b\n"}
BAD = {"path": "calc.py", "content": "def add(a, b):\n    return a - b\n"}
TEST = {"path": "test_calc.py", "content": "import unittest\nfrom calc import add\n\n\nclass T(unittest.TestCase):\n"
                                           "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n\n\n"
                                           "if __name__ == '__main__':\n    unittest.main()\n"}


def block(*files):
    return "\n\n".join(f"### FILE: {f['path']}\n```python\n{f['content'].rstrip()}\n```" for f in files)


class VerifyUnitTests(unittest.TestCase):
    def test_nothing_to_run_without_tests(self):
        r = verify.run_checks([GOOD])
        self.assertFalse(r["ran"])
        self.assertTrue(r["ok"])

    def test_passing_and_failing(self):
        self.assertTrue(verify.run_checks([GOOD, TEST])["ok"])
        bad = verify.run_checks([BAD, TEST])
        self.assertTrue(bad["ran"])
        self.assertFalse(bad["ok"])
        self.assertIn("AssertionError", bad["output"])

    def test_tests_in_subfolder_are_discovered(self):
        sub = {"path": "tests/test_calc.py", "content": TEST["content"]}
        self.assertFalse(verify.run_checks([BAD, sub])["ok"])
        self.assertTrue(verify.run_checks([GOOD, sub])["ok"])

    def test_infinite_loop_times_out(self):
        loop = {"path": "test_loop.py", "content": "import unittest\n\nclass T(unittest.TestCase):\n    def test_x(self):\n        while True:\n            pass\n"}
        r = verify.run_checks([loop], timeout=2)
        self.assertFalse(r["ok"])
        self.assertIn("Timed out", r["output"])

    def test_paths_cannot_escape_the_temp_folder(self):
        evil = {"path": "../escape.py", "content": "x = 1\n"}
        verify.run_checks([GOOD, TEST, evil])
        import pathlib, tempfile
        self.assertFalse((pathlib.Path(tempfile.gettempdir()) / "escape.py").exists())


class VerifyPipelineTests(unittest.TestCase):
    def setUp(self):
        iso = Isolated()
        self.root = iso.__enter__()
        self.addCleanup(iso.__exit__, None, None, None)
        self.core = Cerberus(self.root / "v.db")
        self.core.ollama = self.fake = FakeOllama()
        self.addCleanup(self.core.db.close)
        self.core.gate.set_autonomous(True)

        def reply(model, messages):
            if messages[0]["content"].startswith("You fix code"):
                self.repairs = getattr(self, "repairs", 0) + 1
                return block(GOOD)
            return block(BAD, TEST)
        self.fake.reply_fn = reply

    def run_coding(self, **options):
        return list(self.core.orchestrator.run("write an add function with a test", "coding", {"deep": True, **options}))

    def test_failing_tests_trigger_a_repair_that_fixes_them(self):
        evs = self.run_coding(verify=True)
        self.assertEqual(getattr(self, "repairs", 0), 1)
        files_ev = next(e for e in evs if e["type"] == "files")
        self.assertTrue(files_ev["verify"]["ok"])
        self.assertEqual(files_ev["problems"], [])
        calc = next(f for f in files_ev["files"] if f["path"] == "calc.py")
        self.assertIn("a + b", calc["content"])
        self.assertIn("verify_code", [e.get("name") for e in evs if e["type"] == "tool_call"])
        self.assertIn("Verification:", next(e for e in evs if e["type"] == "final")["text"])

    def test_without_verify_tests_are_not_run(self):
        evs = self.run_coding()
        self.assertEqual(getattr(self, "repairs", 0), 0)
        self.assertNotIn("verify_code", [e.get("name") for e in evs if e["type"] == "tool_call"])

    def test_unrepairable_code_is_reported_honestly(self):
        self.fake.reply_fn = lambda m, msgs: block(BAD, TEST)
        evs = self.run_coding(verify=True)
        files_ev = next(e for e in evs if e["type"] == "files")
        self.assertFalse(files_ev["verify"]["ok"])
        self.assertIn("tests still failing", files_ev["problems"])
        self.assertIn("FAIL", next(e for e in evs if e["type"] == "final")["text"])

    def test_declined_permission_skips_the_run(self):
        self.core.gate.set_autonomous(False)
        self.core.gate.request = lambda *a, **k: False
        evs = self.run_coding(verify=True)
        files_ev = next(e for e in evs if e["type"] == "files")
        self.assertFalse(files_ev["verify"]["ran"])
        self.assertIn("declined", files_ev["verify"]["summary"])


if __name__ == "__main__":
    unittest.main()
