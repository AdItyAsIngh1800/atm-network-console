"""Static audit of exported Cisco running-configs.

Paste ``show running-config`` from each Packet Tracer device into a .txt/.cfg
file and upload it in the Config audit tab. Each check looks for a specific
line (or block) and reports PASS / FAIL / N/A with the evidence it found and
the command to add when it is missing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

PASS, FAIL, NA = "PASS", "FAIL", "N/A"


class _Ctx:
    def __init__(self, text: str):
        self.text = text
        self.lines = [ln.rstrip() for ln in text.splitlines()]
        self.roles = self._roles()

    def find(self, pattern: str) -> Optional[str]:
        rx = re.compile(pattern, re.I)
        for ln in self.lines:
            if rx.search(ln):
                return ln.strip()
        return None

    def count(self, pattern: str) -> int:
        rx = re.compile(pattern, re.I)
        return sum(1 for ln in self.lines if rx.search(ln))

    def block(self, header: str) -> List[str]:
        """Lines under every header matching ``header``, up to the next '!' line."""
        rx = re.compile(header, re.I)
        out: List[str] = []
        inside = False
        for ln in self.lines:
            if rx.search(ln) and not ln.startswith(" "):
                inside = True
                continue
            if inside:
                if ln.strip() == "!" or (ln and not ln.startswith(" ")):
                    inside = False
                    if rx.search(ln):
                        inside = True
                    continue
                out.append(ln.strip())
        return out

    def _roles(self) -> set:
        roles = set()
        if self.find(r"^\s*router (ospf|eigrp|bgp|rip)") or self.find(r"^\s*ip route "):
            roles.add("router")
        if self.find(r"switchport") or self.find(r"^\s*vlan \d+"):
            roles.add("switch")
        return roles or {"other"}


CheckFn = Callable[[_Ctx], Tuple[Optional[bool], str]]


@dataclass
class Check:
    id: str
    category: str
    label: str
    applies: set  # roles this check is relevant for ({"*"} = all)
    fn: CheckFn
    fix: str


def _present(pattern: str, miss: str = "not found") -> CheckFn:
    def fn(c: _Ctx):
        hit = c.find(pattern)
        return (True, hit) if hit else (False, miss)
    return fn


def _hostname(c: _Ctx):
    hit = c.find(r"^hostname\s+\S+")
    if not hit:
        return False, "no hostname"
    if re.search(r"hostname\s+(Router|Switch)$", hit, re.I):
        return False, f"{hit} (still the default name)"
    return True, hit


def _enable_secret(c: _Ctx):
    hit = c.find(r"^enable secret")
    if hit:
        return True, "enable secret ***"
    if c.find(r"^enable password"):
        return False, "uses 'enable password' (reversible); switch to 'enable secret'"
    return False, "no privileged-mode password"


def _vty_ssh(c: _Ctx):
    vty = c.block(r"^line vty")
    if not vty:
        return False, "no 'line vty' block found"
    ti = [ln for ln in vty if ln.lower().startswith("transport input")]
    if not ti:
        return False, "transport input not set (telnet allowed by default)"
    if all(re.fullmatch(r"transport input ssh", ln, re.I) for ln in ti):
        return True, ti[0]
    return False, "; ".join(ti)


def _ipsec(c: _Ctx):
    ike = c.find(r"^crypto (isakmp policy|ikev2 (proposal|policy))")
    ts = c.find(r"^crypto ipsec transform-set")
    if ike and ts:
        return True, f"{ike}  /  {ts}"
    missing = []
    if not ike:
        missing.append("IKE policy")
    if not ts:
        missing.append("IPsec transform-set")
    return False, "missing " + " and ".join(missing)


def _crypto_applied(c: _Ctx):
    hit = c.find(r"^\s+crypto map \S+\s*$") or c.find(r"^\s+tunnel protection ipsec")
    return (True, hit) if hit else (False, "no crypto map / tunnel protection on any interface")


def _acl_applied(c: _Ctx):
    hit = c.find(r"ip access-group \S+ (in|out)")
    if hit:
        return True, hit
    if c.find(r"^(ip )?access-list"):
        return False, "ACL defined but not applied to any interface"
    return False, "no ACL applied"


def _failover_routing(c: _Ctx):
    proto = c.find(r"^router (ospf|eigrp|bgp|rip)")
    if proto:
        return True, proto
    floating = c.find(r"^ip route \S+ \S+ \S+ \d{1,3}\s*$")
    if floating:
        return True, f"floating static: {floating}"
    return False, "only plain static routes, so no automatic failover"


def _stp_protect(c: _Ctx):
    pf = c.find(r"spanning-tree portfast")
    bg = c.find(r"spanning-tree bpduguard|portfast bpduguard")
    if pf and bg:
        return True, f"{pf}  /  {bg}"
    return False, ("PortFast set but no BPDU guard" if pf else "no PortFast / BPDU guard")


def _trunk_pruned(c: _Ctx):
    if not c.find(r"switchport mode trunk"):
        return None, "no trunk ports"
    hit = c.find(r"switchport trunk allowed vlan")
    return (True, hit) if hit else (False, "trunk carries all VLANs")


def _vlans(c: _Ctx):
    n = c.count(r"^\s*vlan \d+")
    return (True, f"{n} VLAN(s) defined") if n else (False, "no VLANs defined")


CHECKS: List[Check] = [
    Check("hostname", "Management", "Hostname set", {"*"}, _hostname, "hostname <NAME>"),
    Check("enable_secret", "Management", "Privileged mode protected with enable secret", {"*"}, _enable_secret,
          "enable secret <strong-password>"),
    Check("pw_encrypt", "Management", "Passwords encrypted in the config", {"*"},
          _present(r"^service password-encryption"), "service password-encryption"),
    Check("ssh2", "Management", "SSH version 2 enabled", {"*"}, _present(r"^ip ssh version 2"),
          "ip ssh version 2   (after: ip domain-name <d> ; crypto key generate rsa modulus 2048)"),
    Check("vty_ssh", "Management", "Remote login is SSH only (no Telnet)", {"*"}, _vty_ssh,
          "line vty 0 4 / transport input ssh / login local"),
    Check("banner", "Management", "Login banner set", {"*"}, _present(r"^banner (motd|login)"),
          "banner motd # Authorised access only #"),
    Check("ntp", "Management", "Time sync (NTP) configured", {"*"}, _present(r"^ntp server"),
          "ntp server <ip>"),
    Check("logging", "Management", "Logging to a syslog host", {"*"}, _present(r"^logging (host )?\d+\.\d+\.\d+\.\d+"),
          "logging host <ip>"),
    Check("vlans", "Layer 2", "VLANs defined", {"switch"}, _vlans, "vlan <id> / name <NAME>"),
    Check("trunk", "Layer 2", "Trunks limited to needed VLANs", {"switch"}, _trunk_pruned,
          "switchport trunk allowed vlan <list>"),
    Check("portsec", "Layer 2", "Port security on access ports", {"switch"}, _present(r"switchport port-security"),
          "switchport port-security / switchport port-security maximum 2 / switchport port-security violation restrict"),
    Check("stp", "Layer 2", "STP edge protection (PortFast + BPDU guard)", {"switch"}, _stp_protect,
          "spanning-tree portfast default / spanning-tree portfast bpduguard default"),
    Check("dhcp_snoop", "Layer 2", "DHCP snooping enabled", {"switch"}, _present(r"^ip dhcp snooping"),
          "ip dhcp snooping / ip dhcp snooping vlan <list>"),
    Check("routing", "Layer 3", "Automatic failover routing (dynamic protocol or floating static)", {"router"},
          _failover_routing, "router ospf 1   or   ip route 0.0.0.0 0.0.0.0 <next-hop> 200"),
    Check("acl_defined", "Security", "Access lists defined", {"router"}, _present(r"^(ip )?access-list"),
          "ip access-list extended <NAME>"),
    Check("acl_applied", "Security", "Access list applied to an interface", {"router"}, _acl_applied,
          "interface <if> / ip access-group <NAME> in"),
    Check("ipsec", "Encryption", "IPsec policy configured (IKE + transform-set)", {"router"}, _ipsec,
          "crypto isakmp policy 10 ... / crypto ipsec transform-set <TS> esp-aes 256 esp-sha-hmac"),
    Check("crypto_applied", "Encryption", "IPsec applied to a WAN interface", {"router"}, _crypto_applied,
          "interface <wan-if> / crypto map <MAP>"),
]


def audit_config(name: str, text: str) -> dict:
    ctx = _Ctx(text)
    host = (ctx.find(r"^hostname\s+\S+") or "").split()[-1:] or [name]
    results = []
    for chk in CHECKS:
        if "*" not in chk.applies and not (chk.applies & ctx.roles):
            continue
        ok, evidence = chk.fn(ctx)
        status = NA if ok is None else (PASS if ok else FAIL)
        results.append({
            "id": chk.id, "category": chk.category, "label": chk.label,
            "status": status, "evidence": evidence, "fix": chk.fix if status == FAIL else "",
        })
    passed = sum(1 for r in results if r["status"] == PASS)
    failed = sum(1 for r in results if r["status"] == FAIL)
    return {
        "file": name,
        "hostname": host[0],
        "roles": sorted(ctx.roles),
        "results": results,
        "passed": passed,
        "failed": failed,
        "score_pct": (100.0 * passed / (passed + failed)) if (passed + failed) else 0.0,
    }


def audit_all(configs: Dict[str, str]) -> List[dict]:
    return [audit_config(name, text) for name, text in sorted(configs.items())]
