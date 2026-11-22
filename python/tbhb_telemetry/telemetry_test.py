"""Behavior and timing checks against real OTLP HTTP transport."""

import contextlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
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
        self.assertEqual(root.name, "sample agent")
        self.assertEqual(root.parent_span_id.hex(), "b" * 16)
        self.assertEqual(root.trace_id.hex(), "a" * 32)
        self.assertEqual(root.trace_state, "vendor=value")
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
        for value, expected in [(None, 0), (9, 9), ("message", 1)]:
            def main():
                raise SystemExit(value)
            self.assertEqual(self.run_command(main), expected)
        with self.assertRaisesRegex(ValueError, "original"):
            self.run_command(lambda: (_ for _ in ()).throw(ValueError("original")))
        root = self.receiver.spans()[-1]
        self.assertEqual(next(a.value.string_value for a in root.attributes if a.key == "error.type"), "ValueError")
        self.assertEqual(len(self.receiver.spans()), 4)
        os.environ["TRACEPARENT"] = os.environ["TRACEPARENT"][:-2] + "00"
        self.run_command()
        self.assertEqual(len(self.receiver.spans()), 5)

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
            self.run_command()
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = self.receiver.endpoint
        self.run_command()
        self.run_command()
        counts = [next(a.value.int_value for a in s.attributes if a.key == "tbhb.export.dropped")
                  for s in self.receiver.spans()]
        self.assertEqual(counts, [0, 2, 0])
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
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["count"], 1)

    def test_endpoint_exporter_disabled_and_capacity(self):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://example.invalid:4318"
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

    def test_fresh_process_quiet(self):
        with socket.socket() as reserved:
            reserved.bind(("localhost", 0))
            os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://localhost:%d" % reserved.getsockname()[1]
            result = subprocess.run([sys.executable, "-c",
                'from tbhb_telemetry import command; raise SystemExit(command("sample", version="1", main=lambda: 0))'],
                capture_output=True, timeout=3)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))


if __name__ == "__main__":
    unittest.main()
