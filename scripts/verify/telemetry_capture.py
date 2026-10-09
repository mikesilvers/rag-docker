"""Disposable, private, bounded OTLP protobuf receiver; never deployed in base stack."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time
from google.protobuf.json_format import MessageToDict
from google.protobuf.message import DecodeError
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest

MESSAGES = {'traces': ExportTraceServiceRequest, 'metrics': ExportMetricsServiceRequest,
            'logs': ExportLogsServiceRequest}
MAX_BODY = 1024 * 1024
MAX_CAPTURE = 16 * 1024 * 1024
MAX_BATCHES = 1000


class Capture:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.batches = []
        self.size = self.requests = self.overflow = 0
        self.mode = 'healthy'

    def accept(self, signal, body):
        message = MESSAGES[signal]()
        message.ParseFromString(body)
        data = MessageToDict(message, preserving_proto_field_name=True)
        size = len(json.dumps(data).encode())
        with self.lock:
            self.requests += 1
            if self.size + size > MAX_CAPTURE or len(self.batches) >= MAX_BATCHES:
                self.overflow += 1
                return False
            self.size += size
            self.batches.append({'signal': signal, 'data': data})
        return True


capture = Capture()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(3)

    def reply(self, code, data=b'', content='application/x-protobuf'):
        self.send_response(code)
        self.send_header('Content-Type', content)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path != '/snapshot':
            self.reply(404)
            return
        with capture.lock:
            data = json.dumps({'batches': capture.batches, 'requests': capture.requests,
                               'overflow': capture.overflow, 'bytes': capture.size}).encode()
        self.reply(200, data, 'application/json')

    def do_POST(self):
        if self.path in ('/reset', '/healthy', '/slow'):
            with capture.lock:
                if self.path == '/reset':
                    capture.reset()
                else:
                    capture.mode = self.path[1:]
            self.reply(200)
            return
        signal = self.path.removeprefix('/v1/')
        if signal not in MESSAGES or self.headers.get('Content-Encoding', '') not in ('', 'identity'):
            self.reply(400)
            return
        try:
            size = int(self.headers.get('Content-Length', '-1'))
            if not 0 <= size <= MAX_BODY:
                self.reply(413)
                return
            body = self.rfile.read(size)
            if len(body) != size:
                self.reply(400)
                return
            if capture.mode == 'slow':
                time.sleep(4)  # Exceeds configured collector 2s timeout, bounded handler lifetime.
                self.reply(503)
                return
            self.reply(200 if capture.accept(signal, body) else 503)
        except (ValueError, DecodeError, socket.timeout):
            self.reply(400)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    slots = threading.BoundedSemaphore(16)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def handle_error(self, request, address):
        pass  # Never print request/body/error content.


if __name__ == '__main__':
    Server(('0.0.0.0', 4319), Handler).serve_forever()
