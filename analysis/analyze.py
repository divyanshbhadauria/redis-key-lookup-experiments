#!/usr/bin/env python3
"""
Analyse a run produced by experiments/run_tests.py.

Usage:
    .venv/bin/python analysis/analyze.py                    # latest run in results/
    .venv/bin/python analysis/analyze.py results/<run_id>

Writes <run>/plots/*.png and <run>/report.md, and prints the findings.
Each analyser is skipped if its CSV is missing, so partial runs (--only) work.
"""
import json
import math
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent

# ---- chart style: thin marks, hairline recessive grid, fixed categorical order
SURFACE, INK, INK_2, MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#8a8984", "#e6e5e1"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
BAND = "#eceae4"  # neutral shading for annotated regions

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "axes.edgecolor": GRID, "axes.linewidth": 0.8, "axes.labelcolor": INK_2,
    "axes.titlecolor": INK, "axes.titlesize": 12, "axes.titleweight": "bold",
    "axes.titlelocation": "left", "axes.labelsize": 10,
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6, "grid.linestyle": "-",
    "xtick.color": MUTED, "ytick.color": MUTED, "xtick.labelsize": 9, "ytick.labelsize": 9,
    "legend.frameon": False, "legend.fontsize": 9, "legend.labelcolor": INK_2,
    "lines.linewidth": 2, "font.size": 10, "figure.dpi": 110,
})


class Report:
    def __init__(self, run: Path):
        self.run, self.plots = run, run / "plots"
        self.plots.mkdir(exist_ok=True)
        self.md: list[str] = []

    def section(self, title, findings: list[str], plot: str | None = None, table: pd.DataFrame | None = None):
        print(f"\n## {title}")
        for f in findings:
            print(f"  - {f}")
        self.md += [f"## {title}", ""] + [f"- {f}" for f in findings] + [""]
        if plot:
            self.md += [f"![{title}](plots/{plot})", ""]
        if table is not None:
            self.md += [table.to_markdown(index=False), ""]

    def save(self, fig, name):
        fig.tight_layout()
        fig.savefig(self.plots / name)
        plt.close(fig)
        return name

    def csv(self, name):
        p = self.run / name
        return pd.read_csv(p) if p.exists() else None


def human(n):
    for unit, div in (("M", 1e6), ("K", 1e3)):
        if abs(n) >= div:
            return f"{n / div:.3g}{unit}"
    return f"{n:g}"


# ------------------------------------------------------------------ analysers

def analyze_scaling(rep: Report):
    df = rep.csv("scaling.csv")
    if df is None:
        return
    flat = df.server_usec_per_call.max() / df.server_usec_per_call.min()
    overhead = (df.client_p50_us / df.server_usec_per_call).median()
    fig, (a, b) = plt.subplots(1, 2, figsize=(10, 3.8))
    for col, label, c in (("client_p50_us", "p50", SERIES[0]), ("client_p99_us", "p99", SERIES[1]),
                          ("client_p999_us", "p99.9", SERIES[2])):
        a.plot(df["keys"], df[col], marker="o", ms=5, color=c, label=label)
    a.set(xscale="log", ylim=(0, None), xlabel="keys in database", ylabel="µs",
          title="GET round-trip latency (client)")
    a.legend()
    b.plot(df["keys"], df.server_usec_per_call, marker="o", ms=5, color=SERIES[0])
    b.set(xscale="log", ylim=(0, None), xlabel="keys in database", ylabel="µs per call",
          title="GET time inside Redis (server)")
    rep.section("1. Lookup cost vs. key count", [
        f"Server-side GET time ranged {df.server_usec_per_call.min():.2f}–{df.server_usec_per_call.max():.2f} µs "
        f"across {human(df['keys'].min())}–{human(df['keys'].max())} keys (max/min = {flat:.2f}×). "
        + ("Effectively flat, as expected for an O(1) hash lookup."
           if flat < 2 else "Not flat. Larger tables hurt CPU cache hit rates (memory latency), not the algorithm."),
        f"Client round-trip p50 is ~{overhead:.0f}× the server time. Most of what a client sees is "
        "network, syscalls and client-library overhead, not the lookup.",
        f"Memory grew to {df.used_memory_mb.max():,.0f} MB at {human(df['keys'].max())} keys "
        f"(~{df.used_memory_mb.max() * 2**20 / df['keys'].max():.0f} B/key including overhead).",
    ], rep.save(fig, "scaling.png"), df)


