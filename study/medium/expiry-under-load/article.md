# What happens when 1M Redis keys expire while you're writing 2M more?

*A follow-up to my Redis-from-source experiments: mass expiry, bulk writes and a hash table resize, all at once. Also, a correction to my own last post.*

---

In my [last post](https://github.com/divyanshbhadauria/redis-key-lookup-experiments) I wrote 1M keys that all expired at the same moment and watched Redis clean them up. It took about 8 seconds, roughly 127K keys per second.

After that I kept asking myself one question. In real life nothing happens in isolation. What if Redis already holds 4M keys, 1M of them expire at once, and while it's busy deleting them I write 2M more? That pushes the main hash table past a power of two, so it has to grow too. Expiry gets up to 25% of the CPU, writes keep coming, the table is being copied... does something give out?

So I tested it. Same old laptop (i5-5350U, 2 cores, 8 GB), same `unstable` build, same config: `hz 10`, `active-expire-effort 1`, no persistence.

## Setup

Four scenarios, each starting from a fresh database:

- **writes_only**: 4.15M keys, then write 2M more. The main table has 4,194,304 buckets (2²²), so this crosses the boundary and forces a resize.
- **expiry_only**: 3.15M normal keys plus 1M keys that all expire at t=0. No writes.
- **expiry_plus_writes**: the same 4.15M keys with 1M expiring, and 2M writes starting at t=0. This is the scenario I actually cared about.
- **writes_presized**: 4.2M keys, so the table is *already* 2²³ = 8,388,608 buckets. Then the same 2M writes. No resize needed.

The keys come from `DEBUG POPULATE`, and the expiring ones get one shared deadline with `PEXPIREAT` (the absolute-time version of `PEXPIRE`), set in batches by a small Lua script. Two writer processes send `SET`s in pipelined batches of 1,000. A third process sends single `GET`s nonstop: that's my "every other client" whose latency I care about. And every 50 ms I poll `INFO` and `DEBUG HTSTATS 0`, which shows the hash table sizes, including the second table while a resize is in progress.

## 1. My 127K keys/second was an idle-server number

The first surprise came before anything else. In `expiry_only`, 1M keys were gone in **1.7 seconds**, not 8. That's about 5× faster than last time.

At first I assumed I'd broken something. Then I went back to `src/expire.c`. Redis has *two* expiry cycles:

- The **slow cycle** runs from `serverCron`, `hz` times a second, and may use up to 25% of the CPU (`ACTIVE_EXPIRE_CYCLE_SLOW_TIME_PERC`).
- The **fast cycle** runs from `beforeSleep`, which is called on every pass of the event loop. It runs for at most 1 ms, and at most once every 2 ms.

```c
if (type == ACTIVE_EXPIRE_CYCLE_FAST) {
    ...
    if (start < last_fast_cycle + (long long)config_cycle_fast_duration*2)
        return;
    last_fast_cycle = start;
}
```

The fast cycle only gets a chance when the event loop is turning, and the event loop turns when clients send commands. In my first experiment the server was almost idle (just my poller every 50 ms), so it was basically only the slow cycle. This time my GET process kept the server busy the whole time.

To check, I ran the same 1M expiring keys twice: once idle, once with a single client sending GETs nonstop.

[[IMAGE:idle_vs_busy]]

- Idle server: all gone after **9.3 s** (~108K keys/s), which matches my last post.
- One busy client: all gone after **1.7 s** (~590K keys/s).

So there's no fixed "Redis expires N keys per second". With traffic, expiry can get up to roughly 25% (slow) + 50% (fast) of the CPU. The busier your server, the faster expired keys get removed, which is a nice property, honestly.

## 2. What other clients feel during mass expiry

Here are the single GETs from the probe process while the 1M keys expired:

[[IMAGE:expiry_stalls]]

Three things are going on:

- Most GETs are unaffected. The median went from **113 µs to 124 µs**.
- About 10% of GETs land in a band around **1.4 ms**. They arrived while a 1 ms fast cycle was running and had to wait for it.
- Every so often a GET takes **~27 ms**. That's a request that arrived just as the slow cycle started and waited for its whole 25 ms budget.

The big stalls are spaced exactly **125 ms** apart. That's the 100 ms cron interval (`hz 10`) plus the ~25 ms the cycle itself takes, because the next timer is scheduled after the current one finishes. You can see the config values directly in the latency.

The p99.9 GET latency went from **0.4 ms to 27 ms** while expiry ran. If you have a strict tail latency target, a big batch of keys expiring at the same time can blow it, even though the average looks fine.

## 3. Crossing a power of two

Now the resize. The main table grows when the number of keys reaches the number of buckets (`dictExpandIfNeeded` in `src/dict.c`). Going from 4.15M to 6.15M keys crosses 4,194,304, so Redis allocates a new table with 8,388,608 buckets and moves entries over a little at a time: one bucket per command that touches the table, plus up to 1 ms in every `serverCron`.

I compared that with the presized run, which does the exact same writes into a table that's already big enough:

[[IMAGE:resize_vs_presized]]

- `used_memory` **jumped by 64 MB in one step** when the resize started. That's the new bucket array: 8,388,608 buckets × 8 bytes. When the move finished, the old 32 MB array was freed.
- While the resize ran, each `SET` cost **1.31 µs** of server time vs **0.57 µs** with the presized table, about 2.3×.
- The slowest write batches were worse too: p99 **54 ms vs 29 ms**, worst **105 ms vs 32 ms**.
- The probe's GET p99 was **2.0 ms vs 0.9 ms**.
- Write throughput: **63K vs 70K keys/s**.

So the resize is spread out and never blocks for long, but you can clearly see it. It's not one big spike, it's a ~30 second stretch of everything being somewhat slower.

## 4. The resize that kept going after the writes stopped

This was the scenario I set out to test: 1M keys expire, and 2M writes start at the same moment.

[[IMAGE:resize_tail]]

The expiry finished in 1.8 s, while writes kept going at about **72K keys/s**. The key count dropped from 4.15M to 3.25M and then climbed back up. It crossed 4,194,304 at about 14 s, and the resize started.

Then the writes finished at 27.8 s, but the resize didn't finish until **67.8 s**. Why? Because the table is moved mostly by commands that touch it. With no traffic, only `serverCron` does it, 1 ms per 100 ms. For those 40 quiet seconds, both tables sat in memory. The old one was freed only at the very end (the drop at ~68 s).

In practice this probably doesn't matter much, since a real server rarely goes completely quiet. But it was not what I expected.

One honest note: in this run, expiry and the resize did **not** overlap. Expiry was too fast (see section 1) and finished long before the key count crossed the boundary. Expiry and writes did overlap, and writes held up fine. To get all three at once I'd need more expiring keys.

## 5. Does the table shrink when keys go away?

In `expiry_only`, the key count went from 4.15M to 3.15M. The main table stayed at **4,194,304 buckets**.

The rule (`dictShrinkIfNeeded`) is that a table only shrinks when it's **12.5% full or less**. A 4,194,304-bucket table won't shrink until you're below 524,288 keys. Redis grows at 100% full and shrinks at 12.5% full, so a key count bouncing around a boundary doesn't make it resize back and forth.

The **expires table**, which only tracks keys with a TTL, did shrink as it emptied: 1,048,576 → 131,072 → 0 buckets. It's small, so that's cheap.

## 6. So did anything give out?

No. Redis runs commands on one thread, so expiry, the resize and my writes just take turns. Writes ran at 63–72K keys/s in every scenario, and no command failed. The cost shows up as latency (the 27 ms expiry stalls, the 2× slower writes during a resize) and as memory (expired keys not yet deleted, plus two tables during a resize).

If any of this runs out, it'll be memory, not CPU. With `maxmemory` set and `noeviction`, those extra MBs are where writes would start failing.

## Can you pre-size the table?

The presized run was clearly the best: flat latency, cheapest `SET`s, no memory step. The price is memory up front: its table started 32 MB bigger.

But there's no config or command to pre-size the keyspace. I used `DEBUG POPULATE`, which you shouldn't do in production. Redis does pre-size for itself when it loads an RDB file or syncs a replica: the file stores the key count, and `rdbLoad` expands the table to that size before reading (`dbExpand` in `src/rdb.c`).

What people actually do:

- **Shard.** In cluster mode every hash slot has its own table (`src/kvstore.c`), so each resize is small and they don't all happen at once.
- **Leave memory headroom** for the resize step: 64 MB at 4M → 8M keys, 128 MB at 8M → 16M, on top of your data.
- **Add jitter to TTLs** instead of giving millions of keys the same deadline. That turns one 27 ms stall into many small ones.
- **Avoid big loads during BGSAVE or an AOF rewrite.** While a fork is running, Redis delays growing the table until there are 4 keys per bucket.
- **Watch** `mem_overhead_db_hashtable_rehashing` and `LATENCY LATEST`.

## What I'd take away

- Expiry speed depends on traffic. Idle: ~108K keys/s. Busy: ~590K keys/s on the same machine.
- Mass expiry is cheap on average but shows up in the tail: ~27 ms stalls every 125 ms.
- Crossing a power of two costs a 64 MB step and ~2× slower writes for the length of the resize.
- With no traffic, a resize can outlive the writes that caused it by a long time.
- Tables grow at 100% full and shrink only at 12.5%. Deleting a quarter of your keys gives back no table memory.
- Nothing ran out of CPU. Memory is the thing to plan for.

Same caveat as last time: one run, one laptop, CPU frequency scaling on. Look at the shapes and ratios, not the exact numbers.

All the scripts and raw data are here: https://github.com/divyanshbhadauria/redis-key-lookup-experiments
