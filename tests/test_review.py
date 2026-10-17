"""Tests for the review log, error memory, self-knowledge, file browser / attached files and Resume Studio."""
import io
import json
import os
import time
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from cerberus import capabilities, config, filectx, mistakes, telemetry
from cerberus.core import Cerberus
from cerberus.modes import MODES
from cerberus.safety import Denied
from cerberus.server import create_app
from cerberus.tools import ToolContext, run_tool
from cerberus.tools import resume as studio

from .helpers import FakeOllama, Isolated

LOG_ON = {"CERBERUS_NO_TELEMETRY": ""}
KEY = "sk-" + "a1b2c3d4e5f6g7h8i9j0"


def events(resp) -> list[dict]:
    return [json.loads(ln) for ln in resp.text.splitlines() if ln.strip()]


def make_docx(path: Path, paragraphs: list[str]) -> None:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = ('<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           f"<w:body>{body}</w:body></w:document>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", xml)


class Base(unittest.TestCase):
    def setUp(self):
        iso = Isolated()
        self.root = iso.__enter__()
        self.addCleanup(iso.__exit__, None, None, None)
        patcher = mock.patch.dict(os.environ, LOG_ON)
        patcher.start()
        self.addCleanup(patcher.stop)
        telemetry._last_prune = time.time()                    # no pruning unless a test asks for it
        self.ws = config.get_workspace()
        self.ws.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- review log
class TelemetryTests(Base):
    def run_events(self, evs, request="Tell me a tale", **kw):
        rec = telemetry.Recorder(request, kw.pop("mode", "writing"), kw.pop("options", {"thinking": "careful"}), kw.pop("origin", "local"))
        for ev in evs:
            rec.feed(ev)
        rec.close()
        return rec

    def good_run(self):
        return [{"type": "start", "thinking": "careful", "models": {"reasoner": "deepseek-r1"}},
                {"type": "stage_start", "key": "ego", "title": "The Writer", "role": "generalist", "model": "llama3"},
                {"type": "token", "kind": "thinking", "text": "hmm"}, {"type": "token", "kind": "content", "text": "Once upon a time"},
                {"type": "stage_end", "key": "ego"},
                {"type": "tool_call", "name": "pc_read"}, {"type": "tool_result", "name": "pc_read", "result": "ERROR: nope"},
                {"type": "council", "phase": "review"},
                {"type": "council", "phase": "found", "issues": [{"by": "mistral", "problem": "wrong date"}]},
                {"type": "final", "text": "The end.", "source": "local"}, {"type": "done"}]

    def test_a_run_writes_one_line_with_timings_tools_and_council(self):
        self.run_events(self.good_run())
        runs = telemetry.read_runs(1)
        self.assertEqual(len(runs), 1)
        r = runs[0]
        self.assertEqual(r["outcome"], "ok")
        self.assertEqual(r["stages"][0]["model"], "llama3")
        self.assertEqual(r["stages"][0]["think_chars"], 3)
        self.assertEqual(r["stages"][0]["content_chars"], 16)
        self.assertIn("first_word_s", r)
        self.assertEqual(r["tool_errors"], 1)
        self.assertFalse(r["tools"][0]["ok"])
        self.assertEqual(r["council"]["found"], 1)
        self.assertEqual(r["council"]["issues"][0]["by"], "mistral")
        self.assertEqual(r["answer_chars"], 8)
        self.assertEqual(r["options"], ["thinking"])

    def test_secrets_never_reach_the_log(self):
        config.update_settings({"cloud": {"providers": {"openai": {"api_key": "hunter2-not-a-pattern"}}}})
        self.run_events([{"type": "final", "text": f"Use {KEY} and hunter2-not-a-pattern, password=swordfish99, Bearer abcdefgh12345678", "source": "local"},
                         {"type": "done"}], request=f"my key is {KEY}")
        raw = (telemetry.log_dir() / next(iter(os.listdir(telemetry.log_dir())))).read_text(encoding="utf-8")
        for leak in (KEY, "hunter2-not-a-pattern", "swordfish99", "abcdefgh12345678"):
            self.assertNotIn(leak, raw)
        self.assertIn("[secret]", raw)

    def test_previews_can_be_switched_off(self):
        config.update_settings({"telemetry": {"previews": False}})
        self.run_events(self.good_run(), request="a private question")
        r = telemetry.read_runs(1)[0]
        self.assertNotIn("request_preview", r)
        self.assertNotIn("answer_preview", r)
        self.assertEqual(r["request_chars"], len("a private question"))

    def test_logging_off_writes_nothing(self):
        config.update_settings({"telemetry": {"enabled": False}})
        self.run_events(self.good_run())
        self.assertFalse(telemetry.log_dir().exists() and any(telemetry.log_dir().iterdir()))

    def test_test_switch_blocks_writes(self):
        with mock.patch.dict(os.environ, {"CERBERUS_NO_TELEMETRY": "1"}):
            self.assertFalse(telemetry.write({"x": 1}))

    def test_a_run_closed_early_is_logged_as_abandoned(self):
        self.run_events(self.good_run()[:3])                     # the window closed mid-answer: no final, no done
        r = telemetry.read_runs(1)[0]
        self.assertEqual(r["outcome"], "abandoned")
        self.assertIn("s", r["stages"][0])

    def test_close_writes_only_once(self):
        rec = self.run_events(self.good_run())
        rec.close()
        self.assertEqual(len(telemetry.read_runs(1)), 1)

    def test_errors_keep_the_trace_redacted(self):
        rec = telemetry.Recorder("x", "writing", {})
        rec.feed({"type": "error", "kind": "internal", "message": "Something went wrong", "detail": f"KeyError: {KEY}"},
                 trace=f"Traceback ... token={KEY}")
        rec.close()
        r = telemetry.read_runs(1)[0]
        self.assertEqual(r["outcome"], "error")
        self.assertNotIn(KEY, json.dumps(r))
        self.assertEqual(r["errors"][0]["kind"], "internal")

    def test_bad_lines_and_old_runs_are_skipped(self):
        telemetry.log_dir().mkdir(parents=True)
        f = telemetry.log_dir() / "runs-202001.jsonl"
        f.write_text('not json\n{"epoch": 1, "id": "old"}\n[1,2]\n', encoding="utf-8")
        self.assertEqual(telemetry.read_runs(30), [])
        self.assertEqual([r["id"] for r in telemetry.read_runs(None)], ["old"])

    def test_old_month_files_are_pruned_but_recent_ones_stay(self):
        telemetry.log_dir().mkdir(parents=True)
        old = telemetry.log_dir() / "runs-200001.jsonl"
        now = telemetry.log_dir() / f"runs-{datetime.now():%Y%m}.jsonl"
        old.write_text("{}\n"), now.write_text("{}\n")
        telemetry._last_prune = 0.0
        telemetry._prune()
        self.assertFalse(old.exists())
        self.assertTrue(now.exists())

    def test_a_big_month_file_rolls_over(self):
        telemetry.log_dir().mkdir(parents=True)
        first = telemetry._month_file()
        first.write_text("x")
        with mock.patch.object(telemetry, "ROLL_BYTES", 0):
            self.assertTrue(telemetry._month_file().name.endswith("-2.jsonl"))

    def test_report_sections_and_suggestions(self):
        for _ in range(3):
            self.run_events(self.good_run())
        self.run_events([{"type": "error", "kind": "ollama", "message": "Ollama is not running"}, {"type": "done"}])
        self.run_events(self.good_run()[:2])
        rep = telemetry.report(7)
        self.assertEqual(rep["runs"], 5)
        self.assertEqual(rep["outcomes"], {"ok": 3, "error": 1, "abandoned": 1})
        self.assertEqual(rep["tools"]["pc_read"]["errors"], 3)
        self.assertEqual(rep["council"]["who_caught_most"], {"mistral": 3})
        self.assertEqual(rep["errors"][0]["kind"], "ollama")
        self.assertTrue(any("ended in an error" in t for t in rep["suggestions"]))
        text = telemetry.render_text(rep)
        for head in ("HOW LONG IT TAKES", "TOOLS", "ERRORS", "WHAT TO TWEAK", "FAILED OR ABANDONED"):
            self.assertIn(head, text)

    def test_empty_report_says_so(self):
        rep = telemetry.report(7)
        self.assertEqual(rep["runs"], 0)
        self.assertIn("No runs logged", telemetry.render_text(rep))

    def test_same_error_with_different_numbers_groups_together(self):
        self.assertEqual(telemetry._signature(r"Cannot open C:\Users\a\b.txt line 12"), telemetry._signature(r"Cannot open C:\Users\z\q.txt line 99"))

    def test_cli_prints_a_report_and_one_run(self):
        self.run_events(self.good_run())
        rid = telemetry.read_runs(1)[0]["id"]
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            telemetry.main(["--days", "7"])
            self.assertIn("CERBERUS REVIEW", out.getvalue())
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            telemetry.main(["--run", rid])
            self.assertEqual(json.loads(out.getvalue())["id"], rid)
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            telemetry.main(["--json"])
            self.assertEqual(json.loads(out.getvalue())["runs"], 1)

    def test_logging_failure_never_breaks_a_run(self):
        with mock.patch.object(telemetry, "_month_file", side_effect=OSError("disk full")):
            self.assertFalse(telemetry.write({"x": 1}))
            self.run_events(self.good_run())                     # must not raise


# ---------------------------------------------------------------- error memory
class MistakeTests(Base):
    def setUp(self):
        super().setUp()
        self.core = Cerberus(self.root / "t.db")
        self.addCleanup(self.core.db.close)
        self.db = self.core.db

    def result(self, name, text):
        mistakes.Watcher(self.db, "coding").feed({"type": "tool_result", "name": name, "result": text})

    def test_classifies_tool_failures(self):
        self.result("pc_read", "ERROR: unknown tool 'pc_nope'. Available: x")
        self.result("pc_write", "ERROR: bad arguments for pc_write: missing 'content'. Expected params: {}")
        self.result("pc_write", "DENIED: the three heads did not all agree, so nothing was changed. mistral refused")
        self.result("pc_read", "DENIED: that path is off limits")
        self.result("web_read", "ERROR: HTTPError: 404")
        self.result("pc_read", "DENIED: the user did not approve this action.")
        self.result("pc_read", "ERROR: this tool only works from the computer Cerberus runs on (blocked for remote clients).")
        self.result("pc_read", "fine, here is the file")
        kinds = {it["kind"] for it in mistakes.load(self.db)}
        self.assertEqual(kinds, {"tool_unknown", "tool_args", "council_refused", "off_limits", "tool_error", "remote_limit"})

    def test_the_users_refusal_is_not_a_mistake(self):
        self.result("pc_delete", "DENIED: the user did not approve this action.")
        self.assertEqual(mistakes.load(self.db), [])

    def test_repeats_are_counted_not_duplicated(self):
        for n in (1, 2, 3):
            self.result("pc_read", f"ERROR: bad arguments for pc_read: nope {n}")
        items = mistakes.load(self.db)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["count"], 3)

    def test_the_memory_is_capped(self):
        for i in range(mistakes.CAP + 20):
            mistakes.record(self.db, "tool_error", f"tool{i}", f"ERROR: unique failure {'x' * i}", "advice", True)
        self.assertEqual(len(mistakes.load(self.db)), mistakes.CAP)

    def test_secrets_are_scrubbed_before_saving(self):
        self.result("web_read", f"ERROR: request to https://x.example failed with token={KEY}")
        self.assertNotIn(KEY, json.dumps(mistakes.load(self.db)))

    def test_lessons_reach_the_prompt_only_when_they_would_help(self):
        self.result("pc_write", "ERROR: bad arguments for pc_write: oops")           # always taught
        self.result("web_read", "ERROR: HTTPError 500")                                # tool_error: taught only after repeating
        block = mistakes.lessons_block(self.db, "write a file")
        self.assertIn("pc_write", block)
        self.assertNotIn("web_read", block)
        self.result("web_read", "ERROR: HTTPError 500")
        self.assertIn("web_read", mistakes.lessons_block(self.db, "read the web"))

    def test_environment_trouble_is_logged_but_not_taught(self):
        mistakes.Watcher(self.db, "coding").feed({"type": "error", "kind": "ollama", "message": "Ollama is not running"})
        mistakes.Watcher(self.db, "coding").feed({"type": "error", "kind": "input", "message": "Say something first."})
        items = mistakes.load(self.db)
        self.assertEqual([it["kind"] for it in items], ["ollama"])
        self.assertEqual(mistakes.lessons_block(self.db, "anything"), "")

    def test_summary_and_forget(self):
        self.result("pc_write", "ERROR: bad arguments for pc_write: oops")
        s = mistakes.summary(self.db)
        self.assertEqual((s["count"], s["teachable"]), (1, 1))
        mistakes.forget(self.db)
        self.assertEqual(mistakes.summary(self.db)["count"], 0)

    def test_the_hand_is_shown_the_lessons(self):
        self.result("pc_write", "ERROR: bad arguments for pc_write: oops")
        self.core.ollama = fake = FakeOllama("Done.")
        self.core.gate.autonomous = True
        list(self.core.orchestrator.run("save a note", "coding", {"allow_actions": True}, "local"))
        seen = "\n".join(m["content"] for _model, msgs in fake.calls for m in msgs if isinstance(m.get("content"), str))
        self.assertIn("Pitfalls from earlier errors", seen)

    def test_watcher_never_raises(self):
        mistakes.Watcher(None, "coding").feed({"type": "tool_result", "name": "x", "result": "ERROR: boom"})


