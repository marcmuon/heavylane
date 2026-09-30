# Onboard a project

Done means a small seeded local/remote run matched from one snapshot, the
user-owned config records project rules, and project policy points at the skill.

1. Find the heavy entry point and its worker control. Read launch scripts and
   identify process pools or worker flags. Pin the intended count in `env`, or
   record a required command flag in `notes`.
2. Identify inputs git will not ship. Use `data` for existing workdir-relative
   files; use `external` for absolute paths under HOME, with the same username
   and home path on both machines. External copies persist and overwrite data.
   Include only required inputs that may be licensed for the second machine.
   Credential, cloud and login configuration paths are refused.
3. Check whether the program needs git (`git: true` or `--git`) and where it
   writes outputs (`fetch`). Record output merge/reconciliation rules in `notes`.
   History shipped with `--git` needs its own secret audit.
4. Adapt `examples/project.example.json` from the heavylane checkout into
   `~/.config/heavylane/projects/<repo>.json`. Configure `workdir` for a
   subdirectory project, `snapshot: "workdir"` to limit scope, and `roots` for
   central selection. Inspect a repo-local `.heavylane.json` before granting
   trust; user-owned `trusted_roots` contains exact git roots.
5. Run `heavylane submit --dry-run -- <small command>`. Verify every shipped
   input, excluded path, worker setting, result path and guard. Fix config until
   the dry-run has the intended values. `heavylane doctor` verifies tools/SSH.
6. Compare one small seeded run from identical bytes:
   ```sh
   heavylane snapshot -o /tmp/heavylane-val.tgz --extract /tmp/heavylane-val
   # Run only the agreed small reference locally in /tmp/heavylane-val/src/<workdir>.
   heavylane submit --snapshot /tmp/heavylane-val.tgz --wait -- <small command>
   heavylane fetch <job>
   ```
   Byte-identical artifacts match by SHA256. For BLAS floats, run
   `python3 <heavylane-checkout>/tests/compare_json.py LOCAL.json REMOTE.json`.
   Counts, hashes, booleans and structure must match exactly. Check both runs
   succeeded; a shared failure is not validation.
7. With the project owner's authorization, add the block from
   `docs/agent-policy.md` to AGENTS.md or CLAUDE.md. Record heavy entry points,
   worker rules and artifact reconciliation. Other agents may own that file.
