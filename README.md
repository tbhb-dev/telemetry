# telemetry

This repository holds tbhb-dev's shared telemetry libraries. Licensed under
Apache-2.0.

## Dashboard

[Agents (live)](dashboards/agents-live.json) reads per-launch spans named
`tbhbdev alpha agent exec` from the Tempo data source (UID `tempo`). It lists
launches with host, harness, model, profile, outcome, run ID, and duration, and
charts launch counts by outcome and by host. Import it through the Grafana API
or UI. The dashboard stays empty until a launcher exports spans.

## Python

The `//python:tbhb_telemetry` Bazel library wraps a synchronous command-line tool:

```python
from tbhb_telemetry import command, resolve, set_attributes

def main():
    resolve("agent")
    set_attributes(**{"tbhb.agent.outcome": "ok"})
    return 0

raise SystemExit(command("sample", version="1.2.3", main=main))
```

Use only fixed command keys or parsed subcommand names with `resolve`, never
argument values. `materialize()` starts the pending root and returns its span
context. The first child span also starts the root. Agent attributes accept
strings up to 128 characters. The setter ignores unknown keys.

Traces use the OpenTelemetry Protocol (OTLP) over HTTP with a collector on the
loopback interface at port 4318. Set
`OTEL_EXPORTER_OTLP_ENDPOINT` to change the port, or `OTEL_SDK_DISABLED=true` to
turn off telemetry. Set `TBHB_TELEMETRY_ALLOW_HOSTS` to a comma-separated list of
exact host names, such as `collector.example`, to allow exports to those hosts.
The default allows only loopback endpoints.
Listed hosts use whatever scheme the endpoint names, so prefer `https`.

## Decision: default endpoint

Decided 2026-11-22 by the telemetry owner.

- The library default stays loopback at port 4318 and the library never discovers
  an endpoint. A default that reaches off-host would send spans where nobody chose.
- The caller picks any other collector with `OTEL_EXPORTER_OTLP_ENDPOINT` and
  `TBHB_TELEMETRY_ALLOW_HOSTS`. Commands that need a private collector resolve its
  host name at run time from the host's own network status. Names stay out of
  this repository, its commits and its pull requests.
- Where no loopback collector runs, the operator sets both variables. Plain
  `http` inside the private network is accepted and tracked as security debt.
- To undo, unset both variables. Unsetting only the allowlist while the endpoint
  still names another host drops the spans.

The package buffers 512 spans and waits up to 250 ms for its single export
attempt. Drop records and the locked counter live under
`$XDG_STATE_HOME/tbhb/telemetry` (default `~/.local/state`).
Each drop record contains `timestamp`, `count`, `reason`, `run_id`, and
`endpoint`. The `run_id` field contains the latest run ID of at most 128
characters set for the command. The `endpoint` field contains the export
destination's host and port, limited to 128 characters. Each field is `null`
when unavailable.
Once `drops.jsonl` reaches 1 MiB, the library skips further appends and keeps
updating the counter.

The installable Python package requires Python 3.12 or newer. Its dependency
pins live in `python/pyproject.toml`, which also supplies the Bazel lock input.
Regenerate the Python dependency lock with:

```sh
uv pip compile python/pyproject.toml --generate-hashes --python-version 3.12 --index-url https://pypi.org/simple --output-file python/requirements.lock
bazelisk mod deps --lockfile_mode=update
```

## Release wheel

CI groups pushes to `main` by commit SHA, so each merge keeps its own run and
publishes its own GitHub release. If a failed release job leaves a draft, rerun
the failed job to reuse and publish the draft. Each release holds the wheel and
its `.sha256` sidecar and uses the tag `sha-<12 hex>` (the first 12 characters of
the merge commit). Releases can finish in any order. New releases aren't marked
latest, and an older release may still carry the mark. Pin the sha tag and
never use `releases/latest`.
tools pins the wheel by https URL and hash, for example
`https://github.com/tbhb-dev/telemetry/releases/download/sha-<12 hex>/tbhb_telemetry-0-py3-none-any.whl`.
The file name never changes; the tag carries the version.

## Contributing

Changes land by pull request into `main`. Each pull request needs one approving
review and a passing `conclusion` check. Use squash merges.
This repository has Issues, Projects, and the wiki turned off.
