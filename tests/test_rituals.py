import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from cerberus import dossier, rituals
from cerberus.core import Cerberus
from cerberus.server import create_app

from .helpers import FakeOllama, Isolated


class RitualTests(unittest.TestCase):
    def setUp(self):
        iso = Isolated()
        self.root = iso.__enter__()
        self.addCleanup(iso.__exit__, None, None, None)
        self.core = Cerberus(self.root / "r.db")
        self.core.ollama = self.fake = FakeOllama("Boss, up. The forge waits.")
        self.addCleanup(self.core.db.close)
        self.addCleanup(self.core.agents.stop)
        self.r = self.core.rituals

    def test_schedule_has_the_dossier_rhythm(self):
        got = {r["id"]: r["time"] for r in self.r.list()}
        self.assertEqual(got, {"morning_forge": "06:00", "mind_soul": "17:00", "reading_hour": "20:00",
                               "blackout_warning": "21:45", "blackout_begin": "22:00"})

    def test_start_registers_jobs_and_agents_resync_keeps_them(self):
        self.r.start()
        ids = {j.id for j in self.core.agents.scheduler.get_jobs()}
        self.assertTrue({f"ritual-{r.id}" for r in rituals.RITUALS} <= ids)
        self.core.agents.sync_jobs()                              # re-syncing agents must not wipe the rituals
        self.assertIn("ritual-morning_forge", {j.id for j in self.core.agents.scheduler.get_jobs()})
        self.assertTrue(all(r["next_run"] for r in self.r.list()))

    def test_disabling_a_ritual_removes_its_job(self):
        self.r.start()
        self.core.update_settings({"rituals": {"enabled": True, "off": ["reading_hour"], "use_model": True}}, replace=("rituals",))
        self.r.sync()
        ids = {j.id for j in self.core.agents.scheduler.get_jobs()}
        self.assertNotIn("ritual-reading_hour", ids)
        self.assertIn("ritual-mind_soul", ids)
        self.assertFalse({x["id"]: x for x in self.r.list()}["reading_hour"]["enabled"])

    def test_model_words_the_big_rituals(self):
        n = self.r.fire("mind_soul")
        self.assertEqual(n["body"], "Boss, up. The forge waits.")
        self.assertEqual(self.core.db.notifications(unread_only=True)[0]["title"], "Mind & Soul Pass")

    def test_template_when_ollama_is_down(self):
        self.fake.is_up = lambda: False
        n = self.r.fire("morning_forge")
        self.assertIn("Morning Forge", n["body"])
        self.assertIn("06:00", n["body"])

    def test_blackout_never_uses_the_model_and_is_plain(self):
        n = self.r.fire("blackout_begin")
        self.assertEqual(self.fake.calls, [])
        self.assertTrue(n["body"].startswith(dossier.plain_name()))
        self.assertIn("sleep", n["body"])
        w = self.r.fire("blackout_warning")
        self.assertIn("Fifteen minutes", w["body"])

    def test_fires_once_a_day_unless_forced(self):
        self.assertIsNotNone(self.r.fire("reading_hour"))
        self.assertIsNone(self.r.fire("reading_hour"))
        self.assertIsNotNone(self.r.fire("reading_hour", force=True))
        self.assertEqual(len(self.core.db.notifications()), 2)

    def test_morning_briefing_carries_markets_and_agent_reports(self):
        self.fake.is_up = lambda: False
        self.core.db.execute("INSERT INTO watchlist(symbol,added) VALUES('NVDA',0)")
        agent = self.core.agents.create("Scout", "x", "y", [], "t", "manual")
        self.core.db.execute("UPDATE agents SET last_run=?, last_status='ok', last_output=? WHERE id=?",
                             (time.time(), "> web_search\nREPORT:\nThree sysadmin roles in Springfield.", agent["id"]))
        with mock.patch("cerberus.tools.finance.fetch_quote", return_value={"symbol": "NVDA", "price": 120.5, "day_change_pct": 2.5}):
            body = self.r.fire("morning_forge")["body"]
        self.assertIn("NVDA 120.5 (+2.5%)", body)
        self.assertIn("Scout reports: Three sysadmin roles", body)

    def test_broken_model_falls_back_to_template(self):
        self.fake.chat = mock.Mock(side_effect=RuntimeError("boom"))
        n = self.r.fire("mind_soul")
        self.assertIn("Mind & Soul Pass", n["body"])

    def test_notification_read_state(self):
        a = self.core.db.add_notification("x", "A", "one")
        b = self.core.db.add_notification("x", "B", "two")
        self.core.db.mark_notifications_read([a])
        self.assertEqual([n["id"] for n in self.core.db.notifications(unread_only=True)], [b])
        self.core.db.mark_notifications_read()
        self.assertEqual(self.core.db.notifications(unread_only=True), [])


