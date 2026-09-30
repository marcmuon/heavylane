# Agent policy template

Paste this block into your AGENTS.md or CLAUDE.md and adapt the heavy commands
to the project. Keep project inputs and worker rules in user-owned config.

```markdown
## Heavy runs

Run full sweeps, multi-worker jobs, and work expected to exceed roughly 2 GB RAM
or five minutes on the execution host with the heavylane skill:
`heavylane submit --label "<purpose>" -- <command...>`.
Check `heavylane host` or `submit --dry-run` for current limits and inputs.
On 75, queue or retry remotely; on 69, report the unavailable host to the user.
Keep heavy jobs remote after any error. If a job may have launched, check its
status before retrying. Wait, fetch outside the checkout, and report the job id,
command, elapsed time, peak process-tree memory and exit code. Review results
before copying artifacts into the repo.
```
