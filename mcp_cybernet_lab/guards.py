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

What a guard is told beside the call itself is in `guard.context`: the label of every server, the
ID of the call being applied, and `embed`, which turns texts into vectors (see Context). A guard
does no I/O of its own: live, `embed` asks the testbed's embedder and every answer is written to
the task's embeddings.jsonl; in replay the same answers are read back from that file
(EmbeddingCache), so a replay sees the vectors its live run saw.

A Session applies calls and results to a guard, for the gateway and for `replay` alike, and numbers
the steps, so a replay can apply them in the order the live guard saw them.

Standard library only: this file is copied into the gateway image.

    guards.py check <guard> [<tree hash>]    can this guard be loaded here? (run in the image build)
"""

from __future__ import annotations

import ast
import base64
import hashlib
import json
import math
import re
import struct
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


def tool_key(server: str, tool: str) -> str:
    """How a single tool is named where servers are named: in a map of labels, in a policy's sinks."""
    return f"{server}/{tool}"


def labels_of(servers: list[dict]) -> dict[str, str]:
    """server id -> label for the servers of a manifest or a testbed ({"id", "label", "tool_labels"}),
    and `server/tool` -> label for every tool that has a label of its own. Without such tools this is
    the map of servers it always was."""
    out = {s["id"]: s["label"] for s in servers}
    for s in servers:
        for tool, label in (s.get("tool_labels") or {}).items():
            out[tool_key(s["id"], tool)] = label
    return out


def label_of(labels: dict[str, str], server: str, tool: str | None = None) -> str | None:
    """The label of a call: the tool's own when it has one, else the server's."""
    if tool is not None and tool_key(server, tool) in labels:
        return labels[tool_key(server, tool)]
    return labels.get(server)


def default_policy(servers: dict[str, dict]) -> dict:
    """Policy version 0: private servers are sources, public servers are sinks. A tool labelled
    public on a server that is not is a sink too, listed as `server/tool`; the server stays what its
    label makes it."""
    return {
        "version": 0,
        "sources": sorted(s for s, v in servers.items() if v["label"] in ("private", "mixed")),
        "sinks": sorted([s for s, v in servers.items() if v["label"] == "public"]
                        + [tool_key(s, tool) for s, v in servers.items() if v["label"] != "public"
                           for tool, label in (v.get("tool_labels") or {}).items() if label == "public"]),
        "patterns": DEFAULT_PATTERNS,
        "values": [],  # literal strings that may never reach a sink
        "check_patterns_at_sinks": False,  # also block pattern matches never seen in a source
    }


class EmbedError(Exception):
    """A text could not be embedded: no embedder in this testbed, the embedder did not answer, or
    (replay) no vector was recorded for the text."""


class TextTooLong(EmbedError):
    """The text has more tokens than the embedding model reads. It is refused, never cut: a guard
    must know that the model did not see all of a text."""


def no_embedder(texts: list[str]) -> list[tuple]:
    raise EmbedError("this testbed has no embedder (gateway.embedder is none)")


def cosine(a, b) -> float:
    """Cosine of two vectors as `embed` returns them."""
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)) or 1.0)


class Context:
    """What a guard is told beside the call itself. The gateway fills it live, `replay` offline.

    labels    server id -> "private", "public" or "mixed", as the testbed declares them; a tool with a
              label of its own is in it as `server/tool`. Ask with label(server, tool).
    call_id   the ID of the call being applied: in on_call the call about to be sent, in on_result
              the call whose result this is. It links a result to its call.
    embed     embed(texts) -> one vector (a tuple of floats, unit length) per text, from the
              testbed's pinned embedding model. Raises TextTooLong for a text over the model's
              token limit and EmbedError when there is no vector to be had. A guard that lets
              either escape fails closed, like any guard that raises.
    """

    def __init__(self, labels: dict[str, str] | None = None, embed=None):
        self.labels = dict(labels or {})
        self.call_id: str | None = None
        self.embed = embed or no_embedder

    def label(self, server: str, tool: str | None = None) -> str | None:
        """The label of a server, or of a call to one of its tools: the tool's own label when the
        testbed gives it one, else the server's."""
        return label_of(self.labels, server, tool)


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
        sinks = self.policy.get("sinks", [])
        if server in sinks:
            sink = server
        elif tool_key(server, tool) in sinks:  # a single tool listed as a sink
            sink = tool_key(server, tool)
        else:
            return True, ""
        blob = json.dumps(arguments, ensure_ascii=False)
        for value, origin in self.tainted.items():
            if value in blob:
                return False, f"tainted value from {origin} sent to sink {sink}"
        for value in self.policy.get("values", []):
            if value and value in blob:
                return False, f"listed value sent to sink {sink}"
        if self.policy.get("check_patterns_at_sinks"):
            for name, rx in self.regexes:
                if rx.search(blob):
                    return False, f"{name} pattern sent to sink {sink}"
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


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def pack_vector(vector) -> str:
    """A vector as text: its float32 values, little-endian, in base64. Exact both ways."""
    return base64.b64encode(struct.pack(f"<{len(vector)}f", *vector)).decode("ascii")


