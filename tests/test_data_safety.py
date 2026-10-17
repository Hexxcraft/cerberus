import contextlib
import json
import sqlite3
import unittest
import zipfile
from unittest import mock

from fastapi.testclient import TestClient

from cerberus import backup, config, vault
from cerberus.core import Cerberus
from cerberus.db import Database
from cerberus.server import create_app

from .helpers import FakeOllama, Isolated

KEY = "sk-live-abcdef123456"


class VaultTests(unittest.TestCase):
    def setUp(self):
        iso = Isolated()
        self.root = iso.__enter__()
        self.addCleanup(iso.__exit__, None, None, None)

    def test_roundtrip_and_tamper(self):
        sealed = vault.protect(KEY)
        self.assertTrue(sealed.startswith(vault.PREFIX))
        self.assertNotIn(KEY, sealed)
        self.assertEqual(vault.unprotect(sealed), KEY)
        self.assertEqual(vault.unprotect(vault.PREFIX + "AAAA"), "")        # foreign/garbled -> must be re-entered
        self.assertEqual(vault.unprotect("plain"), "plain")

    def test_settings_file_never_holds_a_plain_key(self):
        config.update_settings({"github": {"token": "ghp_secret_token_1"},
                                "cloud": {"providers": {"openai": {"api_key": KEY, "model": "gpt-4o"}}}})
        raw = config.SETTINGS_FILE.read_text(encoding="utf-8")
        for secret in (KEY, "ghp_secret_token_1", config.load_settings()["api_token"]):
            self.assertNotIn(secret, raw)
        config._cache = None                                              # reload from disk
        s = config.load_settings()
        self.assertEqual(s["cloud"]["providers"]["openai"]["api_key"], KEY)
        self.assertEqual(s["github"]["token"], "ghp_secret_token_1")

    def test_old_plaintext_settings_get_sealed_on_first_load(self):
        config.SETTINGS_FILE.write_text(json.dumps({"github": {"token": "ghp_old_plain"}, "api_token": "tok"}), encoding="utf-8")
        config._cache = None
        self.assertEqual(config.load_settings()["github"]["token"], "ghp_old_plain")
        self.assertNotIn("ghp_old_plain", config.SETTINGS_FILE.read_text(encoding="utf-8"))


