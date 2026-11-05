"""Discrete-event simulation of ATM / branch transactions across the network.

Each terminal generates transactions as a Poisson process. A transaction is a
request packet to the transaction server, a short processing time, and a
response packet back. Every hop applies serialisation delay, FIFO queueing
(with tail drop), propagation delay, jitter, random loss, and IPsec overhead.

Failures are modelled with two views of the network:

* the data plane sees a failure the instant it happens, so packets sent into a
  dead link or node are lost;
* the control plane (routing) only notices ``detect_s`` seconds later, so
  routes stay stale for that long. This is the failover window.

The terminal retries with exponential backoff until it gets a response or runs
out of attempts. Servers de-duplicate retries, so a retry never double-posts.
"""
from __future__ import annotations

import heapq
import random
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from .topology import Topology, get_topology


@dataclass
class Failure:
    kind: str  # "link" | "node"
    target: str
    t_fail: float
    t_restore: Optional[float] = None

    def active(self, t: float) -> bool:
        return self.t_fail <= t and (self.t_restore is None or t < self.t_restore)


@dataclass
class Settings:
    duration_s: float = 120.0
    tx_per_min: float = 12.0  # per terminal
    load_mult: float = 1.0
    request_bytes: int = 600
    response_bytes: int = 600
    server_workers: int = 8
    server_ms: float = 10.0  # mean processing time
    background_load: float = 0.30  # share of each WAN link used by other traffic
    ipsec: bool = True
    ipsec_overhead_bytes: int = 70
    crypto_ms: float = 0.4
    wan_loss_override_pct: Optional[float] = None
    detect_s: float = 5.0  # routing failure detection time
    retry_timeout_s: float = 1.5
    backoff: float = 1.5
    max_retries: int = 5
    queue_limit_ms: float = 250.0
    failures: List[Failure] = field(default_factory=list)
    seed: int = 42


