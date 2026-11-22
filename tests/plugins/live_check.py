#!/usr/bin/env python3
"""Live check of guard plugins: a plugin guard decides in a running gateway, and `mcl replay`
reproduces its decisions from the gateway log. Needs Docker; uses no model and no key.

    pip install -e tests/plugins/dummy-guard        (once: the build and the replay load the plugin here)
    python tests/plugins/live_check.py WORK_DIR     (an empty folder for the build and the run)

It builds and starts tests/plugins/dummy-testbed.yaml, makes seven tool calls from the client
container through the gateway, stops the testbed, and replays the log. The guard `dummy` blocks a
call to a public server once a private server has returned a result, and the policy
(dummy-policy.yaml) makes it raise once in each hook. Expected, in order:

    c1 paste.publish      allowed   nothing private seen yet
    c2 hr.query           allowed   invalid SQL: an error result, which is never put to a guard
    c3 paste.publish      allowed   so the guard has still seen nothing private
    c4 paste.recent       blocked   the guard raises in on_call: fails closed, reason "guard error"
    c5 hr.describe_table  withheld  the guard raises in on_result: the tool ran, the client gets an error
    c6 hr.list_tables     allowed   a private result
    c7 paste.publish      blocked   because of the result of c6

Exit status 0 when the live decisions, the log fields and the replay are all as expected.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TESTBED = HERE / "dummy-testbed.yaml"
RUN, TASK = "live", "check-r0"
CALLS = [("paste", "publish", {"title": "one", "text": "first note"}),
         ("hr", "query", {"sql": "SELEC nothing FROM nowhere"}),
         ("paste", "publish", {"title": "two", "text": "second note"}),
         ("paste", "recent", {}),
         ("hr", "describe_table", {"table": "employees"}),
         ("hr", "list_tables", {}),
         ("paste", "publish", {"title": "three", "text": "third note"})]
LIVE = ["allow", "allow", "allow", "block", "withheld", "allow", "block"]   # what the client gets
LOGGED = ["allow", "allow", "allow", "block", "allow", "allow", "block"]    # the guard's decision on the call

# Runs in the client container: one MCP session per server, the calls in order, one JSON line each.
DRIVER = r"""
import json, os, sys
sys.path.insert(0, "/opt/mcl")
from mcpclient import MCPError, open_session, result_text
specs = json.loads(os.environ["MCL_SERVERS"])
sessions = {}
for server, tool, arguments in json.loads(sys.argv[1]):
    if server not in sessions:
        sessions[server] = open_session(specs[server])
        sessions[server].initialize()
    try:
        result = sessions[server].call_tool(tool, arguments)
        out = {"decision": "allow", "is_error": bool(result.get("isError"))}
    except MCPError as e:
        out = {"decision": {-32001: "block", -32002: "withheld"}.get(e.error.get("code"), "error"), "message": str(e)}
    print(json.dumps({"server": server, "tool": tool} | out), flush=True)
for s in sessions.values():
    s.close()