class BackupTests(unittest.TestCase):
    def setUp(self):
        iso = Isolated()
        self.root = iso.__enter__()
        self.addCleanup(iso.__exit__, None, None, None)
        self.db = Database(self.root / "live.db")
        self.addCleanup(self.db.close)
        self.db.add_notification("x", "Keep me", "important")
        config.update_settings({"cloud": {"providers": {"openai": {"api_key": KEY}}}, "user_name": "Alex"})

    def test_backup_has_data_but_no_secrets(self):
        z = backup.create_backup(self.db)
        with zipfile.ZipFile(z) as zf:
            self.assertIn("cerberus.db", zf.namelist())
            text = zf.read("settings.json").decode()
            self.assertNotIn(KEY, text)
            self.assertNotIn(config.load_settings()["api_token"], text)
            self.assertIn("Alex", text)
            zf.extract("cerberus.db", self.root / "x")
        with contextlib.closing(sqlite3.connect(self.root / "x" / "cerberus.db")) as conn:
            rows = conn.execute("SELECT title FROM notifications").fetchall()
        self.assertEqual(rows, [("Keep me",)])

    def test_export_to_a_chosen_path(self):
        out = backup.create_backup(self.db, self.root / "mine" / "b.zip")
        self.assertTrue(out.exists())

    def test_auto_backup_once_a_day_and_pruned(self):
        self.assertIsNotNone(backup.auto_backup(self.db))
        self.assertIsNone(backup.auto_backup(self.db))
        d = backup.backups_dir()
        for i in range(10):
            (d / f"cerberus-auto-2020010{i % 10}-00000{i}.zip").write_bytes(b"x")
        with mock.patch("time.strftime", return_value="20990101"):
            backup.auto_backup(self.db)
        self.assertLessEqual(len(list(d.glob("cerberus-auto-*.zip"))), backup.KEEP_AUTO)

    def test_restore_roundtrip_keeps_this_machines_secrets(self):
        z = backup.create_backup(self.db)
        self.db.add_notification("x", "Added later", "gone after restore")
        config.update_settings({"user_name": "Changed"})
        msg = backup.stage_restore(self.db, z)
        self.assertIn("Restart", msg)
        self.db.close()                                                   # a real restore happens before the db opens
        self.assertIn("Restored", backup.apply_pending_restore())
        db2 = Database(self.root / "data" / "cerberus.db")
        self.addCleanup(db2.close)
        self.assertEqual([n["title"] for n in db2.notifications()], ["Keep me"])   # live.db is outside data/; see below
        s = config.load_settings()
        self.assertEqual(s["user_name"], "Alex")
        self.assertEqual(s["cloud"]["providers"]["openai"]["api_key"], KEY)       # kept, not blanked by the backup
        self.assertIsNone(backup.apply_pending_restore())                         # nothing left to apply
        self.assertTrue(list(backup.backups_dir().glob("cerberus-before-restore-*.zip")))

    def test_rejects_things_that_are_not_backups(self):
        junk = self.root / "junk.zip"
        junk.write_bytes(b"not a zip")
        with self.assertRaises(ValueError):
            backup.stage_restore(self.db, junk)
        other = self.root / "other.zip"
        with zipfile.ZipFile(other, "w") as zf:
            zf.writestr("hello.txt", "hi")
        with self.assertRaises(ValueError):
            backup.stage_restore(self.db, other)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        iso = Isolated()
        self.root = iso.__enter__()
        self.addCleanup(iso.__exit__, None, None, None)
        self.legacy = self.root / "old_data"
        self.legacy.mkdir()
        con = sqlite3.connect(self.legacy / "cerberus.db")
        con.executescript("CREATE TABLE t(a); INSERT INTO t VALUES(1);")
        con.close()
        (self.legacy / "settings.json").write_text(json.dumps({"github": {"token": "ghp_legacy_plain"}, "user_name": "Sam"}), encoding="utf-8")
        (self.legacy / "shot.png").write_bytes(b"png")
        for f in config.DATA_DIR.glob("*"):
            f.unlink()                                                    # Isolated pre-created a settings.json
        config._cache = None
        p = mock.patch.object(config, "LEGACY_DATA_DIR", self.legacy)
        p.start()
        self.addCleanup(p.stop)

    def test_moves_data_seals_keys_and_parks_the_old_folder(self):
        note = config.migrate_legacy_data()
        self.assertIn("Moved", note)
        self.assertTrue((config.DATA_DIR / "cerberus.db").exists())
        self.assertFalse((config.DATA_DIR / "shot.png").exists())
        self.assertNotIn("ghp_legacy_plain", config.SETTINGS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(config.load_settings()["github"]["token"], "ghp_legacy_plain")
        self.assertEqual(config.load_settings()["user_name"], "Sam")
        self.assertFalse(self.legacy.exists())
        self.assertTrue(self.legacy.with_name("data.migrated").exists())

    def test_never_overwrites_existing_data(self):
        config.update_settings({"user_name": "Already here"})
        self.assertIsNone(config.migrate_legacy_data())
        self.assertTrue(self.legacy.exists())
        self.assertEqual(config.load_settings()["user_name"], "Already here")


class BackupApiTests(unittest.TestCase):
    def setUp(self):
        iso = Isolated()
        self.root = iso.__enter__()
        self.addCleanup(iso.__exit__, None, None, None)
        self.core = Cerberus(self.root / "a.db")
        self.core.ollama = FakeOllama()
        app = create_app(self.core)
        self.local = TestClient(app, base_url="http://127.0.0.1:8777", client=("127.0.0.1", 5000))
        self.remote = TestClient(app, base_url="http://192.168.1.50:8777", client=("192.168.1.99", 5000))
        self.local.__enter__()
        self.addCleanup(self.local.__exit__, None, None, None)
        self.addCleanup(self.core.db.close)

    def test_export_then_stage_restore(self):
        dest = self.root / "out.zip"
        r = self.local.post("/api/backup", json={"path": str(dest)})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(dest.exists())
        r = self.local.post("/api/backup/restore", json={"path": str(dest)})
        self.assertIn("Restart", r.json()["message"])
        bad = self.local.post("/api/backup/restore", json={"path": str(self.root / "nope.zip")})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.local.post("/api/backup", json={"path": str(self.root / "x.txt")}).status_code, 400)
        self.assertTrue(self.local.get("/api/backup/list").json()["files"])        # auto/before-restore backups exist

    def test_phones_cannot_export_or_restore(self):
        h = {"X-Cerberus-Token": self.core.settings()["api_token"]}
        self.assertEqual(self.remote.post("/api/backup", json={}, headers=h).status_code, 403)
        self.assertEqual(self.remote.post("/api/backup/restore", json={"path": "x.zip"}, headers=h).status_code, 403)
        self.assertEqual(self.remote.get("/api/backup/list", headers=h).status_code, 403)


if __name__ == "__main__":
    unittest.main()
