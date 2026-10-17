import os
import unittest
from pathlib import Path

from cerberus import config
from cerberus.core import Cerberus
from cerberus.tools import run_tool
from cerberus.tools import computer as pc

from .helpers import FakeOllama, Isolated

APPROVE = "VOTE: APPROVE\nREASON: fine"


class ComputerBase(unittest.TestCase):
    def setUp(self):
        iso = Isolated()
        self.root = iso.__enter__()
        self.addCleanup(iso.__exit__, None, None, None)
        self.core = Cerberus(self.root / "t.db")
        self.core.ollama = self.fake = FakeOllama()
        self.fake.reply_fn = lambda m, msgs: APPROVE
        self.core.gate.autonomous = True                       # skip the human click; the heads still vote
        self.addCleanup(self.core.db.close)
        self.ws = config.get_workspace()
        self.ctx = self.core.tool_ctx("local", "test")
        self.ctx.intent = "do the thing the user asked"

    def tool(self, tool_name, /, **args):
        return run_tool(tool_name, args, self.ctx)


class ChangeTests(ComputerBase):
    def test_write_then_undo(self):
        target = self.ws / "notes" / "a.txt"
        out = self.tool("pc_write", path=str(target), content="hello\n")
        self.assertIn("Created", out)
        self.assertEqual(target.read_text(), "hello\n")
        self.assertIn("Removed the file", self.tool("pc_undo"))
        self.assertFalse(target.exists())

    def test_overwrite_keeps_the_old_version_and_crlf(self):
        target = self.ws / "b.txt"
        target.write_bytes(b"one\r\ntwo\r\n")
        self.assertIn("Replaced", self.tool("pc_write", path=str(target), content="three\nfour\n"))
        self.assertEqual(target.read_bytes(), b"three\r\nfour\r\n")
        self.tool("pc_undo")
        self.assertEqual(target.read_bytes(), b"one\r\ntwo\r\n")

    def test_append(self):
        target = self.ws / "c.txt"
        target.write_text("a\n")
        self.tool("pc_append", path=str(target), content="b\n")
        self.assertEqual(target.read_text(), "a\nb\n")
        self.tool("pc_undo")
        self.assertEqual(target.read_text(), "a\n")

    def test_delete_goes_to_undo_and_comes_back(self):
        target = self.ws / "d.txt"
        target.write_text("precious")
        self.assertIn("Deleted", self.tool("pc_delete", path=str(target)))
        self.assertFalse(target.exists())
        self.assertIn("Brought back", self.tool("pc_undo"))
        self.assertEqual(target.read_text(), "precious")

    def test_delete_folder_and_undo(self):
        folder = self.ws / "dir"
        (folder / "sub").mkdir(parents=True)
        (folder / "sub" / "f.txt").write_text("x")
        self.tool("pc_delete", path=str(folder))
        self.assertFalse(folder.exists())
        self.tool("pc_undo")
        self.assertEqual((folder / "sub" / "f.txt").read_text(), "x")

    def test_move_never_overwrites_and_can_be_undone(self):
        a, b = self.ws / "a.txt", self.ws / "b.txt"
        a.write_text("A")
        b.write_text("B")
        self.assertIn("already exists", self.tool("pc_move", source=str(a), destination=str(b)))
        self.assertEqual(b.read_text(), "B")
        c = self.ws / "moved.txt"
        self.assertIn("Moved", self.tool("pc_move", source=str(a), destination=str(c)))
        self.assertTrue(c.exists() and not a.exists())
        self.tool("pc_undo")
        self.assertTrue(a.exists() and not c.exists())

    def test_mkdir_and_history(self):
        self.tool("pc_mkdir", path=str(self.ws / "x" / "y"))
        self.assertTrue((self.ws / "x" / "y").is_dir())
        self.assertIn("mkdir", self.tool("pc_history"))
        self.tool("pc_undo")
        self.assertFalse((self.ws / "x" / "y").exists())

    def test_relative_names_mean_the_workspace(self):
        self.tool("pc_write", path="rel.txt", content="r")
        self.assertEqual((self.ws / "rel.txt").read_text(), "r")

    def test_undo_with_nothing_to_undo(self):
        self.assertIn("nothing to undo", self.tool("pc_undo"))

    def test_oversize_and_binary_are_refused(self):
        self.assertIn("DENIED", self.tool("pc_write", path=str(self.ws / "big.txt"), content="x" * (pc.MAX_WRITE_BYTES + 1)))
        blob = self.ws / "blob.bin"
        blob.write_bytes(b"\x00\x01\x02")
        self.assertIn("DENIED", self.tool("pc_write", path=str(blob), content="text"))
        self.assertEqual(blob.read_bytes(), b"\x00\x01\x02")


