# heavylane

Send heavy jobs from coding agents to a spare Mac over SSH, one at a time. Keep the agents, editor and repos on the laptop; bring back logs, memory measurements and results.

Many agents plus one laptop means swap. A rule in a prompt does not stop ten agents from each starting a heavy job. heavylane puts a kernel `flock` on the execution host, shared by every session using the same root. A busy host returns **75**, an unreachable host **69**, with an explicit instruction to keep the job remote.

## Quick start

Prerequisites: **Python 3.9+, git 2.31+, rsync and ssh on the laptop**; **macOS, Python 3.9+, tmux, uv, rsync and caffeinate on the execution host**. Configure an SSH alias with key authentication that works with `BatchMode=yes`. Use the same username and home path on both machines for `external` inputs. `doctor` checks tools and SSH, and reports the AC network-wake setting.

```sh
git clone https://github.com/YOUR-USER/heavylane.git
cd heavylane
./install.sh
export PATH="$HOME/.local/bin:$PATH"
mkdir -p ~/.config/heavylane/projects
cp examples/hosts.example.json ~/.config/heavylane/hosts.json
# Edit hosts.json: buildbox must be your SSH alias; replace the placeholder address and limits.
heavylane doctor
cd ~/Projects/example-sim
heavylane submit --dry-run -- echo hi
heavylane submit --label "sweep" -- uv run python run_sweep.py --workers 1
heavylane wait <job>
heavylane fetch <job>
```

The installer puts the CLI in `~/.local/bin/heavylane` and the skill in `~/.agents/skills/heavylane`. `./install.sh --claude` also refreshes the optional Claude skill link. See `./install.sh --help` for other optional integration flags. Installation does not overwrite user config.

## Commands

Global flags precede the command: `heavylane --host buildbox host`. `heavylane --help` and each command's `--help` give all options.

| Command | Purpose |
|---|---|
| `heavylane submit [options] -- <command...>` | Snapshot, sync inputs and start a detached job; stdout is its id. `--queue` waits on the host; `--wait` waits locally; `--dry-run` takes no remote action |
| `heavylane wait <job> [--timeout N] [--interval N]` | Wait and return the job's exit code |
| `heavylane status <job> [--json]` | State, elapsed time and peak process-tree memory |
| `heavylane logs <job> [-n N] [--setup] [-f]` | Read or follow output |
| `heavylane fetch <job> [paths...] [--dest DIR] [--partial]` | Fetch logs and workdir-relative or absolute host outputs |
| `heavylane cancel <job>` | Cancel a queued or running job and its process tree |
| `heavylane host [--json]` | Lock, active jobs, memory, disk, power and configured guards |
| `heavylane doctor` | Check prerequisites, key authentication and AC network wake |
| `heavylane list [-n N]` | Recent jobs |
| `heavylane clean [--days N] [jobs...]` | Remove finished job directories |
| `heavylane snapshot -o FILE [--extract DIR]` | Save the exact snapshot for a local reference run |
| `heavylane --version` | CLI version |

| Exit | Meaning |
|---|---|
| 0 | Submit confirmed running/queued or completed successfully; wait succeeded |
| 2 | Invalid command, job id or checkout |
| 75 | Busy; the job did not run. Queue or retry |
| 69 | SSH unavailable or host did not wake |
| 70 | Tool/transfer failure; a launched job may still exist. Check its status before retrying |
| 78 | Invalid config, inaccessible input or missing prerequisite |
| 124 | Wait timed out; the remote job continues |
| 130 / 137 | Cancelled / killed by the memory guard |
| Other | The command's failure code; negative signals become 128 + signal number |

Keep heavy execution remote after any submit error. When a launch is uncertain, inspect the named job before retrying.

## How a job runs

