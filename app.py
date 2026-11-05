"""Banking & ATM network operations console (Plotly Dash).

Run:  pip install -r requirements.txt && python app.py   then open http://127.0.0.1:8050

Tabs: Topology (clickable map) | Simulation (scenarios + live replay) |
Statistics | Test cases (run + CSV report) | Config audit (uploaded running-configs).
The network is modelled in ``data/topology.json``; the engine lives in ``engine/``.
"""
from __future__ import annotations

import base64
import csv
import io
import os
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Dict, List, Optional

from dash import Dash, Input, Output, State, ctx, dcc, html, no_update
from dash.exceptions import PreventUpdate

import charts
from engine import tests_suite
from engine.audit import audit_all
from engine.scenarios import FAILURES, PRESETS, make_failures, preset_controls
from engine.simulator import Settings, SimResult, run_simulation
from engine.topology import get_topology

ROOT = Path(__file__).resolve().parent
TOPO = get_topology()
TERMINALS = sum(TOPO.nodes[s].terminals for s in TOPO.sources)
GRAPH_CFG = {"displayModeBar": False, "responsive": True}

# --------------------------------------------------------------------- run store
RUNS: "OrderedDict[str, SimResult]" = OrderedDict()
RUNS["baseline"] = run_simulation(Settings(), TOPO)
BASELINE_LABEL = "Normal day, no failures"


def _keep(res: SimResult) -> str:
    rid = uuid.uuid4().hex[:8]
    RUNS[rid] = res
    while len(RUNS) > 13:
        for k in RUNS:
            if k != "baseline":
                RUNS.pop(k)
                break
    return rid


def _get(run: Optional[dict]) -> SimResult:
    rid = (run or {}).get("run_id", "baseline")
    return RUNS.get(rid) or RUNS["baseline"]


def _num(v, default: float) -> float:
    try:
        return default if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------ small builders
def kpi_strip(s: dict, include_failover: bool = False) -> html.Div:
    good = s["failed"] == 0
    items = [
        ("Transactions", f"{s['n']:,}", ""),
        ("Completed", f"{'✓' if good else '✕'} {s['success_pct']:.2f}%", "good" if good else "bad"),
        ("Average latency", f"{s['avg_ms']:.0f} ms", ""),
        ("95th percentile", f"{s['p95_ms']:.0f} ms", ""),
        ("Throughput", f"{s['tps']:.1f} / s", ""),
        ("Needed a retry", f"{s['retried']:,}", ""),
        ("Served by DR site", f"{s['dr_served']:,}", ""),
    ]
    if include_failover and s.get("failover_s") is not None:
        items.append(("Failover window", f"{s['failover_s']:.1f} s", ""))
    return html.Div(
        [html.Div([html.Span(v, className="v"), html.Span(label, className="l")], className=f"strip-item {cls}")
         for label, v, cls in items],
        className="strip",
    )


def field(label: str, comp) -> html.Div:
    return html.Div([html.Label(label), comp], className="field")


def number(id_: str, value, mn=None, mx=None, step=None, placeholder=None) -> dcc.Input:
    return dcc.Input(id=id_, type="number", value=value, min=mn, max=mx, step=step, placeholder=placeholder)


def label_for(key: str, tfail: float, trestore: Optional[float], mult: float, detect: float) -> str:
    parts = []
    if key != "none":
        name = FAILURES[key][0]
        parts.append(f"{name} at {tfail:g} s" + (f", restored at {trestore:g} s" if trestore else ""))
    else:
        parts.append("No failure")
    if mult != 1:
        parts.append(f"{mult:g}x traffic")
    if key != "none":
        parts.append(f"detection {detect:g} s")
    return " · ".join(parts)


# ---------------------------------------------------------------------------- layout
PANELS = [("topology", "Topology"), ("sim", "Simulation"), ("stats", "Statistics"),
          ("tests", "Test cases"), ("audit", "Config audit")]

