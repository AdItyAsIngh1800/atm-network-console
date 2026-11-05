"""Test cases that can be shown live to the examiner.

Each test runs the simulator (or inspects the topology / uploaded configs),
compares a measured value with a target, and returns PASS / FAIL / SKIP with
the evidence. ``run_all`` is used by the dashboard; ``tests/test_engine.py``
runs the same tests under pytest.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Callable, Dict, List, Optional

from .audit import audit_all
from .scenarios import make_failures
from .simulator import Settings, run_simulation
from .topology import Topology, get_topology

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"

THRESHOLDS = {
    "p95_ms": 250.0,          # normal-load 95th percentile latency
    "peak_p95_ms": 400.0,     # peak-load 95th percentile latency
    "failover_s": 10.0,       # longest acceptable failover window
    "util_pct": 85.0,         # busiest link at peak
    "min_tx": 10000,          # transactions for the zero-loss run
    "max_avg_attempts": 1.3,  # on a lossy WAN
}


@dataclass
class TestResult:
    id: str
    name: str
    requirement: str
    status: str
    measured: str
    target: str
    detail: str
    seconds: float

    def as_dict(self) -> dict:
        return asdict(self)


class _Ctx:
    def __init__(self, topo: Topology, configs: Optional[Dict[str, str]]):
        self.topo = topo
        self.configs = configs or {}


def _sim(ctx: _Ctx, **kw):
    return run_simulation(Settings(**kw), ctx.topo)


def _res(status, measured, target, detail=""):
    return status, measured, target, detail


# ---------------------------------------------------------------------- tests
def t_connectivity(ctx: _Ctx):
    bad = [s for s in ctx.topo.sources if ctx.topo.route(s) is None]
    return _res(PASS if not bad else FAIL, f"{len(ctx.topo.sources) - len(bad)}/{len(ctx.topo.sources)} sites reach the servers",
                "all sites", ("Unreachable: " + ", ".join(bad)) if bad else "Every branch and ATM site has a path to the primary servers.")


def t_spof(ctx: _Ctx):
    """No single WAN link, ISP or edge router may cut any site off from both DC and DR."""
    topo = ctx.topo
    elements = [("link", l.id) for l in topo.links.values() if l.kind == "wan"]
    elements += [("node", n.id) for n in topo.nodes.values() if n.role == "isp" or n.id in ("dc_r", "dr_r")]
    bad = []
    for kind, ident in elements:
        dl = frozenset([ident]) if kind == "link" else frozenset()
        dn = frozenset([ident]) if kind == "node" else frozenset()
        cut = [s for s in topo.sources if topo.route(s, dl, dn) is None]
        if cut:
            bad.append(f"{ident} isolates {', '.join(cut)}")
    return _res(PASS if not bad else FAIL, f"{len(elements) - len(bad)}/{len(elements)} elements safe to lose",
                "no single point of failure", "; ".join(bad) if bad else "Lost any one WAN link, ISP or DC/DR router and every site still has a path.")


def t_zero_loss(ctx: _Ctx):
    r = _sim(ctx, duration_s=1300.0)
    s = r.summary()
    ok = s["failed"] == 0 and s["n"] >= THRESHOLDS["min_tx"]
    return _res(PASS if ok else FAIL, f"{s['failed']} lost of {s['n']:,}", f"0 lost of {THRESHOLDS['min_tx']:,}+",
                f"{s['retried']} transactions needed a retry and all were recovered; {r.dedup} retries were de-duplicated by the server (no double posting).")


def t_latency(ctx: _Ctx):
    s = _sim(ctx).summary()
    return _res(PASS if s["p95_ms"] <= THRESHOLDS["p95_ms"] else FAIL, f"p95 {s['p95_ms']:.0f} ms",
                f"p95 <= {THRESHOLDS['p95_ms']:.0f} ms", f"avg {s['avg_ms']:.0f} ms, p99 {s['p99_ms']:.0f} ms at normal load.")


def t_peak(ctx: _Ctx):
    r = _sim(ctx, load_mult=5.0, background_load=0.60)
    s = r.summary()
    peak_util = max(v["util_peak_pct"] for v in r.link_stats.values())
    ok = s["failed"] == 0 and s["p95_ms"] <= THRESHOLDS["peak_p95_ms"] and peak_util <= THRESHOLDS["util_pct"]
    return _res(PASS if ok else FAIL, f"p95 {s['p95_ms']:.0f} ms, {s['failed']} lost, busiest link {peak_util:.0f}%",
                f"p95 <= {THRESHOLDS['peak_p95_ms']:.0f} ms, 0 lost, link <= {THRESHOLDS['util_pct']:.0f}%",
                f"5x traffic with 60% of every WAN link already used by other traffic ({s['n']:,} transactions).")


def _failover(ctx: _Ctx, key: str, restore: Optional[float], need_backup_link: Optional[str] = None, need_dr: bool = False):
    r = _sim(ctx, failures=make_failures(key, 30.0, restore))
    s = r.summary()
    fo = s["failover_s"] or 0.0
    problems = []
    if s["failed"]:
        problems.append(f"{s['failed']} transactions lost")
    if fo > THRESHOLDS["failover_s"]:
        problems.append(f"failover took {fo:.1f} s")
    note = ""
    if need_backup_link:
        carried = r.link_stats[need_backup_link]["bytes"]
        if carried == 0:
            problems.append(f"backup link {need_backup_link} carried no traffic")
        note = f"Backup link {need_backup_link} carried {carried / 1e3:.0f} KB. "
    if need_dr:
        if s["dr_served"] == 0:
            problems.append("DR site served nothing")
        note = f"DR site completed {s['dr_served']:,} transactions ({s['dr_share_pct']:.0f}%). "
    detail = note + f"{s['retried']} transactions retried; slowest took {s['max_ms'] / 1000:.1f} s."
    return _res(PASS if not problems else FAIL, f"{s['failed']} lost, failover {fo:.1f} s",
                f"0 lost, failover <= {THRESHOLDS['failover_s']:.0f} s", ("; ".join(problems) + ". " if problems else "") + detail)


def t_wan_failover(ctx: _Ctx):
    return _failover(ctx, "br1_wan", None, need_backup_link="br1_r~isp_b")


def t_isp_failover(ctx: _Ctx):
    return _failover(ctx, "isp_a", None, need_backup_link="dc_r~isp_b")


def t_dr(ctx: _Ctx):
    return _failover(ctx, "dc_down", 90.0, need_dr=True)


def t_lossy(ctx: _Ctx):
    r = _sim(ctx, wan_loss_override_pct=3.0)
    s = r.summary()
    avg_att = 1.0 + s["retries_total"] / s["n"] if s["n"] else 1.0
    ok = s["failed"] == 0 and avg_att <= THRESHOLDS["max_avg_attempts"]
    return _res(PASS if ok else FAIL, f"{s['failed']} lost, {avg_att:.2f} attempts per transaction",
                f"0 lost, <= {THRESHOLDS['max_avg_attempts']} attempts", f"3% packet loss on every WAN hop: {s['retried']} of {s['n']} transactions retried, p99 {s['p99_ms']:.0f} ms.")


def t_encryption(ctx: _Ctx):
    wan = [l for l in ctx.topo.links.values() if l.kind == "wan"]
    bad = [l.id for l in wan if not l.encrypted]
    return _res(PASS if not bad else FAIL, f"{len(wan) - len(bad)}/{len(wan)} WAN links encrypted", "all WAN links IPsec",
                ("Plain-text WAN links: " + ", ".join(bad)) if bad else "Every WAN link (ISP uplinks and DC-DR replication) is an IPsec tunnel; traffic is clear only inside the LANs.")


def t_segmentation(ctx: _Ctx):
    topo = ctx.topo
    problems, checked = [], 0
    zone_of = {n.id: n.zone for n in topo.nodes.values()}
    for s_zone, d_zone in topo.policy.get("must_deny", []):
        checked += 1
        if topo.zone_allows(s_zone, d_zone):
            problems.append(f"{s_zone} -> {d_zone} is allowed")
    svc_zone = zone_of[topo.primary]
    for src in topo.sources:
        checked += 1
        if not topo.zone_allows(zone_of[src], svc_zone):
            problems.append(f"{src} cannot reach the transaction servers")
    return _res(PASS if not problems else FAIL, f"{checked - len(problems)}/{checked} policy rules hold", "all rules hold",
                "; ".join(problems) if problems else "ATM and branch zones can reach only the application tier; database and management zones are unreachable from them.")


def t_configs(ctx: _Ctx):
    if not ctx.configs:
        return _res(SKIP, "no configs uploaded", "IPsec on every router", "Upload exported running-configs in the Config audit tab, then run again.")
    routers = [a for a in audit_all(ctx.configs) if "router" in a["roles"]]
    if not routers:
        return _res(SKIP, "no router configs found", "IPsec on every router", "None of the uploaded files look like router configurations.")
    bad = []
    for a in routers:
        status = {r["id"]: r["status"] for r in a["results"]}
        if status.get("ipsec") != "PASS" or status.get("crypto_applied") != "PASS":
            bad.append(a["hostname"])
    return _res(PASS if not bad else FAIL, f"{len(routers) - len(bad)}/{len(routers)} routers have IPsec applied", "all routers",
                ("Missing IPsec: " + ", ".join(bad)) if bad else "Every uploaded router config defines IPsec and applies it to an interface.")


@dataclass
class TestDef:
    id: str
    name: str
    requirement: str
    fn: Callable[[_Ctx], tuple]


REGISTRY: List[TestDef] = [
    TestDef("T01", "Every site can reach the transaction servers", "Availability", t_connectivity),
    TestDef("T02", "No single point of failure on the WAN", "Availability", t_spof),
    TestDef("T03", "Zero transaction loss over 10,000 transactions", "Zero loss", t_zero_loss),
    TestDef("T04", "Transaction latency at normal load", "Low latency", t_latency),
    TestDef("T05", "Peak-hour load on a busy WAN", "Low latency", t_peak),
    TestDef("T06", "Branch WAN link cut: failover to backup ISP", "Zero loss", t_wan_failover),
    TestDef("T07", "ISP-A outage: all sites fail over to ISP-B", "Zero loss", t_isp_failover),
    TestDef("T08", "Data center down: DR site takes over, then fail back", "Zero loss", t_dr),
    TestDef("T09", "Lossy WAN (3% per hop): retries recover everything", "Zero loss", t_lossy),
    TestDef("T10", "All WAN links are IPsec encrypted", "Encryption", t_encryption),
    TestDef("T11", "ATM and branch zones cannot reach database or management", "Security", t_segmentation),
    TestDef("T12", "Uploaded router configs have IPsec applied", "Encryption", t_configs),
]


def run_test(defn: TestDef, topo: Optional[Topology] = None, configs: Optional[Dict[str, str]] = None) -> TestResult:
    ctx = _Ctx(topo or get_topology(), configs)
    t0 = time.time()
    try:
        status, measured, target, detail = defn.fn(ctx)
    except Exception as exc:  # a broken test must show up as a failure, not crash the demo
        status, measured, target, detail = FAIL, "error", "-", f"{type(exc).__name__}: {exc}"
    return TestResult(defn.id, defn.name, defn.requirement, status, measured, target, detail, time.time() - t0)


def run_all(topo: Optional[Topology] = None, configs: Optional[Dict[str, str]] = None) -> List[TestResult]:
    return [run_test(d, topo, configs) for d in REGISTRY]
