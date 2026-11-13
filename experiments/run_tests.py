#!/usr/bin/env python3
"""
Redis key-retrieval experiments.

Each experiment writes raw measurements to results/<run_id>/<experiment>.csv
plus a meta.json describing the run. Analysis lives in analysis/analyze.py.

Usage:
    .venv/bin/python experiments/run_tests.py                 # all experiments, default sizes
    .venv/bin/python experiments/run_tests.py --only scaling encoding
    .venv/bin/python experiments/run_tests.py --quick         # smaller sizes, fast smoke run

WARNING: every experiment runs FLUSHALL. Point this only at a throwaway server.
"""
import argparse
import csv
import json
import platform
import random
import statistics
import multiprocessing as mp
import time
from datetime import datetime
from pathlib import Path

import redis

ROOT = Path(__file__).resolve().parent.parent
now_ns = time.perf_counter_ns


# ---------------------------------------------------------------- helpers

def write_csv(path: Path, rows: list[dict]):
    if not rows:
        return
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"    -> {path.relative_to(ROOT)} ({len(rows)} rows)")


def server_usec_per_call(r: redis.Redis, cmd: str) -> float | None:
    """Server-side average time per call (excludes network + client overhead)."""
    stats = r.info("commandstats").get(f"cmdstat_{cmd}")
    return stats["usec_per_call"] if stats else None


def pct(values, p):
    s = sorted(values)
    return s[min(len(s) - 1, int(len(s) * p / 100))]


# ---------------------------------------------------------------- experiments

def exp_scaling(r, out, quick):
    """GET latency vs. number of keys. Hash lookups should be O(1): flat line."""
    sizes = [1_000, 10_000, 100_000, 1_000_000] + ([] if quick else [5_000_000])
    samples = 2_000 if quick else 10_000
    rows = []
    r.flushall()
    for n in sizes:
        have = r.dbsize()
        if n > have:
            # DEBUG POPULATE creates key:0..key:n-1 server-side (fast); skips existing keys
            r.execute_command("DEBUG", "POPULATE", n, "key", 16)
        r.config_resetstat()
        lat = []
        for _ in range(samples):
            k = f"key:{random.randrange(n)}"
            t = now_ns()
            r.get(k)
            lat.append((now_ns() - t) / 1000)
        rows.append({
            "keys": n,
            "client_p50_us": round(pct(lat, 50), 2),
            "client_p99_us": round(pct(lat, 99), 2),
            "client_p999_us": round(pct(lat, 99.9), 2),
            "client_mean_us": round(statistics.mean(lat), 2),
            "server_usec_per_call": server_usec_per_call(r, "get"),
            "used_memory_mb": round(r.info("memory")["used_memory"] / 2**20, 1),
        })
        print(f"    {n:>9,} keys  p50={rows[-1]['client_p50_us']}us  server={rows[-1]['server_usec_per_call']}us")
    write_csv(out / "scaling.csv", rows)


def exp_rehash(r, out, quick):
    """
    Insert keys in pipelined batches and time each batch. The main dict grows
    in powers of two; when it does, Redis allocates a new table and migrates
    buckets incrementally. Look for latency bumps near 2^k key counts.

    Per batch we record the client time and also the server-side time (delta of
    cmdstat_set usec), plus whether a rehash is in progress.
    """
    total = 1_100_000 if quick else 4_300_000   # crosses 2^20 / 2^22
    batch = 1_000
    rows = []
    r.flushall()
    r.config_resetstat()
    prev_usec = 0
    p = r.pipeline(transaction=False)
    for start in range(0, total, batch):
        for i in range(start, start + batch):
            p.set(f"rh:{i}", "v")
        t = now_ns()
        p.execute()
        dt = (now_ns() - t) / 1000
        cs = r.info("commandstats")["cmdstat_set"]["usec"]
        mem = r.info("memory")
        rows.append({
            "keys_after": start + batch,
            "batch_us": round(dt, 1),
            "server_batch_us": cs - prev_usec,
            "rehashing_overhead_bytes": mem.get("mem_overhead_db_hashtable_rehashing", 0),
            "used_memory_mb": round(mem["used_memory"] / 2**20, 2),
        })
        prev_usec = cs
    write_csv(out / "rehash.csv", rows)
    # final hash-table stats for context
    try:
        (out / "rehash_htstats.txt").write_text(r.execute_command("DEBUG", "HTSTATS", 0))
    except redis.ResponseError:
        pass


