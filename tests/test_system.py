import json
import sys
import unittest

from fastapi.testclient import TestClient

from cerberus import config, hardware, plugins
from cerberus.core import Cerberus
from cerberus.modes import MODES
from cerberus.server import create_app
from cerberus.tools import REGISTRY, run_tool

from .helpers import FakeOllama, Isolated

APPROVE = "VOTE: APPROVE\nREASON: fine"


class HardwareTests(unittest.TestCase):
    def test_detect_reports_this_machine(self):
        hw = hardware.detect()
        for key in ("cpu", "cores", "ram_gb", "ram_free_gb", "gpu", "vram_gb", "disk_free_gb"):
            self.assertIn(key, hw)
        self.assertGreater(hw["cores"], 0)
        self.assertIn(hardware.tier(hw), ("strong", "good", "light"))
        self.assertTrue(hardware.describe(hw))

    def test_tier_boundaries(self):
        self.assertEqual(hardware.tier({"vram_gb": 16, "ram_gb": 32}), "strong")
        self.assertEqual(hardware.tier({"vram_gb": 16, "ram_gb": 16}), "good")       # big card, small memory
        self.assertEqual(hardware.tier({"vram_gb": 8, "ram_gb": 8}), "good")
        self.assertEqual(hardware.tier({"vram_gb": 0, "ram_gb": 16}), "good")
        self.assertEqual(hardware.tier({"vram_gb": 0, "ram_gb": 8}), "light")
        self.assertEqual(hardware.tier({}), "light")

    def test_recommendations_scale_with_power(self):
        strong = hardware.recommend({"vram_gb": 16, "ram_gb": 32})
        light = hardware.recommend({"vram_gb": 0, "ram_gb": 4})
        self.assertGreater(strong["num_ctx"], light["num_ctx"])
        self.assertTrue(strong["upgrades"] and light["upgrades"])

    def test_pressure_shape(self):
        p = hardware.pressure()
        self.assertEqual(set(p), {"free_gb", "low"})


class PluginTests(unittest.TestCase):
    def setUp(self):
        iso = Isolated()
        self.root = iso.__enter__()
        self.addCleanup(iso.__exit__, None, None, None)
        self.before = set(REGISTRY)
        self.addCleanup(self.cleanup_registry)

    def cleanup_registry(self):
        for name in set(REGISTRY) - self.before:
            REGISTRY.pop(name, None)
        for mod in [m for m in sys.modules if m.startswith("cerberus_plugin_")]:
            sys.modules.pop(mod, None)
        plugins.LOADED.clear()

    def test_good_plugin_loads_and_runs_bad_one_is_skipped_underscore_ignored(self):
        folder = plugins.plugins_dir()
        (folder / "good.py").write_text(
            "from cerberus.tools import ToolContext, tool\n"
            "@tool('plug_double', 'Doubler', 'double a number', {'n': 'number'}, risk='safe')\n"
            "def plug_double(ctx: ToolContext, n: int = 1) -> str:\n    return str(int(n) * 2)\n")
        (folder / "broken.py").write_text(
            "from cerberus.tools import tool\n"
            "@tool('plug_half', 'Broken', 'half registered', {}, risk='safe')\n"
            "def plug_half(ctx): return 'x'\n"
            "raise RuntimeError('oops')\n")
        (folder / "_ignored.py").write_text("raise SystemExit('must never be imported')\n")
        result = {e["file"]: e for e in plugins.load_plugins()}
        self.assertTrue(result["good.py"]["ok"])
        self.assertEqual(result["good.py"]["tools"], ["plug_double"])
        self.assertFalse(result["broken.py"]["ok"])
        self.assertIn("oops", result["broken.py"]["error"])
        self.assertNotIn("plug_half", REGISTRY)                      # half-registered tools are removed
        self.assertNotIn("_ignored.py", result)
        self.assertIn("Doubler", plugins.skills())
        core = Cerberus(self.root / "t.db")
        self.addCleanup(core.db.close)
        self.assertEqual(run_tool("plug_double", {"n": 21}, core.tool_ctx()), "42")

    def test_template_is_copied_but_never_loaded(self):
        result = plugins.load_plugins()
        self.assertTrue((plugins.plugins_dir() / "_template.py").exists())
        self.assertEqual(result, [])
        self.assertNotIn("hello_plugin", REGISTRY)


class ProjectTests(unittest.TestCase):
    def setUp(self):
        iso = Isolated()
        self.root = iso.__enter__()
        self.addCleanup(iso.__exit__, None, None, None)
        self.core = Cerberus(self.root / "t.db")
        self.core.ollama = self.fake = FakeOllama()
        self.fake.reply_fn = lambda m, msgs: APPROVE
        self.core.gate.autonomous = True
        self.addCleanup(self.core.db.close)
        self.proj = self.root / "proj"
        self.proj.mkdir()
        (self.proj / "hello.py").write_text("print('hi from project')\n")
        config.update_settings({"projects": {"Demo": {"path": str(self.proj), "run": f'"{sys.executable}" hello.py',
                                                       "about": "demo"}}}, replace=("projects",))
        self.ctx = self.core.tool_ctx("local", "test")

    def test_registered_project_runs_with_the_saved_command(self):
        self.assertIn("Demo", run_tool("project_list", {}, self.ctx))
        out = run_tool("project_run", {"name": "demo"}, self.ctx)
        self.assertIn("exit code 0", out)
        self.assertIn("hi from project", out)
        self.assertEqual(len(self.fake.calls), 3)                    # all three heads approved it first

    def test_unregistered_or_broken_projects_are_denied_without_asking_the_heads(self):
        self.assertIn("DENIED", run_tool("project_run", {"name": "Nope"}, self.ctx))
        config.update_settings({"projects": {"Gone": {"path": str(self.root / "missing"), "run": "python x.py"}}},
                               replace=("projects",))
        self.assertIn("DENIED", run_tool("project_run", {"name": "Gone"}, self.ctx))
        self.assertEqual(self.fake.calls, [])

    def test_a_head_saying_no_stops_the_run_and_remote_is_blocked(self):
        self.fake.reply_fn = lambda m, msgs: "VOTE: REJECT\nREASON: not now"
        self.assertIn("DENIED", run_tool("project_run", {"name": "Demo"}, self.ctx))
        self.assertIn("only works from the computer",
                      run_tool("project_run", {"name": "Demo"}, self.core.tool_ctx("remote", "phone")))


