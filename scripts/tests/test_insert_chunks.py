"""Ingest retries a just-recreated index only after owned write cleanup (#95)."""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api"))
from services import batch_write, weaviate_client as wc

NOT_READY = ("could not find index for class VfyIngest. It might have been deleted "
             "in the meantime")


class InsertChunksTests(unittest.TestCase):
    def run_insert(self, outcomes, chunks=({"content": "a"}, {"content": "b"})):
        coll = SimpleNamespace(batch=SimpleNamespace(failed_objects=[]))
        client = SimpleNamespace(collections=SimpleNamespace(get=lambda name: coll))
        attempts = []
        outcomes = list(outcomes)

        def insert(collection, records, *, exact, cleanup_owned):
            self.assertIs(collection, coll)
            self.assertFalse(exact)
            self.assertTrue(cleanup_owned)
            attempts.append([record["properties"] for record in records()])
            outcome = outcomes.pop(0) if outcomes else None
            coll.batch.failed_objects = ([SimpleNamespace(message=outcome)]
                                         if outcome else [])
            if outcome:
                raise RuntimeError(f"Weaviate rejected 2 batch object(s): {outcome}")
            return len(attempts[-1])

        with patch.object(wc, "get_client", return_value=client), \
             patch.object(batch_write, "insert", side_effect=insert), \
             patch.object(wc, "_INSERT_RETRY_DELAY", 0):
            wc._insert_chunks_sync("VfyIngest", list(chunks))
        return attempts

    def test_clean_batch_is_written_once(self):
        self.assertEqual(len(self.run_insert([None])), 1)

    def test_index_not_ready_is_retried_after_cleanup(self):
        attempts = self.run_insert([NOT_READY, None])
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[1], [{"content": "a"}, {"content": "b"}])

    def test_index_that_never_appears_fails_after_the_last_attempt(self):
        with self.assertRaisesRegex(RuntimeError, "Weaviate rejected 2 batch"):
            self.run_insert([NOT_READY] * wc._INSERT_ATTEMPTS)

    def test_other_errors_fail_at_once(self):
        with self.assertRaisesRegex(RuntimeError, "vector dimension mismatch"):
            self.run_insert(["vector dimension mismatch"])

    def test_uncertain_cleanup_is_never_retried(self):
        coll = SimpleNamespace(batch=SimpleNamespace(
            failed_objects=[SimpleNamespace(message=NOT_READY)]))
        client = SimpleNamespace(collections=SimpleNamespace(get=lambda name: coll))
        error = batch_write.BatchCleanupError(RuntimeError(NOT_READY), RuntimeError("cleanup uncertain"))
        with patch.object(wc, "get_client", return_value=client), \
             patch.object(batch_write, "insert", side_effect=error) as insert:
            with self.assertRaises(batch_write.BatchCleanupError):
                wc._insert_chunks_sync("VfyIngest", [{"content": "a"}])
        insert.assert_called_once()


if __name__ == "__main__":
    unittest.main()
