import unittest

from cerberus import council as C
from cerberus.core import Cerberus
from cerberus.modes import MODES

from .helpers import FakeOllama, Isolated

LONG_BAD = "The report says WRONGFACT happened in 1850, and the rest of this paragraph is perfectly ordinary prose that goes on a while."
LONG_GOOD = "The report says the event happened in 1850, and the rest of this paragraph is perfectly ordinary prose that goes on a while."


def drive(gen):
    events = []
    while True:
        try:
            events.append(next(gen))
        except StopIteration as stop:
            return events, stop.value


def draft_of(user: str) -> str:
    return user.rsplit("DRAFT:\n", 1)[1] if "DRAFT:\n" in user else user


class ParserTests(unittest.TestCase):
    def test_verdict_ok_fix_and_garbage(self):
        self.assertEqual(C.parse_verdict("VERDICT: OK"), {"ok": True, "issues": []})
        v = C.parse_verdict("Sure.\nVERDICT: FIX\nISSUE: 1850 => the year is invented => remove the year\nISSUE: bad")
        self.assertFalse(v["ok"])
        self.assertEqual(len(v["issues"]), 1)                       # the too-short second issue is ignored
        self.assertEqual(v["issues"][0]["fix"], "remove the year")
        self.assertIsNone(C.parse_verdict("I think it is fine"))      # unreadable = abstain, never an objection
        self.assertIsNone(C.parse_verdict("VERDICT: FIX"))            # FIX without a concrete issue = abstain

    def test_rulings_and_votes(self):
        self.assertEqual(C.parse_rulings("1: TRUE\n2: FALSE", 3), [True, False, False])
        self.assertEqual(C.parse_vote("VOTE: APPROVE\nREASON: fine"), (True, "fine"))
        self.assertEqual(C.parse_vote("vote = reject\nreason: wrong folder")[0], False)
        self.assertEqual(C.parse_vote("looks good to me")[0], None)

    def test_levels(self):
        self.assertEqual(C.level_info("nonsense")["key"], "careful")
        self.assertTrue(C.level_info("quick")["one_head_chat"])
        self.assertFalse(C.level_info("careful")["one_head_chat"])
        self.assertTrue(C.level_info("meticulous")["stage_checks"])


class CouncilBase(unittest.TestCase):
    def setUp(self):
        iso = Isolated()
        self.root = iso.__enter__()
        self.addCleanup(iso.__exit__, None, None, None)
        self.core = Cerberus(self.root / "t.db")
        self.core.ollama = self.fake = FakeOllama()
        self.addCleanup(self.core.db.close)
        self.council = self.core.council

    def run_mode(self, mode="writing", text="Write me a paragraph about the report", options=None):
        return list(self.core.orchestrator.run(text, mode, options or {}, "local"))


