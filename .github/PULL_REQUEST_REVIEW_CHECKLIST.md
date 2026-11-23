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
- Export stays bounded at 250 ms, fails silently and aims never to change a
  command's exit code or output. A malformed endpoint passes that check. Known
  open: Ctrl-C during the export wait replaces the exit status and prints a
  traceback. A change must not widen that gap.
- `OTEL_SDK_DISABLED=true` still stops all export and drop writes.

## Attributes and records

- `set_attributes` accepts only the `tbhb.agent.*` keys and `tbhb.run.id`.
  Values are strings of at most 128 characters, and outcome is `ok`, `refused`
  or `failed`. Spans also carry built-in attributes that this limit doesn't
  cover.
- Known open: `drops.jsonl` has no file size limit, so repeated refused exports
  grow it. A change must not add new unbounded writes.

## Release and CI

- Each workflow job has the narrowest `permissions`. Only `release` gets
  `contents: write`, and only on pushes to `main` in this repository.
- Every action is pinned by full commit sha, and downloaded tools by checksum.
- The smoke test installs the built wheel into a fresh environment and proves a
  real export, not just an import.
- A rerun of the release job for the same sha doesn't duplicate the release.
  A rerun of an already published release compares the tag's commit and the
  wheel's bytes with the sidecar and the rebuilt wheel. Keep those checks.
- The wheel's name, URL pattern and hash sidecar still match the README
  contract that consumers pin.
- Known open: the CI setup action can download Bazelisk without a checksum
  check (security debt).

## Public repository

- No secret, host name, private repository name, internal path, network name or
  address appears in any file, commit or PR text.
- `.github/actionlint.yaml` should be a byte copy of the rules baseline at the
  commit pinned in `MODULE.bazel`. Known open: until the baseline-copy change
  merges, the checked-in file has `labels: []` and the baseline lists the
  `ubuntu-26.04` and `ubuntu-26.04-arm` labels. Compare against the baseline,
  not against the current file.
