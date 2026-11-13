# Redis key-retrieval experiments

```
redis/                     Redis source (not committed: clone redis/redis and build with `make`)
redis-experiments.conf     server config: no persistence, DEBUG enabled for localhost, latency monitor on
scripts/redis.sh           start | stop | status | cli
experiments/run_tests.py   runs the experiments, writes raw CSVs to results/<run_id>/
analysis/analyze.py        reads a run, writes results/<run_id>/report.md and plots/*.png
analysis/build_study.py    builds study/redis-study.html (the write-up page) from a run
results/20261114-014947/   the full run the write-up is based on (raw CSVs, plots, report.md)
study/medium/              the Medium article and its images
data/                      redis.log, pid file
.venv/                     python deps (redis, pandas, matplotlib, tabulate)
```

## Quick start

```bash
# one-time setup
git clone --depth 1 https://github.com/redis/redis.git && (cd redis && make -j4)
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

scripts/redis.sh start
.venv/bin/python experiments/run_tests.py --quick          # ~1-2 min smoke run
.venv/bin/python experiments/run_tests.py                  # full sizes, a few minutes
.venv/bin/python experiments/run_tests.py --only encoding blocking
.venv/bin/python analysis/analyze.py                       # analyses the latest run
.venv/bin/python analysis/analyze.py results/<run_id>      # or a specific one
scripts/redis.sh stop
```

`run_tests.py` runs **FLUSHALL** before each experiment, so only point it at this throwaway server.

## Experiments

| name | question | how |
|---|---|---|
| `scaling` | Is GET really O(1) as the key count grows? | `DEBUG POPULATE` to 1K…5M keys, random GETs; client p50/p99 + server `usec_per_call` |
| `rehash` | What does growing the main hash table cost? | 4.3M SETs in batches of 1000; per-batch server time + `mem_overhead_db_hashtable_rehashing` gauge |
| `encoding` | Where do compact encodings switch to hash tables, and what does it cost? | hash / int-set / zset at 8…1024 elements; `OBJECT ENCODING`, `MEMORY USAGE`, lookup time |
| `blocking` | How much does one slow command hurt everyone else? | separate-process GET probe while running KEYS, SCAN, DEL, UNLINK |
| `pipeline` | How much is round-trip vs. server work? | GET throughput at pipeline depth 1…1000 |
| `expiry` | How fast are expired-but-unread keys reclaimed? | 1M keys with one shared PXAT deadline; poll DBSIZE / `expired_keys` |

## Method notes

- **Client latency ≠ server latency.** A Python round trip is ~100 µs; a GET inside Redis is ~1 µs.
  Analyses use server-side numbers (`INFO commandstats`) wherever the question is about Redis itself.
- **The blocking probe is a separate process**, not a thread. With a thread, Python's GIL made
  the probe stall while the main thread parsed big replies, which measures Python, not Redis.
- **Expiry uses one absolute deadline** (`PXAT`) because writing 1M keys takes seconds; with
  relative TTLs, keys written early would expire early and blur the curve.
- This build is the `unstable` branch (`redis_version:255.255.255`). Sorted sets there use a
  packed B+ tree (`src/zset_btree.c`), not the skiplist from older releases.

## Where to read the source

- `src/dict.c`: the hash table, `_dictExpandIfNeeded`, `dictRehash` (incremental rehash)
- `src/kvstore.c`: per-db / per-slot dicts
- `src/expire.c`: `activeExpireCycle`
- `src/t_hash.c`, `src/t_set.c`, `src/t_zset.c`, `src/zset_btree.c`: encodings and conversions
- `src/lazyfree.c`: UNLINK / async free
