"""Receiver and decoded evidence assertions, run with the locked API dependencies."""
import base64
import importlib.util
import json
import copy
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
import subprocess
import urllib.error
import urllib.request
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.m = load('capture', 'scripts/verify/telemetry_capture.py')
        self.server = self.m.Server(('127.0.0.1', 0), self.m.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)
        self.url = 'http://127.0.0.1:'+str(self.server.server_port)

    def post(self, path, data=b''):
        return urllib.request.urlopen(urllib.request.Request(self.url+path, data=data), timeout=2)

    def test_real_protobuf_decodes_and_reset_zeros_counts(self):
        msg = ExportTraceServiceRequest()
        msg.resource_spans.add().scope_spans.add().spans.add(name='POST /query', trace_id=b'a'*16, span_id=b'b'*8)
        self.assertEqual(self.post('/v1/traces', msg.SerializeToString()).status, 200)
        data = json.load(urllib.request.urlopen(self.url+'/snapshot'))
        self.assertEqual(data['requests'], 1)
        self.assertEqual(data['batches'][0]['data']['resource_spans'][0]['scope_spans'][0]['spans'][0]['name'], 'POST /query')
        self.post('/reset')
        self.assertEqual(json.load(urllib.request.urlopen(self.url+'/snapshot'))['requests'], 0)

    def test_capture_overflow_is_explicit_failure(self):
        self.m.MAX_BATCHES = 1
        self.post('/v1/traces')
        with self.assertRaises(urllib.error.HTTPError) as exc:
            self.post('/v1/traces')
        self.assertEqual(exc.exception.code, 503)
        self.assertEqual(self.m.capture.overflow, 1)

    def test_oversized_body_refused(self):
        self.m.MAX_BODY = 8
        with self.assertRaises(urllib.error.HTTPError) as exc:
            self.post('/v1/traces', b'0123456789')
        self.assertEqual(exc.exception.code, 413)