class CouncilGateTests(ComputerBase):
    def test_one_refusal_blocks_the_change_and_never_asks_the_user(self):
        self.core.gate.autonomous = False
        self.fake.reply_fn = lambda m, msgs: "VOTE: REJECT\nREASON: wrong place"
        out = self.tool("pc_write", path=str(self.ws / "n.txt"), content="x")
        self.assertIn("DENIED: the three heads did not all agree", out)
        self.assertFalse((self.ws / "n.txt").exists())
        self.assertEqual(self.core.gate.pending(), [])
        self.assertEqual(len(self.fake.calls), 1)                  # the first "no" ended the vote

    def test_without_a_council_nothing_changes(self):
        self.core.gate.council = None
        self.assertIn("no council", self.tool("pc_write", path=str(self.ws / "n.txt"), content="x"))
        self.assertFalse((self.ws / "n.txt").exists())

    def test_the_user_still_has_to_agree(self):
        self.core.gate.autonomous = False
        import threading
        import time

        def decline():
            for _ in range(100):
                pend = self.core.gate.pending()
                if pend:
                    self.core.gate.decide(pend[0]["id"], False)
                    return
                time.sleep(0.05)
        th = threading.Thread(target=decline)
        th.start()
        out = self.tool("pc_write", path=str(self.ws / "n.txt"), content="x")
        th.join()
        self.assertIn("user did not approve", out)
        self.assertFalse((self.ws / "n.txt").exists())

    def test_remote_origin_is_blocked_for_every_pc_tool(self):
        ctx = self.core.tool_ctx("remote", "phone")
        for name in ("pc_list", "pc_read", "pc_write", "pc_delete", "pc_undo", "pc_info"):
            self.assertIn("only works from the computer", run_tool(name, {"path": str(self.ws), "content": "x"}, ctx), name)

    def test_full_access_switch(self):
        config.update_settings({"full_access": False})
        out = self.tool("pc_write", path=str(self.ws / "n.txt"), content="x")
        self.assertIn("switched off", out)
        self.assertIn("switched off", self.tool("pc_list", path=str(self.ws)))
        self.assertEqual(self.fake.calls, [])