def analyze_rehash(rep: Report):
    df = rep.csv("rehash.csv")
    if df is None:
        return
    has_server = "server_batch_us" in df
    col = "server_batch_us" if has_server else "batch_us"
    batch = int(df.keys_after.iloc[0])
    df["baseline"] = df[col].rolling(51, center=True, min_periods=5).median()
    df["ratio"] = df[col] / df.baseline

    # rehash periods: contiguous runs where the rehashing-overhead gauge is non-zero
    periods = []
    if "rehashing_overhead_bytes" in df:
        on = df.rehashing_overhead_bytes > 0
        for _, g in df[on].groupby((on != on.shift()).cumsum()[on]):
            periods.append(g)
    rows = []
    for g in periods:
        start_keys = int(g.keys_after.iloc[0])
        inside = df.loc[g.index]
        rows.append({
            "rehash starts at keys": f"{start_keys:,}",
            "nearest 2^k": f"2^{round(math.log2(start_keys))}",
            "lasted (keys inserted)": f"{int(g.keys_after.iloc[-1] - start_keys + batch):,}",
            "new table overhead": f"{g.rehashing_overhead_bytes.max() / 2**20:.1f} MB",
            f"worst {col} inside": round(inside[col].max()),
            "vs rolling median": f"{inside.ratio.max():.1f}×",
        })
    tdf = pd.DataFrame(rows)

    fig, ax = plt.subplots(figsize=(10, 3.8))
    for g in periods:
        ax.axvspan(g.keys_after.iloc[0] - batch, g.keys_after.iloc[-1], color=BAND, zorder=0)
    ax.plot(df.keys_after, df[col], color=SERIES[0], lw=1, label=f"{'server' if has_server else 'client'} time per {batch:,} SETs")
    ax.plot(df.keys_after, df.baseline, color=INK_2, lw=1.5, label="rolling median")
    ax.set(yscale="log", xlabel="keys in database (shaded: rehash in progress)", ylabel="µs per batch",
           title="Insert cost while the main hash table grows")
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: human(v)))
    ax.legend(loc="upper left")

    in_rh = df.index.isin(pd.concat(periods).index) if periods else pd.Series(False, index=df.index)
    findings = [f"{len(df):,} batches of {batch:,} SETs. Median server time {df[col].median():,.0f} µs/batch "
                f"(client saw {df.batch_us.median():,.0f} µs, so the client and network add most of it).",
                f"{len(periods)} rehash periods detected. Each starts when the key count reaches a power of two (the table doubles)."]
    if periods:
        findings.append(f"Median batch during rehash: {df.loc[in_rh, col].median():,.0f} µs vs "
                        f"{df.loc[~in_rh, col].median():,.0f} µs outside. That difference is the cost of "
                        "incremental migration: every write also moves a few buckets to the new table.")
    findings.append("The cost is spread over the whole rehash period rather than one big pause, so you see a "
                    "plateau, not a spike. The one-off cost is allocating the new bucket array (8 bytes × 2^k).")
    rep.section("2. Hash table resizing (rehash)", findings, rep.save(fig, "rehash.png"), tdf if len(tdf) else None)
    hts = rep.run / "rehash_htstats.txt"
    if hts.exists():
        rep.md += ["Final `DEBUG HTSTATS 0`:", "", "```", hts.read_text().strip(), "```", ""]


