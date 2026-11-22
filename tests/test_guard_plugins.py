"""Guard plugins (entry-point group mcp_cybernet_lab.guards), the guard context, the numbered guard
steps and `mcl replay`. The plugin under test is tests/plugins/dummy-guard; the tests register it
themselves, so they do not need it installed. The live half is tests/plugins/live_check.py."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest
import yaml

from mcp_cybernet_lab import compose, guards
from mcp_cybernet_lab.cli import main
from mcp_cybernet_lab.loader import TestbedError, check_buildable, load_testbed
from mcp_cybernet_lab.runner import replay_run

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "tests/plugins/dummy-guard"
TESTBED = ROOT / "tests/plugins/dummy-testbed.yaml"
LABELS = {"hr": "private", "paste": "public"}


class FakeEntryPoint:
    """What importlib.metadata gives for an installed plugin."""

    def __init__(self, name: str, load, package: str = "mcl-dummy-guard", version: str = "0.1.0"):
        self.name, self.load = name, load
        self.dist = types.SimpleNamespace(name=package, version=version)


def register(monkeypatch, **classes) -> None:
    eps = {name: FakeEntryPoint(name, lambda cls=cls: cls) for name, cls in classes.items()}
    monkeypatch.setattr(guards, "plugin_entry_points", lambda: eps)


@pytest.fixture
def dummy(monkeypatch):
    """The dummy plugin, importable and registered as an installed plugin would be."""
    monkeypatch.syspath_prepend(str(PLUGIN / "src"))
    import mcl_dummy_guard
    register(monkeypatch, dummy=mcl_dummy_guard.DummyGuard)
    return mcl_dummy_guard.DummyGuard


class Recorder(guards.Guard):
    """Remembers every step put to it, with the call ID the context held at that moment."""
    name = "recorder"

    def reset(self):
        self.steps = []

    def on_call(self, server, tool, arguments):
        self.steps.append(("call", self.context.call_id, server))
        return True, ""

    def on_result(self, server, tool, arguments, text):
        self.steps.append(("result", self.context.call_id, server))


def line(server, call_id=None, seq_call=None, seq_result=None, text="ok", is_error=False, decision="allow",
         task="t-r0", numbered=True) -> dict:
    e = {"type": "tool_call", "server": server, "tool": "x", "arguments": {}, "decision": decision,
         "reason": "", "result_text": text, "is_error": is_error, "task": task}
    return e | ({"call_id": call_id, "seq_call": seq_call, "seq_result": seq_result} if numbered else {})


# --- loading ---------------------------------------------------------------------------------
def test_a_plugin_guard_is_made_by_name(dummy):
    g = guards.make_guard("dummy", {}, guards.Context(LABELS))
    assert isinstance(g, dummy) and g.context.label("hr") == "private"
    assert guards.guard_info("taint") is None
    info = guards.guard_info("dummy")
    assert info["package"] == "mcl-dummy-guard" and info["version"] == "0.1.0"
    assert info["tree_sha256"] == guards.tree_sha256(PLUGIN / "src/mcl_dummy_guard")


def test_an_unknown_guard_is_named_in_the_error(monkeypatch):
    register(monkeypatch)
    with pytest.raises(guards.UnknownGuard, match="guard 'nope': not built in"):
        guards.make_guard("nope", {})
    assert issubclass(guards.UnknownGuard, KeyError)  # what make_guard raised before plugins existed


def test_a_plugin_cannot_replace_a_built_in_guard(monkeypatch):
    eps = [FakeEntryPoint("taint", lambda: Recorder), FakeEntryPoint("other", lambda: Recorder)]
    monkeypatch.setattr("importlib.metadata.entry_points",
                        lambda: types.SimpleNamespace(select=lambda group: eps if group == guards.ENTRY_POINTS else []))
    assert list(guards.plugin_entry_points()) == ["other"]
    assert guards.guard_class("taint") is guards.Taint and guards.guard_class("other") is Recorder


def test_a_guard_without_a_context_gets_one():
    class Old:  # a guard written without the base class
        def __init__(self, policy): pass
        def reset(self): pass
        def on_call(self, server, tool, arguments): return True, ""
        def on_result(self, server, tool, arguments, text): pass

    guards.GUARDS["old"] = Old
    try:
        assert guards.make_guard("old", {}).context.labels == {}
    finally:
        del guards.GUARDS["old"]


# --- the tree hash and the import rule -----------------------------------------------------------
def test_tree_hash_follows_the_code_and_ignores_compiled_files(tmp_path):
    pkg = tmp_path / "pkg"
    (pkg / "__pycache__").mkdir(parents=True)
    (pkg / "__init__.py").write_text("x = 1\n")
    before = guards.tree_sha256(pkg)
    (pkg / "__pycache__" / "__init__.cpython-311.pyc").write_bytes(b"\0compiled")
    assert guards.tree_sha256(pkg) == before
    (pkg / "__init__.py").write_text("x = 2\n")
    assert guards.tree_sha256(pkg) != before


def test_the_dummy_plugin_imports_only_what_a_plugin_may():
    assert guards.foreign_imports(PLUGIN / "src/mcl_dummy_guard") == []


def test_other_imports_are_found(tmp_path):
    pkg = tmp_path / "heavy"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "import json\nimport yaml\nfrom . import inner\nfrom heavy.inner import y\n"
        "from mcp_cybernet_lab.guards import Guard\nfrom mcp_cybernet_lab import guards\n"
        "from mcp_cybernet_lab import detect\nimport mcp_cybernet_lab.detect\n")
    (pkg / "inner.py").write_text("import numpy.linalg\ny = 1\n")
    assert guards.foreign_imports(pkg) == ["mcp_cybernet_lab", "mcp_cybernet_lab.detect", "numpy.linalg", "yaml"]


def test_check_refuses_a_plugin_with_other_imports_or_another_hash(dummy, tmp_path, monkeypatch):
    assert "mcl-dummy-guard 0.1.0, tree hash" in guards.check("dummy")
    assert guards.check("taint") == "guard 'taint': built in"
    with pytest.raises(ValueError, match="expected 0000"):
        guards.check("dummy", "0000")
    pkg = tmp_path / "heavyguard"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("import yaml\nfrom mcp_cybernet_lab.guards import Guard\n"
                                     "class G(Guard):\n    name = 'heavy'\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    import heavyguard
    register(monkeypatch, heavy=heavyguard.G)
    with pytest.raises(ValueError, match="heavyguard also imports yaml"):
        guards.check("heavy")


# --- the steps of a task -------------------------------------------------------------------------
def test_a_session_numbers_the_steps_and_sets_the_call_in_hand():
    g = Recorder({})
    s = guards.Session(g)
    a = s.call("hr", "x", {})
    b = s.call("paste", "x", {})
    assert (a["call_id"], a["seq_call"], b["call_id"], b["seq_call"]) == ("c1", 1, "c2", 2)
    assert s.result("c2", "paste", "x", {}, "done", False) == 3
    assert s.result("c1", "hr", "x", {}, "rows", False) == 4
    assert g.steps == [("call", "c1", "hr"), ("call", "c2", "paste"), ("result", "c2", "paste"), ("result", "c1", "hr")]


@pytest.mark.parametrize("text, is_error", [("error: no such table", True), (None, None)], ids=["error", "no-answer"])
def test_an_error_result_is_never_put_to_a_guard(text, is_error):
    g = Recorder({})
    s = guards.Session(g)
    call = s.call("hr", "x", {})
    assert s.result(call["call_id"], "hr", "x", {}, text, is_error) is None
    assert g.steps == [("call", "c1", "hr")] and s.seq == 1


def test_the_dummy_guard_blocks_by_label_and_names_the_earlier_call(dummy):
    s = guards.Session(guards.make_guard("dummy", {}, guards.Context(LABELS)))
    assert s.call("paste", "publish", {})["allowed"]
    hr = s.call("hr", "query", {})
    s.result(hr["call_id"], "hr", "query", {}, "rows", False)
    blocked = s.call("paste", "publish", {})
    assert not blocked["allowed"]
    assert blocked["reason"] == "call c3 to public server paste after the result of call c2 from a private server"


# --- replay --------------------------------------------------------------------------------------
def test_replay_does_not_put_a_logged_error_result_to_the_guard(dummy):
    """The gateway never did; replay did until now. With the dummy guard the difference decides a call."""
    log = [line("hr", text="error: no such table", is_error=True, numbered=False),
           line("paste", numbered=False)]
    out = guards.replay(guards.make_guard("dummy", {}), log, LABELS)
    assert [e["replay_allowed"] for e in out] == [True, True]
    g = Recorder({})
    guards.replay(g, log)
    assert g.steps == [("call", "c1", "hr"), ("call", "c2", "paste"), ("result", "c2", "paste")]


def test_replay_applies_the_steps_in_their_logged_order_not_in_line_order(dummy):
    """Two calls in flight at once: the public call was put to the guard before the private result
    came back, so it was allowed. Line order alone would put the private result first."""
    log = [line("hr", "c1", seq_call=1, seq_result=3), line("paste", "c2", seq_call=2, seq_result=4)]
    out = guards.replay(guards.make_guard("dummy", {}), log, LABELS)
    assert [e["replay_allowed"] for e in out] == [True, True]
    old = [{k: v for k, v in e.items() if not k.startswith("seq_")} for e in log]  # a log without the numbers
    out = guards.replay(guards.make_guard("dummy", {}), old, LABELS)
    assert [e["replay_allowed"] for e in out] == [True, False]
    assert [e["replay_call_id"] for e in out] == ["c1", "c2"]


def test_replay_keeps_line_order_in_its_output_and_starts_each_task_afresh(dummy):
    log = [line("paste", "c2", seq_call=2, seq_result=3), line("hr", "c1", seq_call=1, seq_result=4),
           line("paste", "c3", seq_call=5, decision="block", text=None, seq_result=None),
           line("paste", "c1", seq_call=1, seq_result=2, task="u-r0")]
    out = guards.replay(guards.make_guard("dummy", {}), log, LABELS)
    # c2's call (step 2) comes before c1's result (step 4): only c3 rests on a private result.
    assert [(e["task"], e["call_id"], e["replay_allowed"]) for e in out] == [
        ("t-r0", "c2", True), ("t-r0", "c1", True), ("t-r0", "c3", False), ("u-r0", "c1", True)]


def test_replay_reads_labels_from_the_start_line_and_an_argument_overrides_them(dummy):
    start = {"type": "start", "guard": "dummy", "labels": LABELS, "task": "t-r0"}
    log = [start, line("hr", numbered=False), line("paste", numbered=False)]
    assert [e["replay_allowed"] for e in guards.replay(guards.make_guard("dummy", {}), log)] == [True, False]
    assert [e["replay_allowed"] for e in guards.replay(guards.make_guard("dummy", {}), log[1:])] == [True, True]
    public = {"hr": "public", "paste": "public"}
    assert [e["replay_allowed"] for e in guards.replay(guards.make_guard("dummy", {}), log, public)] == [True, True]


def test_replay_of_a_built_in_guard_on_an_old_log_is_what_it_was():
    policy = {"sources": ["hr"], "sinks": ["paste"], "patterns": [{"name": "id", "regex": r"\b[12]\d{9}\b"}]}
    number = "1" + "0" * 9
    log = [dict(line("hr", text=f"id {number}", numbered=False)),
           dict(line("paste", numbered=False), arguments={"text": f"see {number}"}),
           {"type": "message", "server": "hr", "method": "tools/list", "task": "t-r0"}]
    out = guards.replay(guards.make_guard("taint", policy), log)
    assert [(e["replay_allowed"], e["replay_reason"]) for e in out] == [
        (True, ""), (False, "tainted value from id@hr sent to sink paste")]
    assert all(set(e) - set(src) == {"replay_allowed", "replay_reason", "replay_call_id"} for e, src in zip(out, log))


# --- the gateway ---------------------------------------------------------------------------------
@pytest.fixture
def gateway(dummy, monkeypatch, tmp_path):
    """The gateway module as it runs in its container (its own `guards` module), with the dummy guard."""
    monkeypatch.setenv("MCL_GATEWAY", json.dumps({"guard": "dummy", "servers": {
        "hr": {"label": "private", "domain": "corp"}, "paste": {"label": "public", "domain": "public"}}}))
    monkeypatch.setenv("MCL_POLICY_FILE", str(tmp_path / "none.json"))
    for name in ("gateway", "guards"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.syspath_prepend(str(ROOT / "mcp_cybernet_lab"))
    import gateway as module
    import guards as inner
    monkeypatch.setattr(inner, "plugin_entry_points", guards.plugin_entry_points)
    log = tmp_path / "gateway.jsonl"
    module.load_guard()
    module.State.log, module.State.task, module.State.pending = open(log, "w", encoding="utf-8"), "t-r0", {}
    module.write(type="start", guard="dummy", policy_version=None, labels=module.labels(), plugins=module.State.plugins)

    def call(server, request_id, answer=None):
        """One tools/call through the gateway's two hooks; answer None = stop after the call hook."""
        handler = types.SimpleNamespace(server_id=server, headers={"Mcp-Session-Id": "s"})
        body = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                           "params": {"name": "x", "arguments": {}}}).encode()
        blocked = module.Gateway.before(handler, body)
        return blocked, lambda result: module.Gateway.after(
            handler, body, 200, "application/json", json.dumps({"jsonrpc": "2.0", "id": request_id, **result}).encode())

    def read():
        module.State.log.flush()
        return [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()]

    yield types.SimpleNamespace(call=call, read=read)
    module.State.log.close()
    module.State.log = None


