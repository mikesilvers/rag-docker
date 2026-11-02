"""The corpus preparation rules state the app's own ingest limits (#615).

ui/public/corpus-preparation-rules.md is downloaded from the Import page. Every
limit it states is derived here from where the app keeps it: the chunker, the
ingest pipeline, the settings defaults, the chunk-setting bounds, the proxy and
docker-compose.yml. A limit changed without the file fails this test, and so
does a number left in the file that no source accounts for.

Runs in the API image with the repository mounted (02_ingest.sh), or anywhere
the API's dependencies are installed:
    RAG_TEST_API_DIR=api python scripts/tests/test_corpus_rules.py
"""
import ast
import inspect
import os
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, os.environ.get('RAG_TEST_API_DIR') or str(ROOT / 'api'))

import config  # noqa: E402
from models import schemas  # noqa: E402
from services import chunker, ingest_pipeline  # noqa: E402

RULES = ROOT / 'ui/public/corpus-preparation-rules.md'
MIB = 1024 * 1024
GIB = 1024 * MIB


def bounds(annotated):
    """(ge, le) of an Annotated[int, ..., Field(ge=, le=)] type."""
    ge = le = None
    for item in annotated.__metadata__:
        for constraint in getattr(item, 'metadata', ()):
            ge = getattr(constraint, 'ge', ge)
            le = getattr(constraint, 'le', le)
    return ge, le


def proxy_body_limits_mb():
    text = (ROOT / 'proxy/locations.conf').read_text()
    return [int(n) for n in re.findall(r'^\s*client_max_body_size\s+(\d+)m;', text, re.M)]


def compose_zip_settings():
    text = (ROOT / 'docker-compose.yml').read_text()
    return {name.lower(): int(value)
            for name, value in re.findall(r'^\s*(MAX_ZIP_[A-Z_]+):\s*(\d+)\s*$', text, re.M)}


def client_upload_mb():
    text = (ROOT / 'ui/src/api/client.ts').read_text()
    return int(re.search(r'export const MAX_UPLOAD_MB = (\d+)', text).group(1))


def language_split_levels():
    """The separators chunk_language splits at before single characters: each
    is a level at which a piece longer than chunk_size is split again."""
    source = inspect.getsource(chunker.chunk_language)
    separators = ast.literal_eval(re.search(r'separators=(\[.*?\])', source).group(1))
    assert separators[-1] == '', separators
    return len(separators) - 1


def zip_default(name):
    # The class default, not the settings instance: an environment variable
    # in the image must not hide a changed default.
    return config.Settings.model_fields[name].default