class AnswerReviewTests(CouncilBase):
    def test_hallucination_is_caught_and_the_writer_redoes_it(self):
        def reply(model, messages):
            system, user = messages[0]["content"], messages[-1]["content"]
            if "CLAIMED PROBLEMS" in user:
                return "1: TRUE" if "WRONGFACT" in draft_of(user.split("CLAIMED PROBLEMS")[0]) else "1: FALSE"
            if "fact-checker" in system:
                return ("VERDICT: FIX\nISSUE: WRONGFACT => the event is invented => say what the report actually says"
                        if "WRONGFACT" in draft_of(user) else "VERDICT: OK")
            if "CONFIRMED THESE PROBLEMS" in user:
                return LONG_GOOD
            return LONG_BAD
        self.fake.reply_fn = reply
        evs = self.run_mode()
        phases = [e["phase"] for e in evs if e["type"] == "council"]
        self.assertEqual(phases, ["review", "found", "review", "clean"])
        self.assertTrue(any(e["type"] == "stage_start" and e["key"].startswith("fix1-") for e in evs))
        final = next(e for e in evs if e["type"] == "final")["text"]
        self.assertNotIn("WRONGFACT", final)
        stats = self.council.stats()
        self.assertEqual((stats["caught"], stats["fixed"], stats["unresolved"]), (2, 2, 0))
        self.assertTrue(any("event is invented" in x["text"] for x in self.council.lessons()))   # it learned from the slip

    def test_quick_level_skips_all_checking(self):
        self.fake.reply_fn = lambda m, msgs: LONG_BAD
        evs = self.run_mode(options={"thinking": "quick"})
        self.assertFalse([e for e in evs if e["type"] == "council"])
        self.assertIn("WRONGFACT", next(e for e in evs if e["type"] == "final")["text"])

    def test_one_heads_false_alarm_is_overruled(self):
        author = MODES["writing"].stages_for("x", {})[-1].role
        accuser = "the Editor" if author != "editor" else "the Generalist"

        def reply(model, messages):
            system = messages[0]["content"]
            if "CLAIMED PROBLEMS" in messages[-1]["content"]:
                return "1: FALSE"
            if "fact-checker" in system:
                return ("VERDICT: FIX\nISSUE: ordinary prose => this is wrong somehow => change it"
                        if system.startswith(f"You are {accuser}") else "VERDICT: OK")
            return LONG_GOOD
        self.fake.reply_fn = reply
        evs = self.run_mode()
        self.assertFalse([e for e in evs if e["type"] == "stage_start" and e["key"].startswith("fix")])
        self.assertEqual([e["phase"] for e in evs if e["type"] == "council"], ["review", "clean"])
        self.assertEqual(self.council.stats()["false_alarms"], 1)

    def test_one_accuser_is_believed_when_another_head_agrees(self):
        author = MODES["writing"].stages_for("x", {})[-1].role
        accuser = "the Editor" if author != "editor" else "the Generalist"

        def reply(model, messages):
            system, user = messages[0]["content"], messages[-1]["content"]
            if "CLAIMED PROBLEMS" in user:
                return "1: TRUE"
            if "fact-checker" in system:
                return ("VERDICT: FIX\nISSUE: WRONGFACT => invented => remove it"
                        if system.startswith(f"You are {accuser}") and "WRONGFACT" in draft_of(user) else "VERDICT: OK")
            return LONG_GOOD if "CONFIRMED THESE PROBLEMS" in user else LONG_BAD
        self.fake.reply_fn = reply
        evs = self.run_mode()
        self.assertNotIn("WRONGFACT", next(e for e in evs if e["type"] == "final")["text"])

    def test_unresolved_doubts_are_said_out_loud(self):
        def reply(model, messages):
            system = messages[0]["content"]
            if "CLAIMED PROBLEMS" in messages[-1]["content"]:
                return "1: TRUE"
            if "fact-checker" in system:
                return "VERDICT: FIX\nISSUE: WRONGFACT => still invented => remove it"
            return LONG_BAD
        self.fake.reply_fn = reply
        evs = self.run_mode()
        final = next(e for e in evs if e["type"] == "final")["text"]
        self.assertIn("The other heads still doubt", final)
        self.assertEqual(self.council.stats()["unresolved"] > 0, True)
        self.assertEqual(self.council.lessons(), [])                      # doubts that were never settled are not learned

    def test_doubts_are_said_in_conversation_too(self):
        def reply(model, messages):
            system = messages[0]["content"]
            if "CLAIMED PROBLEMS" in messages[-1]["content"]:
                return "1: TRUE"
            if "fact-checker" in system:
                return "VERDICT: FIX\nISSUE: WRONGFACT => still invented => remove it"
            return LONG_BAD
        self.fake.reply_fn = reply
        final = next(e for e in self.run_mode(mode="conversation", text="Tell me about the report") if e["type"] == "final")["text"]
        self.assertIn("still doubt", final)

    def test_nonsense_issue_is_overruled_even_when_both_heads_raise_it(self):
        def reply(model, messages):
            system, user = messages[0]["content"], messages[-1]["content"]
            if "CLAIMED PROBLEMS" in user:
                return "1: FALSE"
            if "fact-checker" in system:
                return "VERDICT: FIX\nISSUE: ordinary prose => this is wrong somehow => change it"
            return LONG_GOOD
        self.fake.reply_fn = reply
        evs = self.run_mode()
        self.assertFalse([e for e in evs if e["type"] == "stage_start" and e["key"].startswith("fix")])
        self.assertEqual([e["phase"] for e in evs if e["type"] == "council"], ["review", "clean"])
        self.assertEqual(self.council.stats()["false_alarms"], 2)

    def test_a_wrecked_correction_is_rejected(self):
        def reply(model, messages):
            system, user = messages[0]["content"], messages[-1]["content"]
            if "CLAIMED PROBLEMS" in user:
                return "1: TRUE"
            if "fact-checker" in system:
                return "VERDICT: FIX\nISSUE: WRONGFACT => invented => remove it"
            if "CONFIRMED THESE PROBLEMS" in user:
                return "ok"
            return LONG_BAD
        self.fake.reply_fn = reply
        final = next(e for e in self.run_mode() if e["type"] == "final")["text"]
        self.assertIn("WRONGFACT", final)                                  # kept the original rather than the stub
        self.assertIn("still doubt", final)

    def test_unreadable_reviews_never_block_an_answer(self):
        self.fake.reply_fn = lambda m, msgs: "no verdict here" if "fact-checker" in msgs[0]["content"] else LONG_GOOD
        evs = self.run_mode()
        self.assertIn("final", [e["type"] for e in evs])
        self.assertEqual([e["phase"] for e in evs if e["type"] == "council"], ["review", "clean"])

    def test_meticulous_checks_each_step(self):
        self.fake.reply_fn = lambda m, msgs: "VERDICT: OK" if "fact-checker" in msgs[0]["content"] else LONG_GOOD
        careful = len([e for e in self.run_mode() if e["type"] == "council" and e["phase"] == "review"])
        meticulous = len([e for e in self.run_mode(options={"thinking": "meticulous"}) if e["type"] == "council" and e["phase"] == "review"])
        self.assertEqual(careful, 1)
        self.assertGreater(meticulous, careful)

    def test_start_event_reports_the_level_and_low_memory_downgrades(self):
        self.fake.reply_fn = lambda m, msgs: LONG_GOOD
        self.assertEqual(self.run_mode()[0]["thinking"], "careful")
        self.core.orchestrator._low_memory = lambda: True
        evs = self.run_mode()
        self.assertEqual(evs[0]["thinking"], "quick")
        self.assertTrue(any(e["type"] == "council" and e["phase"] == "skipped" for e in evs))


