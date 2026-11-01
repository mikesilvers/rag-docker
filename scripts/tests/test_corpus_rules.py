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
import os
import re
import sys
import unittest
from pathlib import Path

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
    # The text at which the overlap and language strategies reach the chunk cap
    # with the default settings: one chunk per (size - overlap) characters.
    at_cap = round(size + (chunks - 1) * (size - overlap), -6)
    text_target = int(text * 0.7)
    chunk_target = int(chunks * 0.8)
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
        f'about {at_cap:,} characters': [at_cap],
        f'{text_target:,} characters': [text_target],
        f'{chunk_target:,} chunks': [chunk_target],
        '70 %': [70],
        '80 %': [80],
    }
    return phrases


def prose(markdown):
    """The file without its fenced code blocks (the conversion example)."""
    return re.sub(r'^```.*?^```[ \t]*$', '', markdown, flags=re.M | re.S)


class CorpusRulesTests(unittest.TestCase):
    def setUp(self):
        self.text = RULES.read_text()
        self.phrases = expected()

    def test_every_limit_is_stated(self):
        text = prose(self.text)
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


if __name__ == '__main__':
    unittest.main()