1. Build a snapshot from `git ls-files -co --exclude-standard`: tracked files and untracked files that are not ignored, with uncommitted edits included and deleted files omitted. Credential-like names, agent state, virtual environments, configured excludes and separately synced data are omitted. The SHA256 manifest records what shipped and what was excluded. `--git` also sends HEAD history.
2. Connect over SSH. If needed, knock port 22 on LAN targets in order: the matching Tailscale peer's LAN address, the cached address reported by the host, the configured `wake_lan_ip`, then optional `wake_lan_host`. Duplicates are removed. The host reports its active interface first, with `en0` as a fallback. A short caffeinate lease covers transfers.
3. Probe the root's host-wide lock; return 75 before upload if busy, unless `--queue`. Sync `data` and `external` into a per-project cache under a separate cache lock. Losing that lease aborts the transfer; an active cache operation retains its lock until its child processes finish. Each job gets isolated copy-on-write clones on APFS. External inputs are placed at their absolute destinations only after the job takes the host lock.
4. A detached tmux session runs a versioned copy of `rr_job.py`. It takes `~/heavylane/host.lock`, runs setup and the command, and holds caffeinate. The command inherits the lock descriptor, so it retains the gate if the wrapper dies. Queued jobs block on that lock; FIFO ordering is not promised.
5. Rebuild the environment: `uv sync --frozen` for `uv.lock`, otherwise a pip venv for `requirements.txt`, or an explicit `setup`. uv projects pin the laptop's Python patch version; the host may need network access to download that interpreter and dependencies. Commands run from the submitting subdirectory. The process-tree RSS is sampled every two seconds, with periodic host swap/free-memory checks. Configured thresholds kill the tree. A failed process sample gets one retry after 0.2 seconds, with a five-second timeout per attempt; persistent failure stops the main process group and records cleanup uncertainty. Separately sessioned descendants may survive when enumeration is unavailable, so ordinary fetch refuses that status. Leftover process-group members are killed on completion. A missing wrapper is reported as `lost`.
6. Fetch results to `~/.local/share/heavylane/results/<project>/<job>/`. Fetch requires a confirmed stopped job with no cleanup error. Active, lost or unknown states require `--partial`, which prints a warning. Keep custom `--dest` paths outside working checkouts. Review artifacts before bringing them into a repo.

The helper is reinstalled when its hash changes or its version differs; a version mismatch prints a message and triggers one reinstall/check. `status.json` records version `0.1.0`.

## Configuration

User-owned config lives outside the checkout:

- Hosts: `~/.config/heavylane/hosts.json`, overridden by `HEAVYLANE_HOSTS_FILE`.
- Projects: `~/.config/heavylane/projects/`, overridden by `HEAVYLANE_PROJECTS_DIR`.
- Host selection: global `--host`, a project's `host`, `HEAVYLANE_HOST`, then the hosts file's `default`.

See [hosts.example.json](examples/hosts.example.json), [project.example.json](examples/project.example.json) and [dot-heavylane.json](examples/dot-heavylane.json). Adapt the input paths before copying a project example to `~/.config/heavylane/projects/example-sim.json`. A repo-local file is named `.heavylane.json`.

Project lookup order:

1. `--config FILE` (explicitly trusted by that invocation).
2. Nearest `.heavylane.json` upward from cwd to the git root; its directory is the default workdir.
3. User project configs whose `roots` contain cwd, using the longest match.
4. User `projects/<main-repo-name>.json`; worktrees share the main repo name.

**Trust gate:** a repo-local config's `external`, `setup`, `env` and absolute `fetch` paths are ignored unless `submit --trust-repo-config` is passed or the exact git repo root appears in `trusted_roots` in the user hosts file or a user project config. A repo cannot trust itself. `snapshot` also accepts that flag. Dry-run reports `repo_config_trusted`, ignored capabilities, inputs, environment, notes and effective guards. Inspect these before granting trust. Trust does not bypass external destination protection.