# ---------------------------------------------------------------- self-knowledge
class CapabilityTests(Base):
    def setUp(self):
        super().setUp()
        self.core = Cerberus(self.root / "t.db")
        self.core.ollama = FakeOllama()
        self.addCleanup(self.core.db.close)

    def test_recognises_questions_about_itself(self):
        for q in ("What can you do?", "which tools do you have", "Can you access my files?", "list your capabilities", "what are your limits"):
            self.assertTrue(capabilities.is_asking(q), q)
        for q in ("Write me a poem about the sea", "Fix the bug in my parser", ""):
            self.assertFalse(capabilities.is_asking(q), q)

    def test_short_block_tells_the_truth_about_actions(self):
        mode = MODES["coding"]
        off = capabilities.short_block(self.core, mode, False)
        on = capabilities.short_block(self.core, mode, True)
        self.assertIn("Actions are OFF", off)
        self.assertIn("Actions are ON", on)
        self.assertIn("Never say you did", off)
        self.assertIn("Cloud Handoff to a big model is OFF", off)

    def test_remote_chats_are_told_they_have_no_pc_tools(self):
        block = capabilities.short_block(self.core, MODES["coding"], True, "remote")
        self.assertIn("another device", block)
        self.assertNotIn("Computer", block)

    def test_describe_is_built_from_the_real_registry_and_hides_pc_tools_from_remote(self):
        local = capabilities.describe(self.core, "local")
        remote = capabilities.describe(self.core, "remote")
        names = lambda d: {t["name"] for g in d["skills"] for t in g["tools"]}
        self.assertIn("pc_read", names(local))
        self.assertNotIn("pc_read", names(remote))
        self.assertGreater(local["tool_count"], remote["tool_count"])
        self.assertEqual(len(local["rites"]), 6)
        self.assertTrue(local["limits"])

    def test_the_resume_skill_is_listed(self):
        self.assertIn("resume_tailor", {t["name"] for g in capabilities.describe(self.core)["skills"] for t in g["tools"]})

    def test_full_text_mentions_state_and_limits(self):
        txt = capabilities.full_text(self.core)
        self.assertIn("State now:", txt)
        self.assertIn("Limits:", txt)

    def test_the_heads_get_it_in_every_stage_and_the_full_list_on_request(self):
        fake = self.core.ollama
        list(self.core.orchestrator.run("Say hello", "conversation", {}, "local"))
        systems = [msgs[0]["content"] for _m, msgs in fake.calls]
        self.assertTrue(systems and all("WHAT CERBERUS CAN AND CANNOT DO" in s for s in systems))
        fake.calls.clear()
        list(self.core.orchestrator.run("What can you do?", "conversation", {}, "local"))
        self.assertTrue(any("CERBERUS CAPABILITIES" in m["content"] for _m, msgs in fake.calls for m in msgs))