def exp_encoding(r, out, quick):
    """
    Small collections use compact encodings (listpack / intset); past a
    threshold Redis converts them to a hash table. Measure memory and lookup
    cost on both sides of the threshold.
    """
    limits = {
        "hash-max-listpack-entries": r.config_get("hash-max-listpack-entries"),
        "set-max-intset-entries": r.config_get("set-max-intset-entries"),
        "zset-max-listpack-entries": r.config_get("zset-max-listpack-entries"),
    }
    (out / "encoding_limits.json").write_text(json.dumps(limits, indent=2))
    sizes = sorted(set(list(range(8, 1025, 8)) + [127, 128, 129, 511, 512, 513]))
    reps = 200 if quick else 1_000
    rows = []
    r.flushall()
    for kind in ("hash", "set_int", "zset"):
        for n in sizes:
            key = f"enc:{kind}:{n}"
            if kind == "hash":
                r.hset(key, mapping={f"f{i}": "v" for i in range(n)})
                probe = lambda: r.hget(key, f"f{random.randrange(n)}")
                cmd = "hget"
            elif kind == "set_int":
                r.sadd(key, *range(n))
                probe = lambda: r.sismember(key, random.randrange(n))
                cmd = "sismember"
            else:
                r.zadd(key, {f"m{i}": i for i in range(n)})
                probe = lambda: r.zscore(key, f"m{random.randrange(n)}")
                cmd = "zscore"
            r.config_resetstat()
            for _ in range(reps):
                probe()
            rows.append({
                "type": kind,
                "elements": n,
                "encoding": r.object("encoding", key),
                "memory_bytes": r.memory_usage(key, samples=0),
                "server_usec_per_call": server_usec_per_call(r, cmd),
            })
            r.delete(key)
    write_csv(out / "encoding.csv", rows)


def exp_blocking(r, out, quick):
    """
    Redis runs commands on one thread. A background client issues GETs
    continuously while the main thread runs expensive commands; the GET
    latency timeline shows how long everyone else was blocked.
    """
    n_keys = 300_000 if quick else 1_000_000
    big_hash = 200_000 if quick else 1_000_000
    r.flushall()
    r.execute_command("DEBUG", "POPULATE", n_keys, "key", 16)

    def make_big(name):
        p = r.pipeline(transaction=False)
        for start in range(0, big_hash, 10_000):
            p.hset(name, mapping={f"f{i}": "v" for i in range(start, start + 10_000)})
        p.execute()

    make_big("big:del")
    make_big("big:unlink")

    # The probe runs in a separate PROCESS: a thread would share Python's GIL
    # with this one, and parsing a huge KEYS reply here would stall the probe
    # for reasons that have nothing to do with Redis. CLOCK_MONOTONIC
    # (time.monotonic_ns) is system-wide, so both processes share a timeline.
    kw = {k: r.connection_pool.connection_kwargs[k] for k in ("host", "port")}
    stop, q = mp.Event(), mp.Queue()
    proc = mp.Process(target=_blocking_prober, args=(kw, n_keys, stop, q), daemon=True)
    proc.start()
    t0 = q.get()  # prober reports its start time once connected
    events = []

    def event(label, cmds, fn):
        time.sleep(0.5)  # quiet gap so each event is visible separately
        r.config_resetstat()
        s = time.monotonic_ns()
        fn()
        e = time.monotonic_ns()
        cs = r.info("commandstats")
        server_us = sum(cs.get(f"cmdstat_{c}", {}).get("usec", 0) for c in cmds)
        events.append({"event": label, "start_ms": round((s - t0) / 1e6, 3),
                       "end_ms": round((e - t0) / 1e6, 3), "duration_ms": round((e - s) / 1e6, 3),
                       "server_ms": round(server_us / 1000, 3)})
        print(f"    {label:<28} client {events[-1]['duration_ms']:>8.1f} ms   server {events[-1]['server_ms']:>8.1f} ms")

    event(f"KEYS * ({n_keys:,} keys)", ["keys"], lambda: r.keys("*"))
    event("SCAN full iteration", ["scan"], lambda: sum(1 for _ in r.scan_iter(count=1000)))
    event(f"DEL hash ({big_hash:,} fields)", ["del"], lambda: r.delete("big:del"))
    event(f"UNLINK hash ({big_hash:,} fields)", ["unlink"], lambda: r.unlink("big:unlink"))
    time.sleep(0.5)
    stop.set()
    timeline = q.get()
    proc.join()
    write_csv(out / "blocking_timeline.csv", timeline)
    write_csv(out / "blocking_events.csv", events)


def _blocking_prober(kw, n_keys, stop, q):
    pr = redis.Redis(**kw)
    pr.ping()
    t0 = time.monotonic_ns()
    q.put(t0)
    timeline = []
    while not stop.is_set():
        t = time.monotonic_ns()
        pr.get(f"key:{random.randrange(n_keys)}")
        timeline.append({"t_ms": round((t - t0) / 1e6, 3), "get_us": round((time.monotonic_ns() - t) / 1000, 1)})
    q.put(timeline)