def ok(text="ok"):
    return {"result": {"content": [{"type": "text", "text": text}]}}


def test_gateway_lines_carry_label_call_id_and_step_numbers(gateway):
    _, hr_returns = gateway.call("hr", 1)           # c1 leaves for the private server
    blocked, paste_returns = gateway.call("paste", 2)  # c2 is put to the guard before c1's result
    assert blocked is None
    hr_returns(ok("rows"))
    paste_returns(ok())
    _, failed = gateway.call("hr", 3)
    failed({"result": {"content": [{"type": "text", "text": "bad sql"}], "isError": True}})
    blocked, _ = gateway.call("paste", 4)
    assert b"after the result of call c1" in blocked
    start, *calls = gateway.read()
    assert start["labels"] == LABELS and start["plugins"][0]["package"] == "mcl-dummy-guard"
    assert start["plugins"][0]["tree_sha256"] == guards.tree_sha256(PLUGIN / "src/mcl_dummy_guard")
    assert [(e["call_id"], e["label"], e["seq_call"], e["seq_result"], e["decision"]) for e in calls] == [
        ("c1", "private", 1, 3, "allow"), ("c2", "public", 2, 4, "allow"),
        ("c3", "private", 5, None, "allow"), ("c4", "public", 6, None, "block")]
    old_fields = ["type", "server", "tool", "arguments", "decision", "reason", "result_text"]
    assert list(calls[0])[:7] == old_fields and list(calls[3])[:7] == old_fields  # the old fields, in their old order
    again = guards.replay(guards.make_guard("dummy", {}), [start, *calls])
    assert [e["replay_allowed"] for e in again] == [e["decision"] == "allow" for e in calls]
    assert again[3]["replay_reason"] == calls[3]["reason"]


