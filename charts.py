"""Plotly figures for the dashboard.

Colour roles (validated reference palette, light surface):
  blue   = traffic served by the primary data center, and the single-hue ramp for magnitude
  orange = traffic served by the DR site
  red    = lost transactions / failed elements (always paired with a label or marker shape)
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional

import plotly.graph_objects as go

from engine.simulator import SimResult, percentile
from engine.topology import Topology

BLUE, ORANGE = "#2a78d6", "#eb6834"
BLUE_L, BLUE_D, BLUE_XD = "#86b6ef", "#1c5cab", "#0d366b"
CRIT, GOOD = "#d03b3b", "#0ca30c"
NAVY, GREY = "#12324A", "#7A8794"
INK, INK2, LINE, GRID = "#15202B", "#4A5866", "#DDE3E9", "#E6EBF0"
FONT = "-apple-system, 'SF Pro Text', 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"

CAUSE_LABELS = {
    "element_down": "Sent into a failed link or device",
    "no_route": "No route to any server",
    "random_loss": "Random line loss",
    "queue_overflow": "Queue full (congestion)",
}


def _style(fig: go.Figure, height: int, title: Optional[str] = None, legend: bool = True) -> go.Figure:
    fig.update_layout(
        height=height,
        margin=dict(l=52, r=16, t=44 if title else 20, b=44),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family=FONT, size=12, color=INK2),
        title=dict(text=title, x=0, xanchor="left", font=dict(size=14, color=INK)) if title else None,
        showlegend=legend,
        legend=dict(orientation="h", x=1, xanchor="right", y=1.14, yanchor="bottom", font=dict(size=11)),
        hoverlabel=dict(bgcolor="white", font=dict(color=INK, size=12), bordercolor=LINE),
    )
    fig.update_xaxes(showgrid=False, zeroline=False, linecolor=LINE, ticks="outside", tickcolor=LINE)
    fig.update_yaxes(gridcolor=GRID, zeroline=False, linecolor="rgba(0,0,0,0)")
    return fig


def _empty(fig: go.Figure, text: str) -> go.Figure:
    fig.add_annotation(text=text, xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False,
                       font=dict(size=13, color=INK2))
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    return fig


def _failure_marks(fig: go.Figure, res: SimResult) -> None:
    groups: Dict[tuple, List[str]] = {}
    for f in res.settings.get("failures") or []:
        groups.setdefault((f["t_fail"], f["t_restore"]), []).append(f["target"])
    for (t_fail, t_restore), targets in groups.items():
        fig.add_vline(x=t_fail, line_dash="dash", line_color=CRIT, line_width=1.5,
                      annotation_text="✕ down: " + ", ".join(targets), annotation_position="top left",
                      annotation_font=dict(size=11, color=CRIT))
        if t_restore is not None:
            fig.add_vline(x=t_restore, line_dash="dot", line_color=GOOD, line_width=1.5,
                          annotation_text="✓ restored", annotation_position="top right",
                          annotation_font=dict(size=11, color=GOOD))


# ------------------------------------------------------------------ live charts
def latency_figure(res: SimResult, upto: Optional[float] = None) -> go.Figure:
    fig = go.Figure()
    idx = [i for i, t in enumerate(res.t_end) if upto is None or t <= upto]
    okx = [res.t_end[i] for i in idx if res.ok[i]]
    oky = [res.latency_ms[i] for i in idx if res.ok[i]]
    bx = [res.t_end[i] for i in idx if not res.ok[i]]
    by = [res.latency_ms[i] for i in idx if not res.ok[i]]
    fig.add_trace(go.Scatter(x=okx, y=oky, mode="markers", name="Completed transaction",
                             marker=dict(size=5, color=BLUE, opacity=0.45),
                             hovertemplate="t=%{x:.1f} s · %{y:.0f} ms<extra>Completed</extra>"))
    if bx:
        fig.add_trace(go.Scatter(x=bx, y=by, mode="markers", name="Lost transaction",
                                 marker=dict(size=8, color=CRIT, symbol="x"),
                                 hovertemplate="t=%{x:.1f} s · gave up after %{y:.0f} ms<extra>Lost</extra>"))
    ts = res.timeseries(5.0)
    half = ts["bucket_s"] / 2
    px = [t for t, v in zip(ts["t"], ts["p95_ms"]) if v is not None and (upto is None or t + half <= upto)]
    py = [v for t, v in zip(ts["t"], ts["p95_ms"]) if v is not None and (upto is None or t + half <= upto)]
    if px:
        fig.add_trace(go.Scatter(x=px, y=py, mode="lines", name="p95 per 5 s",
                                 line=dict(color=BLUE_XD, width=2.5),
                                 hovertemplate="p95 %{y:.0f} ms<extra></extra>"))
    _failure_marks(fig, res)
    top = max(2000.0, (max(res.latency_ms) if res.latency_ms else 0.0) * 1.5)
    _style(fig, 300, "Transaction latency")
    fig.update_xaxes(range=[0, res.end_s], title_text="Simulated time (s)")
    fig.update_yaxes(type="log", range=[1, math.log10(top)], title_text="Latency (ms, log scale)")
    return fig


def throughput_figure(res: SimResult, upto: Optional[float] = None) -> go.Figure:
    fig = go.Figure()
    ts = res.timeseries(2.0)
    half = ts["bucket_s"] / 2
    keep = [i for i, t in enumerate(ts["t"]) if upto is None or t + half <= upto]
    x = [ts["t"][i] for i in keep]
    fig.add_trace(go.Bar(x=x, y=[ts["tps"][i] for i in keep], name="Completed per second", marker_color=BLUE,
                         hovertemplate="%{y:.1f} /s<extra>Completed</extra>"))
    fig.add_trace(go.Bar(x=x, y=[ts["fail_ps"][i] for i in keep], name="Lost per second", marker_color=CRIT,
                         hovertemplate="%{y:.1f} /s<extra>Lost</extra>"))
    fig.update_layout(barmode="stack", bargap=0.15)
    _failure_marks(fig, res)
    _style(fig, 260, "Throughput")
    fig.update_xaxes(range=[0, res.end_s], title_text="Simulated time (s)")
    fig.update_yaxes(title_text="Transactions per second", rangemode="tozero")
    return fig


# --------------------------------------------------------------- statistics tab
def histogram_figure(res: SimResult) -> go.Figure:
    fig = go.Figure()
    lat = sorted(res.latency_ms[i] for i in range(len(res.ok)) if res.ok[i])
    if not lat:
        return _style(_empty(fig, "No completed transactions"), 300, "Latency distribution")
    p95, p99 = percentile(lat, 95), percentile(lat, 99)
    shown = [v for v in lat if v <= p99]
    fig.add_trace(go.Histogram(x=shown, nbinsx=40, marker=dict(color=BLUE, line=dict(color="white", width=1)),
                               name="Transactions", hovertemplate="%{x:.0f} ms · %{y} transactions<extra></extra>"))
    fig.add_vline(x=p95, line_dash="dash", line_color=BLUE_XD, annotation_text=f"p95 {p95:.0f} ms",
                  annotation_position="top right", annotation_font=dict(size=11, color=INK))
    _style(fig, 300, "Latency distribution (fastest 99%)", legend=False)
    fig.update_xaxes(title_text="Latency (ms)")
    fig.update_yaxes(title_text="Transactions")
    return fig


def site_figure(res: SimResult, topo: Topology) -> go.Figure:
    fig = go.Figure()
    per = res.by_source()
    names = [topo.nodes[s].label for s in per]
    fig.add_trace(go.Bar(y=names, x=[per[s]["avg_ms"] for s in per], orientation="h", name="Average",
                         marker_color=BLUE_L, hovertemplate="%{x:.0f} ms<extra>Average</extra>"))
    fig.add_trace(go.Bar(y=names, x=[per[s]["p95_ms"] for s in per], orientation="h", name="95th percentile",
                         marker_color=BLUE_D, texttemplate="%{x:.0f}", textposition="outside",
                         hovertemplate="%{x:.0f} ms<extra>p95</extra>"))
    fig.update_layout(barmode="group", bargap=0.35)
    _style(fig, 300, "Latency by site (ms)")
    fig.update_yaxes(autorange="reversed", gridcolor="rgba(0,0,0,0)")
    fig.update_xaxes(showgrid=True, gridcolor=GRID, rangemode="tozero")
    return fig


def utilization_figure(res: SimResult, topo: Topology) -> go.Figure:
    fig = go.Figure()
    wan = [(lid, st) for lid, st in res.link_stats.items() if st["kind"] == "wan"]
    wan.sort(key=lambda kv: kv[1]["util_peak_pct"], reverse=True)
    wan = wan[:12]
    if not wan:
        return _style(_empty(fig, "No WAN links"), 300, "WAN link utilization")
    names = [lid.replace("~", " – ") for lid, _ in wan]
    fig.add_trace(go.Bar(y=names, x=[st["util_mean_pct"] for _, st in wan], orientation="h", name="Average",
                         marker_color=BLUE, hovertemplate="%{x:.1f}%<extra>Average</extra>"))
    fig.add_trace(go.Scatter(y=names, x=[st["util_peak_pct"] for _, st in wan], mode="markers", name="Busiest second",
                             marker=dict(symbol="diamond", size=9, color=BLUE_XD, line=dict(color="white", width=1.5)),
                             hovertemplate="%{x:.1f}%<extra>Busiest second</extra>"))
    fig.add_vline(x=80, line_dash="dash", line_color=CRIT, annotation_text="80% limit",
                  annotation_position="bottom right", annotation_font=dict(size=11, color=CRIT))
    _style(fig, 340, "WAN link utilization (includes background traffic)")
    fig.update_xaxes(range=[0, 105], ticksuffix="%", showgrid=True, gridcolor=GRID)
    fig.update_yaxes(autorange="reversed", gridcolor="rgba(0,0,0,0)", tickfont=dict(size=11))
    return fig


def drops_figure(res: SimResult) -> go.Figure:
    fig = go.Figure()
    items = sorted(res.drops.items(), key=lambda kv: kv[1], reverse=True)
    if not items:
        return _style(_empty(fig, "No packets were dropped"), 260, "Dropped packets by cause", legend=False)
    fig.add_trace(go.Bar(y=[CAUSE_LABELS.get(k, k) for k, _ in items], x=[v for _, v in items], orientation="h",
                         marker_color=BLUE, texttemplate="%{x:,}", textposition="outside",
                         hovertemplate="%{x:,} packets<extra></extra>"))
    _style(fig, 260, "Dropped packets by cause", legend=False)
    fig.update_yaxes(autorange="reversed", gridcolor="rgba(0,0,0,0)")
    fig.update_xaxes(showgrid=True, gridcolor=GRID, rangemode="tozero")
    return fig


def share_figure(res: SimResult) -> go.Figure:
    fig = go.Figure()
    ts = res.timeseries(5.0)
    fig.add_trace(go.Bar(x=ts["t"], y=ts["dc"], name="Primary data center", marker_color=BLUE,
                         hovertemplate="%{y} transactions<extra>Primary DC</extra>"))
    fig.add_trace(go.Bar(x=ts["t"], y=ts["dr"], name="DR site", marker_color=ORANGE,
                         hovertemplate="%{y} transactions<extra>DR site</extra>"))
    fig.update_layout(barmode="stack", bargap=0.1)
    _failure_marks(fig, res)
    _style(fig, 260, "Which site served the transactions (per 5 s)")
    fig.update_xaxes(range=[0, res.end_s], title_text="Simulated time (s)")
    fig.update_yaxes(title_text="Transactions", rangemode="tozero")
    return fig


# ---------------------------------------------------------------------- topology
_UTIL_BUCKETS = [
    ("WAN link, under 40% busy", 0, 40, BLUE_L),
    ("WAN link, 40-60% busy", 40, 60, BLUE),
    ("WAN link, 60-80% busy", 60, 80, BLUE_D),
    ("WAN link, 80%+ busy", 80, 1e9, BLUE_XD),
]
_SITE_COLOR = {"DC": NAVY, "DR": ORANGE, "WAN": GREY}
_SYMBOL = {"source": "square", "router": "circle", "switch": "circle", "firewall": "hexagon",
           "isp": "diamond", "server": "square"}
_SIZE = {"source": 22, "router": 17, "switch": 14, "firewall": 18, "isp": 22, "server": 19}


def topology_figure(topo: Topology, res: Optional[SimResult] = None) -> go.Figure:
    fails = (res.settings.get("failures") if res else None) or []
    down_links = {f["target"] for f in fails if f["kind"] == "link"}
    down_nodes = {f["target"] for f in fails if f["kind"] == "node"}

    def is_down(link) -> bool:
        return link.id in down_links or link.a in down_nodes or link.b in down_nodes

    fig = go.Figure()
    fig.update_layout(shapes=[
        dict(type="rect", xref="x", yref="y", x0=67, x1=115, y0=26, y1=63, layer="below",
             fillcolor="rgba(18,50,74,0.05)", line=dict(color="rgba(18,50,74,0.28)", width=1)),
        dict(type="rect", xref="x", yref="y", x0=67, x1=115, y0=0, y1=16, layer="below",
             fillcolor="rgba(235,104,52,0.07)", line=dict(color="rgba(235,104,52,0.40)", width=1)),
    ], annotations=[
        dict(x=68, y=61.5, xref="x", yref="y", text="Primary data center", showarrow=False, xanchor="left",
             font=dict(size=12, color=NAVY)),
        dict(x=68, y=14.5, xref="x", yref="y", text="DR site", showarrow=False, xanchor="left",
             font=dict(size=12, color="#b4430f")),
    ])

    styles: Dict[str, dict] = {}

    def style_for(link):
        if is_down(link):
            return "down", "Failed link or device", CRIT, 3.5, "dash"
        if link.kind == "lan":
            return "lan", "LAN link", "#9AA7B4", 1.6, "solid"
        if res is None:
            return "wan", "WAN link (IPsec)", BLUE, 3, "solid"
        peak = res.link_stats[link.id]["util_peak_pct"]
        for label, lo, hi, color in _UTIL_BUCKETS:
            if lo <= peak < hi:
                return label, label, color, 3, "solid"
        return "wan", "WAN link", BLUE, 3, "solid"

    for link in topo.links.values():
        key, name, color, width, dash = style_for(link)
        st = styles.setdefault(key, dict(name=name, color=color, width=width, dash=dash, x=[], y=[]))
        a, b = topo.nodes[link.a], topo.nodes[link.b]
        st["x"] += [a.x, b.x, None]
        st["y"] += [a.y, b.y, None]
    order = ["lan", "down"] + [b[0] for b in _UTIL_BUCKETS] + ["wan"]
    for key in order:
        st = styles.get(key)
        if st:
            fig.add_trace(go.Scatter(x=st["x"], y=st["y"], mode="lines", name=st["name"], hoverinfo="skip",
                                     line=dict(color=st["color"], width=st["width"], dash=st["dash"])))

    mx, my, mt = [], [], []
    for link in topo.links.values():
        a, b = topo.nodes[link.a], topo.nodes[link.b]
        lines = [f"<b>{link.label or link.id}</b>", f"{a.label} – {b.label}",
                 f"{link.lat_ms:g} ms · {link.bw_mbps:g} Mbps · loss {link.loss_pct:g}%",
                 "IPsec encrypted" if link.encrypted else "Not encrypted (inside LAN)" if link.kind == "lan" else "NOT encrypted"]
        if res:
            st = res.link_stats[link.id]
            lines.append(f"busy {st['util_mean_pct']:.0f}% avg, {st['util_peak_pct']:.0f}% peak · {st['bytes'] / 1e3:,.0f} KB carried")
        if is_down(link):
            lines.append("<b>✕ FAILED in this run</b>")
        mx.append((a.x + b.x) / 2)
        my.append((a.y + b.y) / 2)
        mt.append("<br>".join(lines))
    fig.add_trace(go.Scatter(x=mx, y=my, mode="markers", showlegend=False, hoverinfo="text", hovertext=mt,
                             marker=dict(size=14, color="rgba(0,0,0,0)")))

    nodes = list(topo.nodes.values())
    per = res.by_source() if res else {}
    texts = []
    for n in nodes:
        t = [f"<b>{n.label}</b>", f"{n.role}, zone {n.zone}"]
        if n.id in per:
            p = per[n.id]
            t.append(f"{p['ok']:,} done · {p['failed']} lost · avg {p['avg_ms']:.0f} ms · p95 {p['p95_ms']:.0f} ms")
        if n.id in down_nodes:
            t.append("<b>✕ FAILED in this run</b>")
        texts.append("<br>".join(t))
    fig.add_trace(go.Scatter(
        x=[n.x for n in nodes], y=[n.y for n in nodes], mode="markers+text", showlegend=False,
        text=[n.label for n in nodes], textposition="bottom center", textfont=dict(size=11, color=INK2),
        customdata=[n.id for n in nodes], hoverinfo="text", hovertext=texts,
        marker=dict(symbol=[_SYMBOL.get(n.role, "circle") for n in nodes],
                    size=[_SIZE.get(n.role, 16) for n in nodes],
                    color=[CRIT if n.id in down_nodes else _SITE_COLOR.get(n.site, BLUE) for n in nodes],
                    line=dict(color="white", width=2))))

    for label, color in (("Branch / ATM site", BLUE), ("Internet service provider", GREY),
                         ("Data center", NAVY), ("DR site", ORANGE)):
        fig.add_trace(go.Scatter(x=[None], y=[None], mode="markers", name=label,
                                 marker=dict(size=11, color=color, symbol="square")))

    _style(fig, 640)
    fig.update_layout(margin=dict(l=8, r=8, t=8, b=8), hovermode="closest",
                      legend=dict(orientation="h", x=0, xanchor="left", y=-0.02, yanchor="top", font=dict(size=11)))
    fig.update_xaxes(visible=False, range=[-6, 117])
    fig.update_yaxes(visible=False, range=[-6, 64])
    return fig
