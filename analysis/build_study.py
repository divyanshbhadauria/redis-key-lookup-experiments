#!/usr/bin/env python3
"""
Build a shareable HTML write-up of one run (charts drawn as inline SVG from the CSVs).

Usage:
    .venv/bin/python analysis/build_study.py [results/<run_id>] [out.html]
Defaults: latest run, study/redis-study.html
"""
import html
import json
import math
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
W, H = 720, 280
PAD = dict(l=58, r=18, t=16, b=40)


# ------------------------------------------------------------------ svg chart helper

class Scale:
    def __init__(self, lo, hi, a, b, log=False):
        self.lo, self.hi, self.a, self.b, self.log = lo, hi, a, b, log

    def __call__(self, v):
        f = (lambda x: math.log10(x)) if self.log else (lambda x: x)
        return self.a + (f(v) - f(self.lo)) / (f(self.hi) - f(self.lo)) * (self.b - self.a)


def fmt_num(v):
    if v >= 1e6:
        return f"{v / 1e6:g}M"
    if v >= 1e3:
        return f"{v / 1e3:g}K"
    return f"{v:g}"


def chart(cid, series, xlim, ylim, xticks, yticks, xlabel, ylabel, xlog=False, ylog=False,
          bands=(), vlines=(), xfmt=fmt_num, yfmt=fmt_num, tip=None, height=H):
    """series: list of dict(x=[..], y=[..], cls='s1', width=2, label=str)."""
    x = Scale(*xlim, PAD["l"], W - PAD["r"], xlog)
    y = Scale(*ylim, height - PAD["b"], PAD["t"], ylog)
    o = [f'<svg id="{cid}" class="chart" viewBox="0 0 {W} {height}" role="img" aria-label="{html.escape(ylabel)} vs {html.escape(xlabel)}">']
    for b0, b1 in bands:
        o.append(f'<rect class="band" x="{x(b0):.1f}" y="{PAD["t"]}" width="{max(1, x(b1) - x(b0)):.1f}" height="{height - PAD["t"] - PAD["b"]}"/>')
    for t in yticks:
        o.append(f'<line class="grid" x1="{PAD["l"]}" x2="{W - PAD["r"]}" y1="{y(t):.1f}" y2="{y(t):.1f}"/>'
                 f'<text class="tick" x="{PAD["l"] - 8}" y="{y(t) + 4:.1f}" text-anchor="end">{yfmt(t)}</text>')
    for t in xticks:
        o.append(f'<text class="tick" x="{x(t):.1f}" y="{height - PAD["b"] + 18}" text-anchor="middle">{xfmt(t)}</text>')
    o.append(f'<line class="axis" x1="{PAD["l"]}" x2="{W - PAD["r"]}" y1="{height - PAD["b"]}" y2="{height - PAD["b"]}"/>')
    for v, label in vlines:
        o.append(f'<line class="vline" x1="{x(v):.1f}" x2="{x(v):.1f}" y1="{PAD["t"]}" y2="{height - PAD["b"]}"/>'
                 f'<text class="note" x="{x(v) + 5:.1f}" y="{PAD["t"] + 12}">{html.escape(label)}</text>')
    for s in series:
        pts = " ".join(f"{x(a):.1f},{y(min(max(b, ylim[0]), ylim[1])):.1f}" for a, b in zip(s["x"], s["y"]))
        o.append(f'<polyline class="{s["cls"]}" points="{pts}" style="stroke-width:{s.get("width", 2)}"/>')
        if s.get("dots"):
            for a, b in zip(s["x"], s["y"]):
                o.append(f'<circle class="dot {s["cls"]}" cx="{x(a):.1f}" cy="{y(b):.1f}" r="4"/>')
    o.append(f'<text class="label" x="{(PAD["l"] + W - PAD["r"]) / 2}" y="{height - 6}" text-anchor="middle">{html.escape(xlabel)}</text>')
    o.append(f'<text class="label" transform="translate(14,{(PAD["t"] + height - PAD["b"]) / 2}) rotate(-90)" text-anchor="middle">{html.escape(ylabel)}</text>')
    o.append(f'<g class="hover" visibility="hidden"><line class="cross" y1="{PAD["t"]}" y2="{height - PAD["b"]}"/></g>')
    o.append("</svg>")
    if tip:  # hover data: pixel x + tooltip text, nearest-x lookup in JS
        xs, texts = tip
        o.append(f'<script type="application/json" data-for="{cid}">'
                 + json.dumps([[round(x(a), 1), t] for a, t in zip(xs, texts)]) + "</script>")
    return "\n".join(o)