controls = html.Div(className="card controls", children=[
    html.H2("Scenario"),
    html.P(f"{TERMINALS} terminals across {len(TOPO.sources)} sites. Pick a preset or change any value, then run.", className="hint"),
    field("Preset", dcc.Dropdown(id="preset", clearable=False, value="normal",
                                 options=[{"label": v[0], "value": k} for k, v in PRESETS.items()])),
    html.Div(className="group", children=[
        html.H3("Traffic"),
        field("Transactions per minute, per terminal", number("in-rate", 12, 0, 120, 1)),
        field("Load multiplier (1 = normal)", number("in-mult", 1, 0.1, 20, 0.5)),
        html.Div(className="row", children=[
            field("Request size (KB)", number("in-req", 0.6, 0.1, 500, 0.1)),
            field("Response size (KB)", number("in-resp", 0.6, 0.1, 500, 0.1)),
        ]),
    ]),
    html.Div(className="group", children=[
        html.H3("Network"),
        field("Other traffic already on each WAN link (%)", number("in-bg", 30, 0, 95, 5)),
        field("Force packet loss on every WAN hop (%)", number("in-loss", None, 0, 50, 0.5, placeholder="use link defaults")),
        html.Div(className="field", children=[
            dcc.Checklist(id="in-ipsec", className="check", value=["on"],
                          options=[{"label": " IPsec on WAN links (adds 70 B + 0.4 ms per hop)", "value": "on"}])]),
    ]),
    html.Div(className="group", children=[
        html.H3("Failure"),
        field("What fails", dcc.Dropdown(id="in-failure", clearable=False, value="none",
                                         options=[{"label": v[0], "value": k} for k, v in FAILURES.items()])),
        html.Div(className="row", children=[
            field("Fails at (s)", number("in-tfail", 30, 0, 600, 5)),
            field("Restored at (s)", number("in-trestore", None, 1, 600, 5, placeholder="never")),
        ]),
        field("Routing detects failure after (s)", number("in-detect", 5, 0, 120, 1)),
    ]),
    html.Div(className="group", children=[
        html.H3("Terminal retries"),
        html.Div(className="row", children=[
            field("First timeout (s)", number("in-timeout", 1.5, 0.1, 30, 0.1)),
            field("Max retries", number("in-retries", 5, 0, 10, 1)),
        ]),
    ]),
    html.Div(className="group", children=[
        html.Div(className="row", children=[
            field("Duration (s)", number("in-duration", 120, 10, 600, 10)),
            field("Random seed", number("in-seed", 42, 0, 99999, 1)),
        ]),
    ]),
    html.Div(className="btn-row", children=[html.Button("Run simulation", id="btn-run", className="btn btn-primary", n_clicks=0)]),
    html.Div(id="run-msg", className="msg", children="Showing the baseline run. Change the scenario and press Run."),
])

assumptions = html.Details(className="table-view", children=[
    html.Summary("What this simulation assumes"),
    html.Ul([
        html.Li("The map in data/topology.json is a template of the Packet Tracer design. Edit it so devices, links and speeds match your build."),
        html.Li("Routes follow the lowest-latency path, like OSPF or EIGRP would. After a failure, routes stay stale for the detection time, and packets sent into the dead link are lost."),
        html.Li("Each terminal retries with exponential backoff. Servers de-duplicate retries, so a retry never posts a transaction twice."),
        html.Li("If the primary servers are unreachable, traffic goes to the DR site, which is assumed to hold up-to-date data."),
        html.Li("Other traffic on each WAN link is a fixed share of its bandwidth; transactions queue behind it and are dropped if the queue exceeds 250 ms."),
        html.Li("Results are repeatable: the same seed gives the same numbers."),
    ]),
])