# ---------------------------------------------------------------- file browser and attached files
class FileContextTests(Base):
    def test_browse_lists_names_sizes_and_what_can_be_attached(self):
        (self.ws / "notes.txt").write_text("hello")
        (self.ws / "pic.png").write_bytes(b"\x89PNG")
        (self.ws / "sub").mkdir()
        got = filectx.browse(str(self.ws))
        by = {e["name"]: e for e in got["entries"]}
        self.assertTrue(by["sub"]["dir"])
        self.assertEqual(got["entries"][0]["name"], "sub")                  # folders first
        self.assertTrue(by["notes.txt"]["attachable"])
        self.assertEqual(by["notes.txt"]["size"], 5)
        self.assertFalse(by["pic.png"]["attachable"])

    def test_empty_path_lists_starting_places(self):
        got = filectx.browse("")
        self.assertTrue(got["roots"])
        self.assertIn("Workspace", [e["name"] for e in got["entries"]])

    def test_secrets_are_hidden_from_listings_and_refused_when_asked_for(self):
        for name in (".env", "id_rsa"):
            (self.ws / name).write_text("TOKEN=abc")
        (self.ws / "ok.txt").write_text("fine")
        names = [e["name"] for e in filectx.browse(str(self.ws))["entries"]]
        self.assertIn("ok.txt", names)
        self.assertNotIn(".env", names)
        self.assertNotIn("id_rsa", names)
        with self.assertRaises(Denied):
            filectx.preview(str(self.ws / ".env"))
        text, report = filectx.load([str(self.ws / ".env")])
        self.assertEqual(text, "")
        self.assertFalse(report[0]["ok"])

    def test_cerberus_own_data_is_off_limits_and_system_folders_are_readable_but_never_changed(self):
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        (config.DATA_DIR / "settings.json").write_text("{}")
        with self.assertRaises(Denied):
            filectx.browse(str(config.DATA_DIR))
        with self.assertRaises(Denied):
            filectx.preview(str(config.DATA_DIR / "settings.json"))
        win = Path(os.environ.get("WINDIR", r"C:\Windows"))
        self.assertTrue(filectx.browse(str(win))["entries"])                 # reading is fine ...
        from cerberus.tools import computer
        with self.assertRaises(Denied):
            computer.check_write(win / "cerberus_test.txt")                  # ... changing never is

    def test_without_full_access_only_the_workspace_is_reachable(self):
        config.update_settings({"full_access": False})
        self.assertEqual([r["name"] for r in filectx.roots()], ["Workspace"])
        (self.ws / "a.txt").write_text("in")
        self.assertEqual(filectx.preview(str(self.ws / "a.txt"))["text"], "in")
        with self.assertRaises(Denied):
            filectx.browse(str(Path.home()))

    def test_a_file_or_missing_path_is_not_a_folder(self):
        (self.ws / "a.txt").write_text("x")
        with self.assertRaises(Denied):
            filectx.browse(str(self.ws / "a.txt"))
        with self.assertRaises(Denied):
            filectx.browse(str(self.ws / "nope"))

    def test_reads_text_and_word_documents(self):
        (self.ws / "a.txt").write_text("plain words")
        make_docx(self.ws / "cv.docx", ["Jane Doe", "Built things"])
        text, report = filectx.load([str(self.ws / "a.txt"), str(self.ws / "cv.docx")])
        self.assertIn("plain words", text)
        self.assertIn("Jane Doe\nBuilt things", text)
        self.assertTrue(all(r["ok"] for r in report))

    def test_unreadable_files_are_reported_not_fatal(self):
        (self.ws / "x.pdf").write_bytes(b"%PDF-1.4 \x00\x01")
        (self.ws / "bad.docx").write_text("not a zip")
        (self.ws / "empty.txt").write_text("")
        (self.ws / "good.txt").write_text("good")
        text, report = filectx.load([str(self.ws / n) for n in ("x.pdf", "bad.docx", "empty.txt", "good.txt", "missing.txt")])
        self.assertIn("good", text)
        self.assertEqual([r["ok"] for r in report], [False, False, False, True, False])
        self.assertTrue(all(r["note"] for r in report if not r["ok"]))

    def test_limits_on_count_and_size(self):
        big = self.ws / "big.txt"
        big.write_text("z" * 50_000)
        text, report = filectx.load([str(big)])
        self.assertLessEqual(len(text), filectx.PER_FILE_CHARS + 200)
        self.assertEqual(report[0]["note"], "only the first part fit")
        many = []
        for i in range(12):
            p = self.ws / f"f{i}.txt"
            p.write_text(f"file {i}")
            many.append(str(p))
        _t, report = filectx.load(many)
        self.assertEqual(len([r for r in report if r["ok"]]), filectx.MAX_FILES)
        self.assertIn("only the first", report[-1]["note"])

    def test_duplicates_and_junk_are_ignored(self):
        (self.ws / "a.txt").write_text("once")
        text, report = filectx.load([str(self.ws / "a.txt"), str(self.ws / "a.txt"), "", None, 5])
        self.assertEqual(len(report), 1)
        self.assertEqual(text.count("once"), 1)

    def test_attached_text_reaches_every_head(self):
        (self.ws / "facts.txt").write_text("The codeword is PINEAPPLE-42.")
        core = Cerberus(self.root / "t.db")
        self.addCleanup(core.db.close)
        core.ollama = fake = FakeOllama()
        evs = list(core.orchestrator.run("What is the codeword?", "conversation", {"context_files": [str(self.ws / "facts.txt")]}, "local"))
        note = next(e for e in evs if e["type"] == "files_attached")
        self.assertTrue(note["files"][0]["ok"])
        stage_prompts = [msgs[-1]["content"] for _m, msgs in fake.calls]
        self.assertTrue(stage_prompts and all("PINEAPPLE-42" in p for p in stage_prompts))

    def test_remote_origin_never_reads_attached_files(self):
        (self.ws / "facts.txt").write_text("PINEAPPLE-42")
        core = Cerberus(self.root / "t.db")
        self.addCleanup(core.db.close)
        core.ollama = fake = FakeOllama()
        evs = list(core.orchestrator.run("What is the codeword?", "conversation", {"context_files": [str(self.ws / "facts.txt")]}, "remote"))
        self.assertNotIn("files_attached", [e["type"] for e in evs])
        self.assertNotIn("PINEAPPLE-42", json.dumps(fake.calls))


