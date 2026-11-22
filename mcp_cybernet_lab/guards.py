"""Guards: the decision logic the gateway applies to every tool call.

A guard sees the stream of tool calls and their results, and decides for each call whether it
may go through. Guards are pure (no I/O), so the same code runs live in the gateway and offline
when `mcl evolve` replays logged runs against a candidate policy.

Built in:
  passthrough  allows everything (measures the gateway's own effect)
  taint        verbatim taint tracking, the usual baseline: values matching a sensitive pattern
               in results from `sources` are remembered; a call to a `sinks` server whose
               arguments contain one of them is blocked.

Add a guard without changing the lab: a plugin. A plugin is an installed Python package that
names a guard class in the entry-point group mcp_cybernet_lab.guards (the entry point's name is the
guard's name). Like the built-in guards it imports only the standard library and
mcp_cybernet_lab.guards. `gateway.plugins` in a testbed copies it into the gateway image; installed
on the host, the same code replays logged runs. A built-in name always means the built-in guard.

What a guard is told beside the call itself is in `guard.context`: the label of every server and
the ID of the call being applied (see Context).

A Session applies calls and results to a guard, for the gateway and for `replay` alike, and numbers
the steps, so a replay can apply them in the order the live guard saw them.

Standard library only: this file is copied into the gateway image.

    guards.py check <guard> [<tree hash>]    can this guard be loaded here? (run in the image build)
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import sys
from pathlib import Path

ENTRY_POINTS = "mcp_cybernet_lab.guards"

DEFAULT_PATTERNS = [
    {"name": "stripe_key", "regex": r"sk_live_[A-Za-z0-9]{16,}"},
    {"name": "aws_access_key", "regex": r"AKIA[A-Z0-9]{16}"},
    {"name": "github_token", "regex": r"ghp_[A-Za-z0-9]{36}"},
    {"name": "saudi_iban", "regex": r"SA\d{22}"},
    {"name": "saudi_national_id", "regex": r"\b[12]\d{9}\b"},
]


def default_policy(servers: dict[str, dict]) -> dict:
    """Policy version 0: private servers are sources, public servers are sinks."""
    return {
        "version": 0,
        "sources": sorted(s for s, v in servers.items() if v["label"] in ("private", "mixed")),
        "sinks": sorted(s for s, v in servers.items() if v["label"] == "public"),
        "patterns": DEFAULT_PATTERNS,
        "values": [],  # literal strings that may never reach a sink
        "check_patterns_at_sinks": False,  # also block pattern matches never seen in a source
    }


class Context:
    """What a guard is told beside the call itself. The gateway fills it live, `replay` offline.

    labels    server id -> "private", "public" or "mixed", as the testbed declares them
    call_id   the ID of the call being applied: in on_call the call about to be sent, in on_result
              the call whose result this is. It links a result to its call.
    """

    def __init__(self, labels: dict[str, str] | None = None):
        self.labels = dict(labels or {})
        self.call_id: str | None = None

    def label(self, server: str) -> str | None:
        return self.labels.get(server)


class Guard:
    name = "base"

    def __init__(self, policy: dict):
        self.policy = policy
        self.context = Context()
        self.reset()

    def reset(self) -> None:
        """Forget everything seen (called at the start of each task)."""

    def on_call(self, server: str, tool: str, arguments: dict) -> tuple[bool, str]:
        """(allowed, reason) for a tool call about to be sent."""
        return True, ""

    def on_result(self, server: str, tool: str, arguments: dict, text: str) -> None:
        """Observe the text a tool returned."""


class Passthrough(Guard):
    name = "passthrough"


class Taint(Guard):
    name = "taint"

    def __init__(self, policy: dict):
        self.regexes = [(p["name"], re.compile(p["regex"])) for p in policy.get("patterns", [])]
        super().__init__(policy)

    def reset(self):
        self.tainted: dict[str, str] = {}  # value -> "pattern@server"

    def on_result(self, server, tool, arguments, text):
        if server not in self.policy.get("sources", []):
            return
        for name, rx in self.regexes:
            for m in rx.finditer(text or ""):
                self.tainted.setdefault(m.group(0), f"{name}@{server}")

    def on_call(self, server, tool, arguments):
        if server not in self.policy.get("sinks", []):
            return True, ""
        blob = json.dumps(arguments, ensure_ascii=False)
        for value, origin in self.tainted.items():
            if value in blob:
                return False, f"tainted value from {origin} sent to sink {server}"
        for value in self.policy.get("values", []):
            if value and value in blob:
                return False, f"listed value sent to sink {server}"
        if self.policy.get("check_patterns_at_sinks"):
            for name, rx in self.regexes:
                if rx.search(blob):
                    return False, f"{name} pattern sent to sink {server}"
        return True, ""


GUARDS = {g.name: g for g in (Passthrough, Taint)}


class UnknownGuard(KeyError):
    """No built-in guard and no installed plugin has this name."""

    def __str__(self) -> str:
        return str(self.args[0])


RESERVED = ("none",)  # in a testbed file, guard none means no gateway


def installed_entry_points() -> list:
    """Every entry point of the group that an installed package declares, clashes included."""
    from importlib.metadata import entry_points
    eps = entry_points()
    return list(eps.select(group=ENTRY_POINTS) if hasattr(eps, "select") else eps.get(ENTRY_POINTS, []))


def plugin_entry_points() -> dict:
    """name -> entry point, for every guard an installed plugin registers. Built-in names are not
    listed: a plugin cannot replace a built-in guard (and `mcl build` refuses one that tries)."""
    return {ep.name: ep for ep in installed_entry_points() if ep.name not in GUARDS}


def name_clashes(registered: list[tuple[str, str]], where: str) -> dict[str, str]:
    """Guard names that do not mean one thing. registered: (guard name, package) pairs, e.g. of the
    plugins installed on this machine. Name -> a sentence naming both sides."""
    by_name: dict[str, set] = {}
    for name, package in registered:
        by_name.setdefault(name, set()).add(package)
    out = {}
    for name, packages in sorted(by_name.items()):
        listed = ", ".join(sorted(packages))
        if name in GUARDS:
            out[name] = f"guard name '{name}' is a built-in guard and is also registered by plugin {listed} ({where})"
        elif name in RESERVED:
            out[name] = f"guard name '{name}' means no gateway and is also registered by plugin {listed} ({where})"
        elif len(packages) > 1:
            out[name] = f"guard name '{name}' is registered by {len(packages)} plugins: {listed} ({where})"
    return out


def installed_guards() -> list[tuple[str, str]]:
    """(guard name, package) for every guard an installed plugin registers."""
    return [(ep.name, getattr(getattr(ep, "dist", None), "name", None) or ep.value)
            for ep in installed_entry_points()]


def installed_clashes() -> dict[str, str]:
    return name_clashes(installed_guards(), "installed on this machine")


def guard_class(name: str) -> type:
    if name in GUARDS:
        return GUARDS[name]
    ep = plugin_entry_points().get(name)
    if ep is None:
        raise UnknownGuard(f"guard '{name}': not built in ({', '.join(sorted(GUARDS))}) and not registered by "
                           f"an installed plugin (entry-point group {ENTRY_POINTS})")
    return ep.load()


def code_path(cls: type) -> Path:
    """The folder (or single file) of the top-level package a guard class comes from."""
    top = sys.modules[cls.__module__.split(".")[0]]
    paths = list(getattr(top, "__path__", []))
    return Path(paths[0]) if paths else Path(top.__file__)


def _code_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    return sorted(f for f in root.rglob("*") if f.is_file() and is_code(f.relative_to(root).as_posix()))


def is_code(name: str) -> bool:
    """Does a file count for the tree hash? Compiled files do not: they differ from machine to machine."""
    return "__pycache__" not in name.split("/") and not name.endswith((".pyc", ".pyo"))


def files_sha256(files: dict[str, bytes]) -> str:
    """Hash of files given as name -> contents, names relative to the package's own folder with '/'
    (for a package that is one file: the file's name), in the order of the names as text."""
    h = hashlib.sha256()
    for name in sorted(files):
        h.update(name.encode() + b"\0" + files[name])
    return h.hexdigest()


def tree_sha256(root: Path) -> str:
    """Hash of the names and contents of a plugin's code: the files of its top-level package,
    compiled files left out. The same files give the same hash in the wheel `mcl build` makes, in
    the gateway image that installs the wheel, and on the host, however the plugin is installed there."""
    return files_sha256({f.name if root.is_file() else f.relative_to(root).as_posix(): f.read_bytes()
                         for f in _code_files(root)})


def foreign_imports(root: Path) -> list[str]:
    """Imports in a plugin's code that are neither the standard library, nor the plugin itself, nor
    mcp_cybernet_lab.guards. Import statements only: an import made by a call is not seen."""
    own = root.stem if root.is_file() else root.name
    stdlib = getattr(sys, "stdlib_module_names", None)
    found = set()

    def check(module: str, names: list[str] = ()):
        top = module.split(".")[0]
        if top == own or module == "mcp_cybernet_lab.guards" or (stdlib is not None and top in stdlib):
            return
        if module == "mcp_cybernet_lab" and names and all(n == "guards" for n in names):
            return
        found.add(module)

    for f in _code_files(root):
        if f.suffix != ".py":
            continue
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"), filename=str(f))):
            if isinstance(node, ast.Import):
                for a in node.names:
                    check(a.name)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                check(node.module, [a.name for a in node.names])
    return sorted(found)


def guard_info(name: str) -> dict | None:
    """Where a plugin guard comes from: its package, version and the hash of its code. None for a
    built-in guard. The version alone does not identify the code; the hash does."""
    if name in GUARDS:
        return None
    cls = guard_class(name)
    dist = getattr(plugin_entry_points().get(name), "dist", None)
    return {"guard": name, "package": dist.name if dist else None, "version": dist.version if dist else None,
            "tree_sha256": tree_sha256(code_path(cls))}


def make_guard(name: str, policy: dict, context: Context | None = None):
    guard = guard_class(name)(policy)
    if context is not None or getattr(guard, "context", None) is None:
        guard.context = context or Context()
    return guard


GUARD_ERROR = "guard error"  # the reason of a call that is not allowed because its guard raised


def raised(hook: str, e: Exception) -> str:
    return f"{hook}: {type(e).__name__}: {e}"


class Session:
    """The steps of one task, applied to a guard: the same code live (gateway) and offline (replay).

    Each step takes the next number of one counter: `seq_call` when a call is put to the guard,
    `seq_result` when its result is. A result is put to the guard only if there is one and it is
    not an error; otherwise the step does not happen and has no number. The gateway serves calls
    on threads, so the lines of its log are not always in the order of these steps; the numbers are.

    A guard that raises fails closed. In on_call: the call is not allowed, with the reason
    GUARD_ERROR. In on_result: the result must be withheld from the agent (the tool has already
    run on its server). Either way `error` says where and what was raised, apart from the reason,
    so a crash can be told from a decision. The guard is kept; what it holds after raising is its own affair.
    """

    def __init__(self, guard):
        self.guard = guard
        self.seq = 0
        self.calls = 0

    def _at(self, call_id: str) -> None:
        context = getattr(self.guard, "context", None)
        if context is not None:
            context.call_id = call_id

    def call(self, server: str, tool: str, arguments: dict, call_id: str | None = None) -> dict:
        self.calls += 1
        self.seq += 1
        call_id = call_id or f"c{self.calls}"
        self._at(call_id)
        seq, error = self.seq, None
        try:
            allowed, reason = self.guard.on_call(server, tool, arguments)
        except Exception as e:
            allowed, reason, error = False, GUARD_ERROR, raised("on_call", e)
        return {"call_id": call_id, "seq_call": seq, "allowed": bool(allowed), "reason": reason, "error": error}

    def result(self, call_id: str, server: str, tool: str, arguments: dict,
               text: str | None, is_error: bool | None) -> dict:
        """{"seq_result": the step's number, or None if the result was not put to the guard;
        "error": None, or what on_result raised: the result is then to be withheld}"""
        if text is None or is_error:
            return {"seq_result": None, "error": None}
        self.seq += 1
        self._at(call_id)
        seq, error = self.seq, None
        try:
            self.guard.on_result(server, tool, arguments, text)
        except Exception as e:
            error = raised("on_result", e)
        return {"seq_result": seq, "error": error}


def replay(guard, events: list[dict], labels: dict[str, str] | None = None) -> list[dict]:
    """Decisions of `guard` on logged tool-call events, task by task.

    events: gateway log lines. Those with type "tool_call" are replayed: task, server, tool,
    arguments, result_text, is_error, decision; the others are skipped, except that a "start" line
    may give the server labels. `labels` overrides them (logs written before labels were logged
    have none, and then the guard's context has none either).

    Within a task the steps are applied in the order of `seq_call` and `seq_result` when the task
    was logged with them; a line of such a task that has none is not replayed (replay_skipped true,
    replay_allowed None). A task logged without step numbers is applied in line order, each result
    right after its call. The result of a call
    is put to the guard only if this guard allows the call (a blocked call has no result) and, as
    in the gateway, only if the result is not an error. Returns the tool-call lines in their
    logged order, each with replay_allowed, replay_reason, replay_call_id, and for a guard that
    raised replay_error (what Session reports) and replay_withheld (it raised in on_result), and
    replay_skipped.
    """
    out: list[dict] = []
    logged_labels: dict[str, str] | None = None

    def run_task(lines: list[dict], task_labels) -> None:
        guard.reset()
        context = getattr(guard, "context", None)
        if context is not None:
            context.labels = dict(labels if labels is not None else task_labels or {})
            context.call_id = None
        session = Session(guard)
        # A task logged with step numbers is replayed by them. A line without a number in such a
        # task is a result that came back after its own task had ended: this task's guard never
        # saw that call, so it is not put to the guard and is marked as not replayed.
        numbered = any(e.get("seq_call") is not None for e in lines)
        steps = []
        for i, e in enumerate(lines):
            if numbered and e.get("seq_call") is None:
                continue
            steps.append((e["seq_call"] if numbered else 2 * i, 0, i))
            if not numbered:
                steps.append((2 * i + 1, 1, i))
            elif e.get("seq_result") is not None:
                steps.append((e["seq_result"], 1, i))
        decided: dict[int, dict] = {}
        for _, is_result, i in sorted(steps):
            e = lines[i]
            args = e.get("arguments") or {}
            if not is_result:
                decided[i] = session.call(e["server"], e["tool"], args, e.get("call_id")) | {"withheld": False}
            elif decided[i]["allowed"]:
                after = session.result(decided[i]["call_id"], e["server"], e["tool"], args,
                                       e.get("result_text"), e.get("is_error"))
                if after["error"]:
                    decided[i] |= {"error": after["error"], "withheld": True}
        not_replayed = {"allowed": None, "reason": "", "call_id": None, "error": None, "withheld": False}
        for i, e in enumerate(lines):
            d = decided.get(i, not_replayed)
            out.append({**e, "replay_allowed": d["allowed"], "replay_reason": d["reason"],
                        "replay_call_id": d["call_id"], "replay_error": d["error"],
                        "replay_withheld": d["withheld"], "replay_skipped": i not in decided})

    lines, task, task_labels = [], None, None
    for e in events:
        if e.get("type") == "start" and isinstance(e.get("labels"), dict):
            logged_labels = e["labels"]
        if e.get("type") != "tool_call":
            continue
        if lines and e.get("task") != task:
            run_task(lines, task_labels)
            lines = []
        if not lines:
            task, task_labels = e.get("task"), logged_labels
        lines.append(e)
    if lines:
        run_task(lines, task_labels)
    return out


def check(name: str, expected: str | None = None) -> str:
    """Load a guard here and say where it comes from; raise ValueError if it cannot be used."""
    cls = guard_class(name)
    missing = [m for m in ("reset", "on_call", "on_result") if not callable(getattr(cls, m, None))]
    if missing:
        raise ValueError(f"guard '{name}': {cls.__name__} has no {', '.join(missing)}")
    if name in GUARDS:
        return f"guard '{name}': built in"
    root = code_path(cls)
    foreign = foreign_imports(root)
    if foreign:
        raise ValueError(f"guard '{name}': a plugin may import only the standard library and "
                         f"mcp_cybernet_lab.guards; {root.name} also imports {', '.join(foreign)}")
    info = guard_info(name)
    if expected and info["tree_sha256"] != expected:
        raise ValueError(f"guard '{name}': the code here has tree hash {info['tree_sha256']}, expected {expected}")
    make_guard(name, {})
    return f"guard '{name}': {info['package']} {info['version']}, tree hash {info['tree_sha256']}"


if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] != "check":
        sys.exit(__doc__.strip().splitlines()[-1].strip())
    try:
        print(check(*sys.argv[2:4]))
    except (KeyError, ValueError, ImportError) as error:
        sys.exit(f"error: {error}")