class EvidenceTests(unittest.TestCase):
    def fixture(self):
        m = load('e2e', 'scripts/verify/telemetry_e2e.py')
        def attr(key, value):
            return {'key':key, 'value':{'string_value':value}}
        root = {'trace_id':'trace1', 'span_id':'root', 'name':'POST /export', 'links':[{'trace_id':'remote1'}]}
        job = {'trace_id':'trace1', 'span_id':'job', 'parent_span_id':'root', 'name':'rag.export'}
        dep = {'trace_id':'trace1', 'span_id':'dep', 'parent_span_id':'job', 'name':'weaviate.iterate'}
        logs = [{'trace_id':'trace1', 'span_id':span, 'body':{'string_value':body},
                 'attributes':[] if span=='root' else [attr('rag.job_token','a'*32)]}
                for span, body in [('root','rag.api.completed'),('job','rag.job.completed'),('dep','rag.dependency.completed')]]
        metrics = []
        for name in ('rag.api.requests','rag.api.duration','rag.dependency.calls','rag.job.completed'):
            attributes = [attr(k, sorted(m.SCHEMA['metric_values'](name,k))[0]) for k in m.SCHEMA['METRICS'][name][2]]
            kind = 'histogram' if m.SCHEMA['METRICS'][name][0] == 'create_histogram' else 'sum'
            metrics.append({'name':name, kind:{'data_points':[{'as_int':'1','attributes':attributes}]}})
        snapshot = {'overflow':0,'batches':[
            {'signal':'traces','data':{'resource_spans':[{'scope_spans':[{'spans':[root,job,dep]}]}]}},
            {'signal':'logs','data':{'resource_logs':[{'scope_logs':[{'log_records':logs}]}]}},
            {'signal':'metrics','data':{'resource_metrics':[{'scope_metrics':[{'metrics':metrics}]}]}}]}
        return m, snapshot, [('remote1','rag.export')]

    def test_connected_received_tree_is_accepted(self):
        m,snapshot,expected = self.fixture()
        m.inspect(snapshot, expected)

    def test_malformed_capture_shapes_fail_controlled(self):
        m,valid,expected=self.fixture()
        cases=[None,[],{'overflow':0,'batches':1}]
        for path in (('batches',0),('batches',0,'data'),('batches',0,'data','resource_spans'),
                     ('batches',0,'data','resource_spans',0),
                     ('batches',0,'data','resource_spans',0,'scope_spans'),
                     ('batches',0,'data','resource_spans',0,'scope_spans',0),
                     ('batches',0,'data','resource_spans',0,'scope_spans',0,'spans'),
                     ('batches',0,'data','resource_spans',0,'scope_spans',0,'spans',0)):
            for invalid in (None, 1, 'bad'):
                value=copy.deepcopy(valid); target=value
                for key in path[:-1]:target=target[key]
                target[path[-1]]=invalid;cases.append(value)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(AssertionError):m.inspect(value,expected)
        with patch.object(m,'sink',side_effect=[cases[-1],valid]),patch.object(m.time,'sleep'):
            self.assertIs(m.await_capture(expected),valid)

    def test_disabled_transition_terminates_old_pipeline_before_reset(self):
        m,_,_=self.fixture(); events=[]
        with patch.object(m,'enabled',side_effect=lambda value:events.append(('enabled',value))), \
             patch.object(m,'compose',side_effect=lambda *args:events.append(args)), \
             patch.object(m,'sink',side_effect=lambda *args:events.append(args)):
            m.transition('disabled')
        self.assertEqual(events,[('enabled',False),('stop','-t','10','otel-collector'),
                                 ('restart','-t','10','otel-capture'),('/reset',True),('start','otel-collector')])

    def test_duplicate_full_span_identities_rejected_before_lineage(self):
        for index in range(3):
            for change in ({}, {'parent_span_id':'different'}, {'name':'rag.query'},
                           {'links':[{'trace_id':'different'}]}):
                for prepend in (False,True):
                    with self.subTest(index=index,change=change,prepend=prepend):
                        m,snapshot,expected=self.fixture()
                        duplicate=copy.deepcopy(m.records(snapshot,'traces')[index]);duplicate.update(change)
                        batch={'signal':'traces','data':{'resource_spans':[{'scope_spans':[{'spans':[duplicate]}]}]}}
                        snapshot['batches'].insert(0 if prepend else len(snapshot['batches']),batch)
                        with self.assertRaisesRegex(AssertionError,'duplicate received span identity'):
                            m.inspect(snapshot,expected)

    def test_same_span_id_in_separate_traces_is_valid(self):
        m,snapshot,expected=self.fixture()
        other=copy.deepcopy(snapshot['batches'][0])
        for span in other['data']['resource_spans'][0]['scope_spans'][0]['spans']:
            span['trace_id']='trace2'
            if 'links' in span:span['links']=[{'trace_id':'remote2'}]
        snapshot['batches'].append(other)
        # Only the original trace is requested; unrelated valid spans still
        # participate in full received-identity validation.
        m.inspect(snapshot,expected)

    def test_dependency_sibling_is_not_worker_lineage(self):
        m,snapshot,expected = self.fixture()
        m.records(snapshot,'traces')[2]['parent_span_id'] = 'root'
        with self.assertRaisesRegex(AssertionError,'parent chain'):
            m.inspect(snapshot,expected)

    def test_concurrent_job_context_leak_rejected(self):
        m,snapshot,expected = self.fixture()
        m.records(snapshot,'logs')[2]['attributes'][0]['value']['string_value'] = 'b'*32
        with self.assertRaisesRegex(AssertionError,'crossed trace trees'):
            m.inspect(snapshot,expected)

    def test_trace_identity_metric_label_rejected(self):
        m,snapshot,expected = self.fixture()
        metric = m.records(snapshot,'metrics')[0]
        metric['sum']['data_points'][0]['attributes'].append({'key':'trace_id','value':{'string_value':'trace1'}})
        with self.assertRaisesRegex(AssertionError,'schema mismatch'):
            m.inspect(snapshot,expected)

    def test_content_sentinel_prevents_acceptance(self):
        m = load('e2e', 'scripts/verify/telemetry_e2e.py')
        with self.assertRaisesRegex(AssertionError, 'sentinel'):
            m.inspect({'overflow': 0, 'batches': [{'data':m.SENTINELS[0]}]}, [])

    def test_capture_overflow_prevents_acceptance(self):
        m = load('e2e', 'scripts/verify/telemetry_e2e.py')
        with self.assertRaisesRegex(AssertionError, 'overflow'):
            m.inspect({'overflow': 1, 'batches': []}, [])