# --- the testbed file and the build --------------------------------------------------------------
def tb(tmp_path, **gateway):
    f = tmp_path / "t.yaml"
    f.write_text(yaml.safe_dump({"testbed": "t", "defaults": {"transport": "http"}, "domains": [{"id": "d"}],
                                 "servers": [{"id": "a", "runtime": "python", "package": "x", "version": "1", "domain": "d"}],
                                 "clients": [{"id": "agent", "servers": ["a"]}], "gateway": gateway}))
    return f


def test_guard_names_are_checked_at_build_not_by_the_schema(tmp_path, monkeypatch):
    register(monkeypatch)
    loaded = load_testbed(tb(tmp_path, guard="my-guard"))  # any name is a valid file
    with pytest.raises(TestbedError, match="guard 'my-guard': not built in .* Install the plugin on this machine"):
        check_buildable(loaded.testbed)
    with pytest.raises(TestbedError, match="not implemented in this version:\n  - gateway.guard 'fides'"):
        check_buildable(load_testbed(tb(tmp_path, guard="fides")).testbed)
    with pytest.raises(TestbedError, match="gateway.guard: .*id 'My Guard' must match"):
        load_testbed(tb(tmp_path, guard="My Guard"))
    for name in ("none", "passthrough", "taint"):
        check_buildable(load_testbed(tb(tmp_path, guard=name)).testbed)


