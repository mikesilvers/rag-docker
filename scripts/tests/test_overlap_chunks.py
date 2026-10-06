"""Overlap window invariants on inert synthetic text and the ingest worker."""
import os
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.environ.get('RAG_TEST_API_DIR') or str(Path(__file__).resolve().parents[2] / 'api'))
from services import chunker, ingest_pipeline

NEEDS_REPOSITORY = 'needs the whole repository mounted (see scripts/verify/README.md)'


def repository_root():
    parents = Path(__file__).resolve().parents
    root = parents[2] if len(parents) > 2 else None
    return root if root is not None and (root / 'IMPLEMENTATION.md').is_file() else None


def recover(chunks, overlap):
    return chunks[0] + ''.join(c[overlap:] for c in chunks[1:]) if chunks else ''


class OverlapTests(unittest.TestCase):
    def check_windows(self, text, size=1000, overlap=200, minimum=100):
        chunks = chunker.chunk_overlap(text, size, overlap, minimum)
        self.assertEqual(recover(chunks, overlap), text)
        for left, right in zip(chunks, chunks[1:]):
            if overlap:
                self.assertEqual(left[-overlap:], right[:overlap])
        self.assertTrue(all(len(c) <= size for c in chunks[:-1]))
        # One short final window can extend its predecessor by its new suffix.
        bound = size + max(0, min(size, minimum - 1) - overlap)
        self.assertTrue(all(len(c) <= bound for c in chunks))
        return chunks

    def test_long_token_has_bounded_windows_and_exact_coverage(self):
        chunks = self.check_windows('x' * 10000)
        self.assertEqual(len(chunks), 13)
        self.assertEqual(len(chunks[-1]), 400)

    def test_parser_single_newlines_preserve_order_and_overlap(self):
        self.check_windows('\n'.join('synthetic line ' + str(i) for i in range(600)))

    def test_paragraph_separators_and_unicode_are_preserved(self):
        self.check_windows('\n\n'.join('Paragraph ' + str(i) + ' café 🌿 ' * 80 for i in range(30)))

    def test_short_tail_merges_only_new_suffix_with_documented_bound(self):
        chunks = self.check_windows(''.join(chr(65 + i % 26) for i in range(1020)), overlap=10)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(len(chunks[0]), 1020)

    def test_zero_overlap_tail_preserves_exact_text_without_added_separator(self):
        self.assertEqual(len(self.check_windows('x' * 1040, overlap=0)[0]), 1040)

    def test_minimum_larger_than_split_target_keeps_the_tail_policy_bounded(self):
        chunks = self.check_windows('x'*10000,minimum=2000)
        self.assertEqual(len(chunks[-1]),1200)
        self.assertEqual(len(chunks),12)

    def test_tail_at_minimum_remains_separate(self):
        chunks = self.check_windows('x' * 1080, overlap=20)
        self.assertEqual([len(c) for c in chunks], [1000, 100])

    def test_document_shorter_than_minimum_is_not_padded(self):
        self.assertEqual(self.check_windows('short'), ['short'])

    def test_exact_window_and_near_total_overlap_do_not_emit_redundant_tail(self):
        self.assertEqual(self.check_windows('x' * 1000), ['x' * 1000])
        self.assertEqual([len(c) for c in self.check_windows('x' * 1001, overlap=999)], [1000, 1000])

    def test_empty_or_whitespace_only_input_has_no_chunks(self):
        for text in ('', ' \n\t '):
            self.assertEqual(chunker.chunk_overlap(text, 1000, 200, 100), [])

    def test_internal_and_boundary_whitespace_is_preserved(self):
        self.check_windows('   a' + ' ' * 2500 + 'b\n\t')

    def test_invalid_sizes_fail_before_splitting(self):
        for size, overlap, minimum in ((0,0,0),(-1,0,0),(10,-1,0),(10,10,0),
                                      (10,11,0),(10,0,-1),(True,0,0),
                                      (10,False,0),(10,0,True),(10.5,0,0)):
            with self.subTest(values=(size,overlap,minimum)), self.assertRaises(ValueError):
                chunker.chunk_overlap('inert',size,overlap,minimum)

    def test_window_limit_accepts_boundary_and_rejects_before_slicing(self):
        count=chunker.MAX_OVERLAP_WINDOWS
        self.assertEqual(len(chunker.chunk_overlap('x'*(10+count-1),10,9,0)),count)
        class NoSlices(str):
            def __getitem__(self,key):raise AssertionError('window allocated before rejection')
        with self.assertRaisesRegex(ValueError,'per-file limit'):
            chunker.chunk_overlap(NoSlices('x'*(10+count)),10,9,0)

    def test_repeated_payload_limit_has_an_independent_boundary(self):
        with patch.object(chunker,'MAX_OVERLAP_OUTPUT_CHARACTERS',100):
            chunks=chunker.chunk_overlap('x'*55,10,5,0)
            self.assertEqual(sum(map(len,chunks)),100)
            with self.assertRaisesRegex(ValueError,'characters'):
                chunker.chunk_overlap('x'*56,10,5,0)

    def test_over_budget_file_fails_before_storage_and_other_file_continues(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);bad=root/'over-budget.txt';bad.write_text('inert')
            good=root/'accepted.txt';good.write_text('inert')
            job_id='overlap-budget-test'
            job={'status':'queued','files_total':2,'files_completed':0,'files_failed':0,'chunks_stored':0,'errors':[]}
            ingest_pipeline._jobs[job_id]=job
            try:
                with patch.object(ingest_pipeline,'_parse_file',side_effect=[('x'*100000,[]),('inert text',[])]), \
                     patch.object(ingest_pipeline.wc,'_insert_chunks_sync') as store, \
                     patch.object(ingest_pipeline.sources,'store') as retained:
                    ingest_pipeline._process_job_sync(job_id,[bad,good],root,'ReviewOverlap','overlap',1000,999,0.85,100)
                    store.assert_called_once();retained.assert_called_once()
                    self.assertEqual(store.call_args.args[1][0]['source_file'],'accepted.txt')
                self.assertEqual(job['status'],'partial',job)
                self.assertEqual((job['files_completed'],job['files_failed'],job['chunks_stored']),(1,1,1))
                self.assertIn('over-budget.txt',job['errors'][0]);self.assertIn('per-file limit',job['errors'][0])
            finally:ingest_pipeline._jobs.pop(job_id,None)

    def test_live_helper_rejects_empty_parser_output_instead_of_vacuous_success(self):
        root=repository_root()
        if root is None:self.skipTest(NEEDS_REPOSITORY)
        with patch.dict(os.environ,{'RAG_OVERLAP_REAL_EMBEDDING':'0'}), \
             patch.object(ingest_pipeline,'_parse_file',return_value=('',[])), \
             patch.object(ingest_pipeline.wc,'_collection_exists_sync',return_value=False), \
             patch.object(ingest_pipeline.wc,'get_client',return_value=MagicMock()), \
             patch.object(ingest_pipeline.wc,'_delete_collection_sync') as delete, \
             patch.object(ingest_pipeline.wc,'close_client'):
            with self.assertRaisesRegex(AssertionError,'parser returned no nonblank text'):
                runpy.run_path(str(root/'scripts/verify/overlap_chunks.py'))
            delete.assert_called_once()

    def test_entrypoint_uses_the_bounded_overlap_strategy(self):
        text = 'entrypoint' * 1000
        self.assertEqual(recover(chunker.chunk(text, 'overlap', 1000, 200), 200), text)

    def test_small_parameter_matrix_preserves_coverage_and_stated_bound(self):
        for size in range(1, 16):
            for overlap in range(size):
                for minimum in (0, 1, size, size+1, 2*size+1):
                    for length in (1, size, size+1, 2*size-1, 2*size+3):
                        text = ''.join(chr(65 + i % 26) for i in range(length))
                        with self.subTest(size=size, overlap=overlap, minimum=minimum, length=length):
                            self.check_windows(text,size,overlap,minimum)

    def test_ingest_worker_stores_bounded_overlapping_windows(self):
        text = '\n'.join('Inert source line ' + str(i) for i in range(400))
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'inert.txt'; source.write_text(text)
            job_id = 'overlap-controlled'
            job = {'status':'queued', 'files_completed':0, 'files_failed':0, 'chunks_stored':0, 'errors':[], 'files_total':1}
            with patch.dict(ingest_pipeline._jobs, {job_id:job}, clear=True), \
                 patch.object(ingest_pipeline, '_parse_file', return_value=(text,None)), \
                 patch.object(ingest_pipeline.wc, '_insert_chunks_sync') as store, \
                 patch.object(ingest_pipeline.sources, 'store'):
                ingest_pipeline._process_job_sync(job_id,[source],Path(directory),'ReviewOverlap','overlap',1000,200,0.85,100)
            self.assertEqual(job['status'], 'completed', job)
            saved = store.call_args.args[1]
            self.assertTrue(all(len(c['content']) <= 1000 for c in saved))
            self.assertEqual(recover([c['content'] for c in saved],200),text)
            self.assertEqual([c['chunk_index'] for c in saved],list(range(len(saved))))


class ImplementationTests(unittest.TestCase):
    def test_embedded_changed_sources_match_runtime(self):
        root = repository_root()
        if root is None: self.skipTest(NEEDS_REPOSITORY)
        text = (root / 'IMPLEMENTATION.md').read_text()
        for name,fence,language in [('api/services/chunker.py','```','python'),
                                     ('scripts/verify/overlap_chunks.py','```','python'),
                                     ('scripts/verify/README.md','````','markdown'),
                                     ('scripts/verify/02_ingest.sh','```','bash'),
                                     ('scripts/verify/08_overlap.sh','```','bash')]:
            with self.subTest(file=name):
                header = '### ' + name + '\n\n' + fence + language + '\n'
                start = text.index(header) + len(header)
                end = text.index('\n' + fence + '\n',start)
                self.assertEqual(text[start:end], (root/name).read_text().rstrip('\n'))


if __name__ == '__main__': unittest.main()