class SamplingTests(unittest.TestCase):
    def setUp(self):
        self.m = load('e2e_sampling', 'scripts/verify/telemetry_e2e.py')

    def test_compose_stats_queries_one_service_per_command(self):
        value = json.dumps({'CPUPerc':'1.0%', 'MemUsage':'10MiB /256MiB', 'PIDs':'2'})
        with patch.object(self.m, 'compose', return_value=value) as call:
            samples = self.m.stats_snapshot(True)
        self.assertEqual(set(samples), {'api','otel-collector'})
        self.assertEqual([c.args for c in call.call_args_list], [
            ('stats','--no-stream','--format','json','api'),
            ('stats','--no-stream','--format','json','otel-collector')])
        self.assertTrue(all(c.kwargs['capture_stderr'] for c in call.call_args_list))

    def test_unavailable_collector_still_requires_api_measurement(self):
        with patch.object(self.m,'compose',return_value='{"CPUPerc":"1%","MemUsage":"10MiB /256MiB"}') as call:
            samples = self.m.stats_snapshot(False)
        self.assertEqual(call.call_count,1)
        self.assertEqual(samples['otel-collector'], {'state':'stopped_by_test'})
        with patch.object(self.m,'compose',return_value='[]'):
            with self.assertRaisesRegex(ValueError,'missing'):
                self.m.stats_snapshot(False)

    def test_sampling_failure_fails_acceptance_without_raw_diagnostics(self):
        error = subprocess.CalledProcessError(2,['SECRET_COMMAND'],stderr='SENTINEL_SECRET_OUTPUT')
        reached = threading.Event()
        def fail(_):
            reached.set()
            raise error
        with patch.object(self.m,'stats_snapshot',side_effect=fail):
            with self.assertRaisesRegex(AssertionError,'resource sampling failed') as caught:
                with self.m.resource_samples(True):
                    self.assertTrue(reached.wait(2))
        self.assertIn('"exit_code": 2',str(caught.exception))
        self.assertNotIn('SECRET',str(caught.exception))

    def test_workload_failure_preserved_with_sampler_diagnostic_note(self):
        reached = threading.Event()
        def fail(_):
            reached.set()
            raise subprocess.TimeoutExpired('SECRET_COMMAND',8,stderr='SECRET_OUTPUT')
        original = ValueError('primary workload failure')
        with patch.object(self.m,'stats_snapshot',side_effect=fail):
            with self.assertRaises(ValueError) as caught:
                with self.m.resource_samples(True):
                    self.assertTrue(reached.wait(2))
                    raise original
        self.assertIs(caught.exception, original)
        self.assertIn('TimeoutExpired',' '.join(original.__notes__))
        self.assertNotIn('SECRET',' '.join(original.__notes__))

    def test_cleanup_preserves_primary_and_attempts_all_actions(self):
        original = ValueError('primary workload failure')
        reached = []
        def fail():
            reached.append('first')
            raise subprocess.CalledProcessError(4,['secret'],stderr='secret')
        self.m.cleanup([('receiver',fail), ('collector',lambda:reached.append('second'))],original)
        self.assertEqual(reached,['first','second'])
        self.assertIn('"exit_code": 4',' '.join(original.__notes__))
        self.assertNotIn('secret',' '.join(original.__notes__))
        with self.assertRaisesRegex(AssertionError,'cleanup failed'):
            self.m.cleanup([('receiver',fail)])


if __name__ == '__main__':
    unittest.main()
