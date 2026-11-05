"""Run the dashboard's test cases under pytest:  python -m pytest -v"""
import pytest

from engine import tests_suite
from engine.audit import audit_config
from engine.simulator import Settings, run_simulation
from engine.topology import get_topology


@pytest.mark.parametrize("defn", tests_suite.REGISTRY, ids=lambda d: d.id)
def test_dashboard_case(defn):
    result = tests_suite.run_test(defn)
    assert result.status in (tests_suite.PASS, tests_suite.SKIP), f"{result.measured} | {result.detail}"


def test_same_seed_gives_same_result():
    a = run_simulation(Settings(seed=7)).summary()
    b = run_simulation(Settings(seed=7)).summary()
    assert a == b


def test_slow_failure_detection_loses_transactions():
    """Default OSPF dead timer (40 s) must show real loss, otherwise the model is not sensitive."""
    from engine.scenarios import make_failures
    r = run_simulation(Settings(failures=make_failures("isp_a", 30, None), detect_s=40, duration_s=150))
    assert r.summary()["failed"] > 0


def test_audit_flags_weak_switch(tmp_path):
    text = open("data/sample_configs/BR1-SW1.cfg").read()
    out = audit_config("BR1-SW1.cfg", text)
    status = {r["id"]: r["status"] for r in out["results"]}
    assert status["enable_secret"] == "FAIL" and status["vty_ssh"] == "FAIL" and status["portsec"] == "PASS"


def test_topology_loads():
    topo = get_topology()
    assert topo.route("br1") is not None


def test_spof_test_detects_a_missing_backup_isp():
    """Mutation check: remove ISP-B and the 'no single point of failure' test must fail."""
    import json
    from engine.topology import DEFAULT_TOPOLOGY, Topology
    data = json.load(open(DEFAULT_TOPOLOGY))
    data["links"] = [l for l in data["links"] if "isp_b" not in l["id"]]
    data["nodes"] = [n for n in data["nodes"] if n["id"] != "isp_b"]
    broken = Topology(data)
    defn = next(d for d in tests_suite.REGISTRY if d.id == "T02")
    assert tests_suite.run_test(defn, broken).status == tests_suite.FAIL


def test_encryption_test_detects_a_plain_wan_link():
    import json
    from engine.topology import DEFAULT_TOPOLOGY, Topology
    data = json.load(open(DEFAULT_TOPOLOGY))
    data["links"][4]["encrypted"] = False  # first WAN link
    defn = next(d for d in tests_suite.REGISTRY if d.id == "T10")
    assert tests_suite.run_test(defn, Topology(data)).status == tests_suite.FAIL