panel_topology = html.Div(id="panel-topology", className="panel", children=[
    html.Div(className="split", children=[
        html.Div(className="card", children=[
            html.H2("Network map"),
            html.P(id="topo-note", className="hint"),
            dcc.Graph(id="topo-graph", config=GRAPH_CFG, style={"height": "640px"},
                      figure=charts.topology_figure(TOPO, RUNS["baseline"])),
        ]),
        html.Div(className="stack", children=[
            html.Div(id="topo-detail", className="card"),
            html.Div(className="card", children=[
                html.H2("Reading the map"),
                html.P("Blue lines are WAN tunnels shaded by how busy they got in the last run. Red dashed lines are links or devices that failed in that run. Grey lines are LAN links. Hover a line or a device for numbers; click a device for its details.", className="hint"),
            ]),
        ]),
    ]),
])

panel_sim = html.Div(id="panel-sim", className="panel", children=[
    html.Div(className="split-sim", children=[
        controls,
        html.Div(className="stack", children=[
            html.Div(className="card", children=[
                html.Div(className="live-head", children=[
                    html.H2("Live replay"),
                    html.Div(id="live-status"),
                    html.Div(className="btn-row", children=[
                        html.Button("Replay", id="btn-replay", className="btn", n_clicks=0),
                        html.Button("Skip to end", id="btn-skip", className="btn", n_clicks=0),
                    ]),
                ]),
                html.Div(id="live-kpi"),
                dcc.Graph(id="live-latency", config=GRAPH_CFG, style={"height": "300px"}),
                dcc.Graph(id="live-tput", config=GRAPH_CFG, style={"height": "260px"}),
                assumptions,
            ]),
        ]),
    ]),
])

panel_stats = html.Div(id="panel-stats", className="panel", children=[
    html.Div(id="stats-kpi"),
    html.Div(className="grid-2", children=[
        html.Div(className="card", children=[dcc.Graph(id="stats-hist", config=GRAPH_CFG, style={"height": "300px"})]),
        html.Div(className="card", children=[dcc.Graph(id="stats-site", config=GRAPH_CFG, style={"height": "300px"})]),
        html.Div(className="card", children=[dcc.Graph(id="stats-util", config=GRAPH_CFG, style={"height": "340px"})]),
        html.Div(className="card", children=[
            dcc.Graph(id="stats-drops", config=GRAPH_CFG, style={"height": "260px"}),
            dcc.Graph(id="stats-share", config=GRAPH_CFG, style={"height": "260px"}),
        ]),
    ]),
    html.Div(className="card", style={"marginTop": "18px"}, children=[
        html.H2("Link table"),
        html.Div(id="stats-table"),
    ]),
])

panel_tests = html.Div(id="panel-tests", className="panel", children=[
    html.Div(className="card", children=[
        html.Div(className="live-head", children=[
            html.Div([
                html.H2("Test cases"),
                html.P("Each test runs the simulator or inspects the design, compares the result with a target, and shows the evidence.", className="hint"),
            ]),
            html.Div(className="btn-row", children=[
                html.Button("Run all tests", id="btn-tests", className="btn btn-primary", n_clicks=0),
                html.Button("Download report (CSV)", id="btn-tests-dl", className="btn", n_clicks=0),
            ]),
        ]),
        html.Div(id="tests-summary", className="summary-line"),
        dcc.Loading(type="default", children=html.Div(id="tests-body", children=html.P(
            "Press Run all tests. It takes a few seconds.", className="muted"))),
        dcc.Download(id="dl-tests"),
    ]),
])

panel_audit = html.Div(id="panel-audit", className="panel", children=[
    html.Div(className="stack", children=[
        html.Div(className="card", children=[
            html.H2("Config audit"),
            html.P("In Packet Tracer open each device's CLI, run  show running-config , and save the output as a .txt file. Drop the files here to check them against the design rules.", className="hint"),
            dcc.Upload(id="audit-upload", multiple=True, className="dropzone", accept=".txt,.cfg,.conf,.log",
                       children=html.Div(["Drop running-config files here, or ", html.A("choose files")])),
            html.Div(className="btn-row", style={"marginTop": "10px"}, children=[
                html.Button("Try with demo configs", id="btn-demo-cfg", className="btn", n_clicks=0),
                html.Span("Demo configs are made-up examples, not your Packet Tracer build.", className="muted"),
            ]),
        ]),
        html.Div(id="audit-body"),
    ]),
])

