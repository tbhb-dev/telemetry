"""Behavior and timing checks against real OTLP HTTP transport."""

import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from opentelemetry import trace
from tbhb_telemetry.receiver import Receiver
import tbhb_telemetry as telemetry


class TelemetryTest(unittest.TestCase):
    def setUp(self):
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
            os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://localhost:%d" % reserved.getsockname()[1]
            output, errors = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                start = time.monotonic()
                self.assertEqual(self.run_command(lambda: (print("command"), 3)[1]), 3)
                elapsed = time.monotonic() - start
            self.assertEqual((output.getvalue(), errors.getvalue()), ("command\n", ""))
            self.assertEqual(len((self.drops() / "drops.jsonl").read_text().splitlines()), 1)
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
        self.assertEqual(json.loads((self.drops() / "drops.jsonl").read_text().splitlines()[-1])["count"], 9)

    def test_endpoint_allowlist(self):
        entries = ["collector%d.example" % index for index in range(9)]
        longest = "a" * 245 + ".example"
        for allowlist, host, allowed in (
            (None, "collector.example", False),
            ("", "collector.example", False),
            ("collector.example", "collector.example", True),
            ("other.example", "collector.example", False),
            ("COLLECTOR.EXAMPLE", "collector.example", True),
            ("collector.example", "COLLECTOR.EXAMPLE", True),
            (" , , collector.example, , ", "collector.example", True),
            ("*", "collector.example", False),
            (".example.net", "collector.example.net", False),
            ("collector.example", "other.collector.example", False),
            (",".join(entries), entries[7], True),
            (",".join(entries), entries[8], False),
            (longest, longest, True),
            ("a" + longest, "a" + longest, False),
            ("a" * 254 + ",collector.example", "collector.example", True),
            ("other.example", "localhost", True),
        ):
            with self.subTest(allowlist=allowlist, host=host):
                if allowlist is None:
                    os.environ.pop("TBHB_TELEMETRY_ALLOW_HOSTS", None)
                else:
                    os.environ["TBHB_TELEMETRY_ALLOW_HOSTS"] = allowlist
                endpoint = "http://%s:4318" % host
                os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = endpoint
                with patch.object(telemetry, "_export") as export:
                    self.assertEqual(self.run_command(lambda: 7), 7)
                    if allowed:
                        export.assert_called_once_with(unittest.mock.ANY, endpoint)
                        self.assertEqual(len(export.call_args.args[0]), 1)
                    else:
                        export.assert_not_called()
                        record = json.loads((self.drops() / "drops.jsonl").read_text().splitlines()[-1])
                        self.assertEqual((record["reason"], record["count"]), ("endpoint", 1))

    def test_allowlisted_export_failures_and_disabled_preserve_exit(self):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://collector.example:4318"
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
            self.assertEqual(json.loads((self.drops() / "drops.jsonl").read_text().splitlines()[-1])["reason"], "timeout")
        finally:
            release.set()
            self.assertTrue(finished.wait(1))
        os.environ["OTEL_SDK_DISABLED"] = "true"
        with patch.object(telemetry, "_export") as export:
            self.assertEqual(self.run_command(lambda: 5), 5)
            export.assert_not_called()


if __name__ == "__main__":
    unittest.main()
