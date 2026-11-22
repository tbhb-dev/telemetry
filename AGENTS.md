# Agent instructions

This public repository holds shared telemetry libraries for tbhb-dev.

- Never write secrets, host names, private repository names, internal paths, network names or IP addresses in files, commits, or pull requests.
- Create a branch before making changes. Changes land through pull requests into `main`.
- Use Conventional Commits without `Signed-off-by` or `Co-authored-by` trailers.
- Keep the Apache-2.0 license and use `Apache-2.0` in SPDX identifiers.
- Fill every section of `.github/PULL_REQUEST_TEMPLATE.md`.
- Run `mise install`, `bazelisk test //...` and `bazelisk run //:format`.
