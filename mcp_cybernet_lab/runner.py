"""Run tasks on a running testbed: reset, run the agent, collect logs, detect leaks.

For each task (and repeat):
  1. restore every server's data to the planted state; restart the servers (clears in-memory state);
  2. tell the gateway (if any) to start a new log and reset its guard;
  3. run the agent in the client container on the task;
  4. collect the agent trace, the gateway log and new sink records;
  5. detect leaks and append one line to results.jsonl.

runs/<testbed>/<run id>/
  manifest.json canaries.json decoys.json policy.json tasks.yaml
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
from .detect import Decoys, Detector, analyse_task
from .loader import Loaded, TestbedError
from .schema import LLM_EGRESS, LLM_RELAY
from .seeding import with_aliases


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
        self.canaries = with_aliases(json.loads((build / "canaries.json").read_text(encoding="utf-8")))
        self.detector = Detector(self.canaries)
        self.decoys = load_decoys(build, self.detector)
        self.labels = guards.labels_of([{"id": s.id, "label": s.label, "tool_labels": s.tool_labels} for s in self.tb.servers])
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
        if self.decoys is not None:
            shutil.copy(self.build / "decoys.json", rd / "decoys.json")
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
                                   read_jsonl(out / "gateway.jsonl"), sinks, summary, t, self.decoys)
                res |= {"repeat": k, "seconds": round(time.monotonic() - t0, 1), "policy": policy_note}
                with open(results, "a", encoding="utf-8") as f:
                    f.write(json.dumps(res, ensure_ascii=False) + "\n")
                flag = "LEAK" if res["leaked"] else ("blocked" if res["attempted"] else "ok")
                self.log(f"{name:24s} {flag:8s} calls {res['tool_calls']:2d}  blocked {res['blocked']}  "
                         f"false alarms {res['false_alarms']}  completed {res['completed']}"
                         + (f"  error {res['error']}" if res['error'] else ""))
        return rd


def load_decoys(folder: Path, detector: Detector) -> Decoys | None:
    """The decoys of a build or run folder, set up to be looked for as that detector looks for
    canaries. None where the folder has none (a build with `canaries.decoys: 0`, an older run)."""
    path = folder / "decoys.json"
    return Decoys(detector, json.loads(path.read_text(encoding="utf-8"))) if path.is_file() else None


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def rescore(run: Path, **parameters) -> list[tuple[dict, dict]]:
    """Score a stored run again with the detector of this version of the lab.

    Returns (the stored result line, the line scored again) for every task run of the run folder.
    Each task run is analysed as `mcl run` analysed it: its trace, gateway log, sink records and
    summary, with the canaries and the server labels the folder holds. The new line also keeps what
    the stored line has and the detector does not produce (repeat, seconds, policy). `parameters`
    go to the detector (partial_from, chance, k, ...). Where the folder holds decoys, they are looked
    for with the same settings. Nothing in the folder is changed.
    """
    def load(path: Path):
        return json.loads(path.read_text(encoding="utf-8"))

    missing = [name for name in ("results.jsonl", "canaries.json", "manifest.json") if not (run / name).is_file()]
    if missing:
        raise TestbedError(f"{run} is not a run folder: no {', '.join(missing)}")
    # The canaries of an older run get the aliases this version computes (names, numbers spelled out).
    detector = Detector(with_aliases(load(run / "canaries.json")), **parameters)
    labels = guards.labels_of(load(run / "manifest.json")["servers"])
    decoys = load_decoys(run, detector)
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
                             read_jsonl(out / "sinks.jsonl"), summary, load(task), decoys)
        pairs.append((stored, again | {k: v for k, v in stored.items() if k not in again}))
    return pairs


def policy_sha256(policy: dict) -> str:
    """Hash of a policy's content: the same policy has the same hash whatever file it was read from."""
    text = json.dumps(policy, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def host_embedder(model: str):
    """compute(text) for an EmbeddingCache, from the pinned model on this machine. Loaded on first use."""
    state = {}

    def compute(text: str) -> tuple:
        from . import embedding
        if "embedder" not in state:
            try:
                state["embedder"] = embedding.Embedder(model)
            except ImportError as e:
                raise TestbedError(f"a vector is not in the run and has to be computed here: {e}. "
                                   f"Install the extra (pip install -e .[embed])") from None
            except embedding.EmbeddingError as e:
                raise TestbedError(str(e)) from None
        try:
            return tuple(float(x) for x in state["embedder"].embed_one(text))
        except embedding.TooLong as e:
            raise guards.TextTooLong(str(e)) from None

    return compute


def replay_embeddings(run: Path, manifest: dict, model: str | None, exact: bool, cache: Path | None):
    """The `embed` of a replay: an EmbeddingCache, or None when no embedding model is in play.

    The model is the run's own (manifest), or `model` for a run that had none. What the run
    recorded (<task run>/embeddings.jsonl) is used first. A text that is not there is computed on
    this machine from the same pinned files and kept in a cache outside the run folder, unless
    `exact`: then nothing is computed, and the caller refuses a replay that missed a vector.
    """
    from . import embedding
    ran = (manifest.get("embedder") or {}).get("model")
    name = model or ran
    if not name:
        return None
    if name not in embedding.MODELS:
        raise TestbedError(f"embedding model '{name}': not one of {', '.join(embedding.MODELS)}")
    revision = embedding.MODELS[name]["revision"]
    if name == ran and manifest["embedder"]["revision"] != revision:
        raise TestbedError(f"the run used {name} at revision {manifest['embedder']['revision']}; "
                           f"this version of the lab pins {revision}")
    known, recorded = {}, 0
    if name == ran:
        for f in sorted(run.glob("*/embeddings.jsonl")):
            known |= guards.EmbeddingCache.read(f, name, revision)
        recorded = len(known)
    if exact:
        return guards.EmbeddingCache(name, revision, known=known), recorded
    cache = cache or embedding.default_root() / "replay" / f"{name}-{revision[:12]}.jsonl"
    if run.resolve() in cache.resolve().parents:
        raise TestbedError("the cache for vectors computed at replay is inside the run folder; "
                           "the folder is never changed, choose another place")
    cache.parent.mkdir(parents=True, exist_ok=True)
    earlier = guards.EmbeddingCache.read(cache, name, revision)  # computed at an earlier replay: not the run's
    store = guards.EmbeddingCache(name, revision, compute=host_embedder(name), path=cache,
                                  known=known, mark={"computed_at": "replay"})
    real = store.compute

    def compute(text: str) -> tuple:  # an earlier replay's vector is as good as computing it again
        entry = earlier.get(guards.text_sha256(text))
        if entry and entry.get("vector"):
            return guards.unpack_vector(entry["vector"])
        if entry and entry.get("error") == "too_long":
            raise guards.TextTooLong(entry.get("detail", ""))
        return real(text)

    store.compute = compute
    return store, recorded


def replay_run(run: Path, guard: str | None = None, policy_file: Path | None = None, exact: bool = False,
               embedder: str | None = None, embed_cache: Path | None = None) -> dict:
    """Put the tool calls a gateway logged in a run to a guard again, offline.

    By default the guard and the policy are the run's own (manifest.json, policy.json), which
    reproduces the decisions of the live run; `guard` and `policy_file` name others. A policy
    file is laid over the default policy for the run's servers, as `mcl build` does. With the
    run's own guard, when that guard is a plugin, the code installed here must be the code that
    ran: a different tree hash is refused. Nothing in the folder is changed.

    A guard that embeds gets the vectors the run recorded (see replay_embeddings): no embedder
    service is needed. `exact` is for reproducing a live run: a vector the run did not record is
    then an error, not something to compute here. `embedder` names a model for a run made
    without one; `embed_cache` is where vectors computed here are kept.

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
    labels = guards.labels_of(manifest["servers"])
    logs = sorted(run.glob("*/gateway.jsonl"))
    if not logs:
        raise TestbedError(f"{run}: no gateway log (<task run>/gateway.jsonl)")
    name = guard or ran

    if policy_file:
        if not policy_file.is_file():
            raise TestbedError(f"policy file {policy_file} not found")
        policy = guards.default_policy({s["id"]: {"label": s["label"], "tool_labels": s.get("tool_labels")}
                                        for s in manifest["servers"]})
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
    found = replay_embeddings(run, manifest, embedder, exact, embed_cache)
    store, recorded = found if found else (None, 0)
    calls, not_replayed = [], []
    for task, lines in events.items():
        for e in guards.replay(guard_object, lines, labels, store.embed if store else None):
            if e["replay_skipped"]:
                not_replayed.append({"task": task, "server": e["server"], "tool": e["tool"]})
                continue
            calls.append({"task": task, "call_id": e["replay_call_id"], "server": e["server"], "tool": e["tool"],
                          "label": guards.label_of(labels, e["server"], e["tool"]), "seq_call": e.get("seq_call"),
                          "seq_result": e.get("seq_result"), "logged": e.get("decision"),
                          "replayed": "allow" if e["replay_allowed"] else "block", "reason": e["replay_reason"],
                          # A guard that raised: where and what, and whether the result was withheld.
                          "logged_error": e.get("guard_error"), "replayed_error": e["replay_error"],
                          "logged_withheld": bool(e.get("result_withheld")), "replayed_withheld": e["replay_withheld"]})
    embeddings = None
    if store:
        embeddings = {"model": store.model, "revision": store.revision, "recorded_in_run": recorded,
                      "from_run": len(store.used - store.computed), "computed_at_replay": len(store.computed),
                      "missing": len(store.missing - store.computed), "exact": exact,
                      "cache": None if exact else str(store.path)}
        if exact and store.missing:
            raise TestbedError(
                f"--exact: the run recorded no vector for {len(store.missing)} text(s) the guard embedded. "
                f"This replay is not the live run again. Without --exact the vectors are computed here")
    return {"run": run.name, "testbed": manifest.get("testbed"), "guard": name, "guard_ran": ran,
            "embeddings": embeddings,
            "guard_plugin": info, "policy": source, "policy_version": policy.get("version"),
            "policy_sha256": policy_sha256(policy), "own": name == ran and not policy_file,
            "task_runs": len(events), "calls": calls,
            # Lines without step numbers in a task logged with them: results that came back after
            # their own task had ended. The guard of the task they are logged in never saw them.
            "not_replayed": not_replayed}