def test_a_plugin_guard_needs_its_folder_in_the_testbed(tmp_path, dummy):
    with pytest.raises(TestbedError, match="comes from a plugin: list the plugin's folder in gateway.plugins"):
        check_buildable(load_testbed(tb(tmp_path, guard="dummy")).testbed)
    with pytest.raises(TestbedError, match="'nowhere' must be a folder with a pyproject.toml"):
        load_testbed(tb(tmp_path, guard="dummy", plugins=["nowhere"]))
    with pytest.raises(TestbedError, match="gateway.plugins needs a gateway"):
        load_testbed(tb(tmp_path, plugins=[str(PLUGIN)]))


def test_without_plugins_the_gateway_image_is_built_as_before(tmp_path):
    for name in ("enterprise-guarded", "enterprise-attacks"):
        out = compose.build(load_testbed(ROOT / f"testbeds/{name}.yaml"), tmp_path / name, tmp_path / "runs" / name)
        text = (out / "dockerfiles/gateway.Dockerfile").read_text()
        assert text == compose.dockerfile([], ["python3", "/opt/mcl/gateway.py", "serve"])
        assert "plugin" not in text and "PYTHONPATH" not in text
        assert not (out / "context/plugins").exists() and not (out / "context/mcp_cybernet_lab").exists()
        manifest = json.loads((out / "manifest.json").read_text())
        assert manifest["guard_plugin"] is None and manifest["plugins"] == [] and manifest["gateway"]["plugins"] == []