def analyze_encoding(rep: Report):
    df = rep.csv("encoding.csv")
    if df is None:
        return
    lim_p = rep.run / "encoding_limits.json"
    limits = json.loads(lim_p.read_text()) if lim_p.exists() else {}
    types = list(df["type"].unique())
    fig, axes = plt.subplots(2, len(types), figsize=(4 * len(types), 6), sharex=True)
    findings, rows = [], []
    for i, t in enumerate(types):
        d = df[df["type"] == t].sort_values("elements")
        d = d.assign(bytes_per_el=d.memory_bytes / d.elements)
        change = d[d.encoding != d.encoding.shift()].iloc[1:]
        for ax, col, ylabel in ((axes[0, i], "bytes_per_el", "bytes per element"),
                                (axes[1, i], "server_usec_per_call", "µs per lookup (server)")):
            for j, enc in enumerate(d.encoding.unique()):
                s = d[d.encoding == enc]
                ax.plot(s.elements, s[col], color=SERIES[j], lw=2, label=enc)
            for _, c in change.iterrows():
                ax.axvline(c.elements, color=MUTED, lw=1, zorder=0)
            ax.set(ylabel=ylabel, ylim=(0, None))
        axes[0, i].set_title(t)
        axes[0, i].legend()
        axes[1, i].set_xlabel("elements in collection")
        for _, c in change.iterrows():
            before = d[d.elements < c.elements].iloc[-1]
            rows.append({"type": t, "from": before.encoding, "to": c.encoding,
                         "at_elements": int(c.elements),
                         "bytes/elem before": round(before.bytes_per_el, 1),
                         "bytes/elem after": round(c.memory_bytes / c.elements, 1),
                         "memory jump": f"{c.memory_bytes / before.memory_bytes:.2f}×",
                         "lookup µs before": before.server_usec_per_call,
                         "lookup µs after": c.server_usec_per_call})
        small, big = d.iloc[0], d.iloc[-1]
        if len(change) == 0:
            findings.append(f"{t}: stayed `{small.encoding}` up to {int(big.elements)} elements.")
    tdf = pd.DataFrame(rows)
    for r in rows:
        findings.append(
            f"{r['type']}: `{r['from']}` → `{r['to']}` at {r['at_elements']} elements; total memory "
            f"jumps {r['memory jump']} ({r['bytes/elem before']} → {r['bytes/elem after']} B/element), "
            f"lookup {r['lookup µs before']} → {r['lookup µs after']} µs.")
    findings.append("Compact encodings scan linearly (lookup cost grows with size) but are dense. "
                    "Hash tables are O(1) but cost more per element. The config limits pick the crossover: "
                    + ", ".join(f"`{k}`={list(v.values())[0]}" for k, v in limits.items()) + ".")
    rep.section("3. Encoding conversions (compact → hash table)", findings,
                rep.save(fig, "encoding.png"), tdf if len(tdf) else None)


def analyze_blocking(rep: Report):
    tl, ev = rep.csv("blocking_timeline.csv"), rep.csv("blocking_events.csv")
    if tl is None or ev is None:
        return
    base_p50 = tl.get_us.median()
    rows = []
    fig, ax = plt.subplots(figsize=(10, 4))
    for i, e in ev.iterrows():
        ax.axvspan(e.start_ms, e.end_ms, color=BAND, zorder=0)
        w = tl[(tl.t_ms >= e.start_ms - 1) & (tl.t_ms <= e.end_ms + 1)]
        worst = w.get_us.max() if len(w) else float("nan")
        rows.append({"command": e.event, "command_ms": e.duration_ms, "server_ms": e.get("server_ms"),
                     "concurrent GETs": len(w), "worst GET stall ms": round(worst / 1000, 1),
                     "stall vs normal p50": f"{worst / base_p50:,.0f}×"})
        ax.annotate(e.event, (e.start_ms, 1), xycoords=("data", "axes fraction"), xytext=(3, -4 - 12 * (i % 2)),
                    textcoords="offset points", va="top", fontsize=8, color=INK_2)
    ax.scatter(tl.t_ms, tl.get_us / 1000, s=2, color=SERIES[0], linewidths=0)
    ax.set(yscale="log", xlabel="time since start (ms); shaded = expensive command running",
           ylabel="GET latency (ms)", title="Latency of an unrelated client while one client runs expensive commands")
    tdf = pd.DataFrame(rows)
    findings = [f"Background GET p50 when idle: {base_p50:.0f} µs."]
    for r in rows:
        findings.append(f"{r['command']}: {r['server_ms']} ms inside Redis ({r['command_ms']:,.1f} ms seen by its client); worst concurrent GET "
                        f"{r['worst GET stall ms']} ms ({r['stall vs normal p50']} normal).")
    findings.append("KEYS blocks the event loop for the whole scan. SCAN does more total work but in small "
                    "steps, so other clients get in between. UNLINK hands the free to a background thread; "
                    "DEL frees every field on the main thread.")
    rep.section("4. Single-threaded blocking", findings, rep.save(fig, "blocking.png"), tdf)


def analyze_pipeline(rep: Report):
    df = rep.csv("pipeline.csv")
    if df is None:
        return
    base = df.loc[df.depth == 1, "ops_per_sec"].iloc[0]
    best = df.loc[df.ops_per_sec.idxmax()]
    # depth where 90% of the max is reached
    knee = df[df.ops_per_sec >= 0.9 * best.ops_per_sec].depth.min()
    rtt_us = 1e6 / base
    fig, ax = plt.subplots(figsize=(7, 3.8))
    ax.plot(df.depth, df.ops_per_sec, marker="o", ms=5, color=SERIES[0])
    ax.annotate(f"{best.ops_per_sec:,.0f} ops/s", (best.depth, best.ops_per_sec), xytext=(-8, 8),
                textcoords="offset points", ha="right", fontsize=9, color=INK)
    ax.set(xscale="log", ylim=(0, None), xlabel="pipeline depth (commands per round trip)",
           ylabel="GET ops/sec", title="Throughput vs pipeline depth")
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: human(v)))
    rep.section("5. Pipelining", [
        f"Unpipelined: {base:,.0f} ops/s, i.e. one round trip ≈ {rtt_us:.0f} µs.",
        f"Peak {best.ops_per_sec:,.0f} ops/s at depth {int(best.depth)} ({best.ops_per_sec / base:.1f}×). "
        f"90% of peak by depth {int(knee)}.",
        "Past the knee the bottleneck is the client (Python encoding/parsing replies), not Redis. "
        "Compare with `redis-benchmark -P 100` to see the server's own limit.",
    ], rep.save(fig, "pipeline.png"), df)


