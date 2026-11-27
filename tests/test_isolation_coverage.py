"""Unit tests for the two checks of a guarded testbed: `mcl check-isolation` (what a client can
reach) and `mcl check-coverage` (every tool call of the agent is in the gateway log).
No Docker needed: the plan, the report and the comparison are tested on made-up data."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from mcp_cybernet_lab import isolation, netprobe
from mcp_cybernet_lab.cli import main
from mcp_cybernet_lab.compose import build
from mcp_cybernet_lab.loader import TestbedError, load_testbed
from mcp_cybernet_lab.report import gateway_coverage

ROOT = Path(__file__).resolve().parent.parent
HOST = "api.deepseek.com"


# --- mcl check-isolation -----------------------------------------------------------------------
def test_isolation_plan_with_a_relay():
    tb = load_testbed(ROOT / "testbeds/enterprise-attacks.yaml").testbed
    addr = {s.id: {f"net-{s.domain}": [f"10.0.{i}.2"]} for i, s in enumerate(tb.servers)}
    addr |= {"webhook": {"net-public": ["10.0.50.2"]}, "llm-egress": {"net-public": ["10.0.50.4"]},
             "llm-relay": {"net-gateway": ["10.0.99.3"], "net-public": ["10.0.50.3"]},
             "agent": {"net-gateway": ["10.0.99.2"]}, "gateway": {"net-gateway": ["10.0.99.4"]}}
    p = isolation.plan(tb, addr, "93.184.216.34")
    assert p["model"] == {"url": "https://api.deepseek.com/models", "host": HOST, "port": 443,
                          "name": "deepseek-flash", "provider": "deepseek", "key_env": "DEEPSEEK_API_KEY"}
    assert p["relay"] == {"addresses": ["10.0.99.3"]}  # where the client meets it, not its other side
    assert p["outside"] == {"name": "example.com", "address": "93.184.216.34", "public_address": "1.1.1.1"}
    closed = {t["host"]: t for t in p["closed"]}
    assert set(closed) == {s.id for s in tb.servers} | {"webhook", "llm-egress"}  # all but gateway and relay
    assert closed["paste"] == {"what": "public server", "host": "paste", "port": 8080,
                               "addresses": addr["paste"]["net-public"]}
    assert closed["hr"]["what"] == "private server" and closed["translator"]["what"] == "mixed server"
    assert closed["llm-egress"]["port"] == 443 and closed["webhook"]["what"] == "public service"


def test_isolation_plan_with_a_model_service(tmp_path):
    (tmp_path / "tb.yaml").write_text(yaml.safe_dump({
        "testbed": "m", "defaults": {"transport": "http"}, "domains": [{"id": "corp"}],
        "llm": {"provider": "vllm", "model": "m", "base_url": "http://vllm:8000/v1"},
        "servers": [{"id": x, "package": "p", "runtime": "python", "version": "1", "domain": "corp"} for x in "ab"],
        "services": [{"id": "vllm", "image": "vllm/vllm-openai:v0.6.0", "domain": "corp"}],
        "clients": [{"id": "agent", "servers": ["a", "b"]}], "gateway": {"guard": "passthrough"}}))
    tb = load_testbed(tmp_path / "tb.yaml").testbed
    p = isolation.plan(tb, {"a": {"net-corp": ["10.0.1.2"]}}, None)
    assert p["relay"] is None and p["model"]["url"] == "http://vllm:8000/v1/models" and p["model"]["port"] == 8000
    assert [t["host"] for t in p["closed"]] == ["a", "b"]  # the model service is meant to answer
    assert p["closed"][1]["addresses"] == []                # not running: the probe reports it


def test_isolation_is_for_guarded_testbeds(tmp_path, capsys):
    loaded = load_testbed(ROOT / "testbeds/toy-network.yaml")  # no gateway
    with pytest.raises(TestbedError, match="with a gateway"):
        isolation.check(loaded, tmp_path)
    build(loaded, tmp_path / "build" / "toy-network")
    assert main(["--out", str(tmp_path / "build"), "check-isolation", str(loaded.path)]) == 2
    assert "with a gateway" in capsys.readouterr().err


def test_isolation_report():
    text = isolation.render([{"client": "agent", "name": "gateway reachable", "ok": True, "detail": "HTTP 404"},
                             {"client": "agent", "name": "paste does not resolve", "ok": False, "detail": "x"}])
    assert text.splitlines() == ["client agent", "PASS  gateway reachable: HTTP 404",
                                 "FAIL  paste does not resolve: x", "1/2 checks have the expected outcome"]


def test_probe_reads_routes_and_never_prints_the_key(monkeypatch, capsys):
    head = "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
    local = "eth0\t000012AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
    assert netprobe.default_routes(head + local) == []
    default = "eth1\t00000000\t010013AC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
    assert netprobe.default_routes(head + default + local) == ["eth1"]
    monkeypatch.setattr(netprobe, "KEY", "KEY-0123-not-a-real-key")
    netprobe.check("model API answers", True, "the server echoed KEY-0123-not-a-real-key back")
    out = capsys.readouterr().out
    assert "KEY-0123-not-a-real-key" not in out and json.loads(out)["detail"] == "the server echoed [key] back"


# --- mcl check-coverage ------------------------------------------------------------------------
def call(server, tool, value, blocked=False):
    return {"server": server, "tool": tool, "arguments": {"text": value}, "blocked": blocked}


def run_folder(tmp_path, trace, gateway, guard="taint") -> Path:
    """A run folder with one task run: the calls the agent recorded and those the gateway recorded."""
    run = tmp_path / "run"
    (run / "t-r0").mkdir(parents=True, exist_ok=True)
    (run / "manifest.json").write_text(json.dumps({"gateway": {"guard": guard}}))
    (run / "results.jsonl").write_text(json.dumps({"task": "t", "repeat": 0, "run": "t-r0"}) + "\n")
    events = [{"type": "connect", "server": "hr", "ok": True}] + [{"type": "tool_call", **c} for c in trace]
    (run / "t-r0/trace.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    log = [{"type": "start", "guard": guard, "policy_version": 0, "task": "t-r0"},
           {"type": "message", "server": "hr", "method": "initialize", "task": "t-r0"}]
    log += [{"type": "tool_call", "server": c["server"], "tool": c["tool"], "arguments": c["arguments"],
             "decision": "block" if c["blocked"] else "allow", "task": c.get("task", "t-r0")} for c in gateway]
    (run / "t-r0/gateway.jsonl").write_text("".join(json.dumps(e) + "\n" for e in log))
    return run


def test_coverage_when_every_call_is_in_the_gateway_log(tmp_path):
    calls = [call("hr", "query", "a"), call("paste", "create", "SECRET-CANARY", blocked=True)]
    unknown = {"server": None, "tool": None, "arguments": {}, "blocked": False}  # the model made a tool up
    run = run_folder(tmp_path, calls + [unknown], calls)
    ok, text = gateway_coverage(run)
    assert ok and "tool calls in agent traces 2   tool calls in gateway logs 2" in text
    assert "differ (server, tool, arguments, order, block flag): 0" in text
    assert "blocked calls: traces 1, gateway logs 1" in text and "taint v0 1" in text
    assert "never sent): 1" in text
    assert main(["check-coverage", str(run)]) == 0


@pytest.mark.parametrize("trace, gateway, says", [
    ([call("hr", "query", "a"), call("paste", "create", "SECRET-CANARY")], [call("hr", "query", "a")],
     "only in the trace ['paste.create']"),                                    # a call around the gateway
    ([call("hr", "query", "a")], [call("hr", "query", "a"), call("paste", "create", "SECRET-CANARY")],
     "only in the gateway log ['paste.create']"),
    ([call("paste", "create", "SECRET-CANARY")], [call("paste", "create", "other")], "only in the trace"),
    ([call("hr", "query", "a"), call("hr", "query", "b")], [call("hr", "query", "b"), call("hr", "query", "a")],
     "the order or a block flag differs"),
    ([call("paste", "create", "x", blocked=True)], [call("paste", "create", "x")], "block flag differs"),
    ([call("hr", "query", "a")], [call("hr", "query", "a") | {"task": "other-r0"}], "another task's name"),
], ids=["missing-in-gateway", "missing-in-trace", "arguments", "order", "block-flag", "task"])
def test_coverage_reports_every_kind_of_difference(tmp_path, trace, gateway, says):
    run = run_folder(tmp_path, trace, gateway)
    ok, text = gateway_coverage(run)
    assert not ok and says in text and "MISMATCH t-r0" in text
    assert "SECRET-CANARY" not in text  # arguments are compared, never printed
    assert main(["check-coverage", str(run)]) == 1


def test_coverage_needs_a_gateway_log(tmp_path):
    run = run_folder(tmp_path, [call("hr", "query", "a")], [call("hr", "query", "a")])
    (run / "t-r0/gateway.jsonl").unlink()
    ok, text = gateway_coverage(run)
    assert not ok and "no gateway log" in text
    ok, text = gateway_coverage(run_folder(tmp_path, [], [], guard="none"))
    assert not ok and "without a gateway" in text
