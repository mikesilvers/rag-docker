"""The corpus rules' chunk counts hold for the real chunker, for every strategy (#615).

Section 4 of ui/public/corpus-preparation-rules.md tells a user how to count a
file's chunks for each chunking strategy, as a worst case. The maintainer's
Decision on #615: a file prepared by the rules must not fail on import. This
test reads each rule's formula from the file (a changed number changes the
count computed here), builds worst-case inputs the way ingest_pipeline does,
runs the app's own services.chunker.chunk() on them (pre-checks, splitters,
minimum-size merging and the 6,000-character ceiling) and checks that:

- the real count never exceeds any rule that applies to the input, at the
  minimum, default and maximum chunk_size, overlap 0, default and maximum,
  and min_chunk_size 0 and default;
- a file grown to section 5's 8,000-chunk target, as a rule counts it, is
  accepted, not refused by the early per-file checks;
- the Decision's example, 13,000 records of 600 characters, counts over the
  8,000 target under every rule that applies to it.

test_corpus_rules.py checks that the file's numbers match the code constants;
this one checks that the counts built from them hold. The inputs are the worst
cases found by the PR #619 proof scripts. `semantic` runs with stub encoders,
so no model is loaded and no network is needed.

Runs in the API image with the repository mounted (02_ingest.sh), or anywhere
the API's dependencies are installed:
    RAG_TEST_API_DIR=api python scripts/tests/test_corpus_rules_estimates.py
"""
import math
import os
import random
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, os.environ.get('RAG_TEST_API_DIR') or str(ROOT / 'api'))

from services import chunker  # noqa: E402
from unstructured.documents.elements import NarrativeText, Table, Text, Title  # noqa: E402

RULES = ROOT / 'ui/public/corpus-preparation-rules.md'
TARGET_CHUNKS = int(chunker.MAX_CHUNKS_PER_FILE * 0.8)  # section 5: 8,000
TARGET_TEXT = int(chunker.MAX_TEXT_CHARACTERS * 0.7)  # 7,000,000
TARGET_OVERLAP_OUTPUT = int(chunker.MAX_OVERLAP_OUTPUT_CHARACTERS * 0.8)  # 8,000,000

# chunk_size: minimum, default, maximum. Overlap (overlap, language): 0, the
# default where it fits, and the largest allowed. min_chunk_size: 0, default.
SIZES = (50, 1000, 6000)
OVERLAPS = {50: (0, 10, 49), 1000: (0, 200, 999), 6000: (0, 200, 5999)}
MINIMA = (0, 100)

WORDS = {'one': 1, 'two': 2, 'three': 3, 'four': 4}


# ── the rules, read from section 4 ───────────────────────────────────────────

def section_4():
    text = RULES.read_text()
    body = text[text.index('## 4. Chunk count'):text.index('## 5.')]
    return ' '.join(body.split())


def number(found):
    return WORDS[found] if found in WORDS else int(found.replace(',', ''))


# Each rule as the file words it; the groups are the numbers the counts use.
PATTERNS = {
    'fixed': r'\*\*`fixed`\*\*: one chunk for every `chunk_size` characters',
    'overlap': (r'\*\*`overlap`\*\* \(the default\): one chunk for every `chunk_size − chunk_overlap` '
                r'characters\..*? the text plus `chunk_overlap` for every chunk'),
    'language_units': (r'long when it has at least `chunk_size − (\d+)` characters\. Count one chunk for '
                       r'every `\(chunk_size − chunk_overlap\) ÷ (\d+)` characters, plus (\w+) for every '
                       r'long paragraph, long line, long sentence and long word, plus (\w+)\.'),
    'language_any': (r'never more than one chunk for every `\(chunk_size − chunk_overlap\) ÷ (\d+)` '
                     r'characters, plus (\w+)'),
    'language_paragraphs': (r'if no paragraph is long: one chunk per paragraph \(one per blank line, '
                            r'plus one\), but never fewer than one for every `chunk_size − chunk_overlap` '
                            r'characters'),
    'language_short': (r'if every paragraph is shorter than `chunk_size − chunk_overlap − (\d+)`: one '
                       r'chunk for every `chunk_size − chunk_overlap − (\d+) − L` characters, plus (\w+)'),
    'context_aware': (r'Count one chunk for every `chunk_size ÷ (\d+)` characters, plus (\w+) for every '
                      r'table, plus (\w+) for every ([\d,]+) characters, rounded up, of each record or '
                      r'paragraph longer than ([\d,]+) characters'),
    'context_aware_records': (r'one chunk per record, a record longer than ([\d,]+) characters counting as '
                              r'one for every ([\d,]+) characters, rounded up; but never fewer than one for '
                              r'every `chunk_size` characters'),
    'context_aware_short': (r'if every record is shorter than `chunk_size ÷ (\d+)`: one chunk for every '
                            r'`chunk_size − L` characters'),
    'semantic': (r'Count one chunk per sentence \(a sentence ends at a full stop followed by a space or a '
                 r'line break\), plus (\w+) for every ([\d,]+) characters of text, plus (\w+)\.'),
}