def legend(items):
    return '<div class="legend">' + "".join(
        f'<span><i class="sw {c}"></i>{html.escape(t)}</span>' for c, t in items) + "</div>"


# ------------------------------------------------------------------ build

def main():
    runs = sorted(p for p in (ROOT / "results").iterdir() if (p / "meta.json").exists() and "quick" not in p.name)
    run = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else runs[-1]
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "study" / "redis-study.html"
    out.parent.mkdir(exist_ok=True)
    meta = json.loads((run / "meta.json").read_text())
    rd = lambda n: pd.read_csv(run / n)

    # --- scaling
    sc = rd("scaling.csv")
    sc_chart = chart("c-scale", [dict(x=sc["keys"], y=sc.server_usec_per_call, cls="s1", dots=True)],
                     (800, 6e6), (0, 2), [1e3, 1e4, 1e5, 1e6, 5e6], [0, 0.5, 1, 1.5, 2],
                     "keys in the database (log scale)", "µs per GET inside Redis", xlog=True,
                     yfmt=lambda v: f"{v:g}", height=230,
                     tip=(sc["keys"], [f"{k:,} keys · {u} µs server · {c} µs client p50"
                                       for k, u, c in zip(sc["keys"], sc.server_usec_per_call, sc.client_p50_us)]))

    # --- rehash
    rh = rd("rehash.csv")
    rh["median"] = rh.server_batch_us.rolling(51, center=True, min_periods=5).median()
    on = rh.rehashing_overhead_bytes > 0
    bands, periods = [], []
    for _, g in rh[on].groupby((on != on.shift()).cumsum()[on]):
        bands.append((g.keys_after.iloc[0] - 1000, g.keys_after.iloc[-1]))
        periods.append(g)
    big = [g for g in periods if g.keys_after.iloc[0] > 100_000]
    in_rh = rh.index.isin(pd.concat(periods).index)
    med_in, med_out = rh.loc[in_rh, "server_batch_us"].median(), rh.loc[~in_rh, "server_batch_us"].median()
    step = rh.iloc[::4]
    rh_chart = chart("c-rehash",
                     [dict(x=rh.keys_after, y=rh.server_batch_us, cls="s1 faint", width=1),
                      dict(x=rh.keys_after, y=rh["median"], cls="s2", width=2)],
                     (0, rh.keys_after.max()), (200, 5000), [0, 1e6, 2e6, 3e6, 4e6], [250, 500, 1000, 2000, 4000],
                     "keys in the database", "µs of server time per 1,000 SETs", ylog=True, bands=bands,
                     vlines=[(2**20, "2²⁰"), (2**21, "2²¹"), (2**22, "2²²")],
                     tip=(step.keys_after, [f"{k:,} keys · {u:,} µs" + (" · rehashing" if r > 0 else "")
                                            for k, u, r in zip(step.keys_after, step.server_batch_us, step.rehashing_overhead_bytes)]))
    rh_rows = "".join(
        f"<tr><td>2<sup>{round(math.log2(g.keys_after.iloc[0]))}</sup></td><td>{int(g.keys_after.iloc[0]):,}</td>"
        f"<td>{int(g.keys_after.iloc[-1] - g.keys_after.iloc[0] + 1000):,}</td>"
        f"<td>{g.rehashing_overhead_bytes.max() / 2**20:.0f} MB</td></tr>" for g in big)
    htstats = (run / "rehash_htstats.txt").read_text().strip() if (run / "rehash_htstats.txt").exists() else ""

    # --- encoding
    en = rd("encoding.csv")
    hs = en[en["type"] == "hash"].sort_values("elements")
    lp, ht = hs[hs.encoding == "listpack"], hs[hs.encoding != "listpack"]
    enc_chart = chart("c-enc", [dict(x=lp.elements, y=lp.server_usec_per_call, cls="s1"),
                                dict(x=ht.elements, y=ht.server_usec_per_call, cls="s3")],
                      (0, 1030), (0, 6), [0, 128, 256, 384, 512, 640, 768, 896, 1024], [0, 1, 2, 3, 4, 5, 6],
                      "fields in the hash", "µs per HGET inside Redis", vlines=[(512.5, "limit: 512")],
                      xfmt=lambda v: f"{v:g}", yfmt=lambda v: f"{v:g}",
                      tip=(hs.elements, [f"{n} fields · {e} · {u} µs · {m:,} B"
                                         for n, e, u, m in zip(hs.elements, hs.encoding, hs.server_usec_per_call, hs.memory_bytes)]))
    hb, ha = lp.iloc[-1], ht.iloc[0]
    enc_rows = ""
    for t, name in (("hash", "Hash"), ("set_int", "Set of integers"), ("zset", "Sorted set")):
        d = en[en["type"] == t].sort_values("elements").reset_index(drop=True)
        i = d.index[d.encoding != d.encoding.iloc[0]][0]
        b, a = d.iloc[i - 1], d.iloc[i]
        enc_rows += (f"<tr><td>{name}</td><td><code>{b.encoding}</code> → <code>{a.encoding}</code></td>"
                     f"<td>{int(a.elements)}</td><td>{b.memory_bytes / b.elements:.1f} → {a.memory_bytes / a.elements:.1f}</td>"
                     f"<td>{a.memory_bytes / b.memory_bytes:.1f}×</td><td>{b.server_usec_per_call} → {a.server_usec_per_call}</td></tr>")

    # --- blocking
    tl, ev = rd("blocking_timeline.csv"), rd("blocking_events.csv")
    base = tl.get_us.median()
    bl = []
    for _, e in ev.iterrows():
        w = tl[(tl.t_ms >= e.start_ms - 1) & (tl.t_ms <= e.end_ms + 1)]
        bl.append((e.event, e.server_ms, w.get_us.max() / 1000))
    mx = max(b[2] for b in bl)
    bl_rows = "".join(
        f'<div class="bar-row"><span class="bar-name">{html.escape(n)}</span>'
        f'<span class="bar-track"><span class="bar" style="width:{max(0.6, v / mx * 100):.2f}%"></span></span>'
        f'<span class="bar-val">{v:,.1f} ms</span></div>' for n, _, v in bl)

    # --- pipeline
    pp = rd("pipeline.csv")
    pp_chart = chart("c-pipe", [dict(x=pp.depth, y=pp.ops_per_sec, cls="s1", dots=True)],
                     (0.8, 1300), (0, 120_000), [1, 10, 100, 1000], [0, 30_000, 60_000, 90_000, 120_000],
                     "commands per round trip (log scale)", "GETs per second", xlog=True, height=230,
                     tip=(pp.depth, [f"depth {d:,} · {o:,} ops/s" for d, o in zip(pp.depth, pp.ops_per_sec)]))

    # --- expiry
    ex = rd("expiry.csv")
    exw = ex[ex.t_s >= -2]
    n_exp = int(ex.dbsize.max())
    t_at = lambda f: exw[exw.expired_keys >= f * n_exp].t_s.iloc[0]
    t50, t100 = t_at(0.5), t_at(1.0)
    ex_chart = chart("c-exp", [dict(x=exw.t_s, y=exw.dbsize, cls="s1")],
                     (-2, exw.t_s.max()), (0, 1.1e6), [-2, 0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20],
                     [0, 250_000, 500_000, 750_000, 1_000_000], "seconds after every key expired",
                     "keys still in memory", vlines=[(0, "all keys expire")], xfmt=lambda v: f"{v:g}s", height=250,
                     tip=(exw.t_s, [f"t = {t:+.2f}s · {d:,} keys · {m} MB" for t, d, m in zip(exw.t_s, exw.dbsize, exw.used_memory_mb)]))

    conf = (ROOT / "redis-experiments.conf").read_text().strip()
    page = TEMPLATE
    subs = {
        "SC_CHART": sc_chart, "RH_CHART": rh_chart, "RH_ROWS": rh_rows, "HTSTATS": html.escape(htstats),
        "ENC_CHART": enc_chart, "ENC_ROWS": enc_rows, "BL_ROWS": bl_rows, "PP_CHART": pp_chart, "EX_CHART": ex_chart,
        "CONF": html.escape(conf), "SHA": meta.get("redis_git_sha1") or "", "RUN": meta["run_id"],
        "STARTED": meta["started"].replace("T", " "),
        "GET_MIN": f"{sc.server_usec_per_call.min():.2f}", "GET_MAX": f"{sc.server_usec_per_call.max():.2f}",
        "RT_X": f"{(sc.client_p50_us / sc.server_usec_per_call).median():.0f}",
        "CLIENT_P50": f"{sc.client_p50_us.median():.0f}", "MAXKEYS": fmt_num(sc["keys"].max()),
        "MEM_PER_KEY": f"{sc.used_memory_mb.max() * 2**20 / sc['keys'].max():.0f}",
        "MED_IN": f"{med_in:,.0f}", "MED_OUT": f"{med_out:,.0f}", "RH_RATIO": f"{med_in / med_out:.1f}",
        "N_RH": str(len(periods)), "KEYS_MS": f"{bl[0][2]:.0f}", "DEL_MS": f"{bl[2][2]:.0f}",
        "SCAN_MS": f"{bl[1][2]:.1f}", "UNLINK_MS": f"{bl[3][2]:.1f}", "BASE_US": f"{base:.0f}",
        "PP_1": f"{pp.ops_per_sec.iloc[0]:,}", "PP_MAX": f"{pp.ops_per_sec.max():,}",
        "PP_X": f"{pp.ops_per_sec.max() / pp.ops_per_sec.iloc[0]:.1f}",
        "LEGEND_RH": legend([("s1", "server time per batch"), ("s2", "rolling median"), ("band", "rehash in progress")]),
        "LEGEND_ENC": legend([("s1", "listpack (packed array)"), ("s3", "hashtable")]),
        "RH_LASTED": f"{pd.Series([(g.keys_after.iloc[-1] - g.keys_after.iloc[0] + 1000) / 2**round(math.log2(g.keys_after.iloc[0])) for g in big[:-1]]).median():.2f}",
        "SCAN_SERVER": f"{bl[1][1]:.0f}",
        "H_LAST": str(int(hb.elements)), "H_AT": str(int(ha.elements)), "H_US_B": f"{hb.server_usec_per_call:.1f}",
        "H_US_A": f"{ha.server_usec_per_call:.1f}", "H_MEMX": f"{ha.memory_bytes / hb.memory_bytes:.1f}",
        "T50": f"{t50:.1f}", "T100": f"{t100:.1f}", "EX_MEM": f"{ex.used_memory_mb.max():.0f}",
    }
    for k, v in subs.items():
        page = page.replace("{{" + k + "}}", v)
    out.write_text(page)
    print(f"wrote {out}")


TEMPLATE = (Path(__file__).parent / "study_template.html").read_text() if __name__ == "__main__" else ""

if __name__ == "__main__":
    main()