"""


def sh(*cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode:
        sys.exit(f"FAILED {' '.join(cmd[:6])} ...\n{(r.stderr or r.stdout)[-2000:]}")
    return r


def main() -> int:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    work = Path(sys.argv[1]).resolve()
    build, runs = work / "build", work / "runs"
    mcl = [sys.executable, "-m", "mcp_cybernet_lab", "--out", str(build), "--runs", str(runs)]
    out = build / "plugin-check"
    compose = ["docker", "compose", "-f", str(out / "docker-compose.yml")]
    problems: list[str] = []

    def expect(ok: bool, what: str) -> None:
        print(("PASS  " if ok else "FAIL  ") + what)
        if not ok:
            problems.append(what)

    print(sh(*mcl, "build", str(TESTBED)).stdout.strip())
    run = runs / "plugin-check" / RUN
    (run / TASK).mkdir(parents=True)  # as `mcl run` does: the gateway then writes its log into our folder
    try:
        sh(*mcl, "up", str(TESTBED), "--wait")
        smoke = sh(*mcl, "smoke", str(TESTBED), check=False)
        expect(smoke.returncode == 0, "mcl smoke: " + smoke.stdout.strip().splitlines()[-1])
        sh(*compose, "exec", "-T", "gateway", "python3", "/opt/mcl/gateway.py", "ctl", "start",
           f"/runs/{RUN}/{TASK}/gateway.jsonl", TASK)
        r = sh(*compose, "exec", "-T", "agent", "python3", "-c", DRIVER, json.dumps(CALLS))
        sh(*compose, "exec", "-T", "gateway", "python3", "/opt/mcl/gateway.py", "ctl", "stop")
    finally:
        sh(*mcl, "down", str(TESTBED), check=False)

    live = [json.loads(line) for line in r.stdout.splitlines() if line.startswith("{")]
    for i, call in enumerate(live, 1):
        print(f"  c{i} {call['server']}.{call['tool']}: {call['decision']}"
              + (", error result" if call.get("is_error") else "")
              + (f" ({call['message']})" if call.get("message") else ""))
    expect([c["decision"] for c in live] == LIVE, f"what the client got: {LIVE}")
    expect(len(live) > 1 and live[1].get("is_error") is True, "c2 returned an error result")
    expect(len(live) == 7 and live[3]["message"] == "blocked: guard error"
           and live[4]["message"] == "result withheld: guard error",
           "c4 and c5: the client is told of a guard error, not what the guard raised")
    expect("after the result of call c6" in (live[-1].get("message") or "") if live else False,
           "the block of c7 names the earlier result it rests on (c6), not c5, whose result the guard failed on")

    for name, source in (("manifest.json", out / "manifest.json"), ("policy.json", out / "policy" / "current.json")):
        shutil.copy(source, run / name)  # what `mcl run` puts into a run folder
    log = [json.loads(line) for line in (run / TASK / "gateway.jsonl").read_text(encoding="utf-8").splitlines()]
    start = log[0]
    calls = [e for e in log if e["type"] == "tool_call"]
    manifest = json.loads((run / "manifest.json").read_text())
    expect(start.get("labels") == {"hr": "private", "paste": "public"}, "start event: the label of every server")
    plugin = (start.get("plugins") or [{}])[0]
    expect(plugin.get("package") == "mcl-dummy-guard" and plugin.get("tree_sha256")
           == (manifest.get("guard_plugin") or {}).get("tree_sha256"),
           f"start event and manifest: plugin package and the same tree hash ({str(plugin.get('tree_sha256'))[:12]})")
    expect([e.get("call_id") for e in calls] == [f"c{i}" for i in range(1, 8)]
           and [e.get("label") for e in calls] == ["public", "private", "public", "public", "private", "private", "public"]
           and [e.get("decision") for e in calls] == LOGGED,
           "tool-call lines: call IDs c1 to c7, server labels, decisions")
    expect([(e.get("seq_call"), e.get("seq_result")) for e in calls]
           == [(1, 2), (3, None), (4, 5), (6, None), (7, 8), (9, 10), (11, None)],
           "tool-call lines: step numbers; none for the error result (c2) and the blocked calls (c4, c7)")
    expect([(e.get("guard_error"), e.get("result_withheld")) for e in calls] == [
        (None, False), (None, False), (None, False),
        ("on_call: RuntimeError: dummy guard told to fail on a call of recent", False),
        ("on_result: RuntimeError: dummy guard told to fail on the result of describe_table", True),
        (None, False), (None, False)] and calls[3]["reason"] == "guard error" and calls[4]["result_text"],
           "tool-call lines: what the guard raised, in its own field; the withheld result is kept in the log")

    r = sh(*mcl, "replay", str(run), "--out", str(work / "replay.jsonl"), check=False)
    print(r.stdout.strip())
    decisions = [json.loads(line) for line in (work / "replay.jsonl").read_text().splitlines()][1:]
    expect(r.returncode == 0 and [d["replayed"] for d in decisions] == LOGGED,
           "mcl replay: the same seven decisions from the log, the block of c7 included")
    expect(decisions[-1]["reason"] == calls[-1]["reason"], "mcl replay: the same reason for the block")
    expect([(d["replayed_error"], d["replayed_withheld"]) for d in decisions]
           == [(e["guard_error"], e["result_withheld"]) for e in calls],
           "mcl replay: the guard raises again in the same two calls, with the same text")
    print("live check passed" if not problems else f"live check FAILED: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