def read_rules():
    text = section_4()
    rules = {}
    for name, pattern in PATTERNS.items():
        found = re.search(pattern, text)
        if found is None:
            raise AssertionError(f'{RULES.name} section 4 no longer states the {name} rule as this test '
                                 f'reads it; update the pattern and the count together')
        rules[name] = tuple(number(g) for g in found.groups())
    return rules


R = read_rules()


# ── the counts, as a user computes them from section 4 ──────────────────────
# A user rounds a count up. Each function returns None when its rule doesn't
# apply to the input.

def paragraphs(text):
    """The text between blank lines (two line breaks in a row)."""
    return text.split('\n\n')


def blank_lines(text):
    """A run of k line breaks is k - 1 blank lines."""
    return sum(len(run) - 1 for run in re.findall(r'\n+', text))


def long_pieces(text, size):
    (short_by, _, _, _) = R['language_units']
    at_least = size - short_by
    lines = text.split('\n')
    return (sum(len(p) >= at_least for p in paragraphs(text))
            + sum(len(line) >= at_least for line in lines)
            + sum(len(s) >= at_least for line in lines for s in line.split('. '))
            + sum(len(w) >= at_least for line in lines for w in line.split(' ')))


def count_fixed(text, size, overlap):
    return math.ceil(len(text) / size)


def count_overlap(text, size, overlap):
    return math.ceil(len(text) / (size - overlap))


def overlap_output(text, size, overlap):
    """overlap's second count: the text plus chunk_overlap for every chunk."""
    return len(text) + count_overlap(text, size, overlap) * overlap


def count_language_units(text, size, overlap):
    (_, divisor, per_long, plus) = R['language_units']
    return math.ceil(len(text) / ((size - overlap) / divisor) + per_long * long_pieces(text, size) + plus)


def count_language_any(text, size, overlap):
    (divisor, plus) = R['language_any']
    return math.ceil(len(text) / ((size - overlap) / divisor) + plus)


def count_language_paragraphs(text, size, overlap):
    (short_by, _, _, _) = R['language_units']
    if max(map(len, paragraphs(text))) >= size - short_by:
        return None
    return max(blank_lines(text) + 1, math.ceil(len(text) / (size - overlap)))


def count_language_short(text, size, overlap):
    (shorter_than, less, plus) = R['language_short']
    longest = max(map(len, paragraphs(text)))
    if longest >= size - overlap - shorter_than:
        return None
    return math.ceil(len(text) / (size - overlap - less - longest) + plus)


def count_context_aware(text, size, elements):
    (divisor, per_table, per_piece, piece, over) = R['context_aware']
    lengths = [len(str(el)) for el in elements]
    tables = sum(getattr(el, 'category', '') == 'Table' for el in elements)
    cut = sum(per_piece * math.ceil(n / piece) for n in lengths if n > over)
    return math.ceil(len(text) / (size / divisor) + per_table * tables + cut)


def count_context_aware_records(text, size, elements):
    (over, piece) = R['context_aware_records']
    records = sum(1 if len(str(el)) <= over else math.ceil(len(str(el)) / piece) for el in elements)
    return max(records, math.ceil(len(text) / size))


def count_context_aware_short(text, size, elements):
    (divisor,) = R['context_aware_short']
    longest = max(len(str(el)) for el in elements)
    if longest >= size / divisor:
        return None
    return math.ceil(len(text) / (size - longest))


