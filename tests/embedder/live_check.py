#!/usr/bin/env python3
"""Live check of the embedder: a guard embeds through the gateway in a running testbed, the same
text gives the same vector after the embedder is restarted (and, with --rebuild, after its image
is built again from nothing), the embedder is isolated, and `mcl replay --exact` reproduces the run
from embeddings.jsonl with every container stopped. Needs Docker; uses no language model, no key.

    pip install -e tests/plugins/dummy-guard            (once)
    python tests/embedder/live_check.py WORK_DIR [--rebuild]      (WORK_DIR: an empty folder)

The testbed is tests/embedder/embed-testbed.yaml: guard dummy-embed (a public call is blocked when
its text is as similar as 0.8 to an earlier private result), embedding model minilm-paraphrase
(the smallest pinned one; it reads 128 tokens). The calls of each task, in order:

    c1 paste.publish      allowed    nothing private seen yet
    c2 hr.query           allowed    three department names: a private result, embedded
    c3 paste.publish      blocked    the text is that result
    c4 paste.publish      allowed    an ordinary line
    c5 hr.query           withheld   every employee: more than 128 tokens, the model refuses the
                                     text, the guard raises and fails closed

Exit status 0 when everything is as expected.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TESTBED = HERE / "embed-testbed.yaml"
NAME = "embed-check"
CALLS = [("paste", "publish", {"title": "note", "text": "Reminder: the office is closed next Thursday."}),
         ("hr", "query", {"sql": "SELECT name_en FROM departments ORDER BY id LIMIT 3"}),
         ("paste", "publish", {"title": ".", "text": "{RESULT}"}),
         ("paste", "publish", {"title": "note", "text": "The warehouse safety workshop is in the main meeting room."}),
         ("hr", "query", {"sql": "SELECT * FROM employees"})]
LIVE = ["allow", "allow", "block", "allow", "withheld"]
LOGGED = ["allow", "allow", "block", "allow", "allow"]

DRIVER = r"""
import json, os, sys
sys.path.insert(0, "/opt/mcl")
from mcpclient import MCPError, open_session, result_text
specs = json.loads(os.environ["MCL_SERVERS"])
sessions, last = {}, ""
for server, tool, arguments in json.loads(sys.argv[1]):
    if server not in sessions:
        sessions[server] = open_session(specs[server])
        sessions[server].initialize()
    arguments = {k: v.replace("{RESULT}", last) if isinstance(v, str) else v for k, v in arguments.items()}
    try:
        result = sessions[server].call_tool(tool, arguments)
        out = {"decision": "allow", "is_error": bool(result.get("isError"))}
        if server == "hr":
            last = result_text(result)
    except MCPError as e:
        out = {"decision": {-32001: "block", -32002: "withheld"}.get(e.error.get("code"), "error"), "message": str(e)}
    print(json.dumps({"server": server, "tool": tool} | out), flush=True)
for s in sessions.values():
    s.close()
