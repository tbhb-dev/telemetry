"""One bounded, best-effort trace for a synchronous CLI command."""

import contextlib
import functools
import http.client
import ipaddress
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from urllib.parse import urlsplit

_state = None
_installed = False
_KEYS = {"tbhb.agent." + key for key in ("host", "harness", "model", "profile", "outcome")}
_KEYS.add("tbhb.run.id")


def _quiet(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception:
            return None
    return wrapped


@_quiet
def _counter(delta=0, reason=None):
    import fcntl
    directory = Path(os.getenv("XDG_STATE_HOME") or Path.home() / ".local/state") / "tbhb/telemetry"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "drops.lock").open("a") as lock:
        deadline = time.monotonic() + 0.02
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                time.sleep(min(0.002, remaining))
        file = directory / "drops.count"
        try:
            count = int(file.read_text())
        except (FileNotFoundError, ValueError):
            count = 0
        if reason:
            with (directory / "drops.jsonl").open("a") as log:
                if os.stat(log.fileno()).st_size < 1024 * 1024:
                    log.write(json.dumps(dict(timestamp=time.time(), count=delta, reason=reason)) + "\n")
        with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as temporary:
            path = Path(temporary.name)
            try:
                temporary.write(str(max(0, count + delta)))
                temporary.close()
                os.replace(path, file)
            finally:
                path.unlink(missing_ok=True)
        return count


class _Pending:
    def __init__(self, name, version):
        self.name, self.start = name, time.time_ns()
        self.attributes = {"process.executable.name": name, "service.name": name,
                           "service.version": version, "tbhb.cli.command": name,
                           "tbhb.export.dropped": _counter() or 0}
        self.inbound = {key: os.getenv(key.upper(), "") for key in ("traceparent", "tracestate")}
        self.root, self.spans, self.lost = None, [], 0

    def materialize(self):
        if self.root is None:
            from opentelemetry import context, trace
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.sampling import ALWAYS_ON
            from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
            self.provider = TracerProvider(resource=Resource({
                "service.name": self.attributes["service.name"],
                "service.version": self.attributes["service.version"]}),
                sampler=ALWAYS_ON, shutdown_on_exit=False)
            self.provider.add_span_processor(self)
            self.tracer = self.provider.get_tracer("tbhb_telemetry")
            self.root = self.tracer.start_span(self.name, attributes=self.attributes,
                context=TraceContextTextMapPropagator().extract(self.inbound), start_time=self.start,
                record_exception=False, set_status_on_exception=False)
            self.token = context.attach(trace.set_span_in_context(self.root))
        return self.root.get_span_context()

    def on_start(self, span, parent_context=None):
        pass

    def _on_ending(self, span):
        pass

    def on_end(self, span):
        if len(self.spans) < 511 or span.context == self.root.get_span_context():
            self.spans.append(span)
        else:
            self.lost += 1

    def finish(self, code, error):
        from opentelemetry import context, trace
        self.materialize()
        self.root.set_attribute("process.exit.code", code)
        if error or code:
            self.root.set_attribute("error.type", type(error).__name__ if error else "SystemExit")
            self.root.set_status(trace.StatusCode.ERROR)
        self.root.end()
        context.detach(self.token)
        if self.lost:
            _counter(self.lost, "capacity")
        endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://%s:4318" % ipaddress.ip_address(0x7F000001))
        host = urlsplit(endpoint).hostname
        try:
            allowed = host == "localhost" or ipaddress.ip_address(host).is_loopback
        except ValueError:
            allowed = False
        raw_hosts = os.getenv("TBHB_TELEMETRY_ALLOW_HOSTS", "")
        hosts = [entry.strip().lower() for entry in (raw_hosts if len(raw_hosts) <= 4096 else "").split(",")]
        hosts = [entry for entry in hosts if 0 < len(entry) <= 253][:8]
        allowed = allowed or host in hosts
        if not allowed:
            _counter(len(self.spans), "endpoint")
            return
        done, result = threading.Event(), []
        def send():
            try:
                _export(self.spans, endpoint)
                result.append(True)
            except Exception:
                result.append(False)
            finally:
                done.set()
        threading.Thread(target=send, daemon=True).start()
        try:
            completed = done.wait(0.25)
        except KeyboardInterrupt:
            return
        if not completed:
            _counter(len(self.spans), "timeout")
        elif not result[0]:
            _counter(len(self.spans), "export")
        else:
            _counter(-self.attributes["tbhb.export.dropped"])


def _export(spans, endpoint):
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import encode_spans
    url = urlsplit(endpoint)
    connection = (http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection)(
        url.hostname, url.port if url.port is not None else (443 if url.scheme == "https" else 80), timeout=0.25)
    try:
        connection.request("POST", url.path.rstrip("/") + "/v1/traces",
                           encode_spans(spans).SerializeToString(), {"Content-Type": "application/x-protobuf"})
        if not 200 <= connection.getresponse().status < 300:
            raise RuntimeError("collector rejected batch")
    finally:
        connection.close()


class _Tracer:
    def start_span(self, *args, **kwargs):
        from opentelemetry import trace
        try:
            if _state:
                _state.materialize()
                kwargs.update(record_exception=False, set_status_on_exception=False)
                return _state.tracer.start_span(*args, **kwargs)
        except Exception:
            pass
        return trace.INVALID_SPAN

    @contextlib.contextmanager
    def start_as_current_span(self, *args, **kwargs):
        from opentelemetry import trace
        end = kwargs.pop("end_on_exit", True)
        with trace.use_span(self.start_span(*args, **kwargs), end_on_exit=end,
                            record_exception=False, set_status_on_exception=False) as span:
            yield span


@_quiet
def _setup(name, version):
    global _state, _installed
    if os.getenv("OTEL_SDK_DISABLED", "").lower() == "true":
        return
    from opentelemetry import trace
    if not _installed:
        class Provider(trace.TracerProvider):
            def get_tracer(self, *args, **kwargs):
                return _Tracer()
        if isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider):
            trace.set_tracer_provider(Provider())
        _installed = True
    _state = _Pending(name, version)


@_quiet
def resolve(name):
    """Append a fixed command key or parsed subcommand name, never an argument value."""
    if _state:
        _state.name += " " + name
        _state.attributes["tbhb.cli.command"] = _state.name
        if _state.root:
            _state.root.update_name(_state.name)
            _state.root.set_attribute("tbhb.cli.command", _state.name)


@_quiet
def materialize():
    if _state:
        return _state.materialize()


@_quiet
def set_attributes(**kwargs):
    if _state:
        for key, value in kwargs.items():
            if key in _KEYS and isinstance(value, str) and len(value) <= 128:
                if key == "tbhb.agent.outcome" and value not in ("ok", "refused", "failed"):
                    continue
                _state.attributes[key] = value
                if _state.root:
                    _state.root.set_attribute(key, value)


def command(name, *, version, main):
    """Run main, preserving its result, output and exceptions."""
    global _state
    _setup(name, version)
    code, error = 0, None
    try:
        code = main()
        return 0 if code is None else code
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        error = exc if code else None
        raise
    except BaseException as exc:
        code, error = 1, exc
        raise
    finally:
        try:
            if _state:
                _state.finish(0 if code is None else code, error)
        except Exception:
            pass
        _state = None
