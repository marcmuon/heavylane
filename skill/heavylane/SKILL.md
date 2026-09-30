---
name: heavylane
description: Offload heavy simulations, sweeps, backtests and multi-worker jobs with heavylane. Use before a heavy run, on submission errors, to check or fetch a job, or to onboard a project for remote execution.
---

# heavylane

Keep agents, editors and repos on the laptop. Run heavy work on the execution
host through one host-wide lock shared by every session using the configured
root. Project policy decides what is heavy; small, targeted checks stay local.

## Run a job

1. From the intended working directory, run
   `heavylane submit --dry-run -- <command...>`. Read config `notes`, inputs,
   exclusions, environment, trust status and current guards. User config lives
   at `~/.config/heavylane/hosts.json` and `~/.config/heavylane/projects/`.
   If config is NONE and the project needs extra inputs or worker rules, follow
   [ONBOARDING.md](ONBOARDING.md). For missing tools/SSH, use `heavylane doctor`.
2. Submit the exact command:
   `heavylane submit --label "<purpose>" -- <command...>`.
   Stdout gives the job id. A repo-local `.heavylane.json` has limited trust:
   external inputs, setup, env and absolute fetch are ignored unless the user
   approved the repo via `trusted_roots` or you deliberately give
   `--trust-repo-config` after inspecting it. Explicit CLI overrides are trusted.
3. Read the submit exit code:
   - **0:** confirmed running/queued, or successfully finished.
   - **75:** busy. Re-submit with `--queue` or retry later remotely.
   - **69:** unreachable. Retry later or tell the user.
   - **70/78:** tool, transfer or config failure. Correct the named problem.
     If launch is uncertain, check the named job before retrying.
   Keep heavy execution on the host after any error; never replace it with a
   laptop run. `--queue` waits for the lock without a FIFO guarantee.
4. `heavylane wait <job>` returns the job's exit code. Set `--timeout` if needed;
   a wait timeout leaves the remote job running. `heavylane logs <job>` reads
   output; `heavylane status <job> --json` gives detailed measurements.
5. `heavylane fetch <job>` copies logs/results to
   `~/.local/share/heavylane/results/<project>/<job>/`. Extra paths follow the id.
   Use `--dest` only outside checkouts. Fetch refuses running jobs unless
   `--partial`. Read results there and follow project rules before copying them
   back. Append-only outputs require deliberate reconciliation.
6. Report job id, host, command, elapsed time, peak process-tree RSS and exit code.

## Execution details

- `heavylane host` or a submit dry-run shows current guards. Start with the
  project's worker setting; increase only after measurements show headroom.
- Uncommitted work and untracked non-ignored files ship. Name filtering is best
  effort, so inspect the snapshot. `--git` also ships unsanitized commit history.
- `data` inputs are workdir-relative; `external` uses the same absolute home paths
  on both machines and persists. Credential/shell/git dotfile destinations are
  refused. Honor data licensing before copying.
- A sleeping macOS host can wake on the same LAN with network wake enabled on AC
  and the lid open or a display attached. Sandboxed sessions need SSH network
  access. Run `heavylane doctor` for prerequisites.
- `heavylane cancel <job>` stops that job's tree. `clean --days N` removes
  finished job directories; caches and external copies persist.
- Use the configured execution root consistently across sessions. Different
  roots/tools use different locks and must not execute concurrently on one host.
