"""Compare two JSON result files from a local and a remote run of the same snapshot.

python3 tests/compare_json.py LOCAL.json REMOTE.json [--rtol 1e-9] [--atol 1e-12] [--ignore KEY ...]

Exact match is required for strings, ints, bools and structure; floats may differ within
rtol/atol (BLAS results can change in the last bits across CPUs and macOS versions).
Exit 0 when equivalent, 1 otherwise. Prints sha256 equality and the worst float gap.
"""
import argparse
import hashlib
import json
import math
import sys


def walk(a, b, path, o, out):
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k in o.ignore:
                continue
            if k not in a or k not in b:
                out["diffs"].append("%s/%s: present on one side only" % (path, k))
            else:
                walk(a[k], b[k], "%s/%s" % (path, k), o, out)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out["diffs"].append("%s: length %d vs %d" % (path, len(a), len(b)))
        for i, (x, y) in enumerate(zip(a, b)):
            walk(x, y, "%s[%d]" % (path, i), o, out)
    elif isinstance(a, float) or isinstance(b, float):
        if (type(a) not in (int, float) or type(b) not in (int, float)):
            out["diffs"].append("%s: numeric type mismatch %r vs %r" % (path, a, b))
            return
        out["floats"] += 1
        if not math.isfinite(a) or not math.isfinite(b):
            if not (a == b or (math.isnan(a) and math.isnan(b))):
                out["diffs"].append("%s: nonfinite mismatch %r vs %r" % (path, a, b))
            return
        gap = abs(a - b)
        out["max_abs"] = max(out["max_abs"], gap)
        if gap > o.atol + o.rtol * max(abs(a), abs(b)):
            out["diffs"].append("%s: %r vs %r (|d|=%.3g)" % (path, a, b, gap))
    elif type(a) is not type(b) or a != b:
        out["diffs"].append("%s: %r vs %r" % (path, a, b))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("local")
    p.add_argument("remote")
    p.add_argument("--rtol", type=float, default=1e-9)
    p.add_argument("--atol", type=float, default=1e-12)
    p.add_argument("--ignore", nargs="*", default=[], help="keys to skip anywhere (timings, uuids)")
    o = p.parse_args()
    raw = [open(f, "rb").read() for f in (o.local, o.remote)]
    same = hashlib.sha256(raw[0]).digest() == hashlib.sha256(raw[1]).digest()
    out = {"diffs": [], "floats": 0, "max_abs": 0.0}
    walk(json.loads(raw[0]), json.loads(raw[1]), "", o, out)
    print("byte-identical: %s | floats compared: %d | max |diff|: %.3g | mismatches: %d" % (
        same, out["floats"], out["max_abs"], len(out["diffs"])))
    for d in out["diffs"][:30]:
        print("  " + d)
    return 0 if not out["diffs"] else 1


if __name__ == "__main__":
    sys.exit(main())
