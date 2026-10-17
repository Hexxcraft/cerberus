import unittest

from cerberus import dossier, learning, memory
from cerberus.core import Cerberus

from .helpers import FakeOllama, Isolated


class MemoryTests(unittest.TestCase):
    def setUp(self):
        iso = Isolated()
        self.root = iso.__enter__()
        self.addCleanup(iso.__exit__, None, None, None)
        self.core = Cerberus(self.root / "m.db")
        self.core.ollama = self.fake = FakeOllama()
        self.addCleanup(self.core.db.close)
        self.mem = self.core.memory

    def keys(self, facts):
        return {f["key"] for f in facts}

    def test_keyword_fallback_keeps_core_and_matches(self):
        got = self.keys(self.mem.relevant_facts("how should I paint my minis tonight"))
        self.assertEqual(self.mem.last_mode, "keywords")
        self.assertTrue(dossier.core_fact_keys() <= got)
        self.assertIn("hobbies", got)
        self.assertNotIn("home_look", got)
        self.assertNotIn("music_taste", got)

    def test_small_profiles_are_included_whole(self):
        self.core.db.execute("DELETE FROM facts WHERE key NOT IN ('name','current_job')")
        self.assertEqual(self.keys(self.mem.relevant_facts("anything")), {"name", "current_job"})
        self.assertEqual(self.mem.last_mode, "all")

    def test_embeddings_rank_and_cache(self):
        self.fake.installed.append("nomic-embed-text:latest")
        learning.upsert_fact(self.core.db, "favorite_pasta", "loves penne arrabbiata with extra garlic", "preference")
        got = self.keys(self.mem.relevant_facts("favorite pasta penne arrabbiata garlic"))
        self.assertEqual(self.mem.last_mode, "embeddings")
        self.assertIn("favorite_pasta", got)
        self.assertLessEqual(len(got - dossier.core_fact_keys()), memory.FACT_K + 3)
        stored = self.core.db.one("SELECT COUNT(*) AS n FROM embeddings WHERE kind='fact'")["n"]
        self.assertGreaterEqual(stored, 12)
        calls = self.fake.embed_calls
        self.mem.relevant_facts("favorite pasta penne arrabbiata garlic")
        self.assertEqual(self.fake.embed_calls, calls + 1)           # only the query is embedded again
        learning.upsert_fact(self.core.db, "favorite_pasta", "now prefers carbonara", "preference")
        self.mem.relevant_facts("carbonara")
        row = self.core.db.one("SELECT text FROM embeddings WHERE ref='favorite_pasta'")
        self.assertIn("carbonara", row["text"])                      # a changed fact is re-embedded

    def test_embedding_failure_falls_back_quietly(self):
        self.fake.installed.append("nomic-embed-text:latest")
        self.fake.embed_broken = True
        got = self.keys(self.mem.relevant_facts("paint minis"))
        self.assertEqual(self.mem.last_mode, "keywords")
        self.assertIn("hobbies", got)

    def test_recall_messages_by_meaning(self):
        self.fake.installed.append("nomic-embed-text:latest")
        db = self.core.db
        db.add_message("conversation", "user", "I keep thinking about moving my homelab to a rack in the garage")
        db.add_message("conversation", "user", "Dinner was pasta with garlic and basil tonight again")
        new = db.add_message("conversation", "user", "Where should the homelab rack go in the garage")
        echoes = self.mem.recall_messages("Where should the homelab rack go in the garage", exclude_id=new)
        self.assertEqual(len(echoes), 1)
        self.assertIn("homelab", echoes[0])

    def test_recall_messages_keyword_fallback(self):
        db = self.core.db
        db.add_message("conversation", "user", "My brother is visiting from Tampa next weekend")
        new = db.add_message("conversation", "user", "Plan something for my brother visiting")
        echoes = self.mem.recall_messages("Plan something for my brother visiting", exclude_id=new)
        self.assertTrue(any("Tampa" in e for e in echoes))

    def test_prune_removes_orphans(self):
        self.fake.installed.append("nomic-embed-text:latest")
        self.mem.relevant_facts("paint minis")
        self.core.db.execute("DELETE FROM facts WHERE key='hobbies'")
        self.mem.prune()
        self.assertIsNone(self.core.db.one("SELECT 1 FROM embeddings WHERE ref='hobbies'"))

    def test_pipeline_only_injects_relevant_facts(self):
        events = list(self.core.orchestrator.run("help me plan painting my minis this week", "conversation", {"deep": True}))
        self.assertEqual(events[-1]["type"], "done")
        system = " ".join(m["content"] for m in self.fake.calls[0][1])
        self.assertIn("minis", system)
        self.assertNotIn("IKEA", system)


if __name__ == "__main__":
    unittest.main()