def test_a_plugin_is_copied_installed_offline_and_checked_in_the_image(tmp_path, dummy):
    out = compose.build(load_testbed(TESTBED), tmp_path / "b", tmp_path / "runs")
    sha = guards.tree_sha256(PLUGIN / "src/mcl_dummy_guard")
    lines = (out / "dockerfiles/gateway.Dockerfile").read_text().splitlines()
    assert lines[-7:-2] == [
        "COPY mcp_cybernet_lab /opt/mcl/mcp_cybernet_lab",
        "COPY plugins /opt/mcl-plugins",
        "RUN uv pip install --no-deps --no-index --python /usr/bin/python3 --target /opt/mcl/site "
        "/opt/mcl-plugins/dummy-guard",
        "ENV PYTHONPATH=/opt/mcl/site",
        f"RUN python3 /opt/mcl/guards.py check dummy {sha}"]
    assert (out / "context/plugins/dummy-guard/src/mcl_dummy_guard/__init__.py").is_file()
    assert not list((out / "context/plugins").rglob("__pycache__"))
    assert (out / "context/mcp_cybernet_lab/guards.py").read_text() == compose.GUARDS_SHIM
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["guard_plugin"] == {"guard": "dummy", "package": "mcl-dummy-guard", "version": "0.1.0",
                                        "tree_sha256": sha}
    assert [p["name"] for p in manifest["plugins"]] == ["dummy-guard"] and manifest["plugins"][0]["source_sha256"]
    built = yaml.safe_load((out / "docker-compose.yml").read_text())["services"]["gateway"]
    assert set(built) == {"build", "image", "command", "environment", "volumes", "networks", "labels"}
    assert built["labels"]["mcl.guard"] == "dummy"
    # only the gateway image gets the plugin
    assert "plugins" not in (out / "dockerfiles/agent.Dockerfile").read_text()


def test_the_shim_makes_the_package_name_mean_the_gateways_module(tmp_path, monkeypatch):
    """In the image, `mcp_cybernet_lab.guards` must be the module /opt/mcl/gateway.py imports as `guards`."""
    site = tmp_path / "opt"
    (site / "mcp_cybernet_lab").mkdir(parents=True)
    (site / "mcp_cybernet_lab/__init__.py").write_text("")
    (site / "mcp_cybernet_lab/guards.py").write_text(compose.GUARDS_SHIM)
    (site / "guards.py").write_text((ROOT / "mcp_cybernet_lab/guards.py").read_text())
    code = ("import guards\nfrom mcp_cybernet_lab.guards import Guard\nimport mcp_cybernet_lab.guards as g\n"
            "assert Guard is guards.Guard and g is guards\nprint('same module')")
    import subprocess
    r = subprocess.run([sys.executable, "-c", code], cwd=site, capture_output=True, text=True,
                       env={"PYTHONPATH": str(site), "PATH": ""})
    assert r.stdout.strip() == "same module", r.stderr


