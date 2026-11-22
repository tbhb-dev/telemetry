"""In-memory OTLP fixture, including a collector that never responds."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest


class Receiver:
    def __init__(self, stalled=False):
        self.batches, self.release = [], threading.Event()
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                owner.batches.append(ExportTraceServiceRequest.FromString(
                    self.rfile.read(int(self.headers["Content-Length"]))))
                if stalled:
                    owner.release.wait(3)
                else:
                    self.send_response(200)
                    self.end_headers()
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(("localhost", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = "http://localhost:%d" % self.server.server_port

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()

    def spans(self):
        return [span for batch in self.batches for resource in batch.resource_spans
                for scope in resource.scope_spans for span in scope.spans]
