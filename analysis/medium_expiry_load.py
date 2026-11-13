#!/usr/bin/env python3
"""
Charts for the follow-up Medium post (expiry + writes + resize), in the same
style as analysis/analyze.py.

Usage: .venv/bin/python analysis/medium_expiry_load.py results/<run_id> study/medium/expiry-under-load/images
"""
import sys
from pathlib import Path

import matplotlib.ticker as mt
import matplotlib.pyplot as plt
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze import BAND, INK_2, MUTED, SERIES  # noqa: E402  (also applies the shared rcParams)

run, out = Path(sys.argv[1]), Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
probe = pd.read_csv(run / "expiry_load_probe.csv")
writer = pd.read_csv(run / "expiry_load_writer.csv")
poll = pd.read_csv(run / "expiry_load_poll.csv")
ivb = pd.read_csv(run / "expiry_idle_vs_busy.csv")
millions = mt.FuncFormatter(lambda v, _: f"{v / 1e6:g}M")


def save(fig, name):
    fig.tight_layout()
    fig.savefig(out / name, dpi=160)
    plt.close(fig)
    print("wrote", out / name)


def resize_window(sc):
    pl = poll[(poll.scenario == sc) & (poll.t_s >= 0)]
    on = pl[pl.main_ht1 > 0]
    if on.empty:
        return None
    off = pl[(pl.t_s > on.t_s.iloc[0]) & (pl.main_ht1 == 0)]
    return on.t_s.iloc[0], (off.t_s.iloc[0] if len(off) else pl.t_s.max())


# 1. idle vs busy expiry
fig, ax = plt.subplots(figsize=(8, 3.8))
for i, (mode, g) in enumerate(ivb.groupby("mode", sort=False)):
    done = g[(g.t_s > 0) & (g.expiring_left == 0)].t_s.iloc[0]
    ax.plot(g.t_s, g.expiring_left, color=SERIES[i], label=f"{mode}: gone after {done:.1f} s")
ax.axvline(0, color=MUTED, lw=1, zorder=0)
ax.annotate("1M keys expire", (0, 1), xycoords=("data", "axes fraction"), xytext=(4, -4),
            textcoords="offset points", va="top", fontsize=8, color=INK_2)
ax.yaxis.set_major_formatter(millions)
ax.set(ylim=(0, None), xlim=(-1, 10.5), xlabel="seconds after the shared expiry time",
       ylabel="expired keys still in memory", title="Same 1M keys, idle server vs busy server")
ax.legend(loc="upper right")
save(fig, "idle_vs_busy.png")

# 2. GET stalls while expiry runs
p = probe[(probe.scenario == "expiry_only") & (probe.t_s.between(-1, 3))]
fig, ax = plt.subplots(figsize=(8, 3.8))
ax.axvspan(0, poll[(poll.scenario == "expiry_only") & (poll.t_s > 0) & (poll.expires == 0)].t_s.iloc[0],
           color=BAND, zorder=0)
ax.scatter(p.t_s, p.get_us / 1000, s=3, color=SERIES[0], linewidths=0)
ax.annotate("expiry running", (0, 1), xycoords=("data", "axes fraction"), xytext=(4, -4),
            textcoords="offset points", va="top", fontsize=8, color=INK_2)
ax.set(xlabel="seconds after the shared expiry time", ylabel="GET round trip (ms)",
       title="Single GETs from another client while 1M keys expire")
save(fig, "expiry_stalls.png")

# 3. crossing 2^22 vs presized table
fig, (a, b) = plt.subplots(2, 1, figsize=(8, 6.4), sharex=True)
names = {"writes_only": "table grows (4.15M → 6.15M keys)", "writes_presized": "table already big enough (4.2M → 6.2M)"}
for i, (sc, label) in enumerate(names.items()):
    pl = poll[poll.scenario == sc]
    w = writer[writer.scenario == sc]
    a.plot(pl.t_s, pl.used_memory_mb, color=SERIES[i], label=label)
    wb = w.assign(bin=(w.t_s * 2).round() / 2).groupby("bin").batch_ms.max()
    b.plot(wb.index, wb.values, color=SERIES[i], lw=1.3, label=label)
rw = resize_window("writes_only")
for ax in (a, b):
    ax.axvspan(*rw, color=BAND, zorder=0)
a.annotate("+64 MB: new 8.4M-bucket table", (rw[0], 345), xytext=(8, 6), textcoords="offset points",
           fontsize=8, color=INK_2)
a.set(ylabel="used_memory (MB)", title="Writing 2M keys: crossing 4,194,304 vs not")
a.legend(loc="lower right")
b.set(ylabel="slowest 1000-SET batch\nper 0.5 s (ms)", xlabel="seconds since writes started (shaded: table resize in progress)",
      ylim=(0, None))
save(fig, "resize_vs_presized.png")

# 4. resize keeps going after writes stop
pl = poll[(poll.scenario == "expiry_plus_writes") & (poll.t_s >= -1)]
w = writer[writer.scenario == "expiry_plus_writes"]
wend = (w.t_s + w.batch_ms / 1000).max()
rw = resize_window("expiry_plus_writes")
fig, (a, b) = plt.subplots(2, 1, figsize=(8, 6), sharex=True)
for ax in (a, b):
    ax.axvspan(*rw, color=BAND, zorder=0)
    ax.axvline(wend, color=MUTED, lw=1, ls="--")
a.plot(pl.t_s, pl.dbsize, color=SERIES[0])
a.axhline(2**22, color=MUTED, lw=1, ls=":")
a.annotate("2²² = 4,194,304 buckets", (pl.t_s.max(), 2**22), xytext=(-4, 4), textcoords="offset points",
           ha="right", fontsize=8, color=INK_2)
a.annotate("1M keys expire", (1.8, 3.25e6), xytext=(6, -2), textcoords="offset points", fontsize=8, color=INK_2)
a.yaxis.set_major_formatter(millions)
a.set(ylabel="keys", title="1M keys expire, then 2M writes push the table past 2²²")
b.plot(pl.t_s, pl.used_memory_mb, color=SERIES[1])
b.annotate("writes stop", (wend, 1), xycoords=("data", "axes fraction"), xytext=(4, -4),
           textcoords="offset points", va="top", fontsize=8, color=INK_2)
b.annotate("old table freed", (rw[1], 360), xytext=(-6, -14), textcoords="offset points", ha="right",
           fontsize=8, color=INK_2)
b.set(ylabel="used_memory (MB)", xlabel="seconds after the shared expiry time (shaded: table resize in progress)")
save(fig, "resize_tail.png")
