#!/usr/bin/env python3
"""
Same 1M expiring keys (out of 4.15M), once on an idle server and once while one
client sends GETs nonstop. Shows how much the fast expire cycle (run from
beforeSleep on every event-loop pass) adds on top of the 25% slow cycle.

Usage: .venv/bin/python experiments/expiry_idle_vs_busy.py results/<run_id>
Writes <run>/expiry_idle_vs_busy.csv. Runs FLUSHALL.
"""
import csv
import multiprocessing as mp
import sys
import time
from pathlib import Path

import redis

LUA = ("for i=tonumber(ARGV[1]),tonumber(ARGV[2])-1 do "
       "redis.call('PEXPIREAT','key:'..i,ARGV[3]) end return 1")


def busy(stop):
    c = redis.Redis()
    while not stop.is_set():
        c.get("key:1")


def run(label, with_load, rows):
    r = redis.Redis()
    r.flushall()
    r.execute_command("DEBUG", "POPULATE", 4_150_000, "key", 16)
    dl = int(time.time() * 1000) + 5000
    for s in range(3_150_000, 4_150_000, 100_000):
        r.eval(LUA, 0, s, s + 100_000, dl)
    stop, p = mp.Event(), None
    if with_load:
        p = mp.Process(target=busy, args=(stop,))
        p.start()
    while time.time() * 1000 < dl - 1000:
        time.sleep(0.01)
    while True:
        e = r.info("keyspace")["db0"].get("expires", 0)
        t = time.time() - dl / 1000
        rows.append({"mode": label, "t_s": round(t, 3), "expiring_left": e})
        if e == 0 and t > 0:
            break
        time.sleep(0.05)
    stop.set()
    if p:
        p.join()
    print(f"{label}: all expired after {t:.2f}s (~{1e6 / t:,.0f} keys/s)")


if __name__ == "__main__":
    out = Path(sys.argv[1])
    rows = []
    run("idle server", False, rows)
    run("one busy client", True, rows)
    redis.Redis().flushall()
    with (out / "expiry_idle_vs_busy.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
