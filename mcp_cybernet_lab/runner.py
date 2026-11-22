"""Run tasks on a running testbed: reset, run the agent, collect logs, detect leaks.

For each task (and repeat):
  1. restore every server's data to the planted state; restart the servers (clears in-memory state);
  2. tell the gateway (if any) to start a new log and reset its guard;
  3. run the agent in the client container on the task;
  4. collect the agent trace, the gateway log and new sink records;
  5. detect leaks and append one line to results.jsonl.

runs/<testbed>/<run id>/
  manifest.json canaries.json policy.json tasks.yaml
  tasks/<task>.json              what the agent receives
  <task>-r<k>/trace.jsonl        agent events
  <task>-r<k>/summary.json
  <task>-r<k>/gateway.jsonl      gateway decisions (with a gateway)
  <task>-r<k>/sinks.jsonl        sink records during the task
  results.jsonl                  one line per task run

`rescore` reads such a folder and scores every task run again, without Docker and without a model.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import guards
from .detect import Detector, analyse_task
from .loader import Loaded, TestbedError
from .schema import LLM_EGRESS, LLM_RELAY


def load_tasks(path: str | Path) -> list[dict]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if not tasks:
        raise TestbedError(f"{path}: expected a top-level 'tasks' list")
    ids = set()
    for t in tasks:
        if not isinstance(t, dict) or not t.get("id") or not t.get("prompt"):
            raise TestbedError(f"{path}: every task needs 'id' and 'prompt' ({t})")
        if t["id"] in ids:
            raise TestbedError(f"{path}: duplicate task id {t['id']}")
        ids.add(t["id"])
    return tasks


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


class Runner:
    def __init__(self, loaded: Loaded, build: Path, log=print):
        self.tb = loaded.testbed
        self.build = build
        self.manifest = json.loads((build / "manifest.json").read_text())
        self.runs = Path(self.manifest["runs"])
        self.canaries = json.loads((build / "canaries.json").read_text(encoding="utf-8"))
        self.detector = Detector(self.canaries)
        self.labels = {s.id: s.label for s in self.tb.servers}
        self.has_gateway = self.tb.gateway.guard != "none"
        built = yaml.safe_load((build / "docker-compose.yml").read_text())["services"]
        self.relays = {LLM_RELAY, LLM_EGRESS} & set(built)  # the agent's path to a hosted model, if built
        self.log = log
        if self.tb.llm.provider == "none" or not self.tb.llm.model:
            raise TestbedError("the testbed has no llm (llm.provider and llm.model); `mcl run` needs one")
        clients = [c for c in self.tb.clients if c.harness == "agent"]
        if not clients:
            raise TestbedError("no client with harness 'agent'")
        self.client = clients[0].id

    # --- docker helpers ---------------------------------------------------------------
    def compose(self, *args, check=True, capture=True) -> subprocess.CompletedProcess:
        r = subprocess.run(["docker", "compose", "-f", str(self.build / "docker-compose.yml"), *args],
                           capture_output=capture, text=True)
        if check and r.returncode != 0:
            raise TestbedError(f"docker compose {' '.join(args[:3])} failed: {(r.stderr or r.stdout)[-400:]}")
        return r

    def running(self) -> set[str]:
        r = self.compose("ps", "--services", "--status", "running")
        return set(r.stdout.split())

    def reset_state(self, restart: bool) -> None:
        data, pristine = self.build / "data", self.build / "data.pristine"
        if pristine.exists():
            for d in data.iterdir():  # clear contents, keep the bind-mounted folders
                for child in d.iterdir():
                    shutil.rmtree(child) if child.is_dir() else child.unlink()
                shutil.copytree(pristine / d.name, d, dirs_exist_ok=True)
        if restart:
            http = [s.id for s in self.tb.servers if s.transport == "http"]
            if http:
                self.compose("restart", *http)

    def sink_offsets(self) -> dict[str, int]:
        return {p.stem: len(read_jsonl(p)) for p in (self.runs / "_sinks").glob("*.jsonl")}

    def sink_new(self, before: dict[str, int]) -> list[dict]:
        out = []
        for p in (self.runs / "_sinks").glob("*.jsonl"):
            for r in read_jsonl(p)[before.get(p.stem, 0):]:
                out.append({**r, "sink": p.stem})
        return out

    # --- one run --------------------------------------------------------------------
    def run(self, tasks: list[dict], run_id: str | None = None, repeats: int = 1, restart: bool = True,
            policy_note: str | None = None, run_dir: Path | None = None) -> Path:
        need = {self.client} | ({"gateway"} if self.has_gateway else set()) | self.relays
        missing = need - self.running()
        if missing:
            raise TestbedError(f"containers not running: {sorted(missing)}; run `mcl up` first")
        run_id = run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
        rd = run_dir or self.runs / run_id
        (rd / "tasks").mkdir(parents=True, exist_ok=True)
        for f in ("manifest.json", "canaries.json"):
            shutil.copy(self.build / f, rd / f)
        if self.has_gateway:
            shutil.copy(self.build / "policy" / "current.json", rd / "policy.json")
        (rd / "tasks.yaml").write_text(yaml.safe_dump({"tasks": tasks}, allow_unicode=True, sort_keys=False))
        rel = rd.relative_to(self.runs)
        results = rd / "results.jsonl"
        for t in tasks:
            (rd / "tasks" / f"{t['id']}.json").write_text(json.dumps(t, ensure_ascii=False))
        for k in range(repeats):
            for t in tasks:
                name = f"{t['id']}-r{k}"
                out = rd / name
                out.mkdir(exist_ok=True)
                t0 = time.monotonic()
                self.reset_state(restart)
                if self.has_gateway:
                    self.compose("exec", "-T", "gateway", "python3", "/opt/mcl/gateway.py", "ctl", "start",
                                 f"/runs/{rel}/{name}/gateway.jsonl", name)
                before = self.sink_offsets()
                r = self.compose("exec", "-T", self.client, "python3", "/opt/mcl/agent.py",
                                 "--task", f"/runs/{rel}/tasks/{t['id']}.json", "--out", f"/runs/{rel}/{name}",
                                 check=False)
                if self.has_gateway:
                    self.compose("exec", "-T", "gateway", "python3", "/opt/mcl/gateway.py", "ctl", "stop")
                time.sleep(0.5)  # let sinks flush
                sinks = self.sink_new(before)
                with open(out / "sinks.jsonl", "w", encoding="utf-8") as f:
                    for s in sinks:
                        f.write(json.dumps(s, ensure_ascii=False) + "\n")
                summary_path = out / "summary.json"
                summary = json.loads(summary_path.read_text()) if summary_path.exists() else \
                    {"error": f"agent exited {r.returncode}: {(r.stderr or r.stdout)[-300:]}"}
                summary["run"] = name
                res = analyse_task(self.detector, self.labels, read_jsonl(out / "trace.jsonl"),
                                   read_jsonl(out / "gateway.jsonl"), sinks, summary, t)
                res |= {"repeat": k, "seconds": round(time.monotonic() - t0, 1), "policy": policy_note}
                with open(results, "a", encoding="utf-8") as f:
                    f.write(json.dumps(res, ensure_ascii=False) + "\n")
                flag = "LEAK" if res["leaked"] else ("blocked" if res["attempted"] else "ok")
                self.log(f"{name:24s} {flag:8s} calls {res['tool_calls']:2d}  blocked {res['blocked']}  "
                         f"false alarms {res['false_alarms']}  completed {res['completed']}"
                         + (f"  error {res['error']}" if res['error'] else ""))
        return rd


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def rescore(run: Path, **parameters) -> list[tuple[dict, dict]]:
    """Score a stored run again with the detector of this version of the lab.

    Returns (the stored result line, the line scored again) for every task run of the run folder.
    Each task run is analysed as `mcl run` analysed it: its trace, gateway log, sink records and
    summary, with the canaries and the server labels the folder holds. The new line also keeps what
    the stored line has and the detector does not produce (repeat, seconds, policy). `parameters`
    go to the detector (partial_from, chance, ...). Nothing in the folder is changed.
    """
    def load(path: Path):
        return json.loads(path.read_text(encoding="utf-8"))

    missing = [name for name in ("results.jsonl", "canaries.json", "manifest.json") if not (run / name).is_file()]
    if missing:
        raise TestbedError(f"{run} is not a run folder: no {', '.join(missing)}")
    detector = Detector(load(run / "canaries.json"), **parameters)
    labels = {s["id"]: s["label"] for s in load(run / "manifest.json")["servers"]}
    pairs = []
    for stored in read_jsonl(run / "results.jsonl"):
        name = stored.get("run") or f"{stored['task']}-r{stored.get('repeat', 0)}"
        out, task = run / name, run / "tasks" / f"{stored['task']}.json"
        if not out.is_dir() or not task.is_file():
            raise TestbedError(f"{run}: task run {name} cannot be scored again "
                               f"(no folder {name}, or no tasks/{task.name})")
        # Without a summary the agent had failed before it wrote one: the stored error is all there is.
        summary = load(out / "summary.json") if (out / "summary.json").is_file() else {"error": stored.get("error")}
        summary["run"] = name
        again = analyse_task(detector, labels, read_jsonl(out / "trace.jsonl"), read_jsonl(out / "gateway.jsonl"),
                             read_jsonl(out / "sinks.jsonl"), summary, load(task))
        pairs.append((stored, again | {k: v for k, v in stored.items() if k not in again}))
    return pairs


def policy_sha256(policy: dict) -> str:
    """Hash of a policy's content: the same policy has the same hash whatever file it was read from."""
    text = json.dumps(policy, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def replay_run(run: Path, guard: str | None = None, policy_file: Path | None = None) -> dict:
    """Put the tool calls a gateway logged in a run to a guard again, offline.

    By default the guard and the policy are the run's own (manifest.json, policy.json), which
    reproduces the decisions of the live run; `guard` and `policy_file` name others. A policy
    file is laid over the default policy for the run's servers, as `mcl build` does. With the
    run's own guard, when that guard is a plugin, the code installed here must be the code that
    ran: a different tree hash is refused. Nothing in the folder is changed.

    Returns the guard, where it comes from, the policy and its hash, and one entry per tool call.
    """
    def load(path: Path):
        return json.loads(path.read_text(encoding="utf-8"))

    if not (run / "manifest.json").is_file():
        raise TestbedError(f"{run} is not a run folder: no manifest.json")
    manifest = load(run / "manifest.json")
    ran = (manifest.get("gateway") or {}).get("guard", "none")
    if ran == "none":
        raise TestbedError(f"run {run.name} was made without a gateway (gateway.guard none): nothing to replay")
    labels = {s["id"]: s["label"] for s in manifest["servers"]}
    logs = sorted(run.glob("*/gateway.jsonl"))
    if not logs:
        raise TestbedError(f"{run}: no gateway log (<task run>/gateway.jsonl)")
    name = guard or ran

    if policy_file:
        if not policy_file.is_file():
            raise TestbedError(f"policy file {policy_file} not found")
        policy = guards.default_policy({s: {"label": label} for s, label in labels.items()})
        policy.update(yaml.safe_load(policy_file.read_text(encoding="utf-8")) or {})
        source = str(policy_file)
    elif (run / "policy.json").is_file():
        policy, source = load(run / "policy.json"), "the run's policy.json"
    else:
        raise TestbedError(f"{run}: no policy.json; give one with --policy")

    try:
        info = guards.guard_info(name)
        guard_object = guards.make_guard(name, policy, guards.Context(labels))
    except guards.UnknownGuard as e:
        raise TestbedError(f"{e}. Install the plugin on this machine (pip install -e <its folder>)") from None
    events = {log.parent.name: read_jsonl(log) for log in logs}
    if name == ran and info:
        logged = {p.get("tree_sha256") for lines in events.values() for e in lines if e.get("type") == "start"
                  for p in e.get("plugins") or [] if p.get("guard") == name}
        logged |= {(manifest.get("guard_plugin") or {}).get("tree_sha256")} - {None}
        if logged != {info["tree_sha256"]}:
            raise TestbedError(
                f"guard '{name}' is not the code that ran: installed here {info['package']} {info['version']}, "
                f"tree hash {info['tree_sha256']}; the run recorded "
                f"{', '.join(sorted(logged)) or 'no tree hash'}. Install the code of the run, or name "
                f"another guard with --guard")

    clash = guards.installed_clashes().get(name)
    if clash:
        raise TestbedError(f"{clash}: it is not clear which guard to replay. Uninstall or rename a plugin")
    calls, not_replayed = [], []
    for task, lines in events.items():
        for e in guards.replay(guard_object, lines, labels):
            if e["replay_skipped"]:
                not_replayed.append({"task": task, "server": e["server"], "tool": e["tool"]})
                continue
            calls.append({"task": task, "call_id": e["replay_call_id"], "server": e["server"], "tool": e["tool"],
                          "label": labels.get(e["server"]), "seq_call": e.get("seq_call"),
                          "seq_result": e.get("seq_result"), "logged": e.get("decision"),
                          "replayed": "allow" if e["replay_allowed"] else "block", "reason": e["replay_reason"],
                          # A guard that raised: where and what, and whether the result was withheld.
                          "logged_error": e.get("guard_error"), "replayed_error": e["replay_error"],
                          "logged_withheld": bool(e.get("result_withheld")), "replayed_withheld": e["replay_withheld"]})
    return {"run": run.name, "testbed": manifest.get("testbed"), "guard": name, "guard_ran": ran,
            "guard_plugin": info, "policy": source, "policy_version": policy.get("version"),
            "policy_sha256": policy_sha256(policy), "own": name == ran and not policy_file,
            "task_runs": len(events), "calls": calls,
            # Lines without step numbers in a task logged with them: results that came back after
            # their own task had ended. The guard of the task they are logged in never saw them.
            "not_replayed": not_replayed}
