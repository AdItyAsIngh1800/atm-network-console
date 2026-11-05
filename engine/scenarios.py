"""Named failure scenarios and one-click presets shared by the UI and the tests."""
from __future__ import annotations

from typing import List, Optional

from .simulator import Failure

# key -> (label, [(kind, target), ...])
FAILURES = {
    "none": ("No failure", []),
    "br1_wan": ("Branch 1 primary WAN link cut", [("link", "br1_r~isp_a")]),
    "atm1_wan": ("ATM Site 1 primary WAN link cut", [("link", "atm1_r~isp_a")]),
    "dc_link": ("Data center uplink to ISP-A cut", [("link", "dc_r~isp_a")]),
    "isp_a": ("ISP-A outage", [("node", "isp_a")]),
    "dc_down": ("Data center edge router down", [("node", "dc_r")]),
    "isp_both": ("Both ISPs down", [("node", "isp_a"), ("node", "isp_b")]),
}


def make_failures(key: str, t_fail: float, t_restore: Optional[float]) -> List[Failure]:
    _, items = FAILURES[key]
    return [Failure(kind, target, t_fail, t_restore) for kind, target in items]


def failure_label(f: Failure) -> str:
    return f"{'link' if f.kind == 'link' else 'node'} {f.target}"


# Values the Simulation tab controls take when a preset is chosen.
BASE_CONTROLS = dict(
    rate=12, mult=1, req=0.6, resp=0.6, bg=30, loss=None, ipsec=True,
    detect=5, timeout=1.5, retries=5, failure="none", tfail=30, trestore=None, duration=120,
)

PRESETS = {
    "normal": ("Normal day", {}),
    "peak": ("Peak hour (5x traffic, busy WAN)", dict(mult=5, bg=60)),
    "heavy": ("Heavy payloads (cheque images)", dict(mult=10, req=40, resp=2, bg=55)),
    "wan_fail": ("Branch 1 primary WAN cut at 30 s", dict(failure="br1_wan")),
    "isp_a": ("ISP-A outage at 30 s", dict(failure="isp_a")),
    "dc_down": ("Data center down 30-90 s, DR takes over", dict(failure="dc_down", trestore=90)),
    "lossy": ("Lossy WAN (3% loss per hop)", dict(loss=3.0)),
    "ospf_default": ("ISP-A outage with default OSPF timers (40 s)", dict(failure="isp_a", detect=40, duration=150)),
    "isolation": ("Both ISPs down at 30 s", dict(failure="isp_both")),
}


def preset_controls(key: str) -> dict:
    out = dict(BASE_CONTROLS)
    out.update(PRESETS[key][1])
    return out