class SystemApiTests(unittest.TestCase):
    def setUp(self):
        iso = Isolated()
        self.root = iso.__enter__()
        self.addCleanup(iso.__exit__, None, None, None)
        self.core = Cerberus(self.root / "t.db")
        self.core.ollama = FakeOllama()
        self.app = create_app(self.core)
        self.local = TestClient(self.app, base_url="http://127.0.0.1:8777", client=("127.0.0.1", 5000))
        self.remote = TestClient(self.app, base_url="http://192.168.1.50:8777", client=("192.168.1.99", 5000))
        self.local.__enter__()
        self.addCleanup(self.local.__exit__, None, None, None)
        self.addCleanup(self.core.db.close)
        self.h = {"Authorization": f"Bearer {self.core.settings()['api_token']}"}

    def test_system_report(self):
        data = self.local.get("/api/system").json()
        self.assertEqual(data["thinking"]["current"], "careful")
        self.assertEqual([x["key"] for x in data["thinking"]["levels"]], ["quick", "careful", "meticulous"])
        self.assertIn(data["recommended"]["tier"], ("strong", "good", "light"))
        self.assertTrue(data["full_access"])
        self.assertEqual(self.remote.get("/api/system", headers=self.h).status_code, 200)

    def test_tune_is_local_only_and_applies_recommendation(self):
        self.assertEqual(self.remote.post("/api/system/tune", headers=self.h).status_code, 403)
        r = self.local.post("/api/system/tune").json()
        self.assertEqual(self.core.settings()["num_ctx"], r["applied"]["num_ctx"])
        self.assertTrue(self.core.settings()["auto_tuned"])

    def test_first_launch_tunes_itself_once(self):
        self.assertTrue(self.core.settings()["auto_tuned"])

    def test_settings_for_thinking_and_full_access(self):
        self.assertEqual(self.local.put("/api/settings", json={"full_access": False}).status_code, 200)
        self.assertFalse(self.core.settings()["full_access"])
        self.assertEqual(self.local.put("/api/settings", json={"thinking": "meticulous"}).json()["thinking"], "meticulous")
        self.assertEqual(self.local.put("/api/settings", json={"thinking": "warp"}).status_code, 400)
        self.assertEqual(self.local.put("/api/settings", json={"full_access": "maybe"}).status_code, 400)
        self.assertEqual(self.remote.put("/api/settings", json={"full_access": True}, headers=self.h).status_code, 403)

    def test_projects_endpoint_validates_and_is_local_only(self):
        folder = self.root / "p"
        folder.mkdir()
        good = {"My App": {"path": str(folder), "run": "python main.py", "about": "x"}}
        self.assertEqual(self.remote.put("/api/projects", json=good, headers=self.h).status_code, 403)
        self.assertEqual(self.local.put("/api/projects", json=good).status_code, 200)
        self.assertIn("My App", self.local.get("/api/projects").json())
        self.assertEqual(self.local.put("/api/projects", json={"x": {"path": str(folder), "run": ""}}).status_code, 400)
        self.assertEqual(self.local.put("/api/projects", json={"x": {"path": str(self.root / "no"), "run": "a"}}).status_code, 400)
        self.assertEqual(self.local.put("/api/projects", json={"bad/name": {"path": str(folder), "run": "a"}}).status_code, 400)
        self.assertEqual(self.local.put("/api/projects", json={}).status_code, 200)            # clearing works
        self.assertEqual(self.local.get("/api/projects").json(), {})

    def test_plugins_and_lessons_endpoints(self):
        self.assertIn("folder", self.local.get("/api/plugins").json())
        self.core.council.add_lessons("writing", [{"problem": "a distinct remembered mistake here", "fix": ""}])
        self.assertEqual(len(self.local.get("/api/council/lessons").json()), 1)
        self.assertEqual(self.remote.delete("/api/council/lessons", headers=self.h).status_code, 403)
        self.assertEqual(self.local.delete("/api/council/lessons").status_code, 200)
        self.assertEqual(self.local.get("/api/council/lessons").json(), [])

    def test_modes_are_just_their_fun_names(self):
        labels = {m["key"]: m["label"] for m in self.local.get("/api/modes").json()}
        self.assertEqual(set(labels), set(MODES))
        for key, label in labels.items():
            self.assertNotIn(" - ", label, key)
            self.assertNotIn("The ", label, key)
            self.assertEqual(label, label.strip())

    def test_remote_phone_cannot_use_pc_tools_through_the_api(self):
        r = self.remote.post("/api/tools/pc_list", json={"args": {"path": "C:\\"}}, headers=self.h)
        self.assertIn("only works from the computer", json.dumps(r.json()))


if __name__ == "__main__":
    unittest.main()