| Project key | Meaning |
|---|---|
| `project` | Lowercase cache/project name |
| `data` | Existing workdir-relative inputs; cached and cloned |
| `external` | Existing absolute inputs under the laptop's HOME; copied to the same host paths |
| `exclude` | Repo-root-relative trims or globs |
| `env` | Environment variables; pin worker counts here |
| `fetch` | Default output paths, relative to workdir or absolute on the host |
| `workdir` | Existing project subdirectory |
| `snapshot: "workdir"` | Ship only that subdirectory |
| `git` | Include HEAD's history, as with `--git` |
| `setup` | Environment setup command |
| `roots` | User config selection by cwd |
| `trusted_roots` | Exact repo roots approved in user-owned config |
| `notes` | Project rules for agents, such as output reconciliation |
| `mem_limit_gb` | Process-tree resident-memory cap |
| `max_snapshot_mb` | Snapshot size cap (default 500 MB) |
| `host` | Configured host name |

Submit flags can add `--data`, `--external`, `--env K=V`, `--setup`, `--git`, and `--mem-limit-gb`. `--no-config-inputs` removes config data/external lists. `--snapshot FILE` reuses a saved snapshot.

Hosts have `ssh`, a HOME-relative `root` (default `heavylane`), `python`, `path_prefix`, wake addresses, `wake_timeout_s`, `lease_seconds`, `grace_seconds`, `mem_limit_gb`, `swap_growth_kill_gb`, `swap_growth_free_pct` and `min_free_pct`. Missing memory thresholds disable the corresponding guard; set limits appropriate to the host. Run `heavylane host` or a submit dry-run to see them. Every session must use the same host/root for the gate to work. Different roots mean different locks: use only one execution tool/root on a host at once.

The three host pressure guards (`swap_growth_kill_gb`, `swap_growth_free_pct`, `min_free_pct`) always come from the host config. Project overrides are ignored with a message. The process-tree cap `mem_limit_gb` can still come from the project or `--mem-limit-gb`. Dry-run and real submission use the same effective limits.

Known fields are type-checked before SSH and again before the host takes its job lock. Inputs and fetch paths are lists of strings; environment values are strings; numeric limits must be finite and nonnegative. Invalid config returns 78 with the field name. Cancellation returns success only after the host confirms a stopped state; host errors and uncertain states return failure.

## Security

heavylane runs arbitrary command and setup strings through `bash -c` as the SSH user. It is an execution gate, not a sandbox. Use a dedicated host account and run code you trust. Explicit CLI overrides are trusted user instructions. The repo-config gate prevents automatic use of sensitive configuration capabilities; it cannot make untrusted program code safe.

Untracked files that are not gitignored are shipped. The case-insensitive deny list is a best-effort name filter, **not a secret scanner**. Inspect the dry-run exclusions and snapshot, and use a real secret scanner when appropriate. `--git` ships commit history, which is not scrubbed by the snapshot filter and may contain deleted credentials. Snapshot metadata includes local paths, hostname/job metadata and git status; treat fetched logs and metadata as private.

The CLI imports the credential policy from the standalone host helper. External destinations also have a separate login-file guard, so snapshot filtering and host login protection share one credential list without removing the additional host checks.

`external` can overwrite data at absolute host paths. The laptop checks followed input targets and refuses credential/cloud locations; data inputs and regular snapshot reads must stay inside the repo and pass the name filter. The host independently refuses protected names, shell/git dotfiles and history files under HOME, including nested targets and symlink-resolved destinations. External copies persist. `fetch` can pull any path the host account can read, including absolute paths; only fetch permitted outputs.

Job directories remain until `clean`. Cached inputs and external copies remain even after cleaning jobs and require deliberate manual removal. Logs/results can contain sensitive data. `data` and `external` copy datasets to a second machine: check vendor/device licensing first. See [SECURITY.md](SECURITY.md) for reporting guidance.

## Limits

The supported execution host **must be macOS**. Linux code paths exist but are experimental and untested; the Ubuntu CI job tests local code, not Linux remote execution. Non-APFS copies may consume full disk space. This release is designed for one host and one job at a time.

