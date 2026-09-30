"""Smoke fixture for heavylane: a parent plus N spawned children, each holding MB of RAM.

python3 tests/alloc_tree.py --children 2 --mb 400 --seconds 20
Expected peak tree RSS is roughly (children + 1) * mb.
"""
import argparse
import multiprocessing as mp
import time


def hold(mb, seconds):
    block = bytearray(mb * 2**20)
    for i in range(0, len(block), 4096):  # touch every page so it is resident
        block[i] = 1
    time.sleep(seconds)
    return len(block)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--children", type=int, default=2)
    p.add_argument("--mb", type=int, default=400)
    p.add_argument("--seconds", type=float, default=20)
    a = p.parse_args()
    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=hold, args=(a.mb, a.seconds)) for _ in range(a.children)]
    for pr in procs:
        pr.start()
    hold(a.mb, a.seconds)
    for pr in procs:
        pr.join()
    print("alloc_tree done: %d processes x %d MB" % (a.children + 1, a.mb))
