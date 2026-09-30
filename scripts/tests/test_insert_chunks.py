"""Ingest batch writes: failures are reported, and a just-recreated index is retried (#95)."""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR", str(Path(__file__).resolve().parents[2] / "api")))
from services import weaviate_client as wc

NOT_READY = ("could not find index for class VfyIngest. It might have been deleted "
             "in the meantime")


class FakeBatch:
    def __init__(self, coll):
        self.coll = coll

    def __enter__(self):
        self.added = []
        return self

    def add_object(self, properties):
        self.added.append(properties)

    def __exit__(self, *exc):
        # Weaviate reports failures once the batch flushes on exit.
        self.coll.attempts.append(list(self.added))
        outcome = self.coll.outcomes.pop(0) if self.coll.outcomes else None
        self.coll.batch.failed_objects = [
            SimpleNamespace(message=outcome, object_=SimpleNamespace(properties=p))
            for p in self.added] if outcome else []
        return False


class FakeCollection:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.attempts = []
        coll = self
        self.batch = SimpleNamespace(dynamic=lambda: FakeBatch(coll), failed_objects=[])


class InsertChunksTests(unittest.TestCase):
    def run_insert(self, outcomes, chunks=({"content": "a"}, {"content": "b"})):
        coll = self.coll = FakeCollection(outcomes)
        client = SimpleNamespace(collections=SimpleNamespace(get=lambda name: coll))
        with patch.object(wc, "get_client", lambda: client), \
             patch.object(wc, "_INSERT_RETRY_DELAY", 0):
            wc._insert_chunks_sync("VfyIngest", list(chunks))
        return coll

    def test_clean_batch_is_written_once(self):
        coll = self.run_insert([None])
        self.assertEqual(len(coll.attempts), 1)

    def test_index_not_ready_is_retried_with_only_the_failed_objects(self):
        coll = self.run_insert([NOT_READY, None])
        self.assertEqual(len(coll.attempts), 2)
        self.assertEqual(coll.attempts[1], [{"content": "a"}, {"content": "b"}])

    def test_index_that_never_appears_fails_after_the_last_attempt(self):
        with self.assertRaisesRegex(RuntimeError, "2 batch error"):
            self.run_insert([NOT_READY] * wc._INSERT_ATTEMPTS)
        self.assertEqual(len(self.coll.attempts), wc._INSERT_ATTEMPTS)

    def test_other_errors_fail_at_once_and_are_not_retried(self):
        with self.assertRaisesRegex(RuntimeError, "vector dimension mismatch"):
            self.run_insert(["vector dimension mismatch"])
        self.assertEqual(len(self.coll.attempts), 1)

    def test_failures_after_the_flush_are_not_missed(self):
        # The old check ran inside the batch block, before the flush, so this
        # case reported success with nothing stored.
        with self.assertRaises(RuntimeError):
            self.run_insert(["some write failure"])


if __name__ == "__main__":
    unittest.main()
