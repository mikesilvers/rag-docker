"""Per-file chunk limits for every strategy (#111): ceiling, floor and count."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path.insert(0, os.environ.get('RAG_TEST_API_DIR') or str(Path(__file__).resolve().parents[2] / 'api'))
from services import chunker

CEILING = chunker.MAX_CHUNK_CHARACTERS


class FakeModel:
    """Embeds every sentence identically (similar) or one-hot (dissimilar)."""
    def __init__(self, similar):
        self.similar = similar

    def encode(self, sentences, convert_to_numpy=True):
        if self.similar:
            return np.ones((len(sentences), 4))
        vectors = np.zeros((len(sentences), len(sentences) + 1))
        for i in range(len(sentences)):
            vectors[i, i] = 1.0
        return vectors


def semantic(text, similar, threshold=1.0, minimum=0):
    with patch.object(chunker, '_get_semantic_model', return_value=FakeModel(similar)):
        return chunker.chunk(text, 'semantic', similarity_threshold=threshold, min_chunk_size=minimum)


class CeilingTests(unittest.TestCase):
    def test_minimum_merging_never_exceeds_the_ceiling(self):
        # Fixed 50 with a minimum of 6000 used to make one 20,399-character chunk.
        chunks = chunker.chunk('word ' * 4000, 'fixed', chunk_size=50, min_chunk_size=6000)
        self.assertTrue(all(len(c) <= CEILING for c in chunks), [len(c) for c in chunks])
        self.assertEqual(''.join(chunks).replace(' ', ''), ('word ' * 4000).replace(' ', ''))

    def test_overlap_tail_is_not_merged_past_the_ceiling(self):
        # 6000/6000 used to give [6000, 6000, 8001].
        chunks = chunker.chunk('x' * 14001, 'overlap', chunk_size=6000, chunk_overlap_size=0, min_chunk_size=6000)
        self.assertTrue(all(len(c) <= CEILING for c in chunks), [len(c) for c in chunks])
        self.assertEqual(''.join(chunks), 'x' * 14001)

    def test_semantic_chunks_are_split_at_the_ceiling(self):
        text = '. '.join(['alpha beta gamma delta'] * 1000) + '.'
        chunks = semantic(text, similar=True)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= CEILING for c in chunks), [len(c) for c in chunks])

    def test_context_aware_tables_are_split_at_the_ceiling(self):
        class Element:
            def __init__(self, text, category):
                self.text, self.category = text, category
            def __str__(self):
                return self.text
        table = Element('cell ' * 3000, 'Table')
        chunks = chunker.chunk('', 'context_aware', chunk_size=1000, min_chunk_size=0, elements=[table])
        self.assertTrue(all(len(c) <= CEILING for c in chunks), [len(c) for c in chunks])


class FloorTests(unittest.TestCase):
    def test_semantic_with_no_minimum_does_not_flood_tiny_chunks(self):
        text = '. '.join('abcdefghij'[i % 10] for i in range(3000)) + '.'
        chunks = semantic(text, similar=False, threshold=1.0, minimum=0)
        self.assertLess(len(chunks), 3000 // 10)
        self.assertTrue(all(len(c) >= chunker.MIN_SEMANTIC_CHUNK_CHARACTERS for c in chunks[:-1]))

    def test_semantic_keeps_a_larger_saved_minimum(self):
        text = '. '.join(['sentence number one'] * 200) + '.'
        chunks = semantic(text, similar=False, threshold=1.0, minimum=200)
        self.assertTrue(all(len(c) >= 200 for c in chunks[:-1]))


class CountTests(unittest.TestCase):
    def test_fixed_and_language_refuse_too_many_chunks_before_splitting(self):
        text = 'y' * (50 * (chunker.MAX_CHUNKS_PER_FILE + 10))
        for strategy in ('fixed', 'language'):
            with self.subTest(strategy=strategy):
                with patch.object(chunker, 'CharacterTextSplitter') as fixed, \
                     patch.object(chunker, 'RecursiveCharacterTextSplitter') as language:
                    with self.assertRaisesRegex(ValueError, 'per-file limit'):
                        chunker.chunk(text, strategy, chunk_size=50, chunk_overlap_size=0, min_chunk_size=0)
                    fixed.assert_not_called(); language.assert_not_called()

    def test_files_under_the_limit_are_unchanged(self):
        text = 'z' * (50 * 100)
        self.assertEqual(len(chunker.chunk(text, 'fixed', chunk_size=50, min_chunk_size=0)), 100)

    def test_semantic_refuses_too_many_sentences_before_embedding(self):
        text = 'a. ' * (chunker.MAX_SEMANTIC_SENTENCES + 1)
        with patch.object(chunker, '_get_semantic_model') as model:
            with self.assertRaisesRegex(ValueError, 'per-file limit'):
                chunker.chunk(text, 'semantic', min_chunk_size=0)
            model.assert_not_called()


class TextLimitTests(unittest.TestCase):
    def test_every_strategy_refuses_an_oversized_file_before_any_work(self):
        text = 'q' * (chunker.MAX_TEXT_CHARACTERS + 1)
        for strategy in ('fixed', 'overlap', 'language', 'context_aware', 'semantic'):
            with self.subTest(strategy=strategy):
                with patch.object(chunker, 'CharacterTextSplitter') as fixed, \
                     patch.object(chunker, 'RecursiveCharacterTextSplitter') as language, \
                     patch.object(chunker, '_get_semantic_model') as model:
                    with self.assertRaisesRegex(ValueError, 'characters'):
                        chunker.chunk(text, strategy, chunk_size=6000, chunk_overlap_size=0, min_chunk_size=0)
                    fixed.assert_not_called(); language.assert_not_called(); model.assert_not_called()

    def test_ceiling_split_breaks_at_newlines_and_keeps_every_character(self):
        # Newline-separated words with no spaces used to be cut mid-word.
        text = ('linetext\n' * 2000).strip()
        chunks = chunker._cap_chunk_length([text])
        self.assertTrue(all(len(c) <= CEILING for c in chunks))
        self.assertTrue(all(c.endswith('linetext') for c in chunks), [c[-12:] for c in chunks])
        self.assertEqual(''.join(chunks).replace('\n', ''), text.replace('\n', ''))

    def test_ceiling_split_is_linear(self):
        import time
        text = 'w' * 5_000_000
        started = time.perf_counter()
        chunks = chunker._cap_chunk_length([text])
        self.assertLess(time.perf_counter() - started, 2.0)
        self.assertEqual(sum(map(len, chunks)), len(text))


if __name__ == '__main__':
    unittest.main()