def percentile(sorted_vals: List[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


@dataclass
class SimResult:
    settings: dict
    duration_s: float
    end_s: float
    t_start: List[float]
    t_end: List[float]
    latency_ms: List[float]
    ok: List[bool]
    attempts: List[int]
    source: List[str]
    served_by: List[str]
    link_stats: Dict[str, dict]
    drops: Dict[str, int]
    first_fail_s: Optional[float]
    last_blackhole_s: Optional[float]
    dedup: int
    ghost_commits: int

    # ------------------------------------------------------------- summaries
    def _idx(self, upto: Optional[float]) -> List[int]:
        if upto is None:
            return list(range(len(self.t_end)))
        return [i for i, t in enumerate(self.t_end) if t <= upto]

    def summary(self, upto: Optional[float] = None) -> dict:
        idx = self._idx(upto)
        n = len(idx)
        ok_idx = [i for i in idx if self.ok[i]]
        lat = sorted(self.latency_ms[i] for i in ok_idx)
        span = (upto if upto is not None else self.duration_s) or 1.0
        dr = sum(1 for i in ok_idx if self.served_by[i] == "DR")
        out = {
            "n": n,
            "ok": len(ok_idx),
            "failed": n - len(ok_idx),
            "success_pct": (100.0 * len(ok_idx) / n) if n else 100.0,
            "avg_ms": (sum(lat) / len(lat)) if lat else 0.0,
            "p50_ms": percentile(lat, 50),
            "p95_ms": percentile(lat, 95),
            "p99_ms": percentile(lat, 99),
            "max_ms": lat[-1] if lat else 0.0,
            "tps": len(ok_idx) / max(span, 1e-9),
            "retried": sum(1 for i in idx if self.attempts[i] > 1),
            "retries_total": sum(self.attempts[i] - 1 for i in idx),
            "dr_served": dr,
            "dr_share_pct": (100.0 * dr / len(ok_idx)) if ok_idx else 0.0,
            "dedup": self.dedup if upto is None else None,
            "ghost_commits": self.ghost_commits if upto is None else None,
            "failover_s": self.failover_s() if upto is None else None,
        }
        return out

    def failover_s(self) -> Optional[float]:
        """Seconds traffic was being black-holed after the first failure.

        None when nothing failed; 0.0 when something failed but no packet hit it.
        """
        fails = self.settings.get("failures") or []
        if not fails:
            return None
        if self.last_blackhole_s is None or self.first_fail_s is None:
            return 0.0
        return max(0.0, self.last_blackhole_s - self.first_fail_s)

    def by_source(self) -> Dict[str, dict]:
        agg: Dict[str, list] = defaultdict(list)
        for i, s in enumerate(self.source):
            agg[s].append(i)
        out = {}
        for s, idx in agg.items():
            ok_lat = sorted(self.latency_ms[i] for i in idx if self.ok[i])
            out[s] = {
                "n": len(idx),
                "ok": len(ok_lat),
                "failed": len(idx) - len(ok_lat),
                "avg_ms": (sum(ok_lat) / len(ok_lat)) if ok_lat else 0.0,
                "p95_ms": percentile(ok_lat, 95),
                "retried": sum(1 for i in idx if self.attempts[i] > 1),
            }
        return out

    def timeseries(self, bucket_s: float = 5.0) -> dict:
        nb = int(self.end_s // bucket_s) + 1
        lat: List[List[float]] = [[] for _ in range(nb)]
        ok = [0] * nb
        bad = [0] * nb
        dc = [0] * nb
        dr = [0] * nb
        for i, t in enumerate(self.t_end):
            b = min(nb - 1, int(t // bucket_s))
            if self.ok[i]:
                ok[b] += 1
                lat[b].append(self.latency_ms[i])
                if self.served_by[i] == "DR":
                    dr[b] += 1
                else:
                    dc[b] += 1
            else:
                bad[b] += 1
        mid = [(b + 0.5) * bucket_s for b in range(nb)]
        p95 = [percentile(sorted(v), 95) if v else None for v in lat]
        return {
            "t": mid,
            "bucket_s": bucket_s,
            "ok": ok,
            "failed": bad,
            "dc": dc,
            "dr": dr,
            "p95_ms": p95,
            "tps": [o / bucket_s for o in ok],
            "fail_ps": [b / bucket_s for b in bad],
        }


class _Pkt:
    __slots__ = ("tx", "svc", "path", "idx", "kind", "size")

    def __init__(self, tx, svc, path, kind, size):
        self.tx, self.svc, self.path, self.idx, self.kind, self.size = tx, svc, path, 0, kind, size


class _Engine:
    def __init__(self, topo: Topology, s: Settings):
        self.topo, self.s = topo, s
        self.rng = random.Random(s.seed)
        self.now = 0.0
        self._q: list = []
        self._seq = 0
        self.busy: Dict[tuple, float] = {}
        self.bk: Dict[tuple, Dict[int, float]] = defaultdict(lambda: defaultdict(float))
        self.link_bytes: Dict[str, int] = defaultdict(int)
        self.link_drops: Dict[str, int] = defaultdict(int)
        self.drops: Dict[str, int] = defaultdict(int)
        self.blackhole_times: List[float] = []
        self.workers = {
            topo.primary: [0.0] * s.server_workers,
            topo.backup: [0.0] * s.server_workers,
        }
        self.dedup = 0
        self.ghost = 0
        self.txid = 0
        self.r_start: List[float] = []
        self.r_end: List[float] = []
        self.r_lat: List[float] = []
        self.r_ok: List[bool] = []
        self.r_att: List[int] = []
        self.r_src: List[str] = []
        self.r_srv: List[str] = []

    # ----------------------------------------------------------- scheduling
    def _at(self, t, fn, *args):
        heapq.heappush(self._q, (t, self._seq, fn, args))
        self._seq += 1

    def run(self) -> SimResult:
        for src in self.topo.sources:
            self._next_arrival(src, 0.0)
        while self._q:
            t, _, fn, args = heapq.heappop(self._q)
            self.now = t
            fn(*args)
        return self._result()

    # ------------------------------------------------------------- failures
    def _down_sets(self, t: float):
        links, nodes = set(), set()
        for f in self.s.failures:
            if f.active(t):
                (links if f.kind == "link" else nodes).add(f.target)
        return frozenset(links), frozenset(nodes)

    def _link_up(self, link, t: float) -> bool:
        dl, dn = self._down_sets(t)
        return link.id not in dl and link.a not in dn and link.b not in dn

    # --------------------------------------------------------- transactions
    def _next_arrival(self, src: str, t: float):
        rate = self.topo.nodes[src].terminals * self.s.tx_per_min * self.s.load_mult / 60.0
        if rate <= 0:
            return
        t2 = t + self.rng.expovariate(rate)
        if t2 < self.s.duration_s:
            self._at(t2, self._arrival, src)

    def _arrival(self, src: str):
        self._next_arrival(src, self.now)
        self.txid += 1
        tx = {"id": self.txid, "src": src, "t0": self.now, "n": 0, "done": False, "committed": False}
        self._attempt(tx)

    def _attempt(self, tx):
        tx["n"] += 1
        n = tx["n"]
        t = self.now
        dl, dn = self._down_sets(t - self.s.detect_s)  # control plane view (stale)
        r = self.topo.route(tx["src"], dl, dn)
        if r is None:
            self._drop(None, "no_route", t)
        else:
            svc, path = r
            self._forward(_Pkt(tx, svc, path, "req", self.s.request_bytes))
        self._at(t + self.s.retry_timeout_s * self.s.backoff ** (n - 1), self._timeout, tx, n)

    def _timeout(self, tx, n):
        if tx["done"] or tx["n"] != n:
            return
        if n >= self.s.max_retries + 1:
            tx["done"] = True
            if tx["committed"]:
                self.ghost += 1  # server posted it but the terminal never heard back
            self._record(tx, False, None)
        else:
            self._attempt(tx)

    def _record(self, tx, ok: bool, svc: Optional[str]):
        self.r_start.append(tx["t0"])
        self.r_end.append(self.now)
        self.r_lat.append((self.now - tx["t0"]) * 1000.0)
        self.r_ok.append(ok)
        self.r_att.append(tx["n"])
        self.r_src.append(tx["src"])
        self.r_srv.append(self.topo.site_of(svc) if svc else "-")

    # ------------------------------------------------------------ forwarding
    def _drop(self, link, cause: str, t: float):
        self.drops[cause] += 1
        if link is not None:
            self.link_drops[link.id] += 1
        if cause in ("element_down", "no_route"):
            self.blackhole_times.append(t)

    def _forward(self, pkt: _Pkt):
        i = pkt.idx
        if i >= len(pkt.path) - 1:
            self._arrive_end(pkt)
            return
        a, b = pkt.path[i], pkt.path[i + 1]
        link = self.topo.link_between(a, b)
        now = self.now
        if not self._link_up(link, now):
            self._drop(link, "element_down", now)
            return
        loss = link.loss_pct
        if link.kind == "wan" and self.s.wan_loss_override_pct is not None:
            loss = self.s.wan_loss_override_pct
        if loss > 0 and self.rng.random() < loss / 100.0:
            self._drop(link, "random_loss", now)
            return
        enc = link.encrypted and self.s.ipsec
        size = pkt.size + (self.s.ipsec_overhead_bytes if enc else 0)
        bw = link.bw_mbps * 1e6
        if link.kind == "wan":
            bw *= max(0.01, 1.0 - self.s.background_load)
        ser = size * 8.0 / bw
        key = (link.id, a)
        start = max(now, self.busy.get(key, 0.0))
        wait = start - now
        if wait * 1000.0 > self.s.queue_limit_ms:
            self._drop(link, "queue_overflow", now)
            return
        self.busy[key] = start + ser
        self.bk[key][int(start)] += ser
        self.link_bytes[link.id] += size
        delay = wait + ser + link.lat_ms / 1000.0
        if link.jitter_ms > 0:
            delay += abs(self.rng.gauss(0.0, link.jitter_ms)) / 1000.0
        if enc:
            delay += self.s.crypto_ms / 1000.0
        self._at(now + delay, self._hop_arrive, pkt, link)

    def _hop_arrive(self, pkt: _Pkt, link):
        if not self._link_up(link, self.now):
            self._drop(link, "element_down", self.now)  # failed while in flight
            return
        pkt.idx += 1
        self._forward(pkt)

    def _arrive_end(self, pkt: _Pkt):
        tx = pkt.tx
        if pkt.kind == "req":
            if tx["committed"]:
                self.dedup += 1  # retry of something already posted: reply, don't re-post
            else:
                tx["committed"] = True
            workers = self.workers[pkt.svc]
            start = max(self.now, heapq.heappop(workers))
            mean = self.s.server_ms / 1000.0
            svc_t = min(self.rng.expovariate(1.0 / mean), 10 * mean)
            heapq.heappush(workers, start + svc_t)
            self._at(start + svc_t, self._respond, pkt)
        else:
            if tx["done"]:
                return
            tx["done"] = True
            self._record(tx, True, pkt.svc)

    def _respond(self, req: _Pkt):
        resp = _Pkt(req.tx, req.svc, list(reversed(req.path)), "resp", self.s.response_bytes)
        self._forward(resp)

    # ---------------------------------------------------------------- result
    def _result(self) -> SimResult:
        dur = max(1.0, self.s.duration_s)
        bg = self.s.background_load
        stats = {}
        for link in self.topo.links.values():
            keys = [(link.id, link.a), (link.id, link.b)]
            mean_tx = max(sum(self.bk[k].values()) for k in keys) / dur if keys else 0.0
            peak_tx = max((max(self.bk[k].values()) if self.bk[k] else 0.0) for k in keys)
            share = bg if link.kind == "wan" else 0.0
            stats[link.id] = {
                "kind": link.kind,
                "util_mean_pct": 100.0 * (share + (1 - share) * min(mean_tx, 1.0)),
                "util_peak_pct": 100.0 * (share + (1 - share) * min(peak_tx, 1.0)),
                "bytes": self.link_bytes.get(link.id, 0),
                "drops": self.link_drops.get(link.id, 0),
            }
        fails = self.s.failures
        first_fail = min((f.t_fail for f in fails), default=None)
        return SimResult(
            settings=asdict(self.s),
            duration_s=self.s.duration_s,
            end_s=max([self.s.duration_s] + self.r_end),
            t_start=self.r_start,
            t_end=self.r_end,
            latency_ms=self.r_lat,
            ok=self.r_ok,
            attempts=self.r_att,
            source=self.r_src,
            served_by=self.r_srv,
            link_stats=stats,
            drops=dict(self.drops),
            first_fail_s=first_fail,
            last_blackhole_s=max(self.blackhole_times) if self.blackhole_times else None,
            dedup=self.dedup,
            ghost_commits=self.ghost,
        )


def run_simulation(settings: Optional[Settings] = None, topo: Optional[Topology] = None) -> SimResult:
    return _Engine(topo or get_topology(), settings or Settings()).run()
