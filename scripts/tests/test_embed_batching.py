"""Embedding writes are bounded, and only timed-out embeddings are sent again (#613).

A write that leaves vectors to Weaviate's text2vec-ollama uses fixed-size batches
with one request in flight; objects whose embed request ran out of time in Weaviate
are sent again, up to EMBED_TIMEOUT_RETRIES times. Weaviate is a small fake.

Run on the host from api/ with the API's dependencies:

    python -m unittest discover -s ../scripts/tests -p test_embed_batching.py

or in the API image with the checkout mounted (02_ingest.sh does this).
"""
import copy
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api"))

from config import settings
from services import batch_write

TIMEOUT = ('failed to vectorize batch of objects: send POST request: Post '
           '"http://ollama:11434/api/embed": context deadline exceeded')
NOT_READY = "could not find index for class VfyIngest. It might have been deleted in the meantime"
CLIENT_ERROR = "DeadlineExceeded('Deadline Exceeded')"


class Batch:
    """Stores what it is given, except the UUIDs a pass is told to fail."""

    def __init__(self, collection):
        self.collection = collection
        self.failed_objects = []
        self.opened = []                    # ("fixed_size", size, requests) or ("dynamic",)
        self.sent = []                      # the UUIDs added, one list per pass

    def _open(self, mode):
        self.opened.append(mode)
        self.failed_objects = []
        self.pending = []
        return self

    def dynamic(self):
        return self._open(("dynamic",))

    def fixed_size(self, batch_size=100, concurrent_requests=2):
        return self._open(("fixed_size", batch_size, concurrent_requests))

    def __enter__(self):
        return self

    def add_object(self, properties, uuid, vector=None):
        self.pending.append({"id": str(uuid), "properties": copy.deepcopy(properties), "vector": vector})

    def __exit__(self, *args):
        if args[0] is not None:
            return False
        self.sent.append([record["id"] for record in self.pending])
        plan = self.collection.failures
        failures = plan.pop(0) if plan else (lambda ids: {})
        rejected = failures([record["id"] for record in self.pending])
        self.failed_objects = [SimpleNamespace(message=message, object_=SimpleNamespace(uuid=uuid.UUID(key)))
                               for key, message in rejected.items()]
        for record in self.pending:
            if record["id"] not in rejected:
                self.collection.stores[record["id"]] = self.collection.stores.get(record["id"], 0) + 1
                self.collection.rows[record["id"]] = record
        return False


class Collection:
    def __init__(self, failures=()):
        self.rows = {}
        self.stores = {}                    # UUID -> times stored
        self.failures = list(failures)      # per pass: f(sent UUIDs) -> {UUID: message}
        self.batch = Batch(self)
        self.query = SimpleNamespace(fetch_objects=self.fetch_objects)
        self.data = SimpleNamespace(delete_many=self.delete_many)
        self.config = SimpleNamespace(get=lambda: SimpleNamespace(vectorizer=SimpleNamespace(value="text2vec-ollama")))

    def fetch_objects(self, filters, **kwargs):
        return SimpleNamespace(objects=[
            SimpleNamespace(uuid=uuid.UUID(key), properties=copy.deepcopy(row["properties"]),
                            vector={"default": row["vector"] or [0.25, 0.5]})
            for key, row in self.rows.items() if key in filters.value])

    def delete_many(self, where):
        for key in where.value:
            self.rows.pop(key, None)
        return SimpleNamespace(failed=0)


def chunks(n):
    return [{"properties": {"content": f"chunk {i}", "source_file": "a.txt", "chunk_index": i}}
            for i in range(n)]


def vectored(n):
    return [{"id": str(uuid.uuid4()), "vector": [0.25, 0.5],
             "properties": {"content": f"chunk {i}", "source_file": "a.txt", "chunk_index": i}}
            for i in range(n)]


def fail(count, message=TIMEOUT):
    """A pass that fails the first `count` UUIDs it was sent."""
    return lambda ids: {key: message for key in ids[:count]}


class EmbedBatchingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        upload = patch.object(settings, "upload_dir", temp.name)
        upload.start()
        self.addCleanup(upload.stop)

    def insert(self, collection, records, **kwargs):
        kwargs.setdefault("exact", False)
        kwargs.setdefault("cleanup_owned", True)
        return batch_write.insert(collection, records, **kwargs)

    def test_vectorless_records_use_bounded_batches(self):
        collection = Collection()
        self.assertEqual(self.insert(collection, chunks(45)), 45)
        self.assertEqual(collection.batch.opened, [("fixed_size", 20, 1)])
        self.assertEqual((batch_write.EMBED_BATCH_SIZE, batch_write.EMBED_CONCURRENT_REQUESTS), (20, 1))

    def test_records_with_vectors_keep_dynamic_batching(self):
        collection = Collection()
        self.assertEqual(batch_write.insert(collection, vectored(5), exact=False), 5)
        self.assertEqual(collection.batch.opened, [("dynamic",)])

    def test_one_vectorless_record_among_vectored_ones_is_bounded(self):
        collection = Collection()
        records = vectored(3) + [{"id": str(uuid.uuid4()), "properties": {"content": "no vector"}}]
        self.assertEqual(batch_write.insert(collection, records, exact=False), 4)
        self.assertEqual(collection.batch.opened, [("fixed_size", 20, 1)])

    def test_timed_out_embeddings_are_sent_again_alone(self):
        collection = Collection([fail(2)])
        self.assertEqual(self.insert(collection, chunks(5)), 5)
        first, second = collection.batch.sent
        self.assertEqual(len(first), 5)
        self.assertEqual(second, first[:2])
        self.assertEqual(collection.batch.opened, [("fixed_size", 20, 1)] * 2)
        self.assertEqual(sorted(collection.stores.values()), [1] * 5)

    def test_retries_stop_after_the_limit_and_the_write_fails_with_its_counts(self):
        collection = Collection([fail(2)] * (batch_write.EMBED_TIMEOUT_RETRIES + 1))
        with self.assertRaises(RuntimeError) as raised:
            self.insert(collection, chunks(5))
        self.assertEqual(len(collection.batch.sent), batch_write.EMBED_TIMEOUT_RETRIES + 1)
        self.assertEqual(str(raised.exception), "Weaviate rejected 2 batch object(s): " + TIMEOUT)
        self.assertEqual(batch_write.write_counts(raised.exception), (5, 3))
        self.assertEqual(collection.rows, {})            # owned records rolled back

    def test_a_later_pass_can_still_succeed(self):
        collection = Collection([fail(3), fail(1)])
        self.assertEqual(self.insert(collection, chunks(4)), 4)
        self.assertEqual([len(ids) for ids in collection.batch.sent], [4, 3, 1])
        self.assertEqual(sorted(collection.stores.values()), [1] * 4)

    def test_other_failures_are_not_retried(self):
        for name, failures in (
                ("index not ready", fail(1, NOT_READY)),
                ("client-side send error", fail(5, CLIENT_ERROR)),
                ("mixed", lambda ids: {ids[0]: TIMEOUT, ids[1]: NOT_READY})):
            with self.subTest(name):
                collection = Collection([failures])
                with self.assertRaises(RuntimeError) as raised:
                    self.insert(collection, chunks(5))
                self.assertEqual(len(collection.batch.sent), 1)
                self.assertTrue(str(raised.exception).startswith("Weaviate rejected "))
                # _insert_chunks_sync reads the first pass's failures to retry a new index.
                self.assertEqual(len(collection.batch.failed_objects), len(failures(collection.batch.sent[0])))

    def test_a_failure_without_a_uuid_is_not_retried(self):
        class NoUuidBatch(Batch):
            def __exit__(self, *args):
                self.sent.append([record["id"] for record in self.pending])
                self.failed_objects = [SimpleNamespace(message=TIMEOUT, object_=None)]
                return False
        collection = Collection()
        collection.batch = NoUuidBatch(collection)
        with self.assertRaises(RuntimeError):
            self.insert(collection, chunks(2))
        self.assertEqual(len(collection.batch.sent), 1)

    def test_a_send_error_during_a_retry_keeps_the_first_pass_acknowledged(self):
        class SendFailsOnRetry(Batch):
            def add_object(self, properties, uuid, vector=None):
                if len(self.sent) == 1:
                    raise ConnectionError("send failed")
                super().add_object(properties, uuid, vector)
        collection = Collection([fail(2)])
        collection.batch = SendFailsOnRetry(collection)
        with self.assertRaises(ConnectionError) as raised:
            self.insert(collection, chunks(5))
        self.assertEqual(batch_write.write_counts(raised.exception), (5, 3))
        self.assertEqual(collection.rows, {})            # owned records rolled back

    def test_a_stream_that_changes_before_a_retry_fails(self):
        collection = Collection([fail(1)])
        rows = chunks(3)
        calls = []

        def records():
            calls.append(1)
            if len(calls) == 3:                # preflight, first pass, then the retry
                return iter([{"properties": {**rows[0]["properties"], "content": "changed"}}] + rows[1:])
            return iter(rows)
        with self.assertRaises(ValueError) as raised:
            self.insert(collection, records)
        self.assertEqual(str(raised.exception), "Record stream changed after preflight")
        self.assertEqual(len(collection.batch.sent), 1)

    def test_vectorizer_timeout_needs_both_phrases(self):
        timeout = batch_write._vectorizer_timeout
        self.assertTrue(timeout(SimpleNamespace(message=TIMEOUT)))
        self.assertFalse(timeout(SimpleNamespace(message="failed to vectorize batch of objects: model not found")))
        self.assertFalse(timeout(SimpleNamespace(message="context deadline exceeded")))
        self.assertFalse(timeout(SimpleNamespace(message=None)))
        self.assertFalse(timeout(object()))


if __name__ == "__main__":
    unittest.main()
