import json
import os
import random
import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from cerberus import dossier, learning
from cerberus.core import Cerberus
from cerberus.server import create_app

from .helpers import FakeOllama, Isolated


def at(wday: int, hour: int) -> time.struct_time:
    """A struct_time on a given weekday (Mon=0) and hour."""
    return time.struct_time((2026, 1, 5 + wday, hour, 30, 0, wday, 5 + wday, 0))


class DossierTests(unittest.TestCase):
    def setUp(self):
        iso = Isolated()
        self.root = iso.__enter__()
        self.addCleanup(iso.__exit__, None, None, None)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("CERBERUS_PROFILE", None)
        self.core = Cerberus(self.root / "d.db")
        self.core.ollama = self.fake = FakeOllama()
        self.addCleanup(self.core.db.close)

    def test_seeded_on_first_launch(self):
        keys = {f["key"] for f in learning.list_facts(self.core.db)}
        self.assertTrue({"name", "current_job", "trait_blunt", "rhythm_blackout"} <= keys)
        self.assertIn("Alex Example", self.core.db.kv_get("dossier"))
        self.assertEqual(self.core.settings()["user_name"], "Alex Example")
        self.assertEqual(self.core.db.kv_get("seed_version"), dossier.SEED_VERSION)

    def test_seed_is_idempotent_and_respects_edits(self):
        learning.upsert_fact(self.core.db, "current_job", "Systems Administrator", "work", 1.0, "user")
        self.assertFalse(dossier.seed(self.core.db))
        self.core.db.kv_set("seed_version", 0)
        self.assertTrue(dossier.seed(self.core.db))
        job = self.core.db.one("SELECT value FROM facts WHERE key='current_job'")["value"]
        self.assertEqual(job, "Systems Administrator")
        self.assertEqual(self.core.db.one("SELECT COUNT(*) AS n FROM facts WHERE key='name'")["n"], 1)

    def test_profile_summary_carries_the_dossier(self):
        text = learning.profile_summary(self.core.db, name="Alex Example")
        self.assertIn("systems administrator", text.lower())
        self.assertIn("blunt", text)

    def test_blackout_hours(self):
        self.assertTrue(dossier.is_blackout(at(1, 23)))    # Tuesday 11 PM
        self.assertTrue(dossier.is_blackout(at(2, 2)))     # Wednesday 2 AM
        self.assertTrue(dossier.is_blackout(at(6, 22)))    # Sunday 10 PM (workday next)
        self.assertTrue(dossier.is_blackout(at(4, 4)))     # Friday 4 AM
        self.assertFalse(dossier.is_blackout(at(4, 23)))   # Friday night: weekend ahead
        self.assertFalse(dossier.is_blackout(at(5, 3)))    # Saturday 3 AM
        self.assertFalse(dossier.is_blackout(at(2, 14)))   # mid-afternoon
        self.assertFalse(dossier.is_blackout(at(2, 5)))    # 05:30 is fair game

    def test_voice_block_titles_and_blackout(self):
        day = dossier.voice_block(at(2, 14))
        self.assertTrue(any(h in day for h in dossier.honorifics()))
        self.assertIn("Alex", day)
        self.assertNotIn("BLACKOUT", day)
        self.assertIn("put the damn phone down", dossier.voice_block(at(2, 23)))

    def test_only_the_final_head_gets_the_voice(self):
        self.core.update_settings({"learning_enabled": False})
        list(self.core.orchestrator.run("hello there my friend", "conversation", {"deep": True}))
        systems = [msgs[0]["content"] for _, msgs in self.fake.calls[:3]]
        self.assertEqual(["HOW TO TALK" in s for s in systems], [False, False, True])
        self.assertTrue(all("systems administrator" in (msgs[-1]["content"] + msgs[0]["content"]).lower()
                            for _, msgs in self.fake.calls[:3]))  # every head still knows who they are

    def test_voice_block_is_deterministic_with_a_seeded_rng(self):
        a = dossier.voice_block(at(2, 14), random.Random(7))
        b = dossier.voice_block(at(2, 14), random.Random(7))
        self.assertEqual(a, b)

    def test_profile_in_the_data_folder_replaces_the_example(self):
        from cerberus import config
        (config.DATA_DIR / "profile.json").write_text(json.dumps(
            {"name": "Pat Doe", "honorifics": ["Madam"], "facts": [["name", "Pat Doe", "identity"]]}), encoding="utf-8")
        self.assertEqual(dossier.plain_name(), "Pat")
        self.assertEqual(dossier.honorifics(), ["Madam"])
        self.assertIn("Madam", dossier.voice_block(at(2, 14)))

    def test_env_override_wins_and_a_broken_file_falls_back_to_the_example(self):
        good = self.root / "mine.json"
        good.write_text(json.dumps({"name": "Sam Env", "honorifics": ["Captain Env"]}), encoding="utf-8")
        os.environ["CERBERUS_PROFILE"] = str(good)
        self.assertEqual(dossier.plain_name(), "Sam")
        bad = self.root / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        os.environ["CERBERUS_PROFILE"] = str(bad)
        self.assertEqual(dossier.plain_name(), "Alex")
        self.assertIn("Boss", dossier.honorifics())

    def test_forget_keeps_the_dossier_but_erases_the_rest(self):
        app = create_app(self.core)
        c = TestClient(app, base_url="http://127.0.0.1:8777", client=("127.0.0.1", 5000))
        with c:
            c.post("/api/profile/facts", json={"key": "snack", "value": "jerky"})
            c.post("/api/profile/forget")
            keys = {f["key"] for f in c.get("/api/profile").json()["facts"]}
        self.assertNotIn("snack", keys)
        self.assertIn("name", keys)
        self.assertIsNotNone(self.core.db.kv_get("dossier"))


if __name__ == "__main__":
    unittest.main()