app = Dash(__name__, title="Banking & ATM Network Console", update_title=None)
server = app.server  # the WSGI object gunicorn serves in deployment
app.layout = html.Div([
    html.Header(className="masthead", children=[
        html.Div(className="masthead-row", children=[
            html.Div([
                html.H1("Banking & ATM Network"),
                html.P("Operations console for the Packet Tracer design: simulate, measure, test and audit.", className="sub"),
            ]),
            html.Div(id="run-chip", className="chip"),
        ]),
        dcc.RadioItems(id="nav", className="nav", value="topology", inline=True,
                       options=[{"label": label, "value": key} for key, label in PANELS]),
    ]),
    html.Main(className="stage", children=[panel_topology, panel_sim, panel_stats, panel_tests, panel_audit]),
    dcc.Store(id="run-store", data={"run_id": "baseline", "label": BASELINE_LABEL}),
    dcc.Store(id="playhead", data={"pos": 1.0}),
    dcc.Store(id="cfg-store", data={}),
    dcc.Store(id="tests-store", data=None),
    dcc.Interval(id="tick", interval=250, disabled=True),
])


# ----------------------------------------------------------------------- callbacks
@app.callback([Output(f"panel-{k}", "className") for k, _ in PANELS], Input("nav", "value"))
def show_panel(active):
    return ["panel" if k == active else "panel hidden" for k, _ in PANELS]


CONTROL_KEYS = ["rate", "mult", "req", "resp", "bg", "loss", "ipsec", "detect", "timeout", "retries",
                "failure", "tfail", "trestore", "duration"]


@app.callback([Output(f"in-{k}", "value") for k in CONTROL_KEYS], Input("preset", "value"), prevent_initial_call=True)
def apply_preset(key):
    c = preset_controls(key)
    return [(["on"] if c[k] else []) if k == "ipsec" else c[k] for k in CONTROL_KEYS]


@app.callback(
    [Output("run-store", "data"), Output("run-msg", "children"), Output("run-msg", "className")],
    Input("btn-run", "n_clicks"),
    [State(f"in-{k}", "value") for k in CONTROL_KEYS] + [State("in-seed", "value")],
    prevent_initial_call=True,
)
def run_sim(_n, *vals):
    c = dict(zip(CONTROL_KEYS + ["seed"], vals))
    duration = min(max(_num(c["duration"], 120), 10), 600)
    rate = max(_num(c["rate"], 12), 0)
    mult = min(max(_num(c["mult"], 1), 0.1), 20)
    estimate = TERMINALS * rate * mult / 60.0 * duration
    if estimate > 60000:
        return no_update, (f"That is about {estimate:,.0f} transactions, too many for a live run. "
                           "Lower the rate, the load multiplier or the duration."), "msg warn"
    key = c["failure"] if c["failure"] in FAILURES else "none"
    tfail = min(max(_num(c["tfail"], 30), 0), duration)
    trestore = None if c["trestore"] in (None, "") else max(_num(c["trestore"], 0), tfail + 1)
    loss = None if c["loss"] in (None, "") else min(max(_num(c["loss"], 0), 0), 50)
    detect = max(_num(c["detect"], 5), 0)
    settings = Settings(
        duration_s=duration, tx_per_min=rate, load_mult=mult,
        request_bytes=int(max(_num(c["req"], 0.6), 0.05) * 1000), response_bytes=int(max(_num(c["resp"], 0.6), 0.05) * 1000),
        background_load=min(max(_num(c["bg"], 30), 0), 95) / 100.0, ipsec=bool(c["ipsec"]),
        wan_loss_override_pct=loss, detect_s=detect, retry_timeout_s=max(_num(c["timeout"], 1.5), 0.1),
        max_retries=int(min(max(_num(c["retries"], 5), 0), 10)),
        failures=make_failures(key, tfail, trestore), seed=int(_num(c["seed"], 42)),
    )
    res = run_simulation(settings, TOPO)
    s = res.summary()
    rid = _keep(res)
    label = label_for(key, tfail, trestore, mult, detect)
    msg = (f"Finished: {s['n']:,} transactions, {s['failed']} lost, p95 {s['p95_ms']:.0f} ms. "
           "Replaying on the right; see Statistics and Topology for the full picture.")
    return {"run_id": rid, "label": label}, msg, ("msg warn" if s["failed"] else "msg")