class ChangeVoteTests(CouncilBase):
    def vote(self, reply_fn):
        self.fake.reply_fn = reply_fn
        return self.council.vote_change("pc_write", "Create the file x", "make a file")

    def test_unanimous_approval(self):
        ok, why = self.vote(lambda m, msgs: "VOTE: APPROVE\nREASON: fine")
        self.assertTrue(ok)
        self.assertEqual(len(self.fake.calls), 3)
        self.assertEqual(self.council.stats()["changes_approved"], 1)

    def test_one_refusal_stops_it_and_names_the_head(self):
        ok, why = self.vote(lambda m, msgs: "VOTE: REJECT\nREASON: wrong folder" if m.startswith("llama3") else "VOTE: APPROVE\nREASON: ok")
        self.assertFalse(ok)
        self.assertIn("the Generalist: REFUSES", why)
        self.assertIn("wrong folder", why)
        self.assertEqual(self.council.stats()["changes_blocked"], 1)

    def test_unclear_answers_count_as_no(self):
        ok, why = self.vote(lambda m, msgs: "sure, go ahead")
        self.assertFalse(ok)
        self.assertIn("counts as a no", why)

    def test_errors_and_missing_ollama_fail_closed(self):
        def boom(m, msgs):
            raise RuntimeError("kaput")
        self.assertFalse(self.vote(boom)[0])
        self.fake.is_up = lambda: False
        self.assertFalse(self.council.vote_change("pc_write", "x", "y")[0])


class LessonTests(CouncilBase):
    def test_lessons_are_stored_deduped_capped_and_matched(self):
        issue = {"problem": "claimed the invoice total was 400 dollars", "fix": "the total is 450"}
        self.council.add_lessons("writing", [issue])
        self.council.add_lessons("writing", [issue])
        self.assertEqual(len(self.council.lessons()), 1)
        self.assertIn("invoice", self.council.lessons_block("writing", "summarise the invoice"))
        self.assertEqual(self.council.lessons_block("coding", "summarise the invoice"), "")
        for n in range(80):
            self.council.add_lessons("writing", [{"problem": f"distinct mistake number {n} here", "fix": ""}])
        self.assertLessEqual(len(self.council.lessons()), C.LESSON_CAP)
        self.council.forget_lessons()
        self.assertEqual(self.council.lessons(), [])


if __name__ == "__main__":
    unittest.main()