# ---------------------------------------------------------------- server endpoints
class ServerTests(Base):
    def setUp(self):
        super().setUp()
        self.core = Cerberus(self.root / "t.db")
        self.core.ollama = self.fake = FakeOllama()
        self.app = create_app(self.core)
        self.local = TestClient(self.app, base_url="http://127.0.0.1:8777", client=("127.0.0.1", 5000))
        self.remote = TestClient(self.app, base_url="http://192.168.1.50:8777", client=("192.168.1.99", 5000))
        self.local.__enter__()
        self.addCleanup(self.local.__exit__, None, None, None)
        self.addCleanup(self.core.db.close)
        self.h = {"X-Cerberus-Token": self.core.settings()["api_token"]}

    def test_local_only_endpoints_refuse_remote_clients(self):
        for method, path in (("get", "/api/telemetry/report"), ("get", "/api/mistakes"), ("delete", "/api/mistakes"),
                             ("get", "/api/files/browse"), ("get", "/api/files/preview?path=x"), ("get", "/api/resume"),
                             ("put", "/api/resume")):
            r = getattr(self.remote, method)(path, headers=self.h)
            self.assertEqual(r.status_code, 403, path)

    def test_report_json_and_text(self):
        list(self.core.orchestrator.run("hello there", "conversation", {}, "local"))
        rep = self.local.get("/api/telemetry/report?days=7").json()
        self.assertEqual(rep["runs"], 1)
        text = self.local.get("/api/telemetry/report?days=7&text=true")
        self.assertIn("CERBERUS REVIEW", text.text)
        self.assertTrue(text.headers["content-type"].startswith("text/plain"))

    def test_capabilities_are_origin_aware(self):
        local = self.local.get("/api/capabilities").json()
        remote = self.remote.get("/api/capabilities", headers=self.h).json()
        self.assertGreater(local["tool_count"], remote["tool_count"])
        self.assertIn("CERBERUS CAPABILITIES", self.local.get("/api/capabilities?text=true").text)

    def test_mistakes_list_and_forget(self):
        mistakes.record(self.core.db, "tool_args", "pc_write", "ERROR: bad arguments", "use the right names", True)
        got = self.local.get("/api/mistakes").json()
        self.assertEqual((got["count"], got["teachable"]), (1, 1))
        self.assertEqual(self.local.delete("/api/mistakes").status_code, 200)
        self.assertEqual(self.local.get("/api/mistakes").json()["count"], 0)

    def test_file_endpoints_browse_preview_and_refuse(self):
        (self.ws / "a.txt").write_text("hi there")
        listing = self.local.get("/api/files/browse", params={"path": str(self.ws)}).json()
        self.assertIn("a.txt", [e["name"] for e in listing["entries"]])
        self.assertEqual(self.local.get("/api/files/preview", params={"path": str(self.ws / "a.txt")}).json()["text"], "hi there")
        (self.ws / ".env").write_text("TOKEN=1")
        self.assertEqual(self.local.get("/api/files/preview", params={"path": str(self.ws / ".env")}).status_code, 403)
        self.assertEqual(self.local.get("/api/files/browse", params={"path": str(config.DATA_DIR)}).status_code, 403)

    def test_phones_cannot_attach_files(self):
        (self.ws / "secret.txt").write_text("PINEAPPLE-42")
        evs = events(self.remote.post("/api/chat", headers=self.h, json={"message": "what does it say", "mode": "conversation",
                                                                         "options": {"context_files": [str(self.ws / "secret.txt")]}}))
        self.assertNotIn("files_attached", [e["type"] for e in evs])          # the option is dropped before the run starts
        self.assertNotIn("PINEAPPLE-42", json.dumps(self.fake.calls))

    def test_settings_show_workings_and_telemetry(self):
        self.assertFalse(self.local.get("/api/settings").json()["show_workings"])
        self.assertTrue(self.local.put("/api/settings", json={"show_workings": 1}).json()["show_workings"] is True)
        got = self.local.put("/api/settings", json={"telemetry": {"enabled": False, "keep_months": 999}}).json()["telemetry"]
        self.assertEqual((got["enabled"], got["keep_months"], got["previews"]), (False, 24, True))
        self.assertEqual(self.local.put("/api/settings", json={"telemetry": "yes"}).status_code, 400)
        self.assertEqual(self.remote.put("/api/settings", headers=self.h, json={"show_workings": True}).status_code, 403)

    def test_an_internal_crash_is_plain_for_everyone_and_technical_only_locally(self):
        def boom(*a, **k):
            raise RuntimeError(f"kaboom with {KEY}")
            yield
        self.fake.chat_stream = boom
        loc = events(self.local.post("/api/chat", json={"message": "hello", "mode": "conversation"}))
        rem = events(self.remote.post("/api/chat", headers=self.h, json={"message": "hello", "mode": "conversation"}))
        le = next(e for e in loc if e["type"] == "error")
        re_ = next(e for e in rem if e["type"] == "error")
        self.assertIn("Something went wrong", le["message"])
        self.assertIn("RuntimeError", le["detail"])
        self.assertNotIn("detail", re_)
        self.assertNotIn("_trace", le)
        self.assertNotIn("kaboom", json.dumps(rem))
        self.assertNotIn("Traceback", json.dumps(loc + rem))
        logged = json.dumps(telemetry.read_runs(1))
        self.assertIn("Traceback", logged)                      # the log keeps what the chat hides ...
        self.assertNotIn(KEY, logged)                           # ... minus any secrets
        self.assertEqual(mistakes.load(self.core.db)[0]["kind"], "internal")

    def test_resume_get_put_validation(self):
        proj = self.root / "Resume"
        (proj / "src" / "job_agent").mkdir(parents=True)
        (proj / "src" / "job_agent" / "scraper.py").write_text("")
        base = self.ws / "cv.txt"
        base.write_text("Jane Doe, engineer")
        bad = lambda body: self.local.put("/api/resume", json=body).status_code
        self.assertEqual(bad({"path": str(self.root / "nowhere")}), 400)
        self.assertEqual(bad({"feed_urls": ["ftp://x.example/{query}"]}), 400)
        self.assertEqual(bad({"feed_urls": [f"https://x{i}.example/{{query}}" for i in range(11)]}), 400)
        self.assertEqual(bad({"base_resume": str(self.ws / "missing.txt")}), 400)
        self.assertEqual(bad({"applicant": "me"}), 400)
        ok = self.local.put("/api/resume", json={"path": str(proj), "base_resume": str(base), "feed_urls": "https://a.example/{query}\nhttps://b.example/{query}",
                                                 "applicant": {"name": " Jane ", "email": "j@x.example", "evil": "x"}})
        self.assertEqual(ok.status_code, 200)
        got = ok.json()
        self.assertEqual(got["folder"], str(proj))
        self.assertEqual(len(got["feed_urls"]), 2)
        self.assertEqual(got["applicant"]["name"], "Jane")
        self.assertNotIn("evil", got["applicant"])


