"""Behavior and timing checks against real OTLP HTTP transport."""

import contextlib
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
from ipaddress import IPv6Address
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from textwrap import dedent
import threading
import time
import unittest
from unittest.mock import patch

from opentelemetry import trace
from tbhb_telemetry.receiver import Receiver
import tbhb_telemetry as telemetry


class TelemetryTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(telemetry, "_run_id", None))
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.environment = patch.dict(os.environ, {
            "XDG_STATE_HOME": self.directory.name, "OTEL_SDK_DISABLED": "false",
            "TRACEPARENT": "00-" + "a" * 32 + "-" + "b" * 16 + "-01", "TRACESTATE": "vendor=value"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        os.environ.pop("TBHB_TELEMETRY_ALLOW_HOSTS", None)
        self.receiver = Receiver()
        self.addCleanup(self.receiver.close)
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = self.receiver.endpoint

    def run_command(self, main=lambda: 0):
        return telemetry.command("sample", version="1.2.3", main=main)

    def drops(self):
        return Path(self.directory.name) / "tbhb/telemetry"

    def test_trace(self):
        def main():
            self.assertIsNone(telemetry._state.root)
            telemetry.resolve("agent")
            telemetry.set_attributes(**{**{"tbhb.agent." + k: "ok" for k in
                ("host", "harness", "model", "profile", "outcome")}, "tbhb.run.id": "run", "unknown": "ignored"})
            with trace.get_tracer("test").start_as_current_span("child") as child:
                self.assertEqual(child.parent, telemetry.materialize())
            return 7
        self.assertEqual(self.run_command(main), 7)
        child, root = self.receiver.spans()
        self.assertEqual(child.parent_span_id, root.span_id)
        self.assertEqual((root.name, root.parent_span_id.hex(), root.trace_id.hex(), root.trace_state),
                         ("sample agent", "b" * 16, "a" * 32, "vendor=value"))
        attrs = {a.key: a.value for a in root.attributes}
        self.assertEqual(attrs["process.exit.code"].int_value, 7)
        for key, value in {"process.executable.name": "sample", "service.name": "sample",
            "service.version": "1.2.3", "tbhb.cli.command": "sample agent", "error.type": "SystemExit",
            **{"tbhb.agent." + k: "ok" for k in ("host", "harness", "model", "profile", "outcome")},
            "tbhb.run.id": "run"}.items():
            self.assertEqual(attrs[key].string_value, value)
        self.assertNotIn("unknown", attrs)
        self.assertEqual(root.status.code, 2)

    def test_exits_and_exception(self):
        for value, code in ((None, 0), (2, 2), ("msg", 1)):
            original = SystemExit(value)
            def main():
                raise original
            with self.assertRaises(SystemExit) as caught:
                self.run_command(main)
            self.assertIs(caught.exception, original)
            script = 'import sys\ndef main():\n    print("command")\n    sys.exit(%r)\n' % value
            expected = (code, b"command\n", b"msg\n" if code == 1 else b"")
            for disabled in (None, "false", "true"):
                with self.subTest(value=value, disabled=disabled):
                    before = len(self.receiver.spans())
                    call = "main()" if disabled is None else (
                        'from tbhb_telemetry import command\ncommand("sample", version="1", main=main)')
                    result = subprocess.run([sys.executable, "-c", script + call], capture_output=True,
                        env={**os.environ, "OTEL_SDK_DISABLED": disabled or "false"}, timeout=3)
                    self.assertEqual((result.returncode, result.stdout, result.stderr), expected)
                    self.assertEqual(len(self.receiver.spans()), before + (disabled == "false"))
            attrs = {a.key: a.value for a in self.receiver.spans()[-1].attributes}
            self.assertEqual(attrs["process.exit.code"].int_value, code)
            if code:
                self.assertEqual(attrs["error.type"].string_value, "SystemExit")
        with self.assertRaisesRegex(ValueError, "original"):
            self.run_command(lambda: (_ for _ in ()).throw(ValueError("original")))
        root = self.receiver.spans()[-1]
        self.assertEqual(next(a.value.string_value for a in root.attributes if a.key == "error.type"), "ValueError")
        self.assertEqual(len(self.receiver.spans()), 7)
        os.environ["TRACEPARENT"] = os.environ["TRACEPARENT"][:-2] + "00"
        self.run_command()
        self.assertEqual(len(self.receiver.spans()), 8)

    def test_exception_message_stays_out_of_spans(self):
        marker = "private-exception-marker"
        def main():
            tracer = trace.get_tracer("test")
            for start in (tracer.start_span, tracer.start_as_current_span):
                with self.assertRaises(ValueError):
                    with start("child", record_exception=True, set_status_on_exception=True):
                        raise ValueError(marker)
            raise ValueError(marker)
        with self.assertRaisesRegex(ValueError, marker):
            self.run_command(main)
        spans = self.receiver.spans()
        self.assertEqual(len(spans), 3)
        for batch in self.receiver.batches:
            self.assertNotIn(marker.encode(), batch.SerializeToString())
        for child in spans[:-1]:
            self.assertEqual(len(child.events), 0)
            self.assertEqual(child.status.code, 0)
        self.assertEqual(next(a.value.string_value for a in spans[-1].attributes
                              if a.key == "error.type"), "ValueError")

    def test_resource_has_only_service_attributes(self):
        with patch.dict(os.environ, {"OTEL_RESOURCE_ATTRIBUTES": "private.marker=hidden,service.name=override",
                                     "OTEL_SERVICE_NAME": "override"}):
            self.run_command()
        resource = self.receiver.batches[-1].resource_spans[0].resource
        self.assertEqual({a.key: a.value.string_value for a in resource.attributes},
                         {"service.name": "sample", "service.version": "1.2.3"})

    def test_held_drop_lock_is_bounded(self):
        self.run_command()  # Warm SDK imports before measuring lock overhead.
        with (self.drops() / "drops.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            start = time.monotonic()
            self.assertEqual(self.run_command(lambda: 5), 5)
            self.assertLess(time.monotonic() - start, 0.1)
        self.assertEqual(len(self.receiver.spans()), 2)

    def test_corrupt_drop_count_recovers(self):
        self.drops().mkdir(parents=True)
        file = self.drops() / "drops.count"
        for corrupt in ("", "corrupt"):
            with self.subTest(corrupt=corrupt):
                file.write_text(corrupt)
                with patch.object(telemetry, "_export", side_effect=RuntimeError):
                    self.assertEqual(self.run_command(), 0)
                self.assertEqual(file.read_text(), "1")
                self.run_command()
                root = self.receiver.spans()[-1]
                self.assertEqual(next(a.value.int_value for a in root.attributes
                                      if a.key == "tbhb.export.dropped"), 1)
                self.assertEqual(file.read_text(), "0")

    def test_drop_count_replacement_is_atomic(self):
        self.assertEqual(telemetry._counter(2), 0)
        replace = os.replace
        def check_replace(source, destination):
            self.assertEqual(source.parent, self.drops())
            self.assertEqual(destination, self.drops() / "drops.count")
            self.assertEqual(destination.read_text(), "2")
            self.assertEqual(source.read_text(), "5")
            replace(source, destination)
        with patch.object(telemetry.os, "replace", side_effect=check_replace) as replacement:
            self.assertEqual(telemetry._counter(3), 2)
            replacement.assert_called_once()
        self.assertEqual((self.drops() / "drops.count").read_text(), "5")
        self.assertEqual({p.name for p in self.drops().iterdir()}, {"drops.count", "drops.lock"})

    def test_drop_log_stops_appending_at_size_limit(self):
        self.drops().mkdir(parents=True)
        log = self.drops() / "drops.jsonl"
        at_limit = b"{}".ljust(1024 * 1024 - 1) + b"\n"
        log.write_bytes(at_limit)
        self.assertEqual(telemetry._counter(1, "endpoint"), 0)
        self.assertEqual(log.read_bytes(), at_limit)

        below_limit = at_limit[:-2] + b"\n"
        log.write_bytes(below_limit)
        self.assertEqual(telemetry._counter(1, "endpoint"), 1)
        with log.open("rb") as records:
            self.assertEqual(records.readline(), below_limit)
            appended, = records
        self.assertEqual(json.loads(appended)["reason"], "endpoint")

    def test_empty_state_home_uses_home(self):
        working = Path(self.directory.name) / "working"
        working.mkdir()
        previous = Path.cwd()
        try:
            os.chdir(working)
            with patch.dict(os.environ, {"XDG_STATE_HOME": "", "HOME": self.directory.name}):
                self.assertEqual(self.run_command(), 0)
            self.assertEqual(list(working.iterdir()), [])
            self.assertEqual((Path(self.directory.name) /
                              ".local/state/tbhb/telemetry/drops.count").read_text(), "0")
        finally:
            os.chdir(previous)

    def test_failures_and_recovery(self):
        self.run_command()  # Warm SDK imports before measuring transport overhead.
        with socket.socket() as reserved:
            reserved.bind(("localhost", 0))
            authority = "%s:%d" % ("localhost", reserved.getsockname()[1])
            os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://" + authority
            def main():
                telemetry.set_attributes(**{"tbhb.run.id": "closed-port"})
                print("command")
                return 3
            output, errors = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                start = time.monotonic()
                self.assertEqual(self.run_command(main), 3)
                elapsed = time.monotonic() - start
            self.assertEqual((output.getvalue(), errors.getvalue()), ("command\n", ""))
            line, = (self.drops() / "drops.jsonl").read_text().splitlines()
            record = json.loads(line)
            self.assertEqual((record["reason"], record["run_id"], record["endpoint"]),
                             ("export", "closed-port", authority))
            print("closed-port overhead: %.2f ms" % (elapsed * 1000))
            self.assertLess(elapsed, 0.1)
            result = subprocess.run([sys.executable, "-c",
                'from tbhb_telemetry import command; raise SystemExit(command("sample", version="1", main=lambda: 0))'],
                capture_output=True, timeout=3)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))
            self.run_command()
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = self.receiver.endpoint
        self.run_command()
        self.run_command()
        counts = [next(a.value.int_value for a in s.attributes if a.key == "tbhb.export.dropped")
                  for s in self.receiver.spans()]
        self.assertEqual(counts, [0, 3, 0])
        self.assertEqual((self.drops() / "drops.count").read_text(), "0")

    def test_refused_endpoint_drop_context(self):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "https://user@example.invalid/path?key=value"
        run_id = "run-" + "a" * 130
        def main():
            telemetry.set_attributes(**{"tbhb.run.id": "previous"})
            telemetry.set_attributes(**{"tbhb.run.id": run_id})
        with patch.object(telemetry, "_export") as export:
            self.assertEqual(self.run_command(main), 0)
            host = ".".join(("a" * 60, "b" * 60, "c" * 20, "example.invalid"))
            os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = f"http://{host}:4318"
            self.assertEqual(self.run_command(), 0)
            export.assert_not_called()
        first, second = map(json.loads, (self.drops() / "drops.jsonl").read_text().splitlines())
        self.assertEqual((first["reason"], first["run_id"], first["endpoint"]),
                         ("endpoint", run_id[:128], "example.invalid:443"))
        self.assertEqual((second["reason"], second["run_id"], second["endpoint"]),
                         ("endpoint", run_id[:128], host[:123] + ":4318"))

    def test_malformed_endpoint_drop_context(self):
        telemetry.set_attributes(**{"tbhb.run.id": "malformed"})
        log = self.drops() / "drops.jsonl"
        for endpoint in ("http://[example.invalid", "http://example.invalid:invalid", "http:///path"):
            with self.subTest(endpoint=endpoint):
                before = len(log.read_text().splitlines()) if log.exists() else 0
                os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = endpoint
                self.assertEqual(self.run_command(lambda: 4), 4)
                appended = log.read_text().splitlines()[before:]
                self.assertEqual(len(appended), 1)
                record = json.loads(appended[0])
                self.assertEqual((record["reason"], record["run_id"], record["endpoint"]),
                                 ("endpoint", "malformed", None))

    def test_ipv6_drop_context(self):
        authority = f"[{IPv6Address(1)}]:4318"
        telemetry._counter(1, "endpoint", telemetry.urlsplit(f"http://{authority}"))
        line, = (self.drops() / "drops.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(line)["endpoint"], authority)

    def test_stalled(self):
        stalled = Receiver(stalled=True)
        self.addCleanup(stalled.close)
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = stalled.endpoint
        start = time.monotonic()
        self.assertEqual(self.run_command(lambda: 4), 4)
        elapsed = time.monotonic() - start
        print("stalled-listener overhead: %.2f ms" % (elapsed * 1000))
        self.assertLess(elapsed, 0.35)
        lines = (self.drops() / "drops.jsonl").read_text().splitlines()
        self.assertEqual((len(lines), json.loads(lines[0])["count"]), (1, 1))

    def test_sigint_during_export_wait_preserves_exit(self):
        telemetry._counter(2)
        script = dedent("""\
            import signal
            import sys
            import threading
            from unittest.mock import patch
            import tbhb_telemetry as telemetry

            waiting, release = threading.Event(), threading.Event()
            wait = threading.Event.wait
            main_thread = threading.get_ident()

            def stalled_export(*args):
                waiting.wait()
                signal.pthread_kill(main_thread, signal.SIGINT)  # cspell:ignore pthread
                release.wait()

            def wait_for_export(event, timeout=None):
                if timeout == 0.25:
                    waiting.set()
                return wait(event, timeout)

            def main():
                print("command output")
                return 7

            with (
                patch.object(telemetry, "_export", stalled_export),
                patch.object(threading.Event, "wait", wait_for_export),
            ):
                sys.exit(telemetry.command("sample", version="1", main=main))
            """)
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=3)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (7, b"command output\n", b""))
        self.assertEqual((self.drops() / "drops.count").read_text(), "2")
        self.assertFalse((self.drops() / "drops.jsonl").exists())

    def test_endpoint_exporter_disabled_and_capacity(self):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://collector.example:4318"
        with patch.object(telemetry, "_export") as export:
            self.assertEqual(self.run_command(), 0)
            export.assert_not_called()
        self.assertEqual(len((self.drops() / "drops.jsonl").read_text().splitlines()), 1)
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = self.receiver.endpoint
        with patch.object(telemetry, "_export", side_effect=RuntimeError):
            self.assertEqual(self.run_command(lambda: 6), 6)
        os.environ["OTEL_SDK_DISABLED"] = "true"
        with patch("opentelemetry.sdk.trace.TracerProvider") as provider:
            self.assertEqual(self.run_command(), 0)
            provider.assert_not_called()
        os.environ["OTEL_SDK_DISABLED"] = "false"
        def main():
            for _ in range(520):
                trace.get_tracer("test").start_span("child").end()
        self.assertEqual(self.run_command(main), 0)
        self.assertEqual(len(self.receiver.spans()), 512)
        record = json.loads((self.drops() / "drops.jsonl").read_text().splitlines()[-1])
        self.assertEqual((record["reason"], record["count"], record["endpoint"]),
                         ("capacity", 9, self.receiver.endpoint.removeprefix("http://")))

    def test_endpoint_allowlist(self):
        entries = ["collector%d.example" % index for index in range(9)]
        longest = "a" * 245 + ".example"
        capped = "collector.example".ljust(4096)
        getaddrinfo = socket.getaddrinfo
        def resolve(host, port, *args, **kwargs):
            return getaddrinfo("127.0.0.1", self.receiver.server.server_port, *args, **kwargs)
        def main():
            print("command output")
            print("command error", file=sys.stderr)
            return 7
        for allowlist, host, allowed in (
            (None, "collector.example", False),
            ("", "collector.example", False),
            (" \t ", "collector.example", False),
            (",,,", "collector.example", False),
            ("collector.example", "collector.example", True),
            ("other.example", "collector.example", False),
            ("COLLECTOR.EXAMPLE", "collector.example", True),
            ("collector.example", "COLLECTOR.EXAMPLE", True),
            (" , , collector.example, , ", "collector.example", True),
            ("*", "collector.example", False),
            (".example", "collector.example", False),
            ("collector.example", "other.collector.example", False),
            ("collector.example", "collector.example.", False),
            ("collector.example:4318", "collector.example", False),
            ("collector.example", "collector.example@other.collector.example", False),
            ("other.collector.example", "collector.example@other.collector.example", True),
            (None, "192.0.2.1", False),
            ("192.0.2.1", "192.0.2.1", True),
            ("192.0.2.10", "192.0.2.1", False),
            (None, "[2001:db8::1]", False),
            ("2001:db8::1", "[2001:db8::1]", True),
            ("2001:db8:0:0:0:0:0:1", "[2001:db8::1]", False),
            (",".join(entries), entries[7], True),
            (",".join(entries), entries[8], False),
            (longest, longest, True),
            ("a" + longest, "a" + longest, False),
            ("a" * 254 + ",collector.example", "collector.example", True),
            (capped, "collector.example", True),
            (capped + " ", "collector.example", False),
            (capped + " ", "localhost", True),
            ("other.example", "localhost", True),
        ):
            with self.subTest(allowlist=allowlist, host=host):
                if allowlist is None:
                    os.environ.pop("TBHB_TELEMETRY_ALLOW_HOSTS", None)
                else:
                    os.environ["TBHB_TELEMETRY_ALLOW_HOSTS"] = allowlist
                endpoint = "http://%s:4318" % host
                os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = endpoint
                before = len(self.receiver.spans())
                output, errors = io.StringIO(), io.StringIO()
                with patch.object(socket, "getaddrinfo", side_effect=resolve) as lookup, \
                        contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    self.assertEqual(self.run_command(main), 7)
                    self.assertEqual((output.getvalue(), errors.getvalue()),
                                     ("command output\n", "command error\n"))
                    if allowed:
                        lookup.assert_called_once_with(telemetry.urlsplit(endpoint).hostname, 4318, 0, socket.SOCK_STREAM)
                        self.assertEqual(len(self.receiver.spans()), before + 1)
                    else:
                        lookup.assert_not_called()
                        self.assertEqual(len(self.receiver.spans()), before)
                        record = json.loads((self.drops() / "drops.jsonl").read_text().splitlines()[-1])
                        self.assertEqual((record["reason"], record["count"]), ("endpoint", 1))

    def export_to_loopback(self, host, family):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                requests.append((self.command, self.path))
                self.send_response(200)
                self.end_headers()
            def log_message(self, *args):
                pass
        class Server(ThreadingHTTPServer):
            address_family = family
        try:
            server = Server((host, 0), Handler)
        except OSError as error:
            if family == socket.AF_INET6:
                self.skipTest("IPv6 loopback ::1 cannot be bound: %s" % error)
            raise
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            authority = "[%s]" % host if family == socket.AF_INET6 else host
            telemetry._export([], "http://%s:%d/" % (authority, server.server_port))
            self.assertEqual(requests, [("POST", "/v1/traces")])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_export_ipv6_loopback(self):
        self.export_to_loopback("::1", socket.AF_INET6)

    def test_export_ipv4_loopback(self):
        self.export_to_loopback("127.0.0.1", socket.AF_INET)

    def test_export_default_ports(self):
        for scheme, host, port, constructor in (
            ("http", "2001:db8::1", 80, "HTTPConnection"),
            ("https", "fd00::1:10", 443, "HTTPSConnection"),
        ):
            with self.subTest(scheme=scheme), patch.object(telemetry.http.client, constructor) as connection:
                connection.return_value.getresponse.return_value.status = 200
                telemetry._export([], "%s://[%s]/" % (scheme, host))
                connection.assert_called_once_with(host, port, timeout=0.25)

    def test_endpoint_paths_preserve_process_output_and_exit(self):
        script = ('import sys\ndef main():\n    print("command output")\n'
                  '    print("command error", file=sys.stderr)\n    return 7\n')
        baseline = subprocess.run([sys.executable, "-c", script + "sys.exit(main())"],
                                  capture_output=True, timeout=3)
        for endpoint, allowed in (("http://example.invalid", False), (self.receiver.endpoint, True)):
            with self.subTest(allowed=allowed):
                before = len(self.receiver.spans())
                result = subprocess.run([sys.executable, "-c", script +
                    'from tbhb_telemetry import command\nsys.exit(command("sample", version="1", main=main))'],
                    env={**os.environ, "OTEL_EXPORTER_OTLP_ENDPOINT": endpoint}, capture_output=True, timeout=3)
                self.assertEqual((result.stdout, result.stderr, result.returncode),
                                 (baseline.stdout, baseline.stderr, baseline.returncode))
                self.assertEqual(len(self.receiver.spans()), before + allowed)
                if not allowed:
                    record = json.loads((self.drops() / "drops.jsonl").read_text().splitlines()[-1])
                    self.assertEqual((record["reason"], record["run_id"], record["endpoint"]),
                                     ("endpoint", None, "example.invalid:80"))

    def test_allowlisted_export_failures_and_disabled_preserve_exit(self):
        endpoint = "http://collector.example:4318"
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = endpoint
        os.environ["TBHB_TELEMETRY_ALLOW_HOSTS"] = "collector.example"
        with patch.object(telemetry, "_export", side_effect=RuntimeError) as export:
            self.assertEqual(self.run_command(lambda: 6), 6)
            export.assert_called_once()
        self.assertEqual(json.loads((self.drops() / "drops.jsonl").read_text().splitlines()[-1])["reason"], "export")
        release, finished = threading.Event(), threading.Event()
        def stalled_export(spans, endpoint):
            release.wait(3)
            finished.set()
        try:
            with patch.object(telemetry, "_export", side_effect=stalled_export) as export:
                start = time.monotonic()
                self.assertEqual(self.run_command(lambda: 4), 4)
                self.assertLess(time.monotonic() - start, 0.35)
                export.assert_called_once()
            record = json.loads((self.drops() / "drops.jsonl").read_text().splitlines()[-1])
            self.assertEqual((record["reason"], record["endpoint"]),
                             ("timeout", endpoint.removeprefix("http://")))
        finally:
            release.set()
            self.assertTrue(finished.wait(1))
        os.environ["OTEL_SDK_DISABLED"] = "true"
        with patch.object(telemetry, "_export") as export:
            self.assertEqual(self.run_command(lambda: 5), 5)
            export.assert_not_called()


if __name__ == "__main__":
    unittest.main()