@app.callback(
    [Output("playhead", "data"), Output("tick", "disabled")],
    [Input("tick", "n_intervals"), Input("run-store", "data"), Input("btn-replay", "n_clicks"), Input("btn-skip", "n_clicks")],
    State("playhead", "data"),
)
def playback(_tick, _run, _replay, _skip, head):
    trig = ctx.triggered_id
    if trig in ("run-store", "btn-replay"):
        return {"pos": 0.0}, False
    if trig == "btn-skip":
        return {"pos": 1.0}, True
    if trig == "tick":
        pos = min(1.0, float((head or {}).get("pos", 0.0)) + 0.03)
        return {"pos": pos}, pos >= 1.0
    return {"pos": 1.0}, True


@app.callback(
    [Output("live-kpi", "children"), Output("live-latency", "figure"), Output("live-tput", "figure"), Output("live-status", "children")],
    [Input("playhead", "data"), Input("run-store", "data")],
)
def render_live(head, run):
    res = _get(run)
    pos = float((head or {}).get("pos", 1.0))
    finished = pos >= 1.0
    upto = None if finished else pos * res.end_s
    t_now = res.end_s if finished else upto
    down = [f for f in (res.settings.get("failures") or [])
            if f["t_fail"] <= t_now and (f["t_restore"] is None or t_now < f["t_restore"])]
    if down:
        pill = html.Span("✕ Down: " + ", ".join(f["target"] for f in down), className="status-pill down")
    elif res.settings.get("failures") and finished:
        pill = html.Span("✓ All links up at end of run", className="status-pill up")
    else:
        pill = html.Span("✓ All links up", className="status-pill up")
    status = html.Div([html.Span(f"t = {t_now:.0f} s of {res.end_s:.0f} s  ", className="muted"), pill])
    return (kpi_strip(res.summary(upto)), charts.latency_figure(res, upto), charts.throughput_figure(res, upto), status)


@app.callback(
    [Output("stats-kpi", "children"), Output("stats-hist", "figure"), Output("stats-site", "figure"),
     Output("stats-util", "figure"), Output("stats-drops", "figure"), Output("stats-share", "figure"),
     Output("stats-table", "children")],
    Input("run-store", "data"),
)
def render_stats(run):
    res = _get(run)
    rows = [html.Tr([html.Td(lid.replace("~", " – "), className="mono"), html.Td(st["kind"].upper()),
                     html.Td(f"{st['util_mean_pct']:.1f}%"), html.Td(f"{st['util_peak_pct']:.1f}%"),
                     html.Td(f"{st['bytes'] / 1e3:,.0f}"), html.Td(f"{st['drops']:,}")])
            for lid, st in sorted(res.link_stats.items(), key=lambda kv: kv[1]["util_peak_pct"], reverse=True)]
    table = html.Table(className="data", children=[
        html.Thead(html.Tr([html.Th(h) for h in ("Link", "Type", "Average busy", "Busiest second", "KB carried", "Packets dropped")])),
        html.Tbody(rows),
    ])
    return (kpi_strip(res.summary(), include_failover=True), charts.histogram_figure(res),
            charts.site_figure(res, TOPO), charts.utilization_figure(res, TOPO), charts.drops_figure(res),
            charts.share_figure(res), table)