"""


def sh(*cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode:
        sys.exit(f"FAILED {' '.join(cmd[:7])} ...\n{(r.stderr or r.stdout)[-2500:]}")
    return r


def vectors(task: Path) -> dict[str, str]:
    """text hash -> the line's vector or error, as written, for one task."""
    out = {}
    for line in (task / "embeddings.jsonl").read_text(encoding="utf-8").splitlines():
        e = json.loads(line)
        out[e["sha256"]] = e.get("vector") or f"error {e.get('error')}: {e.get('detail')}"
    return out


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--rebuild"]
    if len(args) != 1:
        sys.exit(__doc__)
    rebuild = "--rebuild" in sys.argv
    work = Path(args[0]).resolve()
    build, runs = work / "build", work / "runs"
    mcl = [sys.executable, "-m", "mcp_cybernet_lab", "--out", str(build), "--runs", str(runs)]
    out, run = build / NAME, runs / NAME / "live"
    compose = ["docker", "compose", "-f", str(out / "docker-compose.yml")]
    problems: list[str] = []

    def expect(ok: bool, what: str) -> None:
        print(("PASS  " if ok else "FAIL  ") + what, flush=True)
        if not ok:
            problems.append(what)

    def task(name: str) -> list[dict]:
        (run / name).mkdir(parents=True)
        sh(*compose, "exec", "-T", "gateway", "python3", "/opt/mcl/gateway.py", "ctl", "start",
           f"/runs/live/{name}/gateway.jsonl", name)
        r = sh(*compose, "exec", "-T", "agent", "python3", "-c", DRIVER, json.dumps(CALLS))
        sh(*compose, "exec", "-T", "gateway", "python3", "/opt/mcl/gateway.py", "ctl", "stop")
        return [json.loads(line) for line in r.stdout.splitlines() if line.startswith("{")]

    print(sh(*mcl, "build", str(TESTBED)).stdout.strip().splitlines()[-1])
    tasks = ["first-r0", "restarted-r0"] + (["rebuilt-r0"] if rebuild else [])
    try:
        sh(*mcl, "up", str(TESTBED), "--wait")
        isolation = sh(*mcl, "check-isolation", str(TESTBED), check=False)
        lines = isolation.stdout.splitlines()
        print("\n".join(l for l in lines if "mbedder" in l or l.startswith(("server", "gateway", "client"))
                        or "checks have" in l))
        expect(isolation.returncode == 0, "mcl check-isolation: " + lines[-2])
        expect(sum("embedder `embedder`" in l and l.startswith("PASS") for l in lines) >= 6
               and any(l.startswith("PASS  no default route in the embedder") for l in lines)
               and any(l.startswith("PASS  the embedder answers the gateway") for l in lines),
               "the agent and both servers cannot reach the embedder, it has no way out, the gateway reaches it")
        live = task(tasks[0])
        for i, call in enumerate(live, 1):
            print(f"  c{i} {call['server']}.{call['tool']}: {call['decision']}"
                  + (f" ({call['message']})" if call.get("message") else ""))
        expect([c["decision"] for c in live] == LIVE, f"what the client got: {LIVE}")
        sh(*compose, "restart", "embedder")
        sh(*compose, "up", "-d", "--wait")
        expect([c["decision"] for c in task(tasks[1])] == LIVE, "the same after the embedder was restarted")
        if rebuild:
            sh(*compose, "build", "--no-cache", "embedder")
            sh(*compose, "up", "-d", "--wait")
            expect([c["decision"] for c in task(tasks[2])] == LIVE, "the same after its image was built again from nothing")
        size = sh("docker", "image", "inspect", f"mcl-{NAME}-embedder", "--format", "{{.Size}}").stdout.strip()
        print(f"  embedder image: {int(size) / 1e6:.0f} MB")
    finally:
        sh(*mcl, "down", str(TESTBED), check=False)

    for name, source in (("manifest.json", out / "manifest.json"), ("policy.json", out / "policy" / "current.json")):
        shutil.copy(source, run / name)
    log = [json.loads(l) for l in (run / tasks[0] / "gateway.jsonl").read_text(encoding="utf-8").splitlines()]
    calls = [e for e in log if e["type"] == "tool_call"]
    manifest = json.loads((run / "manifest.json").read_text())
    expect(log[0].get("embedder") == {k: manifest["embedder"][k] for k in ("model", "revision")},
           f"start line and manifest: the embedding model at its revision ({manifest['embedder']['revision'][:12]})")
    expect([e["decision"] for e in calls] == LOGGED and "is similar (" in calls[2]["reason"]
           and "to the result of call c2" in calls[2]["reason"], f"log: c3 blocked, {calls[2]['reason'][-70:]}")
    expect(str(calls[4]["guard_error"]).startswith("on_result: TextTooLong:") and calls[4]["result_withheld"],
           f"log: c5 withheld, {calls[4]['guard_error']}")
    first = vectors(run / tasks[0])
    expect(len(first) == 4 and sum(v.startswith("error too_long") for v in first.values()) == 1,
           "embeddings.jsonl: three vectors and one refusal, by text hash, and no text")
    for name in tasks[1:]:
        expect(vectors(run / name) == first, f"{name}: every text has the same vector, byte for byte, as in the first task")

    r = sh(*mcl, "replay", str(run), "--exact", "--out", str(work / "replay.jsonl"), check=False)
    print(r.stdout.strip())
    decisions = [json.loads(l) for l in (work / "replay.jsonl").read_text().splitlines()][1:]
    same = all((d["logged"], d["logged_error"], d["logged_withheld"]) == (d["replayed"], d["replayed_error"], d["replayed_withheld"])
               for d in decisions)
    running = sh(*compose, "ps", "-q", check=False).stdout.split()
    expect(r.returncode == 0 and same and len(decisions) == 5 * len(tasks) and not running,
           "mcl replay --exact with every container stopped: the same decisions, reasons and guard error")

    # A run made without an embedder has no vectors: replay computes them here, from the same pinned files.
    bare = work / "runs-bare" / "live"
    shutil.copytree(run, bare, ignore=shutil.ignore_patterns("embeddings.jsonl"))
    r = sh(*mcl, "replay", str(bare), "--embed-cache", str(work / "host-cache.jsonl"), "--out", str(work / "host.jsonl"),
           check=False)
    if "Install the extra" in r.stderr or "is missing" in r.stderr:
        print("  (skipped: the model is not installed on this machine, so nothing can be computed here)")
    else:
        print("\n".join(l for l in r.stdout.splitlines() if "embedding model" in l or "same decision" in l))
        host = {}
        for line in (work / "host-cache.jsonl").read_text().splitlines():
            e = json.loads(line)
            host[e["sha256"]] = e.get("vector") or f"error {e.get('error')}: {e.get('detail')}"
        expect(r.returncode == 0, "replay without recorded vectors: computed on this machine, the same decisions")
        expect(not list(bare.rglob("embeddings.jsonl")), "and nothing was written into the run folder")
        print(f"  vectors computed on the host that are byte for byte those of the container: "
              f"{sum(host.get(k) == v for k, v in first.items())} of {len(first)}")
    print("live check passed" if not problems else f"live check FAILED: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
