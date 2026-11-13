#!/usr/bin/env python3
"""
Analyse the expiry_load experiment: mass expiry + bulk writes + main-table resize.

Usage: .venv/bin/python analysis/expiry_load.py [results/<run_id>]   (default: latest run that has it)
Writes results/<run_id>/expiry_load_report.md and plots/expiry_load.png.
"""
import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
ORDER = ["writes_only", "expiry_only", "expiry_plus_writes", "writes_presized"]
COLORS = {"writes_only": "#2a6fdb", "expiry_only": "#d98c1f",
          "expiry_plus_writes": "#c43c4c", "writes_presized": "#3a9a5b"}


def find_run():
    if len(sys.argv) > 1:
        return Path(sys.argv[1]).resolve()
    runs = sorted(p.parent for p in (ROOT / "results").glob("*/expiry_load_poll.csv"))
    if not runs:
        sys.exit("no run with expiry_load found")
    return runs[-1]


def q(s, p):
    return round(float(s.quantile(p / 100)), 1) if len(s) else float("nan")


def first_t(df, mask):
    hit = df[mask]
    return round(float(hit["t_s"].iloc[0]), 2) if len(hit) else None


def main():
    run = find_run()
    probe = pd.read_csv(run / "expiry_load_probe.csv")
    writer = pd.read_csv(run / "expiry_load_writer.csv")
    poll = pd.read_csv(run / "expiry_load_poll.csv")
    summ = pd.read_csv(run / "expiry_load_summary.csv").set_index("scenario")
    params = json.loads((run / "expiry_load_params.json").read_text())

    rows, latency_rows, notes = [], [], []
    for sc in ORDER:
        p, w, pl = probe[probe.scenario == sc], writer[writer.scenario == sc], poll[poll.scenario == sc]
        settled = summ.loc[sc, "settled_s"]
        active = p[(p.t_s >= 0) & (p.t_s <= settled)]
        base = p[p.t_s < 0]

        # per-phase GET latency, using the poller timeline to label each probe sample
        pl_sorted = pl.sort_values("t_s")
        lab = pd.merge_asof(active.sort_values("t_s"), pl_sorted[["t_s", "expires", "main_ht1", "exp_ht1"]],
                            on="t_s", direction="backward")
        expiring = lab.expires > 0
        rehash = (lab.main_ht1 > 0)
        for phase, m in [("baseline (t<0)", None), ("expiry running", expiring & ~rehash),
                         ("main-table rehash", rehash & ~expiring), ("expiry + rehash", expiring & rehash),
                         ("neither", ~expiring & ~rehash)]:
            s = base.get_us if m is None else lab[m].get_us
            if len(s) < 50:
                continue
            latency_rows.append({"scenario": sc, "phase": phase, "samples": len(s),
                                 "p50_us": q(s, 50), "p99_us": q(s, 99), "p99.9_us": q(s, 99.9),
                                 "max_ms": round(s.max() / 1000, 2)})

        grow_start = first_t(pl, (pl.main_ht1 > 0) & (pl.t_s >= 0))
        grow_end = None
        if grow_start is not None:
            after = pl[pl.t_s > grow_start]
            grow_end = first_t(after, after.main_ht1 == 0)
        exp_done = first_t(pl, (pl.t_s >= 0) & (pl.expires == 0)) if pl.expires.max() > 0 else None
        writes_end = round(float((w.t_s + w.batch_ms / 1000).max()), 2) if len(w) else None
        exp_sizes = [int(x) for x in pl[pl.t_s >= 0].exp_ht0.drop_duplicates()]
        rows.append({
            "scenario": sc,
            "start_keys": int(pl[pl.t_s < 0].dbsize.iloc[-1]),
            "peak_keys": int(pl.dbsize.max()),
            "end_keys": int(pl.dbsize.iloc[-1]),
            "main_table_start": int(pl[pl.t_s < 0].main_ht0.iloc[-1]),
            "main_table_end": int(pl.main_ht0.iloc[-1]),
            "resize_start_s": grow_start, "resize_end_s": grow_end,
            "expiry_done_s": exp_done,
            "writes_done_s": writes_end,
            "write_keys_per_s": round(params["new_keys"] / writes_end) if writes_end else None,
            "batch_p50_ms": q(w.batch_ms, 50) if len(w) else None,
            "batch_p99_ms": q(w.batch_ms, 99) if len(w) else None,
            "batch_max_ms": round(w.batch_ms.max(), 1) if len(w) else None,
            "mem_start_mb": float(pl[pl.t_s < 0].used_memory_mb.iloc[-1]),
            "mem_peak_mb": float(pl.used_memory_mb.max()),
            "mem_end_mb": float(pl.used_memory_mb.iloc[-1]),
            "rehash_overhead_peak_mb": float(pl.rehashing_overhead_mb.max()),
            "expires_table_sizes": " → ".join(f"{x:,}" for x in exp_sizes) if pl.expires.max() > 0 else "",
            "server_set_us": summ.loc[sc, "server_set_usec_per_call"],
        })

    res = pd.DataFrame(rows)
    lat = pd.DataFrame(latency_rows)

    # ---- plot
    plots = run / "plots"
    plots.mkdir(exist_ok=True)
    fig, axes = plt.subplots(4, 1, figsize=(10, 12), sharex=True)
    for sc in ORDER:
        p, pl, w = probe[probe.scenario == sc], poll[poll.scenario == sc], writer[writer.scenario == sc]
        c = COLORS[sc]
        b = p.assign(bin=(p.t_s * 4).round() / 4).groupby("bin").get_us.quantile(0.99) / 1000
        axes[0].plot(b.index, b.values, color=c, label=sc, lw=1.2)
        if len(w):
            wb = w.assign(bin=(w.t_s * 2).round() / 2).groupby("bin").batch_ms.max()
            axes[1].plot(wb.index, wb.values, color=c, lw=1.2)
        axes[2].plot(pl.t_s, pl.dbsize / 1e6, color=c, lw=1.5)
        axes[3].plot(pl.t_s, pl.used_memory_mb, color=c, lw=1.5)
        r = res.set_index("scenario").loc[sc]
        if r.resize_start_s is not None and pd.notna(r.resize_start_s):
            for ax in axes:
                ax.axvspan(r.resize_start_s, r.resize_end_s or pl.t_s.max(), color=c, alpha=0.07)
    axes[2].axhline(2**22 / 1e6, color="grey", ls=":", lw=1)
    axes[2].text(axes[2].get_xlim()[0], 2**22 / 1e6, " 2²² = 4,194,304", va="bottom", fontsize=8, color="grey")
    axes[0].set_ylabel("probe GET p99 (ms)\nper 250 ms")
    axes[1].set_ylabel("slowest write batch\n(ms, per 500 ms)")
    axes[2].set_ylabel("keys (millions)")
    axes[3].set_ylabel("used_memory (MB)")
    axes[3].set_xlabel("seconds since the shared expiry deadline (t=0: writes start)")
    axes[0].legend(fontsize=8, ncol=2)
    for ax in axes:
        ax.grid(alpha=0.3)
    axes[0].set_title("Mass expiry + bulk writes + main-table resize (shaded: resize in progress)")
    fig.tight_layout()
    fig.savefig(plots / "expiry_load.png", dpi=120)

    md = [f"# expiry_load: {run.name}", "",
          f"hz={params['hz']}, active-expire-effort={params['active_expire_effort']['active-expire-effort']}, "
          f"{params['new_keys']:,} new keys from {params['writers']} writer processes (pipelined, 1000/batch).", "",
          "## Per scenario", "", res.to_markdown(index=False), "",
          "## Probe GET latency by phase (single GETs from a separate process)", "",
          lat.to_markdown(index=False), "", "![expiry_load](plots/expiry_load.png)", ""]
    (run / "expiry_load_report.md").write_text("\n".join(md))
    print("\n".join(md))


if __name__ == "__main__":
    main()
