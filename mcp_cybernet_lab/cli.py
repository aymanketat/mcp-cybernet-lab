"""MCP CyberNet Lab command line (`mcl`, or `python -m mcp_cybernet_lab`).

    mcl validate T.yaml                 check the file; no Docker needed
    mcl build    T.yaml                 write build/<testbed>/ (compose, Dockerfiles, data, canaries)
    mcl up       T.yaml                 build images and start the containers
    mcl smoke    T.yaml                 every client initializes every server, as the agent would
    mcl check-isolation T.yaml          with a gateway: a client reaches its model and the gateway, nothing else
    mcl run      T.yaml --tasks F.yaml  run the agent on each task; detect leaks; write runs/<testbed>/<id>/
    mcl report   RUN_DIR                rates with 95% intervals
    mcl rescore  RUN_DIR                score a stored run again with this version's detector; no Docker needed
    mcl compare  RUN_A RUN_B            paired comparison (exact McNemar)
    mcl replay RUN_DIR [--guard G]      with a gateway log: the decisions of a guard on the logged calls, offline
    mcl check-coverage RUN_DIR          with a gateway: every tool call of the agent is in the gateway log
    mcl reset    T.yaml                 restore every server's data to the planted state
    mcl down     T.yaml                 stop and remove the containers and networks
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from . import __version__
from .compose import build
from .loader import TestbedError, check_buildable, load_testbed


def load_extensions(sub) -> None:
    """Register subcommands from optional plugins (entry-point group mcp_cybernet_lab.commands).

    A plugin exposes a callable register(subparsers, cli_module) that adds its own parsers. When no
    plugin is installed, the public CLI is unchanged. Research extensions ship as separate packages.
    """
    try:
        from importlib.metadata import entry_points
    except ImportError:  # pragma: no cover
        return
    import sys as _sys
    eps = entry_points()
    group = eps.select(group="mcp_cybernet_lab.commands") if hasattr(eps, "select") \
        else eps.get("mcp_cybernet_lab.commands", [])
    for ep in group:
        try:
            ep.load()(sub, _sys.modules[__name__])
        except Exception as e:  # a broken plugin must not break the base CLI
            print(f"warning: could not load extension {ep.name}: {e}", file=_sys.stderr)


def build_dir(args, name: str) -> Path:
    return Path(args.out) / name


def runs_dir(args, name: str) -> Path:
    return Path(args.runs) / name


def compose(out: Path, *cmd: str, capture: bool = False) -> subprocess.CompletedProcess:
    full = ["docker", "compose", "-f", str(out / "docker-compose.yml"), *cmd]
    return subprocess.run(full, capture_output=capture, text=True)


def need_build(out: Path) -> None:
    if not (out / "docker-compose.yml").exists():
        raise TestbedError(f"{out} is not built; run `mcl build` first")


def loaded_and_build(args):
    loaded = load_testbed(args.testbed)
    out = build_dir(args, loaded.testbed.testbed)
    need_build(out)
    return loaded, out


def cmd_validate(args) -> int:
    loaded = load_testbed(args.testbed)
    tb = loaded.testbed
    for w in loaded.warnings:
        print(f"warning: {w}")
    http = sum(s.transport == "http" for s in tb.servers)
    print(f"ok: {tb.testbed} — {len(tb.servers)} servers ({http} http, {len(tb.servers) - http} stdio), "
          f"{len(tb.clients)} clients, {len(tb.domains)} domains, {len(tb.services)} services, "
          f"gateway {tb.gateway.guard}{', llm egress relay' if tb.llm.egress == 'relay' else ''}, seed {tb.seed}")
    try:
        check_buildable(tb)
    except TestbedError as e:
        print(f"note: valid, but {e}")
    return 0


def cmd_build(args) -> int:
    loaded = load_testbed(args.testbed)
    for w in loaded.warnings:
        print(f"warning: {w}")
    out = build(loaded, build_dir(args, loaded.testbed.testbed), runs_dir(args, loaded.testbed.testbed))
    manifest = json.loads((out / "manifest.json").read_text())
    print(f"built {out} ({manifest['canaries']} canaries, commit {manifest['commit']})")
    return 0


def cmd_up(args) -> int:
    _, out = loaded_and_build(args)
    return compose(out, "up", "-d", "--build", "--wait" if args.wait else "--quiet-pull").returncode


def unknown_tool_labels(tb, results: list[dict]) -> list[tuple[str, str]]:
    """(server, what is wrong) for every tool a testbed labels that its server does not offer.
    results: the smoke lines, each with the server it reached and the tools that server listed. A
    server no client reached is not judged here: its connection has failed already."""
    offered: dict[str, set] = {}
    for res in results:
        if res.get("ok"):
            offered.setdefault(res["server"], set()).update(res.get("tools") or [])
    return [(s.id, f"tool_labels names '{tool}', and the server has no such tool (it has: {', '.join(sorted(offered[s.id]))})")
            for s in tb.servers for tool in s.tool_labels if s.id in offered and tool not in offered[s.id]]


def cmd_smoke(args) -> int:
    loaded, out = loaded_and_build(args)
    tb = loaded.testbed
    results, failed = [], 0
    for c in tb.clients:
        r = compose(out, "exec", "-T", c.id, "python3", "/opt/mcl/probe.py", "all", capture=True)
        lines = [l for l in r.stdout.splitlines() if l.startswith("{")]
        if not lines:
            print(f"FAIL {c.id}: {(r.stderr or r.stdout).strip()[-300:]}")
            failed += 1
            continue
        for line in lines:
            res = json.loads(line)
            res["server_info"] = res.pop("server", None)
            res |= {"client": c.id, "server": res.pop("id")}
            results.append(res)
            failed += not res["ok"]
            detail = (f"{res['server_info']} {res['version']} — {res['transport']}, {len(res['tools'])} tools, "
                      f"{res['ms']} ms" if res["ok"] else res.get("error"))
            print(f"{'ok  ' if res['ok'] else 'FAIL'} {c.id} -> {res['server']}: {detail}")

    # A label for a tool the server does not have would score a secret sent to the real tool as
    # nothing, silently: a misspelled name fails here.
    for server, error in unknown_tool_labels(tb, results):
        print(f"FAIL {server}: {error}")
        failed += 1
        results.append({"ok": False, "server": server, "client": None, "error": error})

    # Record the versions actually installed, next to what the YAML asked for.
    installed = {}
    for svc in [s.id for s in tb.servers if s.transport == "http"] + [c.id for c in tb.clients]:
        r = compose(out, "exec", "-T", svc, "sh", "-c", "cat /opt/mcp/*/installed.json 2>/dev/null", capture=True)
        for chunk in r.stdout.replace("}{", "}\n{").splitlines():
            if chunk.strip():
                d = json.loads(chunk)
                installed[d["id"]] = d["version"]
    manifest = json.loads((out / "manifest.json").read_text())
    for s in manifest["servers"]:
        s["installed_version"] = installed.get(s["id"])
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
    (out / "smoke.json").write_text(json.dumps(results, indent=1))
    print(f"{len(results) - failed}/{len(results)} connections ok; results in {out / 'smoke.json'}")
    return 1 if failed else 0


def cmd_check_isolation(args) -> int:
    from .isolation import check, render
    loaded, out = loaded_and_build(args)
    results = check(loaded, out)
    (out / "isolation.json").write_text(json.dumps(results, indent=1))
    print(render(results))
    print(f"results in {out / 'isolation.json'}")
    return 0 if all(r["ok"] for r in results) else 1


def cmd_run(args) -> int:
    from .report import report
    from .runner import Runner, load_tasks
    loaded, out = loaded_and_build(args)
    tasks = load_tasks(args.tasks)
    if args.limit:
        tasks = tasks[:args.limit]
    runner = Runner(loaded, out)
    rd = runner.run(tasks, run_id=args.run_id, repeats=args.repeats, restart=not args.no_restart)
    print(report(rd))
    print(f"results: {rd / 'results.jsonl'}")
    return 0


def cmd_web(args) -> int:
    from .web import serve
    project = Path(args.dir).resolve()
    testbeds = Path(args.testbeds_dir) if args.testbeds_dir else project / "testbeds"
    catalogue = next((project / "docs").glob("*mcp-server-catalogue*.md"), None) \
        if (project / "docs").is_dir() else None
    serve(testbeds, project, catalogue, host=args.host, port=args.port, open_browser=not args.no_browser)
    return 0


def cmd_report(args) -> int:
    from .report import report
    print(report(Path(args.run)))
    return 0


def cmd_rescore(args) -> int:
    from .report import rescored
    from .runner import rescore
    run, out = Path(args.run), Path(args.out) if args.out else None
    if out and out.resolve() == (run / "results.jsonl").resolve():
        raise TestbedError("--out names the stored results of the run; they are kept, choose another file")
    pairs = rescore(run, **({"k": args.k} if args.k is not None else {}))
    print(rescored(run.name, pairs))
    if out:
        with open(out, "w", encoding="utf-8") as f:
            for _, again in pairs:
                f.write(json.dumps(again, ensure_ascii=False) + "\n")
        print(f"result lines scored again: {out}")
    else:
        print("nothing was written; --out FILE keeps the result lines scored again")
    return 0


def cmd_replay(args) -> int:
    from .report import replay_same, replayed
    from .runner import replay_run
    run, out = Path(args.run), Path(args.out) if args.out else None
    if out and run.resolve() in out.resolve().parents:
        raise TestbedError("--out is inside the run folder; the folder is never changed, choose another place")
    result = replay_run(run, args.guard, Path(args.policy) if args.policy else None, exact=args.exact,
                        embedder=args.embedder, embed_cache=Path(args.embed_cache) if args.embed_cache else None)
    print(replayed(result))
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "replay"} | {k: v for k, v in result.items() if k != "calls"},
                               ensure_ascii=False) + "\n")
            for c in result["calls"]:
                f.write(json.dumps({"type": "decision"} | c, ensure_ascii=False) + "\n")
        print(f"decisions: {out}")
    else:
        print("nothing was written; --out FILE keeps the decisions")
    differ = sum(not replay_same(c) for c in result["calls"])
    return 1 if result["own"] and differ else 0


def cmd_compare(args) -> int:
    from .report import compare
    print(compare(Path(args.run_a), Path(args.run_b), args.metric))
    return 0


def cmd_check_coverage(args) -> int:
    from .report import gateway_coverage
    ok, text = gateway_coverage(Path(args.run))
    print(text)
    return 0 if ok else 1


def cmd_reset(args) -> int:
    _, out = loaded_and_build(args)
    data, pristine = out / "data", out / "data.pristine"
    if not pristine.exists():
        print("nothing to reset (no server data)")
        return 0
    import shutil
    for d in data.iterdir():  # clear contents, keep the folders: they are bind-mounted
        for child in d.iterdir():
            shutil.rmtree(child) if child.is_dir() else child.unlink()
        shutil.copytree(pristine / d.name, d, dirs_exist_ok=True)
    print(f"reset {data}")
    return 0


def cmd_down(args) -> int:
    _, out = loaded_and_build(args)
    return compose(out, "down", "--remove-orphans").returncode


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mcl", description="MCP CyberNet Lab: containerised MCP network testbeds.")
    p.add_argument("--version", action="version", version=f"mcp-cybernet-lab {__version__}")
    p.add_argument("--out", default="build", help="folder for build outputs (default: build)")
    p.add_argument("--runs", default="runs", help="folder for run outputs (default: runs)")
    sub = p.add_subparsers(dest="command", required=True)
    simple = [("validate", cmd_validate), ("build", cmd_build), ("up", cmd_up), ("smoke", cmd_smoke),
              ("check-isolation", cmd_check_isolation), ("reset", cmd_reset), ("down", cmd_down)]
    for name, fn in simple:
        sp = sub.add_parser(name)
        sp.add_argument("testbed", help="path to the testbed YAML file")
        if name == "up":
            sp.add_argument("--wait", action="store_true", help="wait until containers are running")
        sp.set_defaults(fn=fn)

    sp = sub.add_parser("run", help="run the agent on tasks and measure leaks")
    sp.add_argument("testbed")
    sp.add_argument("--tasks", required=True)
    sp.add_argument("--repeats", type=int, default=1)
    sp.add_argument("--limit", type=int, help="only the first N tasks")
    sp.add_argument("--run-id")
    sp.add_argument("--no-restart", action="store_true", help="do not restart servers between tasks")
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("report")
    sp.add_argument("run", help="a run folder (runs/<testbed>/<id>)")
    sp.set_defaults(fn=cmd_report)
    sp = sub.add_parser("check-coverage", help="a guarded run: is every tool call in the gateway log?")
    sp.add_argument("run", help="a run folder (runs/<testbed>/<id>)")
    sp.set_defaults(fn=cmd_check_coverage)
    sp = sub.add_parser("compare")
    sp.add_argument("run_a")
    sp.add_argument("run_b")
    sp.add_argument("--metric", default="leaked",
                    choices=["leaked", "disclosed", "partial", "semantic_leak", "attempted", "completed"],
                    help="the field compared (default leaked; disclosed is the headline)")
    sp.set_defaults(fn=cmd_compare)
    sp = sub.add_parser("rescore", help="score a stored run again with this version's leak detector")
    sp.add_argument("run", help="a run folder (runs/<testbed>/<id>); it is read and never changed")
    sp.add_argument("--out", help="write the result lines scored again to this file (`mcl report FILE` reads it)")
    sp.add_argument("--k", type=int, help="facts of a fact record that make a semantic leak (default 3)")
    sp.set_defaults(fn=cmd_rescore)

    sp = sub.add_parser("replay", help="put the tool calls a gateway logged to a guard again, offline")
    sp.add_argument("run", help="a run folder made with a gateway (runs/<testbed>/<id>); it is read and never changed")
    sp.add_argument("--guard", help="a built-in guard or an installed plugin guard (default: the run's guard)")
    sp.add_argument("--policy", help="a policy file, YAML or JSON, laid over the default policy (default: the run's policy.json)")
    sp.add_argument("--out", help="write one line per decision to this file, after a line describing the replay")
    sp.add_argument("--exact", action="store_true",
                    help="reproduce a live run: every vector must be one the run recorded (embeddings.jsonl); "
                         "a missing one is an error")
    sp.add_argument("--embedder", help="an embedding model for a run that was made without one (computed on this machine)")
    sp.add_argument("--embed-cache", help="where vectors computed at replay are kept (default: next to the model files; "
                                          "never in the run folder)")
    sp.set_defaults(fn=cmd_replay)

    sp = sub.add_parser("web", help="local web UI to configure and run testbeds")
    sp.add_argument("--dir", default=".", help="project directory (default: current)")
    sp.add_argument("--testbeds-dir", help="folder of testbed YAML files (default: <dir>/testbeds)")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8000)
    sp.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    sp.set_defaults(fn=cmd_web)

    load_extensions(sub)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except TestbedError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