def expected():
    """{phrase the file must contain: the number(s) it accounts for}."""
    upload_mb = proxy_body_limits_mb()[0]
    expanded = zip_default('max_zip_expanded_bytes')
    members = zip_default('max_zip_members')
    ratio = zip_default('max_zip_member_ratio')
    exempt = ingest_pipeline._RATIO_EXEMPT_BYTES
    size_min, size_max = bounds(schemas.ChunkSize)
    min_min, min_max = bounds(schemas.MinChunkSize)
    fields = schemas.IngestConfig.model_fields
    size = fields['chunk_size'].default
    overlap = fields['chunk_overlap'].default
    minimum = fields['min_chunk_size'].default
    chunks = chunker.MAX_CHUNKS_PER_FILE
    text = chunker.MAX_TEXT_CHARACTERS
    text_target = int(text * 0.7)
    chunk_target = int(chunks * 0.8)
    output_target = int(chunker.MAX_OVERLAP_OUTPUT_CHARACTERS * 0.8)
    # Section 4's worst-case counts with the default settings: characters per
    # chunk, and the text at which a file reaches the chunk target. fixed and
    # overlap cut by size; language and context_aware keep pieces whole, so a
    # chunk can be only a little over half full.
    per_chunk = {'fixed': size, 'overlap': size - overlap,
                 'language': (size - overlap) // 2, 'context_aware': size // 2}
    # _cap_chunk_length cuts an over-long chunk at whitespace no earlier than
    # half the cap, so a piece holds at least this many characters.
    cap_piece = chunker.MAX_CHUNK_CHARACTERS // 2
    # language's count whatever the structure: 2 per (size - overlap) for the
    # packing, plus 2 for each long piece, and at each separator level the long
    # pieces are disjoint and at least chunk_size long, so at most text / size.
    levels = language_split_levels()
    language_any = 2 + 2 * levels
    per_chunk_any = (size - overlap) // language_any
    for name, value in (('expanded', expanded % GIB), ('exempt', exempt % MIB)):
        assert value == 0, f'{name} is no longer a whole number of GiB/MiB; reword the file'
    phrases = {
        f'{upload_mb} MB': [upload_mb],
        f'{expanded // GIB} GiB': [expanded // GIB],
        f'{members:,} entries': [members],
        f'{ratio:,} times its compressed size': [ratio],
        f'{exempt // MIB} MiB': [exempt // MIB],
        f'{text:,} characters': [text, chunker.MAX_OVERLAP_OUTPUT_CHARACTERS,
                                 ingest_pipeline.MAX_JSON_TEXT_CHARACTERS],
        f'{chunks:,} chunks': [chunks, chunker.MAX_OVERLAP_WINDOWS],
        f'{ingest_pipeline.MAX_JSON_RECORDS:,} records': [ingest_pipeline.MAX_JSON_RECORDS],
        f'{ingest_pipeline.MAX_JSONL_LINE_BYTES:,} bytes': [ingest_pipeline.MAX_JSONL_LINE_BYTES],
        f'{ingest_pipeline.MAX_GZIP_DECOMPRESSED_BYTES // MIB} MB':
            [ingest_pipeline.MAX_GZIP_DECOMPRESSED_BYTES // MIB],
        f'{ingest_pipeline.MAX_JSON_INPUT_BYTES:,} bytes': [ingest_pipeline.MAX_JSON_INPUT_BYTES],
        f'{chunker.MAX_SEMANTIC_SENTENCES:,} sentences': [chunker.MAX_SEMANTIC_SENTENCES],
        f'{chunker.MAX_CHUNK_CHARACTERS:,} characters': [chunker.MAX_CHUNK_CHARACTERS],
        f'{size_min:,} to {size_max:,}': [size_min, size_max],
        f'{min_min:,} to {min_max:,}': [min_min, min_max],
        f'default {size:,}': [size],
        f'default {overlap:,}': [overlap],
        f'default {minimum:,}': [minimum],
        f'{text_target:,} characters': [text_target],
        f'{chunk_target:,} chunks': [chunk_target],
        f'{output_target:,} characters (80 %': [output_target],
        f'one for every {cap_piece:,} characters': [cap_piece],
    }
    for strategy in ('overlap', 'language', 'context_aware'):
        phrases[f'one chunk per {per_chunk[strategy]:,} characters'] = [per_chunk[strategy]]
    for strategy in per_chunk:
        at_target = chunk_target * per_chunk[strategy]
        phrases[f'{chunk_target:,} chunks at about {at_target:,} characters'] = [at_target]
    phrases[f'(chunk_size − chunk_overlap) ÷ {language_any}` characters'] = [language_any]
    phrases[f'one chunk per {per_chunk_any:,} characters'] = [per_chunk_any]
    phrases[f'{chunk_target:,} chunks at about {chunk_target * per_chunk_any:,} characters'] = [
        chunk_target * per_chunk_any]
    phrases.update({
        '70 %': [70],
        '80 %': [80],
    })
    return phrases


def prose(markdown):
    """The file without its fenced code blocks (the conversion example)."""
    return re.sub(r'^```.*?^```[ \t]*$', '', markdown, flags=re.M | re.S)


class CorpusRulesTests(unittest.TestCase):
    def setUp(self):
        self.text = RULES.read_text()
        self.phrases = expected()

    def test_every_limit_is_stated(self):
        # Whitespace collapsed, so a phrase may wrap across lines.
        text = ' '.join(prose(self.text).split())
        for phrase in self.phrases:
            with self.subTest(phrase=phrase):
                # Whole numbers only: "10,000 chunks" must not match "110,000 chunks".
                self.assertTrue(re.search(r'(?<![\d,])' + re.escape(phrase), text),
                                f'{RULES.name} does not state "{phrase}"')

    def test_no_unaccounted_number(self):
        # Every number of two or more digits outside the code blocks. Single
        # digits (section numbers, "UTF-8", "2 GiB") are left out; the units
        # among them are checked as phrases above.
        accounted = {n for numbers in self.phrases.values() for n in numbers}
        for found in re.findall(r'\d+(?:,\d{3})*', prose(self.text)):
            if len(found.replace(',', '')) < 2:
                continue
            with self.subTest(number=found):
                self.assertIn(int(found.replace(',', '')), accounted,
                              f'{found} in {RULES.name} is not one of the app\'s limits')

    def test_operator_settings_agree(self):
        compose = compose_zip_settings()
        for name in ('max_zip_expanded_bytes', 'max_zip_members', 'max_zip_member_ratio'):
            with self.subTest(setting=name):
                self.assertEqual(compose.get(name), zip_default(name))
        limits = proxy_body_limits_mb()
        self.assertGreaterEqual(len(limits), 2, limits)
        self.assertEqual(set(limits), {client_upload_mb()}, limits)


def language_count(text, size, overlap):
    """Section 4's `language` count, as a user computes it."""
    long_at = size - 3
    lines = text.split('\n')
    long_pieces = (sum(len(p) >= long_at for p in text.split('\n\n'))
                   + sum(len(line) >= long_at for line in lines)
                   + sum(len(s) >= long_at for line in lines for s in line.split('. '))
                   + sum(len(w) >= long_at for line in lines for w in line.split(' ')))
    return 2 * len(text) / (size - overlap) + 2 * long_pieces + 1


def semantic_count(text):
    """Section 4's `semantic` count: sentences, plus one per 3,000 characters, plus one."""
    sentences = text.count('. ') + text.count('.\n') + 1
    return sentences + len(text) / (chunker.MAX_CHUNK_CHARACTERS // 2) + 1


def at_target(record, count):
    """Records joined by a blank line, as many as keep the count at the chunk target."""
    target = int(chunker.MAX_CHUNKS_PER_FILE * 0.8)
    n = max(1, int(10 * target / count('\n\n'.join([record] * 10))))
    while count('\n\n'.join([record] * n)) > target:
        n -= 1
    return '\n\n'.join([record] * n)


class SimilarModel:
    """Embeds every sentence alike, so semantic merges them all into one chunk."""
    def encode(self, sentences, convert_to_numpy=True):
        return np.ones((len(sentences), 4))


class FileAtTargetIsAcceptedTests(unittest.TestCase):
    """A file at section 5's chunk target, counted as section 4 says, is not
    refused (#615, PR #619 review)."""

    def test_language_with_long_lines_inside_records(self):
        # The review's shape: records of 50 line pairs, a short field then a
        # field just over chunk_size. Each long line flushes the short buffer
        # before it and leaves a short tail: about two extra chunks per line.
        # Counting two per long record instead, the first two were refused.
        def field(n):
            return ('w ' * n)[:n - 1] + 'z'
        for size, overlap, minimum, short, long in ((1000, 0, 0, 4, 1001), (1500, 0, 100, 100, 1600),
                                                    (1000, 200, 100, 100, 1100)):
            with self.subTest(size=size, overlap=overlap, minimum=minimum):
                record = '\n'.join(f'a: {field(short - 3)}\nt: {field(long - 3)}' for _ in range(50))
                text = at_target(record, lambda t: language_count(t, size, overlap))
                chunks = chunker.chunk(text, 'language', size, overlap, 0.85, minimum)
                self.assertLessEqual(len(chunks), language_count(text, size, overlap))

    def test_semantic_chunks_cut_into_more_pieces_than_sentences(self):
        # Sentences of 5,999 characters, all on one topic, with whitespace
        # only every 3,001 characters: the 6,000-character ceiling cuts the
        # merged chunk into a piece per 3,001 characters, two per sentence.
        text = (('w' * 3000 + ' ' + 'w' * 2999 + '. ') * 300).strip()
        with patch.object(chunker, '_get_semantic_model', return_value=SimilarModel()):
            chunks = chunker.chunk(text, 'semantic', similarity_threshold=0.5, min_chunk_size=0)
        self.assertGreater(len(chunks), 300)
        self.assertLessEqual(len(chunks), semantic_count(text))


if __name__ == '__main__':
    unittest.main()