def exp_pipeline(r, out, quick):
    """Throughput vs pipeline depth: how much is network round-trip vs server work."""
    n_keys = 100_000
    total_ops = 50_000 if quick else 200_000
    r.flushall()
    r.execute_command("DEBUG", "POPULATE", n_keys, "key", 16)
    rows = []
    for depth in (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000):
        ops = total_ops if depth > 1 else total_ops // 4  # unpipelined is slow
        keys = [f"key:{random.randrange(n_keys)}" for _ in range(ops)]
        t = now_ns()
        if depth == 1:
            for k in keys:
                r.get(k)
        else:
            p = r.pipeline(transaction=False)
            for i in range(0, ops, depth):
                for k in keys[i:i + depth]:
                    p.get(k)
                p.execute()
        secs = (now_ns() - t) / 1e9
        rows.append({"depth": depth, "ops": ops, "seconds": round(secs, 3), "ops_per_sec": round(ops / secs)})
        print(f"    depth {depth:>4}: {rows[-1]['ops_per_sec']:>9,} ops/s")
    write_csv(out / "pipeline.csv", rows)


def exp_expiry(r, out, quick):
    """
    Keys with a TTL are removed lazily (on access) and by a background cycle
    that samples random keys. Nobody touches the keys here, so the curve shows
    the active-expiry rate alone.

    Every key gets the same absolute deadline (PXAT), so they all become
    expired at one instant; t=0 in the output is that instant.
    """
    n = 200_000 if quick else 1_000_000
    margin_s = 10 if quick else 30          # must exceed the time it takes to write n keys
    watch_after = 10 if quick else 20
    r.flushall()
    r.config_resetstat()
    deadline_ms = int(time.time() * 1000) + margin_s * 1000
    w0 = time.time()
    p = r.pipeline(transaction=False)
    for i in range(n):
        p.set(f"exp:{i}", "x" * 32, pxat=deadline_ms)
        if i % 10_000 == 9_999:
            p.execute()
    p.execute()
    write_s = time.time() - w0
    if time.time() * 1000 > deadline_ms:
        raise RuntimeError(f"writing took {write_s:.1f}s, longer than the {margin_s}s margin; raise margin_s")
    deadline = deadline_ms / 1000
    rows = []
    while time.time() < deadline + watch_after:
        info = r.info()
        rows.append({
            "t_s": round(time.time() - deadline, 3),   # seconds relative to the shared expiry instant
            "dbsize": r.dbsize(),
            "expired_keys": info["expired_keys"],
            "used_memory_mb": round(info["used_memory"] / 2**20, 2),
        })
        time.sleep(0.05)
    (out / "expiry_params.json").write_text(json.dumps({"keys": n, "write_s": round(write_s, 2), "hz": r.config_get("hz")["hz"]}))
    write_csv(out / "expiry.csv", rows)


EXPERIMENTS = {
    "scaling": exp_scaling,
    "rehash": exp_rehash,
    "encoding": exp_encoding,
    "blocking": exp_blocking,
    "pipeline": exp_pipeline,
    "expiry": exp_expiry,
}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=6379)
    ap.add_argument("--only", nargs="+", choices=EXPERIMENTS, help="run only these experiments")
    ap.add_argument("--quick", action="store_true", help="smaller sizes for a fast smoke run")
    args = ap.parse_args()

    r = redis.Redis(host=args.host, port=args.port, socket_timeout=120, decode_responses=True)
    info = r.info("server")
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + ("-quick" if args.quick else "")
    out = ROOT / "results" / run_id
    out.mkdir(parents=True)

    chosen = args.only or list(EXPERIMENTS)
    meta = {
        "run_id": run_id, "quick": args.quick, "experiments": chosen,
        "redis_version": info["redis_version"], "redis_git_sha1": info.get("redis_git_sha1"),
        "io_threads": r.config_get("io-threads").get("io-threads"),
        "host": platform.node(), "python": platform.python_version(),
        "started": datetime.now().isoformat(timespec="seconds"),
    }
    print(f"run {run_id} -> {out.relative_to(ROOT)}")
    for name in chosen:
        print(f"[{name}] {EXPERIMENTS[name].__doc__.strip().splitlines()[0]}")
        t = time.time()
        EXPERIMENTS[name](r, out, args.quick)
        meta.setdefault("durations_s", {})[name] = round(time.time() - t, 1)
    meta["finished"] = datetime.now().isoformat(timespec="seconds")
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    r.flushall()
    print(f"done. analyse with: .venv/bin/python analysis/analyze.py {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