class DenylistTests(ComputerBase):
    def assertBlocked(self, tool_name, /, **args):
        before = len(self.fake.calls)
        out = self.tool(tool_name, **args)
        self.assertTrue(out.startswith("DENIED"), f"{tool_name} {args} -> {out}")
        self.assertEqual(len(self.fake.calls), before, "the heads must not even be asked about a forbidden change")

    def test_system_and_program_folders(self):
        for path in (r"C:\Windows\cerberus_test.txt", r"C:\Program Files\x.txt", r"C:\ProgramData\x.txt", "C:\\x.txt"):
            self.assertBlocked("pc_write", path=path, content="x")
        self.assertBlocked("pc_delete", path=r"C:\Windows\System32")

    def test_cerberus_own_data_and_code(self):
        self.assertBlocked("pc_write", path=str(config.DATA_DIR / "settings.json"), content="{}")
        self.assertBlocked("pc_read", path=str(config.DATA_DIR / "settings.json"))
        self.assertBlocked("pc_write", path=str(config.ROOT / "cerberus" / "safety.py"), content="x")
        self.assertBlocked("pc_delete", path=str(config.ROOT / "cerberus"))
        self.assertBlocked("pc_write", path=str(config.DATA_DIR / "plugins" / "evil.py"), content="x")

    def test_secret_files_and_folders(self):
        home = Path.home()
        self.assertBlocked("pc_read", path=str(home / ".ssh" / "id_rsa"))
        self.assertBlocked("pc_write", path=str(home / ".aws" / "credentials"), content="x")
        self.assertBlocked("pc_read", path=str(self.ws / ".env"))
        self.assertBlocked("pc_write", path=str(self.ws / "server.pem"), content="x")
        self.assertBlocked("pc_read", path=str(self.ws / "vault.kdbx"))
        local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        self.assertBlocked("pc_read", path=str(local / "Google" / "Chrome" / "User Data" / "Default" / "Login Data"))

    def test_programs_and_shortcuts_outside_the_workspace(self):
        elsewhere = Path(os.environ.get("TEMP", str(self.root))) / "cerberus_x"
        self.assertBlocked("pc_write", path=str(elsewhere / "run.bat"), content="echo hi")
        self.assertBlocked("pc_write", path=str(elsewhere / "a.exe"), content="x")
        self.assertIn("Created", self.tool("pc_write", path=str(self.ws / "tool.bat"), content="echo hi"))   # workspace is allowed

    def test_main_folders_cannot_be_deleted_or_moved_whole(self):
        home = Path.home()
        for p in (home, home / "Documents", home / "Desktop", home / "Downloads"):
            with self.assertRaises(Exception):
                pc.check_write(p, destructive=True)
        with self.assertRaises(Exception):
            pc.check_write(home.parent, destructive=True)

    def test_odd_paths(self):
        for path in (r"\\server\share\x.txt", "//server/share/x.txt", str(self.ws / "a.txt:hidden"), "", "bad\x00name"):
            self.assertBlocked("pc_write", path=path, content="x")

    def test_traversal_is_resolved_before_checking(self):
        sneaky = str(self.ws) + "\\.." * 30 + "\\Windows\\cerberus_test_x.txt"
        self.assertBlocked("pc_write", path=sneaky, content="x")
        self.assertFalse(Path("C:/Windows/cerberus_test_x.txt").exists())

    def test_a_folder_holding_protected_things_cannot_be_deleted(self):
        with self.assertRaises(Exception):
            pc.check_write(config.ROOT.parent, destructive=True)


class ReadingTests(ComputerBase):
    def test_list_read_find(self):
        (self.ws / "docs").mkdir()
        (self.ws / "docs" / "invoice1.txt").write_text("total: 450 dollars")
        (self.ws / "docs" / "notes.txt").write_text("nothing")
        (self.ws / "docs" / ".env").write_text("SECRET=1")
        listing = self.tool("pc_list", path=str(self.ws / "docs"))
        self.assertIn("invoice1.txt", listing)
        self.assertNotIn(".env", listing)
        self.assertIn("450", self.tool("pc_read", path=str(self.ws / "docs" / "invoice1.txt")))
        found = self.tool("pc_find", root=str(self.ws), name="*.txt", contains="450")
        self.assertIn("invoice1.txt", found)
        self.assertNotIn("notes.txt", found)
        self.assertNotIn(".env", self.tool("pc_find", root=str(self.ws), name="env"))
        self.assertEqual(self.fake.calls, [])                       # reading needs no vote

    def test_reading_binary_and_missing(self):
        blob = self.ws / "b.bin"
        blob.write_bytes(b"\x00\x01")
        self.assertIn("binary", self.tool("pc_read", path=str(blob)))
        self.assertIn("does not exist", self.tool("pc_read", path=str(self.ws / "nope.txt")))

    def test_pc_info_describes_the_machine(self):
        out = self.tool("pc_info")
        self.assertIn("GB", out)
        self.assertIn("workspace", out)

    def test_whole_pc_is_readable_outside_the_workspace(self):
        out = self.tool("pc_list", path=str(Path.home()))
        self.assertNotIn("DENIED", out)


if __name__ == "__main__":
    unittest.main()
