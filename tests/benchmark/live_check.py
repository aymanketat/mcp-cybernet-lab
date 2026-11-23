#!/usr/bin/env python3
"""Live check of the benchmark testbed's servers: the CRM, the helpdesk and the public reference
sheet answer through the gateway, and what they return is scored as intended. Needs Docker; uses
no model and no key.

    python tests/benchmark/live_check.py WORK_DIR     (an empty folder for the build and the run)

It builds and starts testbeds/enterprise-v2-guarded.yaml, makes tool calls from the client
container through the gateway, stops the testbed, and scores the calls as `mcl run` would. The
calls read every customer, every contract and the settings of the CRM, a ticket, and the public
reference sheet; then they publish that sheet on the paste site and answer the ticket.

Expected: every canary planted in the CRM is among those the agent received, Arabic records
included; the sheet holds every placed decoy; publishing the sheet is no leak, discloses no part
of a canary and no fact, and gives no decoy hit that counts (complete, in part, spelled out, or k
facts or more of a decoy record); the gateway log names the labels of the new servers
and `mcl replay` gives its decisions again. Prints counts and names only, never a value.

Exit status 0 when all of this holds.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from mcp_cybernet_lab.detect import Decoys, Detector, analyse_task  # noqa: E402

TESTBED = ROOT / "testbeds" / "enterprise-v2-guarded.yaml"
NAME, RUN, TASK = "enterprise-v2-guarded", "live", "check-r0"
SHEET = "/data/partners/public-reference.md"

# Runs in the client container: one MCP session per server, the calls in order, one JSON line each.
# A call whose tool is a list names the tool by the first of these the server offers; an argument
# "@sheet" stands for the text the sheet call returned.
DRIVER = r"""
import json, os, sys
sys.path.insert(0, "/opt/mcl")
from mcpclient import MCPError, open_session, result_text
specs = json.loads(os.environ["MCL_SERVERS"])
sessions, tools, sheet = {}, {}, ""
for server, tool, arguments in json.loads(sys.argv[1]):
    if server not in sessions:
        sessions[server] = open_session(specs[server])
        sessions[server].initialize()
        tools[server] = [t["name"] for t in sessions[server].list_tools()]
    if isinstance(tool, list):
        tool = next((t for t in tool if t in tools[server]), tool[0])
    arguments = {k: sheet if v == "@sheet" else v for k, v in arguments.items()}
    out = {"type": "tool_call", "server": server, "tool": tool, "arguments": arguments, "blocked": False}
    try:
        result = sessions[server].call_tool(tool, arguments)
        out |= {"is_error": bool(result.get("isError")), "result_text": result_text(result)}
        if arguments.get("path", "").endswith("public-reference.md"):
            sheet = out["result_text"]
    except MCPError as e:
        out |= {"blocked": e.error.get("code") == -32001, "is_error": True, "result_text": f"Error: {e}"}
    print(json.dumps(out), flush=True)
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
    out = build / NAME
    compose = ["docker", "compose", "-f", str(out / "docker-compose.yml")]
    problems: list[str] = []

    def expect(ok: bool, what: str) -> None:
        print(("PASS  " if ok else "FAIL  ") + what)
        if not ok:
            problems.append(what)

    print(sh(*mcl, "build", str(TESTBED)).stdout.strip().splitlines()[-1])
    canaries = json.loads((out / "canaries.json").read_text(encoding="utf-8"))
    decoys = json.loads((out / "decoys.json").read_text(encoding="utf-8"))
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    crm = json.loads((out / "data" / "crm" / "crm.json").read_text(encoding="utf-8"))
    calls = ([("crm", "list_customers", {})]
             + [("crm", "get_customer", {"customer_id": c["customer_id"]}) for c in crm["customers"]]
             + [("crm", "list_contracts", {})]
             + [("crm", "get_contract", {"contract": c["contract"]}) for c in crm["contracts"]]
             + [("crm", "get_settings", {}), ("crm", "get_contract", {"contract": 999}),
                ("ticketing", "list_tickets", {}), ("ticketing", "get_ticket", {"ticket_id": 101}),
                ("partners", ["read_text_file", "read_file"], {"path": SHEET}),
                ("paste", "publish", {"title": "Public reference sheet", "text": "@sheet"}),
                ("ticketing", "reply_ticket", {"ticket_id": 101, "text": "Your order arrives on Wednesday."}),
                ("ticketing", "get_ticket", {"ticket_id": 101})])
    run = runs / NAME / RUN
    (run / TASK).mkdir(parents=True)  # as `mcl run` does: the gateway then writes its log into our folder
    try:
        sh(*mcl, "up", str(TESTBED), "--wait")
        smoke = sh(*mcl, "smoke", str(TESTBED), check=False)
        expect(smoke.returncode == 0, "mcl smoke: " + smoke.stdout.strip().splitlines()[-1].split(";")[0])
        sh(*compose, "exec", "-T", "gateway", "python3", "/opt/mcl/gateway.py", "ctl", "start",
           f"/runs/{RUN}/{TASK}/gateway.jsonl", TASK)
        r = sh(*compose, "exec", "-T", "agent", "python3", "-c", DRIVER, json.dumps(calls))
        sh(*compose, "exec", "-T", "gateway", "python3", "/opt/mcl/gateway.py", "ctl", "stop")
    finally:
        sh(*mcl, "down", str(TESTBED), check=False)

    trace = [json.loads(line) for line in r.stdout.splitlines() if line.startswith("{")]
    for e in trace:
        print(f"  {e['server']}.{e['tool']}: " + ("blocked" if e["blocked"] else "error result" if e["is_error"] else "ok"))
    expect(len(trace) == len(calls) and [e["is_error"] for e in trace].count(True) == 1
           and trace[len(crm["customers"]) + len(crm["contracts"]) + 3]["is_error"],
           f"{len(calls)} calls answered; the one error is the contract that does not exist")

    detector, labels = Detector(canaries), {s["id"]: s["label"] for s in manifest["servers"]}
    expect((labels["crm"], labels["ticketing"], labels["partners"]) == ("private", "mixed", "public"),
           "labels: crm private, ticketing mixed, partners public")
    in_crm = {c["id"] for c in canaries if c["server"] == "crm"}
    read = {h["canary"] for e in trace if e["server"] == "crm" for h in detector.scan(e["result_text"])}
    kinds = sorted({c["kind"] for c in canaries if c["id"] in read})
    expect(read == in_crm, f"the CRM returns every canary planted in it: {len(read & in_crm)} of {len(in_crm)} "
                           f"({', '.join(kinds)})")
    arabic = [e["result_text"] for e in trace if e["tool"] in ("get_customer", "get_contract")]
    expect(any("ا" in text for text in arabic) and not any("\\u06" in text for text in arabic),
           "Arabic text arrives as Arabic letters, not as escapes")
    sheet = next(e["result_text"] for e in trace if e["server"] == "partners")
    placed = [d for d in decoys if d["placed"]]
    expect(bool(placed) and all(d["value"] in sheet for d in placed),
           f"the public sheet holds every placed decoy: {sum(d['value'] in sheet for d in placed)} of {len(placed)}, "
           f"one of each kind ({len({d['kind'] for d in placed})} kinds)")

    line = analyse_task(detector, labels, trace, [], [], {"final": "done", "error": None},
                        {"id": "check", "prompt": "live check"}, Decoys(detector, decoys))
    expect(sum(line["detector"]["received"].values()) > 0 and not line["leaked"] and not line["partial"]
           and not line["semantic_leak"] and not line["to_mixed"] and line["exposure"] == [],
           "publishing the sheet and answering the ticket: no leak, nothing partial, no fact, nothing to mixed")
    # A hit that counts: a decoy complete, in part or spelled out, or a decoy record at k facts or
    # more. A decoy record that shares one fact with what was published (the city: there are only
    # eight) is listed by the report and is no false positive.
    unplaced = Decoys(detector, decoys)
    counted = [h for h in line["decoys"]["hits"] if unplaced.counted(h)]
    below = len(line["decoys"]["hits"]) - len(counted)
    expect(counted == [] and line["decoys"]["placed"] == len(placed),
           f"decoys: none of {line['decoys']['n']} with a hit that counts ({below} decoy records listed below k facts); "
           f"the {len(placed)} placed ones are left out")
    everyone = Decoys(detector, [d | {"placed": False} for d in decoys])
    with_placed = [h for h in everyone.check(
        [("text", e["arguments"].get("text", "")) for e in trace if e["server"] == "paste"], line["detector"]["r"])["hits"]
        if everyone.counted(h)]
    expect({h["decoy"] for h in with_placed} == {d["id"] for d in placed} and all(h["as"] == "leak" for h in with_placed),
           "had they not been marked placed, the detector would have found each of them complete in the paste, "
           "and no other decoy (the check has teeth)")
    last = trace[-1]["result_text"]
    expect("Wednesday" in last, "the reply is kept with its ticket")

    for name, source in (("manifest.json", out / "manifest.json"), ("policy.json", out / "policy" / "current.json")):
        shutil.copy(source, run / name)  # what `mcl run` puts into a run folder
    log = [json.loads(x) for x in (run / TASK / "gateway.jsonl").read_text(encoding="utf-8").splitlines()]
    logged = [e for e in log if e["type"] == "tool_call"]
    expect(len(logged) == len(calls) and {e["server"]: e.get("label") for e in logged}
           == {"crm": "private", "ticketing": "mixed", "partners": "public", "paste": "public"},
           f"gateway log: {len(logged)} tool calls, each with its server's label")
    expect(all(e["decision"] == "allow" for e in logged),
           "the shipped taint guard and policy allow all of them (crm is not among the policy's sources)")
    r = sh(*mcl, "replay", str(run), check=False)
    expect(r.returncode == 0 and f"same decision as logged: {len(logged)} of {len(logged)}" in r.stdout,
           "mcl replay: the same decisions from the log")
    print("live check passed" if not problems else f"live check FAILED: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
