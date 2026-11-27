"""The names a policy file may list as its sources and sinks (`compose.initial_policy`).

A name is a server of the testbed, or `server/tool` for a server of the testbed, which is how a single
tool is listed since a tool can have a label of its own (`tool_labels`). Until 27 Nov 2026 the check
accepted names of servers only, while the default policy of a testbed with a public tool on a mixed
server lists that tool among its sinks. On the guarded benchmark testbed every policy file that did
not replace the default sinks therefore failed to build, with any guard. No Docker, no model.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
import yaml

from mcp_cybernet_lab import compose, guards
from mcp_cybernet_lab.loader import TestbedError, load_testbed

from test_guard_plugins import PLUGIN, dummy, make_wheel  # noqa: F401  (dummy is a fixture)

ROOT = Path(__file__).resolve().parent.parent
GUARDED = ROOT / "testbeds" / "enterprise-v2-guarded.yaml"
REPLY = "ticketing/reply_ticket"
# its eight servers, the three of the shipped attacks and the translator
SERVERS = ["crm", "fetch", "files", "hr", "news", "partners", "paste", "ticketing", "translator", "utilities", "utilities-v2", "web"]


def guarded(tmp_path, policy: dict | None, **gateway):
    """The guarded benchmark testbed with another policy file (and, when given, another guard)."""
    loaded = load_testbed(GUARDED)
    f = tmp_path / "policy.yaml"
    f.write_text("# a policy file that sets nothing\n" if policy is None else yaml.safe_dump(policy), encoding="utf-8")
    gate = loaded.testbed.gateway.model_copy(update={"policy": str(f)} | gateway)
    return dataclasses.replace(loaded, testbed=loaded.testbed.model_copy(update={"gateway": gate}))


def built_policy(loaded, tmp_path) -> dict:
    out = compose.build(loaded, tmp_path / "out", tmp_path / "runs")
    return json.loads((out / "policy" / "current.json").read_text(encoding="utf-8"))


# --- the three builds that failed --------------------------------------------------------------------
def test_a_policy_file_that_sets_nothing_builds_with_the_default_sinks(tmp_path):
    policy = built_policy(guarded(tmp_path, None), tmp_path)
    assert policy["sinks"] == ["fetch", "news", "partners", "paste", REPLY, "utilities", "utilities-v2", "web"]
    assert policy["sources"] == ["crm", "files", "hr", "ticketing", "translator"] and policy["version"] == 0


def test_a_policy_file_that_lists_the_tool_among_its_sinks_builds(tmp_path):
    policy = built_policy(guarded(tmp_path, {"version": 1, "sources": ["hr", "crm"], "sinks": ["paste", REPLY]}), tmp_path)
    assert policy["sinks"] == ["paste", REPLY] and policy["sources"] == ["hr", "crm"]
    # ... and the lab's guard reads the name as a call to that tool and to no other tool of the server.
    key = "sk_live_" + "A1b2C3d4" * 3
    guard = guards.make_guard("taint", policy, guards.Context({"crm": "private", "ticketing": "mixed", "paste": "public",
                                                               REPLY: "public"}))
    guard.on_result("crm", "get_settings", {}, f"key {key}")
    assert guard.on_call("ticketing", "get_ticket", {"note": key})[0] is True
    assert guard.on_call("ticketing", "reply_ticket", {"text": key})[0] is False


def test_a_plugin_guard_with_a_policy_file_that_sets_no_sinks_builds(tmp_path, dummy, monkeypatch):  # noqa: F811
    """What a guard plugin's own policy file looks like: keys of its own, and no list of sinks."""
    monkeypatch.setattr(compose, "build_wheel", make_wheel)
    loaded = guarded(tmp_path, {"version": 2, "families": ["A", "B"], "threshold": 4}, guard="dummy", plugins=[str(PLUGIN)])
    out = compose.build(loaded, tmp_path / "out", tmp_path / "runs")
    policy = json.loads((out / "policy" / "current.json").read_text(encoding="utf-8"))
    assert policy["families"] == ["A", "B"] and policy["threshold"] == 4 and policy["version"] == 2
    assert REPLY in policy["sinks"] and "ticketing" in policy["sources"] and "ticketing" not in policy["sinks"]
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["guard_plugin"]["guard"] == "dummy"
    built = yaml.safe_load((out / "docker-compose.yml").read_text(encoding="utf-8"))["services"]["gateway"]
    assert built["labels"]["mcl.guard"] == "dummy"


# --- what still fails --------------------------------------------------------------------------------
@pytest.mark.parametrize("policy, says", [
    ({"sinks": ["paste", "pastebin"]}, r"gateway.policy: unknown servers \['pastebin'\]$"),
    ({"sources": ["hr", "payroll"]}, r"gateway.policy: unknown servers \['payroll'\]$"),
    ({"sinks": ["paste", "helpdesk/reply_ticket"]}, r"gateway.policy: tools on unknown servers \['helpdesk/reply_ticket'\]$"),
    ({"sources": ["mail/read"], "sinks": ["pastebin", "mail/send", "paste"]},
     r"gateway.policy: unknown servers \['pastebin'\]; tools on unknown servers \['mail/read', 'mail/send'\]$"),
    ({"sinks": ["ticketing/"]}, r"gateway.policy: \['ticketing/'\] name no tool \(a single tool is listed as server/tool\)$"),
    ({"sinks": ["/reply_ticket"]}, r"gateway.policy: tools on unknown servers \['/reply_ticket'\]$"),
], ids=["server-in-sinks", "server-in-sources", "tool-on-unknown-server", "both-kinds", "no-tool", "no-server"])
def test_an_unknown_server_fails_and_so_does_a_tool_on_one(tmp_path, policy, says):
    with pytest.raises(TestbedError, match=says):
        compose.initial_policy(guarded(tmp_path, policy))


def test_the_tool_of_a_known_server_is_not_checked_at_build(tmp_path):
    """The build has no list of the tools a server offers: a server lists them when it runs, and
    `mcl smoke` reads that list. So a tool's name is accepted on any server of the testbed, with a
    label of its own or without one. A policy may make a sink of a tool the testbed does not label."""
    policy = compose.initial_policy(guarded(tmp_path, {"sinks": ["paste", "ticketing/get_ticket", "crm/no_such_tool"]}))
    assert policy["sinks"] == ["paste", "ticketing/get_ticket", "crm/no_such_tool"]


# --- no build that worked changes --------------------------------------------------------------------
def test_every_shipped_testbed_has_the_first_policy_it_had():
    """Three shipped testbeds name a policy file, the same one. Its lists are servers, so the check
    passed before and passes now, and the policy written is the file over the testbed's default."""
    shipped = yaml.safe_load((ROOT / "policies" / "enterprise-v0.yaml").read_text(encoding="utf-8"))
    assert shipped["sources"] == ["hr"] and shipped["sinks"] == ["paste", "web", "fetch"]
    with_policy = []
    for path in sorted((ROOT / "testbeds").glob("*.yaml")):
        loaded = load_testbed(path)
        tb = loaded.testbed
        default = guards.default_policy({s.id: {"label": s.label, "tool_labels": s.tool_labels} for s in tb.servers})
        policy = compose.initial_policy(loaded)
        if tb.gateway.policy:
            with_policy.append(tb.testbed)
            assert policy == default | shipped, path.name
        else:
            assert policy == default, path.name
    assert with_policy == ["enterprise-attacks", "enterprise-guarded", "enterprise-v2-guarded"]


def test_the_guarded_benchmark_testbed_knows_these_servers():
    assert sorted(s.id for s in load_testbed(GUARDED).testbed.servers) == SERVERS