@app.callback([Output("topo-graph", "figure"), Output("topo-note", "children")], Input("run-store", "data"))
def render_topology(run):
    res = _get(run)
    return charts.topology_figure(TOPO, res), f"Showing the last run: {(run or {}).get('label', BASELINE_LABEL)}"


def _detail_default() -> List:
    return [html.H2("Selected device"), html.P("Click a device on the map to see its role, links and traffic.", className="hint")]


@app.callback(Output("topo-detail", "children"), [Input("topo-graph", "clickData"), Input("run-store", "data")])
def node_detail(click, run):
    nid = None
    if click and click.get("points"):
        cd = click["points"][0].get("customdata")
        nid = cd[0] if isinstance(cd, list) and cd else cd
    if nid not in TOPO.nodes:
        return _detail_default()
    res = _get(run)
    n = TOPO.nodes[nid]
    kv = [("Role", n.role), ("Zone", n.zone), ("Site", n.site)]
    if n.terminals:
        kv.append(("Terminals", str(n.terminals)))
    stats = res.by_source().get(nid)
    if stats:
        kv += [("Completed", f"{stats['ok']:,}"), ("Lost", f"{stats['failed']:,}"),
               ("Avg / p95", f"{stats['avg_ms']:.0f} / {stats['p95_ms']:.0f} ms"), ("Needed a retry", f"{stats['retried']:,}")]
    items = []
    for nbr, link in TOPO.adj[nid]:
        st = res.link_stats[link.id]
        kind = "IPsec tunnel" if link.encrypted else "LAN"
        items.append(html.Li([html.B(TOPO.nodes[nbr].label),
                              html.Div(f"{kind} · {link.lat_ms:g} ms · {link.bw_mbps:g} Mbps · {st['util_peak_pct']:.0f}% busiest", className="muted")]))
    out = [html.H2(n.label)]
    if n.note:
        out.append(html.P(n.note, className="hint"))
    out.append(html.Dl([x for k, v in kv for x in (html.Dt(k), html.Dd(v))], className="kv"))
    out += [html.H3("Connected to", style={"margin": "12px 0 0", "fontSize": "13px"}), html.Ul(items, className="link-list")]
    return out


@app.callback(Output("run-chip", "children"), Input("run-store", "data"))
def render_chip(run):
    s = _get(run).summary()
    good = s["failed"] == 0
    return [html.B("Last run: "), (run or {}).get("label", BASELINE_LABEL), " · ",
            html.Span(f"{'✓' if good else '✕'} {s['success_pct']:.1f}% completed", className="ok" if good else "bad")]


# --- test cases
def tests_table(results: List[dict]) -> html.Table:
    mark = {"PASS": ("✓", "pass"), "FAIL": ("✕", "fail"), "SKIP": ("–", "skip")}
    rows = []
    for r in results:
        icon, cls = mark[r["status"]]
        rows.append(html.Tr([
            html.Td(r["id"], className="mono"),
            html.Td([html.Div(r["name"], className="t-name"), html.Div(r["detail"], className="t-detail")]),
            html.Td(r["requirement"]),
            html.Td(html.Span(f"{icon} {r['status']}", className=f"badge {cls}")),
            html.Td(r["measured"]),
            html.Td(r["target"]),
        ]))
    return html.Table(className="data", children=[
        html.Thead(html.Tr([html.Th(h) for h in ("ID", "Test", "Requirement", "Result", "Measured", "Target")])),
        html.Tbody(rows),
    ])


