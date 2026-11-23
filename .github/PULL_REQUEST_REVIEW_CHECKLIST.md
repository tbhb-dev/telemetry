# Review checklist

Reviewers check these on top of the global checklist. Each item comes from a
defect that reached review in this repository.

## Endpoint and export

- The default endpoint stays loopback at port 4318. Nothing discovers an
  endpoint or reaches off-host without the caller setting
  `OTEL_EXPORTER_OTLP_ENDPOINT`.
- A non-loopback host exports only when `TBHB_TELEMETRY_ALLOW_HOSTS` names it
  exactly. A refused host writes an `endpoint` drop record and gets no DNS
  lookup.
- Documented undo steps are run, not read. Unsetting only the allowlist while an
  off-host endpoint stays set drops spans, so the undo is to unset both.
- Export stays bounded at 250 ms, fails silently and never changes a command's
  exit code or output. Check Ctrl-C during the export wait and a malformed
  `OTEL_*` variable.
- `OTEL_SDK_DISABLED=true` still stops all export and drop writes.

## Attributes and records

- Attribute names stay under `tbhb.agent.*` and `tbhb.run.id`. Values are
  strings of at most 128 characters, and outcome is `ok`, `refused` or `failed`.
- Drop records and the locked counter keep a size bound.

## Release and CI

- Each workflow job has the narrowest `permissions`. Only `release` gets
  `contents: write`, and only on pushes to `main` in this repository.
- Every action is pinned by full commit sha, and downloaded tools by checksum.
- The smoke test installs the built wheel into a fresh environment and proves a
  real export, not just an import.
- A rerun of the release job for the same sha neither duplicates nor silently
  accepts a release with different bytes.
- The wheel's name, URL pattern and hash sidecar still match what consumers pin.

## Public repository

- No secret, host name, private repository name, internal path, network name or
  address appears in any file, commit or PR text.
- A change that touches `.github/actionlint.yaml` stays a byte copy of the rules
  baseline.