def unpack_vector(packed: str) -> tuple:
    raw = base64.b64decode(packed)
    return struct.unpack(f"<{len(raw) // 4}f", raw)


class EmbeddingCache:
    """Vectors by the SHA-256 of their text, for one model at one revision. The `embed` of a context.

    Live, in the gateway: `compute` asks the embedder, and every answer, a vector or a refusal, is
    appended to `path` (the task's embeddings.jsonl). The text itself is never written.
    In replay: `known` holds what the run recorded; a text that is not there is counted in
    `missing` and, if there is a `compute` (the model on this machine), computed, kept in `path`
    (a cache outside the run folder) and counted in `computed`.

    compute(text) -> a vector; raises TextTooLong or EmbedError. Each line of a file:
      {"sha256", "model", "revision", "dim", "vector"}                       vector as pack_vector writes it
      {"sha256", "model", "revision", "error": "too_long" | "unavailable", "detail"}
    A recorded refusal is raised again with the same words, so a guard that failed on it live
    fails the same way in replay.
    """

    def __init__(self, model: str, revision: str, compute=None, path=None, known: dict | None = None,
                 mark: dict | None = None):
        self.model, self.revision, self.compute, self.path = model, revision, compute, path
        self.entries: dict[str, dict] = dict(known or {})
        self.mark = mark or {}  # extra fields for lines written here, e.g. that they were computed at replay
        self.used: set[str] = set()      # texts answered from what was known
        self.computed: set[str] = set()  # texts computed here
        self.missing: set[str] = set()   # texts that were not known (computed or not)

    @staticmethod
    def read(path, model: str, revision: str) -> dict:
        """The lines of an embeddings file that are of this model at this revision, by text hash."""
        out = {}
        try:
            lines = open(path, encoding="utf-8").read().splitlines()
        except OSError:
            return out
        for line in lines:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("model") == model and e.get("revision") == revision and e.get("sha256"):
                out[e["sha256"]] = e
        return out

    def _write(self, entry: dict) -> None:
        if self.path is not None:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry | self.mark) + "\n")

    def embed(self, texts: list[str]) -> list[tuple]:
        out = []
        for text in texts:
            key = text_sha256(text)
            entry = self.entries.get(key)
            if entry is not None:
                self.used.add(key)
            else:
                self.missing.add(key)
                if self.compute is None:
                    raise EmbedError("no vector was recorded for a text of this call")
                entry = {"sha256": key, "model": self.model, "revision": self.revision}
                try:
                    vector = tuple(self.compute(text))
                    entry |= {"dim": len(vector), "vector": pack_vector(vector)}
                except TextTooLong as e:
                    entry |= {"error": "too_long", "detail": str(e)}
                except EmbedError as e:
                    entry |= {"error": "unavailable", "detail": str(e)}
                self.entries[key] = entry
                self.computed.add(key)
                self._write(entry)
            if entry.get("error"):
                raise (TextTooLong if entry["error"] == "too_long" else EmbedError)(entry.get("detail", ""))
            out.append(unpack_vector(entry["vector"]))
        return out


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


def replay(guard, events: list[dict], labels: dict[str, str] | None = None, embed=None) -> list[dict]:
    """Decisions of `guard` on logged tool-call events, task by task.

    events: gateway log lines. Those with type "tool_call" are replayed: task, server, tool,
    arguments, result_text, is_error, decision; the others are skipped, except that a "start" line
    may give the server labels. `labels` overrides them (logs written before labels were logged
    have none, and then the guard's context has none either). `embed` is the context's embed
    function for the replay, usually an EmbeddingCache holding what the run recorded; without
    one, a guard that embeds gets what it has (by default: no embedder).

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
            if embed is not None:
                context.embed = embed
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