class RitualApiTests(unittest.TestCase):
    def setUp(self):
        iso = Isolated()
        self.root = iso.__enter__()
        self.addCleanup(iso.__exit__, None, None, None)
        self.core = Cerberus(self.root / "a.db")
        self.core.ollama = FakeOllama("Boss, rise.")
        self.app = create_app(self.core)
        self.local = TestClient(self.app, base_url="http://127.0.0.1:8777", client=("127.0.0.1", 5000))
        self.remote = TestClient(self.app, base_url="http://192.168.1.50:8777", client=("192.168.1.99", 5000))
        self.local.__enter__()
        self.addCleanup(self.local.__exit__, None, None, None)
        self.addCleanup(self.core.db.close)

    def test_fire_list_and_read(self):
        self.assertEqual(len(self.local.get("/api/rituals").json()), 5)
        fired = self.local.post("/api/rituals/mind_soul/fire").json()
        self.assertEqual(fired["body"], "Boss, rise.")
        unread = self.local.get("/api/notifications", params={"unread": True}).json()
        self.assertEqual(len(unread), 1)
        self.local.post("/api/notifications/read", json={"ids": [unread[0]["id"]]})
        self.assertEqual(self.local.get("/api/notifications", params={"unread": True}).json(), [])
        self.assertEqual(len(self.local.get("/api/notifications").json()), 1)
        self.assertEqual(self.local.post("/api/rituals/bogus/fire").status_code, 404)

    def test_phone_can_read_but_not_fire_or_configure(self):
        h = {"X-Cerberus-Token": self.core.settings()["api_token"]}
        self.assertEqual(self.remote.get("/api/notifications", headers=h).status_code, 200)
        self.assertEqual(self.remote.post("/api/rituals/mind_soul/fire", headers=h).status_code, 403)
        self.assertEqual(self.remote.put("/api/settings", json={"rituals": {"enabled": False}}, headers=h).status_code, 403)

    def test_phone_cursor_does_not_consume_the_desktop_queue(self):
        a = self.core.db.add_notification("x", "A", "one")
        b = self.core.db.add_notification("x", "B", "two")
        h = {"X-Cerberus-Token": self.core.settings()["api_token"]}
        got = self.remote.get("/api/notifications", params={"after": a}, headers=h).json()
        self.assertEqual([n["id"] for n in got], [b])
        self.assertEqual(len(self.local.get("/api/notifications", params={"unread": True}).json()), 2)   # still unread for the PC

    def test_pwa_shell_is_installable_and_public(self):
        m = self.remote.get("/manifest.webmanifest")                       # no token: browsers fetch it anonymously
        self.assertEqual(m.status_code, 200)
        self.assertEqual(m.json()["display"], "standalone")
        self.assertIn("service", self.remote.get("/sw.js").text.lower())
        for size in (192, 512):
            r = self.remote.get(f"/icon-{size}.png")
            self.assertEqual((r.status_code, r.headers["content-type"]), (200, "image/png"))
        self.assertEqual(self.remote.get("/icon-64.png").status_code, 404)
        self.assertIn('rel="manifest"', self.remote.get("/").text)

    def test_settings_silence_a_ritual(self):
        r = self.local.put("/api/settings", json={"rituals": {"enabled": True, "off": ["reading_hour", "nonsense"]}})
        self.assertEqual(r.json()["rituals"]["off"], ["reading_hour"])
        by_id = {x["id"]: x for x in self.local.get("/api/rituals").json()}
        self.assertFalse(by_id["reading_hour"]["enabled"])
        self.assertTrue(by_id["morning_forge"]["enabled"])


if __name__ == "__main__":
    unittest.main()