@app.callback(
    [Output("tests-body", "children"), Output("tests-summary", "children"), Output("tests-store", "data")],
    Input("btn-tests", "n_clicks"), State("cfg-store", "data"), prevent_initial_call=True,
)
def run_tests(_n, cfgs):
    results = [r.as_dict() for r in tests_suite.run_all(TOPO, cfgs or {})]
    n_pass = sum(r["status"] == "PASS" for r in results)
    n_fail = sum(r["status"] == "FAIL" for r in results)
    n_skip = sum(r["status"] == "SKIP" for r in results)
    summary = [html.Span(f"✓ {n_pass} passed", className="p"), html.Span(f"✕ {n_fail} failed", className="f"),
               html.Span(f"– {n_skip} skipped", className="s")]
    return tests_table(results), summary, results


@app.callback(Output("dl-tests", "data"), Input("btn-tests-dl", "n_clicks"), State("tests-store", "data"), prevent_initial_call=True)
def download_tests(_n, results):
    if not results:
        raise PreventUpdate
    buf = io.StringIO()
    cols = ["id", "name", "requirement", "status", "measured", "target", "detail"]
    writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(results)
    return dcc.send_string(buf.getvalue(), "test-report.csv")


# --- config audit
def _decode(contents: str) -> str:
    _, b64 = contents.split(",", 1)
    return base64.b64decode(b64).decode("utf-8", errors="replace")


def audit_view(audits: List[dict], demo: set) -> html.Div:
    if not audits:
        return html.Div()
    mean = sum(a["score_pct"] for a in audits) / len(audits)
    cards = [html.Div(className="card", children=[html.Div(className="summary-line", children=[
        html.Span(f"{len(audits)} device config(s) checked"), html.Span(f"Average score {mean:.0f}%")])])]
    for a in audits:
        rows = []
        for r in a["results"]:
            icon, cls = {"PASS": ("✓", "pass"), "FAIL": ("✕", "fail"), "N/A": ("–", "skip")}[r["status"]]
            evidence = [html.Div(r["evidence"], className="mono muted")]
            if r["fix"]:
                evidence.append(html.Div(["Add: ", html.Span(r["fix"], className="fix")]))
            rows.append(html.Tr([html.Td(r["category"]), html.Td(r["label"]),
                                 html.Td(html.Span(f"{icon} {r['status']}", className=f"badge {cls}")), html.Td(evidence)]))
        head = html.Div(className="dev-head", children=[
            html.H2(a["hostname"]), html.Span(a["file"], className="muted mono"),
            html.Span(" / ".join(a["roles"]), className="muted"),
            html.Div(html.Div(style={"width": f"{a['score_pct']:.0f}%"}), className="meter"),
            html.Span(f"{a['score_pct']:.0f}%  ({a['passed']} of {a['passed'] + a['failed']} checks)"),
        ] + ([html.Span("Demo data", className="demo-tag")] if a["file"] in demo else []))
        cards.append(html.Div(className="card", children=[head, html.Table(className="data", children=[
            html.Thead(html.Tr([html.Th(h) for h in ("Area", "Check", "Result", "Evidence")])), html.Tbody(rows)])]))
    return html.Div(cards, className="stack")


@app.callback(
    [Output("audit-body", "children"), Output("cfg-store", "data")],
    [Input("audit-upload", "contents"), Input("btn-demo-cfg", "n_clicks")],
    [State("audit-upload", "filename")], prevent_initial_call=True,
)
def run_audit(contents, _demo_clicks, filenames):
    configs: Dict[str, str] = {}
    demo: set = set()
    if ctx.triggered_id == "btn-demo-cfg":
        for path in sorted((ROOT / "data" / "sample_configs").glob("*.cfg")):
            configs[path.name] = path.read_text(encoding="utf-8")
            demo.add(path.name)
    elif contents:
        for text, name in zip(contents, filenames or []):
            configs[name] = _decode(text)
            if "DEMO DATA" in configs[name][:120]:
                demo.add(name)
    if not configs:
        raise PreventUpdate
    return audit_view(audit_all(configs), demo), configs


if __name__ == "__main__":
    app.run(debug=os.environ.get("DASH_DEBUG") == "1", port=int(os.environ.get("PORT", "8050")))
