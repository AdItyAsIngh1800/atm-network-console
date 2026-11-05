"""Network model: nodes, links, routing and zone policy.

The topology is loaded from ``data/topology.json`` so it can be edited without
touching code. Routing is plain Dijkstra on link latency, skipping any failed
link or node, which is what OSPF/EIGRP would converge to.
"""
from __future__ import annotations

import heapq
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Tuple

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_TOPOLOGY = DATA_DIR / "topology.json"


@dataclass(frozen=True)
class Node:
    id: str
    label: str
    role: str  # source | router | switch | firewall | isp | server
    zone: str
    site: str
    x: float
    y: float
    terminals: int = 0
    note: str = ""


@dataclass(frozen=True)
class Link:
    id: str
    a: str
    b: str
    kind: str  # lan | wan
    lat_ms: float
    bw_mbps: float
    loss_pct: float = 0.0
    jitter_ms: float = 0.0
    encrypted: bool = False
    label: str = ""


class Topology:
    def __init__(self, data: dict):
        self.name: str = data.get("name", "Network")
        self.note: str = data.get("note", "")
        self.nodes: Dict[str, Node] = {n["id"]: Node(**n) for n in data["nodes"]}
        self.links: Dict[str, Link] = {l["id"]: Link(**l) for l in data["links"]}
        self.primary: str = data["services"]["primary"]
        self.backup: str = data["services"]["backup"]
        self.policy: dict = data.get("policy", {"allow": [], "must_deny": []})
        self.adj: Dict[str, List[Tuple[str, Link]]] = {n: [] for n in self.nodes}
        self._pair: Dict[Tuple[str, str], Link] = {}
        for link in self.links.values():
            if link.a not in self.nodes or link.b not in self.nodes:
                raise ValueError(f"link {link.id} refers to an unknown node")
            self.adj[link.a].append((link.b, link))
            self.adj[link.b].append((link.a, link))
            self._pair[(link.a, link.b)] = link
            self._pair[(link.b, link.a)] = link
        self.sources: List[str] = [n.id for n in self.nodes.values() if n.role == "source"]
        self._cache: dict = {}

    @classmethod
    def load(cls, path: Path | str = DEFAULT_TOPOLOGY) -> "Topology":
        with open(path, "r", encoding="utf-8") as fh:
            return cls(json.load(fh))

    # ------------------------------------------------------------------ graph
    def link_between(self, a: str, b: str) -> Link:
        return self._pair[(a, b)]

    def site_of(self, node_id: str) -> str:
        return self.nodes[node_id].site

    def shortest_path(
        self,
        src: str,
        dst: str,
        down_links: FrozenSet[str] = frozenset(),
        down_nodes: FrozenSet[str] = frozenset(),
    ) -> Optional[List[str]]:
        key = (src, dst, down_links, down_nodes)
        if key in self._cache:
            return self._cache[key]
        path: Optional[List[str]] = None
        if src not in down_nodes and dst not in down_nodes:
            dist = {src: 0.0}
            prev: Dict[str, str] = {}
            heap = [(0.0, src)]
            done = set()
            while heap:
                d, u = heapq.heappop(heap)
                if u in done:
                    continue
                done.add(u)
                if u == dst:
                    break
                for v, link in self.adj[u]:
                    if link.id in down_links or v in down_nodes:
                        continue
                    nd = d + link.lat_ms
                    if nd < dist.get(v, 1e18):
                        dist[v] = nd
                        prev[v] = u
                        heapq.heappush(heap, (nd, v))
            if dst in done:
                path = [dst]
                while path[-1] != src:
                    path.append(prev[path[-1]])
                path.reverse()
        self._cache[key] = path
        return path

    def route(
        self,
        src: str,
        down_links: FrozenSet[str] = frozenset(),
        down_nodes: FrozenSet[str] = frozenset(),
    ) -> Optional[Tuple[str, List[str]]]:
        """Where a transaction from ``src`` goes: the primary service if it is
        reachable, otherwise the DR service, otherwise nowhere."""
        for svc in (self.primary, self.backup):
            path = self.shortest_path(src, svc, down_links, down_nodes)
            if path:
                return svc, path
        return None

    def path_latency_ms(self, path: List[str]) -> float:
        return sum(self.link_between(a, b).lat_ms for a, b in zip(path, path[1:]))

    # ----------------------------------------------------------------- policy
    def zone_allows(self, src_zone: str, dst_zone: str) -> bool:
        if src_zone == dst_zone:
            return True
        for s, d in self.policy.get("allow", []):
            if s in (src_zone, "*") and d in (dst_zone, "*"):
                return True
        return False


_TOPO: Optional[Topology] = None


def get_topology() -> Topology:
    global _TOPO
    if _TOPO is None:
        _TOPO = Topology.load()
    return _TOPO