def analyze_expiry(rep: Report):
    df = rep.csv("expiry.csv")
    if df is None:
        return
    params_p = rep.run / "expiry_params.json"
    params = json.loads(params_p.read_text()) if params_p.exists() else {}
    n = params.get("keys", int(df.dbsize.max()))

    def t_when(frac):
        hit = df[df.expired_keys >= frac * n]
        return hit.t_s.iloc[0] if len(hit) else None

    t50, t99, t100 = t_when(0.5), t_when(0.99), t_when(1.0)
    window = df[(df.expired_keys > 0.1 * n) & (df.expired_keys < 0.9 * n)]
    rate = ((window.expired_keys.iloc[-1] - window.expired_keys.iloc[0]) /
            (window.t_s.iloc[-1] - window.t_s.iloc[0])) if len(window) > 1 else None

    fig, ax = plt.subplots(figsize=(8, 3.8))
    ax.plot(df.t_s, df.dbsize, color=SERIES[0], label="keys still in memory")
    ax.axvline(0, color=MUTED, lw=1, zorder=0)
    ax.annotate("all keys expire", (0, 1), xycoords=("data", "axes fraction"), xytext=(4, -4),
                textcoords="offset points", va="top", fontsize=8, color=INK_2)
    ax.set(ylim=(0, None), xlabel="seconds relative to the shared expiry time", ylabel="keys",
           title="Active expiry: keys nobody reads after they expire")
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: human(v)))
    fmt = lambda t: f"{t:.2f}s" if t is not None else "not reached in the observation window"
    hz = params.get("hz", "?")
    rep.section("6. Key expiry without access", [
        f"{n:,} keys all set to expire at the same instant (PXAT), never read again; hz={hz}.",
        f"Time after expiry until 50% removed: {fmt(t50)}; 99%: {fmt(t99)}; 100%: {fmt(t100)}.",
        f"Removal rate mid-curve ≈ {rate:,.0f} keys/s." if rate else "Not enough points to estimate a rate.",
        "Expired-but-unread keys are only removed by the background cycle, which runs `hz` times a second "
        "under a CPU-time budget. Until then they still use memory and count in DBSIZE. "
        "Raising `active-expire-effort` or `hz` speeds this up at the cost of CPU.",
        f"Memory: {df.used_memory_mb.max():.1f} MB → {df.used_memory_mb.iloc[-1]:.1f} MB.",
    ], rep.save(fig, "expiry.png"))


ANALYSERS = [analyze_scaling, analyze_rehash, analyze_encoding, analyze_blocking, analyze_pipeline, analyze_expiry]


def main():
    if len(sys.argv) > 1:
        run = Path(sys.argv[1]).resolve()
    else:
        runs = sorted(p for p in (ROOT / "results").iterdir() if (p / "meta.json").exists())
        if not runs:
            sys.exit("no completed runs in results/. run experiments/run_tests.py first")
        run = runs[-1]
    meta = json.loads((run / "meta.json").read_text())
    rep = Report(run)
    rep.md += [f"# Redis experiments: run `{meta['run_id']}`", "",
               f"Redis `{meta['redis_version']}` (sha `{meta.get('redis_git_sha1')}`), "
               f"io-threads={meta.get('io_threads')}, quick={meta['quick']}, host `{meta['host']}`, "
               f"{meta['started']} → {meta['finished']}.", ""]
    print(f"analysing {run}")
    for fn in ANALYSERS:
        try:
            fn(rep)
        except Exception as e:  # keep going; one broken analyser shouldn't hide the rest
            print(f"  !! {fn.__name__} failed: {e!r}")
            rep.md += [f"> {fn.__name__} failed: `{e!r}`", ""]
    (run / "report.md").write_text("\n".join(rep.md))
    print(f"\nreport: {run / 'report.md'}\nplots:  {rep.plots}")


if __name__ == "__main__":
    main()