def sentences(text):
    return text.count('. ') + text.count('.\n') + 1


def count_semantic(text):
    (per_piece, piece, plus) = R['semantic']
    return math.ceil(sentences(text) + per_piece * len(text) / piece + plus)


LANGUAGE_RULES = {'units': count_language_units, 'any structure': count_language_any,
                  'paragraphs': count_language_paragraphs, 'short paragraphs': count_language_short}
JSON_CONTEXT_RULES = {'characters': count_context_aware, 'records': count_context_aware_records,
                      'short records': count_context_aware_short}


# ── inputs ───────────────────────────────────────────────────────────────────

def fill(n, every=2):
    """n characters, a space every `every` characters, no space at either end."""
    if n <= 0:
        return ''
    if every <= 0:
        return 'x' * n
    s = (('w' * (every - 1)) + ' ') * (n // every + 1)
    s = s[:n]
    return s[:-1] + 'z' if s.endswith(' ') else s


def solid(n):
    return 'y' * n


def json_text(records):
    return '\n\n'.join(records)


def json_input(records):
    """Text and elements as ingest_pipeline builds them for JSON and JSON Lines:
    the records joined by a blank line, one Text element per record."""
    return json_text(records), [Text(text=r) for r in records]


# language: one repeat of each structure, and what the repeats are joined by.
# Short then long at every level, at and just over the long threshold, nested
# levels, the PR #619 reviewer's records of line pairs, extra blank lines,
# no whitespace, pieces just over half full, and random JSON-like records.
def pairs(C):
    """Line pairs per record: the reviewer's 50, fewer at a large chunk_size."""
    return max(3, min(50, 20_000 // C))


LANGUAGE_SHAPES = {
    'records of short and long line pairs': (
        lambda i, C: '\n'.join(f'a: {fill(1)}\nt: {fill(C - 2)}' for _ in range(pairs(C))), '\n\n'),
    'records of 100 and long line pairs': (
        lambda i, C: '\n'.join(f'a: {fill(97)}\nt: {fill(C - 2)}' for _ in range(pairs(C))), '\n\n'),
    'short and long paragraphs': (lambda i, C: 's\n\n' + fill(C + 1), '\n\n'),
    'short and long paragraphs at the threshold': (lambda i, C: 's\n\n' + fill(C - 2), '\n\n'),
    'short and long lines': (lambda i, C: 's\n' + fill(C + 1), '\n'),
    'short and long lines at the threshold': (lambda i, C: 's\n' + fill(C - 1), '\n'),
    'short and long sentences': (lambda i, C: 's. ' + fill(C + 1), '. '),
    'short and long sentences at the threshold': (lambda i, C: 's. ' + fill(C - 2), '. '),
    'short and long words': (lambda i, C: 's ' + solid(C + 1), ' '),
    'short and long words at the threshold': (lambda i, C: 's ' + solid(C - 1), ' '),
    'every level nested': (lambda i, C: 'q\n\nz\ny. x ' + solid(C), '\n\n'),
    'every level nested, thirds': (
        lambda i, C: 'q\n\n' + fill(C // 3) + '\n' + fill(C // 3) + '. ' + fill(C // 3) + ' ' + solid(C + 1),
        '\n\n'),
    'long sentences in lines': (lambda i, C: '\n'.join('k. ' + fill(C + 1) for _ in range(3)), '\n'),
    'three line breaks': (lambda i, C: 's\n\n\n' + fill(C - 3), '\n\n'),
    'seven line breaks': (lambda i, C: 's' + '\n' * 7 + fill(C - 3), '\n'),
    'no whitespace': (lambda i, C: solid(C * 3 + 7), ''),
    'paragraphs just over half': (lambda i, C: fill(C // 2 + 1), '\n\n'),
    'lines just over half': (lambda i, C: fill(C // 2 + 1), '\n'),
    'sentences just over half': (lambda i, C: fill(C // 2 + 1), '. '),
    'words of half': (lambda i, C: solid(C // 2), ' '),
    'short paragraphs': (lambda i, C: fill(max(C // 8, 1)), '\n\n'),
    'random records': (lambda i, C: '\n'.join(
        f'f{j}: ' + fill(random.Random(i * 31 + j).choice([3, 40, C // 2, C - 4, C + 1, 2 * C]),
                         random.Random(i + j).choice([0, 2, 7, 40]))
        for j in range(random.Random(i).randint(1, 5))), '\n\n'),
}


def language_text(shape, size, units):
    unit, sep = LANGUAGE_SHAPES[shape]
    return sep.join(unit(i, size) for i in range(units))


def units_for(shape, size, budget=25_000):
    """Enough repeats for many chunks, few enough to keep a run quick."""
    two = len(language_text(shape, size, 2))
    return max(3, min(400, 2 * budget // max(two, 1)))


# context_aware on JSON: one record's length for each record, by index. Records
# just over chunk_size / 2 and a third, of chunk_size + 1, a tiny record between
# long ones, records over 6,000 with whitespace only every 3,001 characters, so
# the ceiling cuts them at 3,000, and a random mix.
def record(kind, n):
    if kind == 'sparse':
        return fill(n, 3001)
    if kind == 'solid':
        return solid(n)
    return fill(n, 7)


RECORD_SHAPES = {
    'records just over half': lambda i, C: ('prose', C // 2 + 1),
    'records just over a third': lambda i, C: ('prose', C // 3 + 1),
    'records of a quarter': lambda i, C: ('prose', max(C // 4, 8)),
    'records of chunk_size + 1': lambda i, C: ('prose', C + 1),
    'records of chunk_size + 1, no whitespace': lambda i, C: ('solid', C + 1),
    'tiny and long records': lambda i, C: ('prose', 8) if i % 2 == 0 else ('solid', C + 1),
    'records of 6,001, cut at 3,000': lambda i, C: ('sparse', 6001),
    'records of 9,000, cut at 3,000': lambda i, C: ('sparse', 9000),
    'records of 12,001, cut at 3,000': lambda i, C: ('sparse', 12001),
    'tiny and 6,001 records': lambda i, C: ('prose', 8) if i % 2 == 0 else ('sparse', 6001),
    'random records': lambda i, C: (
        random.Random(i).choice(['prose', 'solid', 'sparse']),
        random.Random(i + 1).choice([8, 60, C // 2, C - 4, C + 1, 6001, 9000])),
}


def records_for(shape, size, count):
    build = RECORD_SHAPES[shape]
    return [record(*build(i, size)) for i in range(count)]


# context_aware on other files: Title, NarrativeText and Table elements, the
# text joined by single line breaks as ingest_pipeline._parse_file does.
def document(shape, size, count):
    els = []
    for i in range(count):
        if shape == 'tiny paragraphs and tables':
            els.append(NarrativeText(text='p') if i % 2 == 0 else Table(text='t'))
        elif shape == 'paragraphs just over half and tables':
            els.append(NarrativeText(text=fill(size // 2 + 1, 7)) if i % 2 == 0 else Table(text=fill(30, 7)))
        elif shape == 'tables over 6,000':
            els.append(Title(text='t') if i % 2 == 0 else Table(text=fill(6001 + 2999 * (i % 3), 3001)))
        elif shape == 'paragraphs over 6,000':
            els.append(Title(text='t') if i % 2 == 0 else NarrativeText(text=fill(6001 + 3000 * (i % 3), 3001)))
        else:  # mixed
            r = random.Random(i)
            kind = r.choice([Title, NarrativeText, NarrativeText, Table])
            els.append(kind(text=fill(r.choice([5, 40, size // 2 + 1, size + 1, 6001, 9000]),
                                      r.choice([7, 3001]))))
    return '\n'.join(str(el) for el in els), els


DOCUMENT_SHAPES = ('tiny paragraphs and tables', 'paragraphs just over half and tables',
                   'tables over 6,000', 'paragraphs over 6,000', 'mixed')


# semantic: sentences, with the encoder stubbed so the topics are known.
SEMANTIC_SHAPES = {
    'sentences of 60': lambda k: '. '.join(fill(60) for _ in range(k)) + '.',
    'sentences ending at a line break': lambda k: '.\n'.join(fill(60) for _ in range(k)) + '.',
    'sentences of 6,001': lambda k: '. '.join(fill(6001) for _ in range(k)) + '.',
    'sentences of 6,001, no whitespace': lambda k: '. '.join(solid(6001) for _ in range(k)) + '.',
    'sentences of 5,999, cut at 3,000': lambda k: '. '.join(
        'w' * 3000 + ' ' + 'w' * 2998 for _ in range(k)) + '.',
    'sentences of 12,004, cut at 3,000': lambda k: '. '.join(fill(3001 * 4, 3001) for _ in range(k)) + '.',
    'sentences of 2,999, no whitespace': lambda k: '. '.join(solid(2999) for _ in range(k)) + '.',
    'no sentence end': lambda k: fill(3001 * k, 3001),
    'tiny and long sentences': lambda k: '. '.join(
        fill(1) if i % 2 else fill(6100, 3050) for i in range(k)) + '.',
}


class OneTopic:
    """Every sentence alike: semantic merges them all, and the ceiling cuts."""
    def encode(self, sentences, convert_to_numpy=True):
        return np.ones((len(sentences), 4))


class NewTopics:
    """Every sentence a new topic: one chunk per sentence."""
    def encode(self, sentences, convert_to_numpy=True):
        vectors = np.zeros((len(sentences), len(sentences) + 1))
        vectors[np.arange(len(sentences)), np.arange(len(sentences))] = 1.0
        return vectors


ENCODERS = {'one topic': (OneTopic(), 0.5), 'new topics': (NewTopics(), 1.0)}


def run(text, strategy, size, overlap=0, minimum=0, elements=None, encoder=None):
    if encoder is None:
        return chunker.chunk(text, strategy, chunk_size=size, chunk_overlap_size=overlap,
                             min_chunk_size=minimum, elements=elements)
    model, threshold = encoder
    with patch.object(chunker, '_get_semantic_model', return_value=model):
        return chunker.chunk(text, strategy, chunk_size=size, chunk_overlap_size=overlap,
                             similarity_threshold=threshold, min_chunk_size=minimum)


def grow(build, load, start=40):
    """The most repeats n whose input keeps load(input) at or under 1: the
    input at the targets. load is the largest of count / 8,000 and the like,
    or None when the rule doesn't apply. The counts grow about linearly with
    n: extrapolate from a sample, bracket, then bisect."""
    def fits(n):
        value = load(build(n))
        return value is not None and value <= 1
    sample = load(build(start))
    if sample is None or not fits(1):
        return None
    guess = max(1, int(start / sample))
    low, high = guess, guess
    while low > 1 and not fits(low):
        low = max(1, low * 9 // 10)
    while fits(high):
        high = high * 11 // 10 + 1
    while high - low > 1:
        mid = (low + high) // 2
        low, high = (mid, high) if fits(mid) else (low, mid)
    return low


def loads(count, *limits):
    """load() for grow: a count against the chunk target, and the text."""
    def load(text, *args):
        value = count(text, *args)
        if value is None:
            return None
        return max(value / TARGET_CHUNKS, len(text) / TARGET_TEXT, *(limit(text) for limit in limits))
    return load


# ── tests ────────────────────────────────────────────────────────────────────

class CountsHoldTests(unittest.TestCase):
    """The real count is at most each rule's count, on worst-case inputs."""

    def assert_holds(self, chunk, counts, label):
        """chunk() gives no more chunks than any count; or, if the file is
        refused, every count that applies is over the 10,000-chunk limit."""
        try:
            chunks = chunk()
        except ValueError as exc:
            for rule, value in counts.items():
                if value is not None:
                    with self.subTest(case=label, rule=rule):
                        self.assertGreater(value, chunker.MAX_CHUNKS_PER_FILE, f'{label}: refused ({exc}) '
                                                                               f'at {value} counted')
            return None
        for rule, value in counts.items():
            if value is None:
                continue
            with self.subTest(case=label, rule=rule):
                self.assertLessEqual(len(chunks), value, f'{label}: {rule} counts {value}, '
                                                         f'the chunker gave {len(chunks)}')
        return chunks

    def test_fixed(self):
        for size in SIZES:
            for minimum in MINIMA:
                length = max(5, 12_000 // size) * size + size // 2 + 1
                for name, text in (('prose', fill(length, 7)), ('no whitespace', solid(length)),
                                   ('records just over half', json_text([fill(size // 2 + 1, 7)] * 12))):
                    self.assert_holds(lambda: run(text, 'fixed', size, 0, minimum),
                                      {'fixed': count_fixed(text, size, 0)}, f'fixed {size}/{minimum} {name}')

    def test_overlap_chunks_and_text_across_them(self):
        for size in SIZES:
            for overlap in OVERLAPS[size]:
                for minimum in MINIMA:
                    length = max(5, 12_000 // size) * size + size // 2 + 1
                    for name, text in (('prose', fill(length, 7)), ('no whitespace', solid(length)),
                                       ('one window and one character', solid(size + 1))):
                        label = f'overlap {size}/{overlap}/{minimum} {name}'
                        chunks = self.assert_holds(lambda: run(text, 'overlap', size, overlap, minimum),
                                                   {'overlap': count_overlap(text, size, overlap)}, label)
                        if chunks is not None:
                            with self.subTest(case=label, rule='text across the chunks'):
                                self.assertLessEqual(sum(map(len, chunks)), overlap_output(text, size, overlap))

    def test_language(self):
        for size in SIZES:
            for overlap in OVERLAPS[size]:
                for minimum in MINIMA:
                    for shape in LANGUAGE_SHAPES:
                        text = language_text(shape, size, units_for(shape, size))
                        label = f'language {size}/{overlap}/{minimum} {shape}'
                        self.assert_holds(lambda: run(text, 'language', size, overlap, minimum),
                                          {rule: count(text, size, overlap)
                                           for rule, count in LANGUAGE_RULES.items()}, label)

    def test_context_aware_records(self):
        for size in SIZES:
            for minimum in MINIMA:
                for shape in RECORD_SHAPES:
                    sample = sum(map(len, records_for(shape, size, 10)))
                    n = max(10, min(600, 10 * 150_000 // max(sample, 1)))
                    text, elements = json_input(records_for(shape, size, n))
                    self.assert_holds(lambda: run(text, 'context_aware', size, 0, minimum, elements),
                                      {rule: count(text, size, elements)
                                       for rule, count in JSON_CONTEXT_RULES.items()},
                                      f'context_aware {size}/{minimum} {shape}')

    def test_context_aware_documents(self):
        for size in SIZES:
            for minimum in MINIMA:
                for shape in DOCUMENT_SHAPES:
                    text, elements = document(shape, size, 120)
                    self.assert_holds(lambda: run(text, 'context_aware', size, 0, minimum, elements),
                                      {'characters': count_context_aware(text, size, elements)},
                                      f'context_aware {size}/{minimum} {shape}')

    def test_semantic(self):
        for minimum in MINIMA:
            for encoder in ENCODERS:
                for shape, build in SEMANTIC_SHAPES.items():
                    for k in (1, 2, 3, 40):
                        text = build(k)
                        self.assert_holds(lambda: run(text, 'semantic', 1000, 200, minimum,
                                                      encoder=ENCODERS[encoder]),
                                          {'semantic': count_semantic(text)},
                                          f'semantic {minimum} {encoder} {shape} x{k}')


class FileAtTargetIsAcceptedTests(unittest.TestCase):
    """An input grown to the 8,000-chunk target, as one rule counts it (and to
    the text targets), is accepted and gives no more chunks than the rule."""

    def check(self, label, text, count, chunk):
        value = count(text)
        self.assertLessEqual(value, TARGET_CHUNKS, label)
        self.assertLessEqual(len(text), TARGET_TEXT, label)
        try:
            chunks = chunk(text)
        except ValueError as exc:
            self.fail(f'{label}: refused at {value} counted chunks: {exc}')
        self.assertLessEqual(len(chunks), value, label)
        return chunks

    def test_fixed_and_overlap(self):
        size = 50

        def build(n):
            return fill(n * size + 3, 7)
        n = grow(build, loads(lambda t: count_fixed(t, size, 0)))
        self.check('fixed 50', build(n), lambda t: count_fixed(t, size, 0),
                   lambda t: run(t, 'fixed', size, 0, 0))
        for overlap in OVERLAPS[size]:
            n = grow(build, loads(lambda t: count_overlap(t, size, overlap),
                                  lambda t: overlap_output(t, size, overlap) / TARGET_OVERLAP_OUTPUT), start=4)
            text = build(n)
            chunks = self.check(f'overlap 50/{overlap}', text, lambda t: count_overlap(t, size, overlap),
                                lambda t: run(t, 'overlap', size, overlap, 0))
            self.assertLessEqual(sum(map(len, chunks)), overlap_output(text, size, overlap))

    def test_language(self):
        every_shape = ('records of short and long line pairs', 'short and long lines', 'every level nested',
                       'paragraphs just over half', 'short paragraphs', 'no whitespace', 'random records')
        cases = [(50, 10, 0, every_shape), (50, 49, 0, every_shape), (50, 0, 100, every_shape),
                 (1000, 200, 100, ('records of short and long line pairs', 'paragraphs just over half',
                                   'short paragraphs'))]
        for size, overlap, minimum, shapes in cases:
            for shape in shapes:
                for rule, count in LANGUAGE_RULES.items():
                    def build(n, shape=shape, size=size):
                        return language_text(shape, size, n)
                    n = grow(build, loads(lambda t, count=count, size=size, overlap=overlap:
                                          count(t, size, overlap)), start=4)
                    if n is None:
                        continue  # the rule doesn't apply to this shape
                    with self.subTest(size=size, overlap=overlap, shape=shape, rule=rule):
                        self.check(f'language {size}/{overlap}/{minimum} {shape} {rule}', build(n),
                                   lambda t: count(t, size, overlap),
                                   lambda t: run(t, 'language', size, overlap, minimum))

    def test_context_aware(self):
        cases = [(50, 0), (50, 100), (1000, 100), (6000, 0)]
        shapes = ('records just over half', 'records just over a third', 'tiny and long records',
                  'records of 6,001, cut at 3,000', 'tiny and 6,001 records', 'random records')
        for size, minimum in cases:
            for shape in shapes:
                for rule, count in JSON_CONTEXT_RULES.items():
                    # The counts read records as text; the Text elements are
                    # built once, for the run.
                    def build(n, shape=shape, size=size):
                        return records_for(shape, size, n)
                    n = grow(build, lambda records, count=count, size=size: loads(
                        lambda t: count(t, size, records))(json_text(records)), start=10)
                    if n is None:
                        continue
                    text, elements = json_input(build(n))
                    with self.subTest(size=size, minimum=minimum, shape=shape, rule=rule):
                        self.check(f'context_aware {size}/{minimum} {shape} {rule}', text,
                                   lambda t: count(t, size, elements),
                                   lambda t: run(t, 'context_aware', size, 0, minimum, elements))

    def test_semantic(self):
        for encoder in ENCODERS:
            for shape in ('sentences of 60', 'sentences of 5,999, cut at 3,000', 'no sentence end'):
                build = SEMANTIC_SHAPES[shape]
                n = grow(build, loads(count_semantic), start=10)
                with self.subTest(encoder=encoder, shape=shape):
                    self.check(f'semantic {encoder} {shape}', build(n), count_semantic,
                               lambda t: run(t, 'semantic', 1000, 200, 0, encoder=ENCODERS[encoder]))


class DecisionExampleTests(unittest.TestCase):
    """The Decision on #615: 13,000 records of 600 characters give 13,000
    chunks with context_aware and language. Every rule that applies to them
    counts the file over the 8,000 target, so the rules say to split it."""

    def test_13000_records_of_600(self):
        size, overlap, minimum = 1000, 200, 100
        text, elements = json_input([fill(600, 7)] * 13000)
        counts = {f'language {rule}': count(text, size, overlap) for rule, count in LANGUAGE_RULES.items()}
        counts.update({f'context_aware {rule}': count(text, size, elements)
                       for rule, count in JSON_CONTEXT_RULES.items()})
        applying = {rule: value for rule, value in counts.items() if value is not None}
        self.assertIn('language paragraphs', applying)
        self.assertIn('context_aware records', applying)
        for rule, value in applying.items():
            with self.subTest(rule=rule):
                self.assertGreater(value, TARGET_CHUNKS)
        # And over the text target, which fixed and overlap go by.
        self.assertGreater(len(text), TARGET_TEXT)
        for strategy, kwargs in (('context_aware', {'elements': elements}), ('language', {})):
            with self.subTest(strategy=strategy):
                with self.assertRaises(ValueError):
                    run(text, strategy, size, overlap if strategy == 'language' else 0, minimum, **kwargs)


if __name__ == '__main__':
    unittest.main()
