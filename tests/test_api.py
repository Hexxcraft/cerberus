import json
import unittest

from fastapi.testclient import TestClient

from cerberus.core import Cerberus
from cerberus.server import create_app

from .helpers import FakeOllama, Isolated


def events(resp) -> list[dict]:
    return [json.loads(ln) for ln in resp.text.splitlines() if ln.strip()]


class ApiTests(unittest.TestCase):
    def setUp(self):
        iso = Isolated()
        self.root = iso.__enter__()
        self.addCleanup(iso.__exit__, None, None, None)
        self.core = Cerberus(self.root / "t.db")
        self.core.ollama = self.fake = FakeOllama()
        self.app = create_app(self.core)
        self.local = TestClient(self.app, base_url="http://127.0.0.1:8777", client=("127.0.0.1", 5000))
        self.remote = TestClient(self.app, base_url="http://192.168.1.50:8777", client=("192.168.1.99", 5000))
        self.local.__enter__()
        self.addCleanup(self.local.__exit__, None, None, None)
        self.addCleanup(self.core.db.close)

    # -- auth
    def test_ping_is_public_but_api_needs_token_remotely(self):
        self.assertEqual(self.remote.get("/api/ping").status_code, 200)
        self.assertEqual(self.remote.get("/api/status").status_code, 401)
        token = self.core.settings()["api_token"]
        self.assertEqual(self.remote.get("/api/status", headers={"Authorization": f"Bearer {token}"}).status_code, 200)
        self.assertEqual(self.remote.get("/api/status", headers={"Authorization": "Bearer nope"}).status_code, 401)

    def test_browser_tricks_blocked_locally(self):
        self.assertEqual(self.local.get("/api/status", headers={"Origin": "http://evil.example"}).status_code, 403)
        self.assertEqual(self.local.get("/api/status", headers={"Host": "evil.example"}).status_code, 403)
        self.assertEqual(self.local.get("/api/status").status_code, 200)

    def test_remote_cannot_change_settings_agents_or_autonomy(self):
        h = {"X-Cerberus-Token": self.core.settings()["api_token"]}
        self.assertEqual(self.remote.put("/api/settings", json={"user_name": "x"}, headers=h).status_code, 403)
        self.assertEqual(self.remote.post("/api/safety/autonomous", json={"enabled": True}, headers=h).status_code, 403)
        self.assertEqual(self.remote.post("/api/agents", json={"name": "x"}, headers=h).status_code, 403)
        self.assertEqual(self.remote.post("/api/safety/seal", headers=h).status_code, 200)

    def test_secrets_never_leave_in_settings(self):
        self.local.put("/api/settings", json={"cloud": {"providers": {"openai": {"api_key": "sk-supersecret1234"}}},
                                              "github": {"token": "ghp_secret0000"}})
        body = self.local.get("/api/settings").text
        self.assertNotIn("supersecret", body)
        self.assertNotIn("ghp_secret", body)
        self.assertNotIn(self.core.settings()["api_token"], body)
        self.assertTrue(self.local.get("/api/settings").json()["cloud"]["providers"]["openai"]["api_key_set"])

    def test_settings_validation(self):
        self.assertEqual(self.local.put("/api/settings", json={"mode": "bogus"}).status_code, 400)
        self.assertEqual(self.local.put("/api/settings", json={"cloud": {"provider": "bogus"}}).status_code, 400)
        r = self.local.put("/api/settings", json={"api_token": "hijack", "user_name": "Ada"})
        self.assertEqual(r.json()["user_name"], "Ada")
        self.assertNotEqual(self.core.settings()["api_token"], "hijack")

    # -- chat
    def test_every_mode_streams_and_ends_with_final_and_done(self):
        for mode in self.local.get("/api/modes").json():
            evs = events(self.local.post("/api/chat", json={"message": "Hello there, tell me something", "mode": mode["key"],
                                                             "options": {"deep": True}}))
            types = [e["type"] for e in evs]
            self.assertEqual(types[0], "run", mode["key"])
            self.assertIn("final", types, mode["key"])
            self.assertEqual(types[-1], "done", mode["key"])
            self.assertEqual(len([e for e in evs if e["type"] == "stage_start"]) >= 3, True, mode["key"])

    def test_thinking_levels_choose_one_head_or_three(self):
        self.assertEqual(self.core.settings()["thinking"], "careful")          # accuracy over speed by default
        careful = events(self.local.post("/api/chat", json={"message": "hi", "mode": "conversation"}))
        self.assertEqual(len([e for e in careful if e["type"] == "stage_start"]), 3)
        self.assertEqual(self.local.put("/api/settings", json={"thinking": "quick"}).status_code, 200)
        self.fake.calls.clear()
        fast = events(self.local.post("/api/chat", json={"message": "hi", "mode": "conversation"}))
        self.assertEqual([e["key"] for e in fast if e["type"] == "stage_start"], ["ego"])
        self.assertEqual(len(self.fake.calls), 1)
        deep = events(self.local.post("/api/chat", json={"message": "Why is the sky blue?", "mode": "conversation"}))
        self.assertEqual(len([e for e in deep if e["type"] == "stage_start"]), 3)
        self.assertEqual(self.local.put("/api/settings", json={"thinking": "bogus"}).status_code, 400)

    def test_thinking_can_be_chosen_per_request(self):
        self.assertEqual(self.core.settings()["thinking"], "careful")
        one = events(self.local.post("/api/chat", json={"message": "hi", "mode": "conversation", "options": {"thinking": "quick"}}))
        self.assertEqual(one[1]["thinking"], "quick")
        self.assertEqual([e["key"] for e in one if e["type"] == "stage_start"], ["ego"])
        self.assertEqual(self.core.settings()["thinking"], "careful")          # the saved default is untouched
        three = events(self.local.post("/api/chat", json={"message": "hi", "mode": "conversation", "options": {"thinking": "meticulous"}}))
        self.assertEqual(three[1]["thinking"], "meticulous")

    def test_per_mode_model_override(self):
        self.fake.installed = ["deepseek-r1:latest", "llama3:latest", "mistral:latest", "qwen2.5-coder:7b"]
        r = self.local.put("/api/settings", json={"mode_models": {"coding": {"generalist": "qwen2.5-coder"}}})
        self.assertEqual(r.status_code, 200, r.text)
        evs = events(self.local.post("/api/chat", json={"message": "make a thing", "mode": "coding"}))
        by_key = {e["key"]: e["model"] for e in evs if e["type"] == "stage_start"}
        self.assertEqual(by_key["builder"], "qwen2.5-coder:7b")
        self.assertEqual(by_key["architect"], "deepseek-r1:latest")
        self.assertEqual(self.local.put("/api/settings", json={"mode_models": {"nope": {}}}).status_code, 400)
        self.local.put("/api/settings", json={"mode_models": {}})        # an empty object clears the overrides
        self.assertEqual(self.core.settings()["mode_models"], {})

    def test_three_heads_use_three_models(self):
        evs = events(self.local.post("/api/chat", json={"message": "hi", "mode": "conversation", "options": {"deep": True}}))
        models = {e["model"] for e in evs if e["type"] == "stage_start"}
        self.assertEqual(models, {"deepseek-r1:latest", "llama3:latest", "mistral:latest"})

    def test_history_and_learning(self):
        self.local.post("/api/chat", json={"message": "I am a night owl and I love synthwave music", "mode": "conversation"})
        hist = self.local.get("/api/history?mode=conversation").json()
        self.assertEqual([m["role"] for m in hist], ["user", "assistant"])
        self.assertEqual(self.local.get("/api/profile").json()["bond"]["interactions"], 1)
        self.local.post("/api/profile/facts", json={"key": "favorite color", "value": "black"})
        keys = [f["key"] for f in self.local.get("/api/profile").json()["facts"]]
        self.assertIn("favorite_color", keys)
        self.assertEqual(self.local.post("/api/profile/forget").status_code, 200)
        facts = self.local.get("/api/profile").json()["facts"]
        self.assertNotIn("favorite_color", [f["key"] for f in facts])
        self.assertTrue(facts and all(f["source"] == "dossier" for f in facts))

    def test_unknown_mode_and_empty_message(self):
        self.assertEqual(events(self.local.post("/api/chat", json={"message": "x", "mode": "zzz"}))[-1]["type"], "done")
        evs = events(self.local.post("/api/chat", json={"message": "  ", "mode": "coding"}))
        self.assertIn("error", [e["type"] for e in evs])

    def test_coding_mode_extracts_and_saves_files(self):
        self.fake.reply = "### FILE: hello/app.py\n```python\nprint('hello')\n```\n\n## Notes\nRun with python app.py"
        h = {}
        self.core.gate.set_autonomous(True)
        evs = events(self.local.post("/api/chat", json={"message": "make hello app", "mode": "coding",
                                                         "options": {"save_files": True, "project": "hello"}}, headers=h))
        files_ev = next(e for e in evs if e["type"] == "files")
        self.assertEqual(files_ev["files"][0]["path"], "hello/app.py")
        self.assertTrue((self.root / "workspace" / "hello" / "hello" / "app.py").exists())

    def test_cloud_handoff_failure_falls_back_to_local(self):
        self.local.put("/api/settings", json={"cloud": {"enabled": True, "provider": "openai"}})
        evs = events(self.local.post("/api/chat", json={"message": "hello", "mode": "conversation"}))
        types = [e["type"] for e in evs]
        self.assertIn("cloud_error", types)
        self.assertEqual(next(e for e in evs if e["type"] == "final")["source"], "local")

    # -- agents
    def test_agent_lifecycle(self):
        agents = self.local.get("/api/agents").json()
        self.assertGreaterEqual(len(agents), 3)
        self.assertFalse(any(a["active"] for a in agents))
        r = self.local.post("/api/agents", json={"name": "Scribe", "role": "Notes", "backstory": "quiet",
                                                 "skills": ["File Reading", "Not A Skill"], "task": "list files",
                                                 "schedule": "every 30 minutes", "active": True})
        self.assertEqual(r.status_code, 200, r.text)
        agent = r.json()
        self.assertEqual(agent["skills"], ["File Reading"])
        self.assertIsNotNone(agent["next_run"])
        self.assertEqual(self.local.post("/api/agents", json={"name": "Scribe", "schedule": "manual"}).status_code, 400)
        self.assertEqual(self.local.post("/api/agents", json={"name": "Bad", "schedule": "whenever"}).status_code, 400)
        upd = self.local.put(f"/api/agents/{agent['id']}", json={"active": False}).json()
        self.assertIsNone(upd["next_run"])
        self.assertEqual(self.local.delete(f"/api/agents/{agent['id']}").status_code, 200)

    def test_agent_run_records_output(self):
        aid = next(a["id"] for a in self.local.get("/api/agents").json() if a["name"] == "Repo Keeper")
        self.core.agents.run(aid, "manual", wait=True)
        runs = self.local.get(f"/api/agents/{aid}/runs").json()
        self.assertEqual(len(runs), 1)
        self.assertIn(runs[0]["status"], ("ok", "partial"))

    # -- tools / safety / misc
    def test_tool_call_goes_through_gate(self):
        r = self.local.post("/api/tools/list_files", json={"args": {}})
        self.assertEqual(r.status_code, 200)
        r = self.local.post("/api/tools/read_file", json={"args": {"path": "../../secret"}})
        self.assertIn("DENIED", r.json()["result"])

    def test_skills_catalog_and_budget(self):
        self.assertIn("Web Searching", self.local.get("/api/skills").json())
        self.local.post("/api/budget", json={"kind": "income", "amount": 100, "category": "gig"})
        self.local.post("/api/budget", json={"kind": "expense", "amount": 30.5, "category": "food"})
        self.assertIn("net +69.50", self.local.get("/api/budget").json()["summary"])
        self.assertEqual(self.local.post("/api/budget", json={"kind": "gift", "amount": 5}).status_code, 400)

    def test_samples_and_style(self):
        text = "I really love long, winding sentences; they carry me away, you know? " * 6
        self.assertEqual(self.local.post("/api/samples", json={"kind": "writing", "title": "essay", "content": text}).status_code, 200)
        self.assertEqual(self.local.post("/api/samples", json={"kind": "writing", "content": "short"}).status_code, 400)
        r = self.local.post("/api/profile/style/rebuild").json()
        self.assertEqual(r["samples"], 1)
        self.assertIn("Average sentence length", r["text"])

    def test_web_page_served(self):
        self.assertIn("Cerberus", self.local.get("/").text)


if __name__ == "__main__":
    unittest.main()