Wake needs the same LAN, “Wake for network access” enabled on AC power, and the lid open or an external display. Otherwise keep the host awake. Wake uses a TCP knock on port 22, not a magic packet; an optional hostname depends on LAN name resolution. The CLI never starts a local replacement job. The agent's no-local-fallback policy still matters: another command/tool can bypass this gate.

## Agent integration

[docs/agent-policy.md](docs/agent-policy.md) is the short block to paste into AGENTS.md or CLAUDE.md. [skill/heavylane/SKILL.md](skill/heavylane/SKILL.md) covers submit, wait, fetch and reporting. [ONBOARDING.md](skill/heavylane/ONBOARDING.md) covers project inputs and a small seeded comparison. The policy guides agents; the host lock enforces concurrency for submitted jobs.

## Validation

The original implementation was validated on 2026-09-25 using the same snapshots and fixed seeds on a laptop and a spare Mac:

| Workload | Laptop | Host | Result |
|---|---|---|---|
| Causal report | 248 s | 108 s | Byte-identical reports |
| A 14 GB equity-backtest loader | Laptop swapped | 602 s, 14.0 GB peak, no swap | Byte-identical to the earlier laptop report |
| Synthetic statistics self-test | 66 s | 47 s | 16,585 floats within 2.8e-14; discrete fields exact |
| Seeded model run | 16 s | 20 s | Byte-identical; matched reference |
| Data harness with git metadata | 61 s | 27 s | Byte-identical; matched reference |

These are prior workload measurements, not new release benchmarks. `tests/compare_json.py` compares outputs; `tests/alloc_tree.py` exercises process-tree memory. Run `python3 -m unittest -v` for the public behavioral suite. CI runs it and the privacy gate on macOS/Ubuntu with Python 3.9/3.12.

Private repositories can also manually dispatch `.github/workflows/ci-self-hosted.yml` on `main`. It requires a macOS ARM64 runner labeled `heavylane-ci`, with `python3.9`, `python3.12`, git and rsync on PATH. Prefer a dedicated, temporary runner; this workflow runs trusted private code only and skips public repositories. Its results cover macOS; the hosted Ubuntu jobs provide separate Linux coverage.

`scripts/scrub_check.sh` checks generic privacy patterns in release files, including decoded base64 text. `--history` also checks all reachable commits. For a private-source release audit, supply an external JSON file with `terms` (strings) and optional `allow` (paths mapped to exact permitted lines): `scripts/scrub_check.sh --patterns-file /absolute/private-audit.json`. Repeat with `--history`. Keep that policy outside the repository; `HEAVYLANE_SCRUB_PATTERNS_FILE` can also select it. A generic CI check alone cannot establish that all organization-specific names have been removed.

## Related tools

[SkyPilot SSH node pools](https://docs.skypilot.ai/en/stable/reservations/existing-machines.html) require Debian-based Linux hosts. [dstack SSH fleets](https://dstack.ai/docs/concepts/fleets/) require Linux and Docker. [GNU parallel](https://www.gnu.org/software/parallel/man.html)/[sem](https://www.gnu.org/software/parallel/sem.html), [pueue](https://github.com/Nukesor/pueue) and [nq](https://github.com/leahneukirchen/nq) are useful transfer, semaphore and queue building blocks. [imbue offload](https://github.com/imbue-ai/offload) fans test runs out to cloud sandboxes. heavylane combines a working-tree snapshot, one remote gate, wake and process-tree/host memory guards for a spare Mac.

## Layout

`bin/heavylane` is the laptop CLI; `lib/rr_job.py` is the standalone host helper. Both use only Python's standard library. On the host, `~/heavylane/` contains `host.lock`, `lock-holder.json`, `bin/`, `cache/<project>/` and `jobs/<id>/` with source, metadata, logs, memory samples, status and exit code. Local state lives in `~/.local/share/heavylane/`.

MIT licensed. See [CHANGELOG.md](CHANGELOG.md).
