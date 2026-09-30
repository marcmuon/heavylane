# Security policy

Version 0.1.x is supported for security reports. This tool executes arbitrary
code as the SSH account; it is not an isolation boundary. Use a dedicated
account and trusted code. Read the README Security section before shipping
inputs, git history or results.

Report vulnerabilities privately through the repository's GitHub Security
“Report a vulnerability” action when available. If that action is unavailable,
contact the repository owner through their GitHub profile and request a private
reporting channel before sharing details. Keep secrets, addresses, home paths,
job logs and proprietary inputs out of public issues. A minimal synthetic
reproduction, version, platform and expected/observed behavior are useful.

The name-based secret filter is best effort. Audit untracked files and git
history yourself. The repo-config trust gate covers configuration capabilities;
it does not restrict what a submitted program can do. External input copies
can overwrite data, and fetched paths can read anything accessible to the host
account. Job cleanup does not remove caches or absolute external copies.
