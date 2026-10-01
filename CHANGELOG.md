# Changelog

## Unreleased

- `swap_growth_free_pct: 0` disables the swap-growth guard instead of silently
  reverting to the 25% default.
- A submit that fails after the snapshot upload removes its half-built job
  directory on the host; before, nothing could list or clean it.

## 0.1.0 — 2026-09-29

- Initial release with fresh history and example-only config.
- User config outside the checkout, explicit repo-config trust and protected
  external destinations on the host.
- Host-wide job lock, detached execution, secret-filtered snapshots, result
  fetching, process-tree/host memory guards and LAN wake.
- Prerequisite doctor, readable missing-tool errors, safe PATH and shell quoting.
- Versioned host status and one reinstall on CLI/helper version mismatch.
- Denied/resolved input checks, explicit process-telemetry failures, truthful
  terminal submission exit codes and strict nonfinite/discrete comparisons.
- Agent skill/policy, optional installer integrations, behavioral tests and
  macOS/Ubuntu CI for Python 3.9 and 3.12.

Supported execution hosts are macOS. Linux execution remains experimental.