# --- mcl replay ----------------------------------------------------------------------------------
def run_folder(tmp_path, plugin_sha: str | None, guard="dummy") -> Path:
    run = tmp_path / "runs/t/live"
    (run / "t-r0").mkdir(parents=True)
    info = {"guard": guard, "package": "mcl-dummy-guard", "version": "0.1.0", "tree_sha256": plugin_sha}
    (run / "manifest.json").write_text(json.dumps({
        "testbed": "t", "gateway": {"guard": guard}, "guard_plugin": info if plugin_sha else None,
        "servers": [{"id": s, "label": label} for s, label in LABELS.items()]}))
    (run / "policy.json").write_text(json.dumps({"version": 0, "sources": ["hr"], "sinks": ["paste"], "patterns": []}))
    log = [{"type": "start", "guard": guard, "labels": LABELS, "plugins": [info] if plugin_sha else [], "task": "t-r0"},
           line("hr", "c1", 1, 2), line("paste", "c2", 3, None, text=None, decision="block")]
    (run / "t-r0/gateway.jsonl").write_text("".join(json.dumps(e) + "\n" for e in log))
    return run


def test_mcl_replay_reproduces_a_run_and_writes_only_with_out(tmp_path, dummy, capsys):
    sha = guards.tree_sha256(PLUGIN / "src/mcl_dummy_guard")
    run = run_folder(tmp_path, sha)
    before = sorted((p, p.stat().st_mtime_ns) for p in run.rglob("*"))
    assert main(["replay", str(run)]) == 0
    text = capsys.readouterr().out
    assert f"guard   dummy (plugin mcl-dummy-guard 0.1.0, tree hash {sha})" in text
    assert "policy  the run's policy.json, version 0, sha256 " in text
    assert "same decision as logged: 2 of 2" in text and "nothing was written" in text
    assert sorted((p, p.stat().st_mtime_ns) for p in run.rglob("*")) == before
    out = tmp_path / "decisions.jsonl"
    assert main(["replay", str(run), "--out", str(out)]) == 0
    head, *decisions = [json.loads(l) for l in out.read_text().splitlines()]
    assert head["type"] == "replay" and head["guard_plugin"]["tree_sha256"] == sha and len(head["policy_sha256"]) == 64
    assert [(d["call_id"], d["label"], d["logged"], d["replayed"]) for d in decisions] == [
        ("c1", "private", "allow", "allow"), ("c2", "public", "block", "block")]
    assert "arguments" not in decisions[0] and "result_text" not in decisions[0]


def test_mcl_replay_refuses_other_code_than_the_code_that_ran(tmp_path, dummy, capsys):
    run = run_folder(tmp_path, "f" * 64)
    assert main(["replay", str(run)]) == 2
    err = capsys.readouterr().err
    assert "guard 'dummy' is not the code that ran" in err and "f" * 64 in err
    assert main(["replay", str(run), "--guard", "taint"]) == 0  # another guard is no reproduction: no check
    assert "the run was made with dummy" in capsys.readouterr().out


def test_mcl_replay_with_another_guard_or_policy_reports_the_differences(tmp_path, dummy, capsys):
    run = run_folder(tmp_path, None, guard="passthrough")
    (run / "t-r0/gateway.jsonl").write_text("".join(json.dumps(e) + "\n" for e in [
        line("hr", numbered=False), line("paste", numbered=False)]))
    assert main(["replay", str(run)]) == 0 and "guard   passthrough (built in)" in capsys.readouterr().out
    assert main(["replay", str(run), "--guard", "dummy"]) == 0  # differences from another guard are a finding
    text = capsys.readouterr().out
    assert "same decision as logged: 1 of 2" in text
    assert "t-r0 c2 paste.x: logged allow, replayed block (call c2 to public server paste after" in text
    policy = tmp_path / "p.yaml"
    policy.write_text("version: 7\nsinks: []\n")
    assert main(["replay", str(run), "--policy", str(policy)]) == 0
    assert f"policy  {policy}, version 7" in capsys.readouterr().out


def test_mcl_replay_exits_1_when_a_run_is_not_reproduced_and_refuses_to_write_into_it(tmp_path, dummy, capsys):
    run = run_folder(tmp_path, guards.tree_sha256(PLUGIN / "src/mcl_dummy_guard"))
    assert main(["replay", str(run), "--out", str(run / "x.jsonl")]) == 2
    assert "inside the run folder" in capsys.readouterr().err
    text = (run / "t-r0/gateway.jsonl").read_text().replace('"decision": "block"', '"decision": "allow"')
    (run / "t-r0/gateway.jsonl").write_text(text)
    assert main(["replay", str(run)]) == 1


def test_mcl_replay_needs_a_guarded_run(tmp_path, capsys):
    run = run_folder(tmp_path, None, guard="none")
    assert main(["replay", str(run)]) == 2 and "without a gateway" in capsys.readouterr().err
