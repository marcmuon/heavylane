#!/bin/sh
# Generic release checks; private audit terms belong in an external policy file.
set -eu
cd "$(git rev-parse --show-toplevel)"
python3 - "$@" <<'PYCODE'
import argparse
import base64
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys

parser = argparse.ArgumentParser(description="Scan release files and decoded base64 text for private data")
parser.add_argument("--history", action="store_true", help="scan every commit reachable from any ref")
parser.add_argument("--patterns-file", default=os.environ.get("HEAVYLANE_SCRUB_PATTERNS_FILE"),
                    help="external JSON policy with terms and optional exact-line allow exceptions")
args = parser.parse_args()
policy = {"terms": [], "allow": {}}
if args.patterns_file:
    try:
        policy = json.loads(Path(args.patterns_file).read_text())
        if not isinstance(policy, dict) or not isinstance(policy.get("terms"), list):
            raise ValueError("terms must be a list")
        if not all(isinstance(term, str) and term for term in policy["terms"]):
            raise ValueError("terms must contain nonempty strings")
        if not isinstance(policy.get("allow", {}), dict):
            raise ValueError("allow must be an object")
        if not all(isinstance(path, str) and isinstance(lines, list) and
                   all(isinstance(line, str) for line in lines)
                   for path, lines in policy.get("allow", {}).items()):
            raise ValueError("allow entries must contain lists of exact lines")
    except (OSError, ValueError) as error:
        sys.exit("cannot read audit policy: %s" % error)
term_pattern = re.compile("|".join(re.escape(term) for term in policy["terms"]), re.IGNORECASE) if policy["terms"] else None
home_pattern = re.compile(r"/(?:Users|home)/([^/\s\"'<>;]+)")
ip_pattern = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
encoded_pattern = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{2,}={0,2}(?![A-Za-z0-9+/=])")
network_pattern = re.compile(r"\b[\w.-]+\.ts\.net\b", re.IGNORECASE)
example_ips = {
    "examples/hosts.example.json": {"192.168.1.50"},
    "tests/test_heavylane.py": {"100.64.1.2", "192.168.1.50", "192.168.1.51", "192.168.1.52"},
    "scripts/scrub_check.sh": {"100.64.0.0", "100.64.1.2", "192.168.1.50", "192.168.1.51", "192.168.1.52"},
}


def git(*arguments):
    return subprocess.run(["git"] + list(arguments), check=True, capture_output=True).stdout


def reasons(path, line):
    found = set()
    if line in policy.get("allow", {}).get(path, []):
        return found
    if term_pattern and term_pattern.search(line):
        found.add("private audit term")
    if any(match.group(1) not in {"example", "you", "USER"} for match in home_pattern.finditer(line)):
        found.add("personal home path")
    if network_pattern.search(line):
        found.add("private network hostname")
    for match in ip_pattern.finditer(line):
        try:
            address = ipaddress.ip_address(match.group())
        except ValueError:
            continue
        if (address.is_private or address in ipaddress.ip_network("100.64.0.0/10")) and not address.is_loopback:
            if str(address) not in example_ips.get(path, set()):
                found.add("private address")
    return found


def line_reasons(path, line):
    found = reasons(path, line)
    for match in encoded_pattern.finditer(line):
        try:
            raw = base64.b64decode(match.group(), validate=True)
            if base64.b64encode(raw).decode("ascii") != match.group():
                continue
            decoded = raw.decode("utf-8")
        except (ValueError, UnicodeError):
            continue
        for decoded_line in decoded.splitlines():
            found.update("encoded " + reason for reason in reasons(path, decoded_line))
    return found


def scan(path, content):
    hits = []
    location = "[redacted path]" if line_reasons("release path", path) else path
    for number, line in enumerate(content.splitlines(), 1):
        found = line_reasons(path, line)
        if found:
            # Print locations and categories, never private matching text.
            hits.append("%s:%d: %s" % (location, number, ", ".join(sorted(found))))
    return hits


def scan_path(name):
    found = scan("release path", name)
    if name == "hosts.json" or name.startswith(("projects/", ".planning/", ".omc/")):
        found.append("release path: private configuration/state file")
    return found


hits = []
if args.history:
    commits = git("rev-list", "--all").splitlines()
    for commit in commits:
        revision = commit.decode("ascii")
        message = git("show", "-s", "--format=%B", revision).decode("utf-8", "replace")
        hits.extend(scan("commit " + revision, message))
        names = git("diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "-m", "-z", revision).split(b"\0")
        for name in sorted(set(name.decode("utf-8", "replace") for name in names if name)):
            hits.extend("%s %s" % (revision[:12], hit) for hit in scan_path(name))
        patch = git("show", "--format=", "--root", "--diff-merges=first-parent", revision).decode("utf-8", "replace")
        path = None
        for line in patch.splitlines():
            if line.startswith("diff --git "):
                path = line.rsplit(" b/", 1)[-1]
            if path and line[:1] in "+- " and not line.startswith(("+++", "---")):
                hits.extend("%s %s" % (revision[:12], hit) for hit in scan(path, line[1:]))
    label, count = "history", len(commits)
else:
    paths = sorted(set(path.decode("utf-8") for path in git("ls-files", "-co", "--exclude-standard", "-z").split(b"\0") if path))
    for name in paths:
        path = Path(name)
        hits.extend(scan_path(name))
        if path.is_symlink():
            hits.extend(scan(name, str(path.readlink())))
        elif path.is_file():
            hits.extend(scan(name, path.read_bytes().decode("utf-8", "replace")))
    label, count = "tree", len(paths)
if hits:
    print("\n".join(hits))
    print("scrub %s: FAIL (%d hits)" % (label, len(hits)))
    sys.exit(1)
print("scrub %s: PASS (%d %s; %d external terms)" %
      (label, count, "commits" if label == "history" else "files", len(policy["terms"])))
PYCODE