# ---------------------------------------------------------------- Resume Studio
class ResumeStudioTests(Base):
    def setUp(self):
        super().setUp()
        self.core = Cerberus(self.root / "t.db")
        self.core.ollama = self.fake = FakeOllama()
        self.fake.reply_fn = lambda m, msgs: "VOTE: APPROVE\nREASON: fine"
        self.core.gate.autonomous = True
        self.addCleanup(self.core.db.close)
        self.ctx = self.core.tool_ctx("local", "test")
        self.ctx.intent = "apply for a job"
        self.proj = self.root / "Resume"
        (self.proj / "src" / "job_agent").mkdir(parents=True)
        (self.proj / "src" / "job_agent" / "scraper.py").write_text("")
        self.base = self.ws / "cv.txt"
        self.base.write_text("Jane Doe\n- Led a team of 4 engineers at Acme using Python and cut costs 30%")
        self.core.update_settings({"resume": {"path": str(self.proj), "feed_urls": [], "base_resume": str(self.base),
                                              "applicant": {"name": "Jane Doe", "email": "j@x.example", "phone": "", "linkedin": ""}}},
                                  replace=("resume",))
        self.jobs = [{"title": "Python Dev", "company": "Initech", "description": "build things", "url": "https://jobs.example/1"},
                     {"title": "Intern", "company": "Local", "description": "learn", "url": "http://127.0.0.1/apply"},
                     {"title": "Walk-in", "company": "Shop", "description": "stack shelves", "url": ""}]
        self.core.db.kv_set(studio.JOBS_KEY, {"query": "python", "location": "", "ts": time.time(), "jobs": self.jobs})

    def tool(self, name, /, **args):
        return run_tool(name, args, self.ctx)

    def test_every_tool_is_local_only(self):
        remote = ToolContext(gate=self.core.gate, db=self.core.db, origin="remote")
        for name in ("resume_status", "resume_find_jobs", "resume_tailor", "resume_stage"):
            self.assertIn("blocked for remote", run_tool(name, {"query": "x", "job_number": 1}, remote), name)

    def test_job_numbers_are_checked(self):
        for bad, words in ((0, "no job number 0"), (4, "no job number 4"), ("abc", "Give the job's number"), (None, "Give the job's number")):
            with self.assertRaises(Denied) as cm:
                studio._job(self.core.db, bad)
            self.assertIn(words, str(cm.exception))
        self.assertEqual(studio._job(self.core.db, "2")["company"], "Local")
        self.core.db.kv_set(studio.JOBS_KEY, {})
        with self.assertRaises(Denied):
            studio._job(self.core.db, 1)

    def test_a_model_cannot_smuggle_in_a_url(self):
        out = self.tool("resume_stage", job_number="https://evil.example/apply")
        self.assertTrue(out.startswith("DENIED"))

    def test_claim_check_flags_names_and_numbers_not_in_the_base(self):
        base = self.base.read_text()
        self.assertEqual(studio.unsupported(["- Led a team of 4 engineers at Acme using Python"], base), [])
        flagged = studio.unsupported(["- Managed 50 engineers at Google using Kubernetes and cut costs 90%"], base)
        for tok in ("50", "Google", "Kubernetes", "90%"):
            self.assertIn(tok, flagged)

    def test_find_jobs_falls_back_to_the_built_in_boards_and_numbers_the_list(self):
        found = [{"title": "Rust Dev", "company": "Hooli", "description": "write rust", "url": "https://h.example/1", "source": "remotive"},
                 {"title": "No description", "company": "X", "description": "", "url": ""}]
        with mock.patch.object(studio.jobboards, "search_jobs", return_value=found), mock.patch.object(studio, "_call") as call:
            out = self.tool("resume_find_jobs", query="rust")
        call.assert_not_called()
        self.assertIn("Found 1 jobs", out)
        self.assertIn("1. Rust Dev @ Hooli", out)
        self.assertEqual(studio.stored(self.core.db)["jobs"][0]["company"], "Hooli")

    def test_find_jobs_with_feeds_uses_the_worker(self):
        self.core.update_settings({"resume": {"feed_urls": ["https://feed.example/{query}"]}})
        worker = {"ok": True, "jobs": [{"title": "T", "company": "C", "description": "d", "url": "https://c.example"}]}
        with mock.patch.object(studio, "_call", return_value=worker) as call:
            out = self.tool("resume_find_jobs", query="python", location="Ohio")
        self.assertEqual(call.call_args.args[0], "find")
        self.assertEqual(call.call_args.args[1]["feed_urls"], ["https://feed.example/{query}"])
        self.assertIn("Found 1 jobs", out)
        with mock.patch.object(studio, "_call", return_value={"ok": False, "error": "timed out"}):
            self.assertIn("job search failed: timed out", self.tool("resume_find_jobs", query="python"))
        self.assertEqual(self.tool("resume_find_jobs", query="  "), "Say what kind of job to look for.")

    def test_tailor_saves_a_file_and_warns_about_invented_claims(self):
        steps = [{"ok": True, "bullets": ["- Led 4 engineers at Acme", "- Won an award at Google"]},
                 {"ok": True, "path": str(self.proj / "output" / "001_Initech_Resume.docx")}]
        with mock.patch.object(studio, "_call", side_effect=steps) as call:
            out = self.tool("resume_tailor", job_number=1)
        self.assertEqual([c.args[0] for c in call.call_args_list], ["tailor", "build"])
        self.assertIn("001_Initech_Resume.docx", out)
        self.assertIn("CHECK BEFORE USING", out)
        self.assertIn("Google", out.split("CHECK BEFORE USING")[1])
        self.assertEqual(call.call_args_list[0].args[1]["base"].replace("\r\n", "\n"), self.base.read_text().replace("\r\n", "\n"))

    def test_tailor_falls_back_to_text_when_word_is_unavailable(self):
        steps = [{"ok": True, "bullets": ["- Led 4 engineers at Acme"]}, {"ok": False, "error": "python-docx missing"}]
        with mock.patch.object(studio, "_call", side_effect=steps):
            out = self.tool("resume_tailor", job_number=1)
        self.assertIn(".txt", out)
        self.assertTrue(any((self.proj / "output").glob("001_*.txt")))

    def test_tailor_refuses_without_a_base_resume_or_a_list(self):
        self.core.update_settings({"resume": {"base_resume": ""}}, replace=())
        self.core.update_settings({"resume": {"path": str(self.proj), "feed_urls": [], "base_resume": "", "applicant": {}}}, replace=("resume",))
        self.assertIn("no base resume", self.tool("resume_tailor", job_number=1).lower())

    def test_stage_refuses_private_addresses_missing_links_and_missing_playwright(self):
        self.assertIn("private network", self.tool("resume_stage", job_number=2))
        self.assertIn("no web application link", self.tool("resume_stage", job_number=3))
        with mock.patch.object(studio, "check_public_url"), mock.patch.object(studio.importlib.util, "find_spec", return_value=None):
            self.assertIn("playwright", self.tool("resume_stage", job_number=1))

    def test_stage_opens_the_stored_page_only_and_submits_nothing(self):
        with mock.patch.object(studio, "check_public_url"), mock.patch.object(studio.importlib.util, "find_spec", return_value=object()), \
                mock.patch.object(studio.subprocess, "Popen") as popen:
            out = self.tool("resume_stage", job_number=1)
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0][2], "stage")
        spec = next((self.root / "data" / "resume").glob("stage-*.json"))
        data = json.loads(spec.read_text())
        self.assertEqual(data["url"], "https://jobs.example/1")
        self.assertEqual(set(data["profile"]), {"name", "email", "phone", "linkedin"})
        self.assertIn("nothing has been sent", out)
        self.assertIn("phone", out)                                  # unsaved contact fields are named, not invented

    def test_stage_needs_the_heads_to_agree(self):
        self.fake.reply_fn = lambda m, msgs: "VOTE: REJECT\nREASON: not now"
        with mock.patch.object(studio, "check_public_url"), mock.patch.object(studio.importlib.util, "find_spec", return_value=object()), \
                mock.patch.object(studio.subprocess, "Popen") as popen:
            out = self.tool("resume_stage", job_number=1)
        self.assertIn("did not all agree", out)
        popen.assert_not_called()

    def test_status_reports_readiness_and_what_is_missing(self):
        out = self.tool("resume_status")
        self.assertIn(str(self.proj), out)
        self.assertIn("Base resume: ready", out)
        self.assertIn("missing phone, linkedin", out)
        self.assertIn("built-in Remotive/Arbeitnow", out)
        self.core.update_settings({"resume": {"path": str(self.root / "nowhere")}})
        self.assertIn("Not ready", self.tool("resume_status"))

    def test_the_project_folder_is_validated(self):
        self.assertEqual(studio.folder(), self.proj)
        self.core.update_settings({"resume": {"path": str(self.root)}})
        with self.assertRaises(Denied):
            studio.folder()

    def test_call_returns_the_workers_result_or_a_plain_error(self):
        ok = {"out": 'noise\n@@RESULT@@{"ok": true, "n": 1}\n', "err": "", "code": 0, "timed_out": False}
        with mock.patch.object(studio.guard, "child_run", return_value=ok):
            self.assertEqual(studio._call("find", {}, 5), {"ok": True, "n": 1})
        crash = {"out": "", "err": "Traceback\nValueError: bad", "code": 1, "timed_out": False}
        with mock.patch.object(studio.guard, "child_run", return_value=crash):
            self.assertEqual(studio._call("find", {}, 5), {"ok": False, "error": "ValueError: bad"})
        slow = {"out": "", "err": "", "code": -1, "timed_out": True}
        with mock.patch.object(studio.guard, "child_run", return_value=slow):
            self.assertIn("5 seconds", studio._call("find", {}, 5)["error"])
        self.assertEqual(list((self.root / "data" / "resume").glob("find-*.json")), [])       # job files are cleaned up

    def test_hunt_has_the_resume_skill(self):
        self.assertIn("Resume Studio", MODES["jobs"].hand_skills)


class GuiClientTests(unittest.TestCase):
    def test_a_query_parameter_called_path_does_not_clash_with_the_url_path(self):
        from cerberus.gui.client import ApiClient
        c = ApiClient("http://127.0.0.1:1")
        resp = mock.Mock(status_code=200, content=b"{}", text="plain")
        resp.json.return_value = {}
        with mock.patch.object(c.session, "request", return_value=resp) as req, mock.patch.object(c.session, "get", return_value=resp) as get:
            c.get("/files/browse", path="C:\\x")
            self.assertEqual(req.call_args.kwargs["params"], {"path": "C:\\x"})
            self.assertEqual(c.get_text("/telemetry/report", days=7, text="true"), "plain")
            self.assertEqual(get.call_args.kwargs["params"], {"days": 7, "text": "true"})


if __name__ == "__main__":
    unittest.main()
