# Guard plugins, richer gateway events and `mcl replay` (work package 3, items 3a and 3c)

22 November 2026, branch `wp3-extension-points`. The branch starts from a merge (`d0453e2`) of
`wp1-llm-egress` (`4f2414a`) and `wp2-oracle-v2` (`ab745fc`), because later packages need both. Work
package 3 of the FlowGuard lab requirements (revision 3.2, 22 November 2026, in Ayman Ketat's thesis
records): item 3a (guard plugins) and item 3c (richer gateway events). Item 3b (the embedding
sidecar) is not in this change; it follows on its own branch.

| Round | Commits | What |
|---|---|---|
| First | `3e55305` (code), `f90f6cd` (this note) | Plugins, the context, the numbered steps, `mcl replay` |
| Second, after review of the first report | the two commits after `f90f6cd` | A guard that raises fails closed and is logged. Name clashes are refused at build. Plugins are built into wheels on the host, so any build backend works. The rung replay of 22 November was run again on the new code. |

The note describes the code as it is after the second round.

**Outcome.** A guard can now live in its own package and run in the gateway without a change to the
lab. The gateway log records, for every tool call, the server's label, a call ID and the order in
which the guard saw the call and its result. `mcl replay` puts a logged run to a guard again and
reproduces the decisions. With no plugin configured, every shipped testbed builds to the same
`docker-compose.yml` and the same Dockerfiles as before, the built-in guards give the logged
decision on every call of the 18 November runs, and `mcl report`, `mcl rescore` and
`mcl check-coverage` print what they printed before.

## What a plugin is

A plugin is an installed Python package that names a guard class in the entry-point group
`mcp_cybernet_lab.guards`. The entry point's name is the guard's name. This is the pattern the lab
already uses for commands (`mcp_cybernet_lab.commands`).

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "mcl-dummy-guard"
version = "0.1.0"

[project.entry-points."mcp_cybernet_lab.guards"]
dummy = "mcl_dummy_guard:DummyGuard"
```

```yaml
gateway:
  guard: dummy
  plugins: [dummy-guard]     # package folders, relative to the testbed file
```

The contract:

- **Imports.** A plugin imports only the standard library and `mcp_cybernet_lab.guards`, as the
  built-in guards do. `guards.foreign_imports` reads the import statements of the plugin's code and
  lists any other. An import made by a function call (`importlib.import_module`) is not seen by
  that check; it still fails when the guard is loaded in the image, where nothing else is installed.
- **A name means one thing.** `mcl build` of a testbed with a gateway refuses when two plugins
  register the same guard name, or when a plugin registers `passthrough`, `taint` or `none`. The
  message names both sides. This is checked twice: among the plugins installed on the host (what
  replay will load) and among the wheels built from `gateway.plugins` (what the gateway will load).
- **Interface.** `reset`, `on_call` and `on_result`, unchanged. Subclassing `Guard` is the simple
  way; a class with those three methods also works.
- **Installed on the host too.** `mcl build` checks the name against the built-in guards and the
  plugins installed on the host, and replay loads the plugin there: `pip install -e <folder>`.
  A plugin guard whose folder is missing from `gateway.plugins` is refused at build.

`gateway.guard` is now any name of the lab's ID form. Unknown names fail at `mcl build`, not in the
schema. `mcphunt` and `fides` are refused with the same message as before unless a plugin registers them.

## How a plugin gets into the gateway image

Only when `gateway.plugins` is not empty, `mcl build` builds each folder into a wheel on the host
(`python -m pip wheel --no-deps`, on a copy of the folder, so nothing is left in it), puts the
wheels into `context/plugins/` and adds five lines to the gateway's Dockerfile:

```
COPY mcp_cybernet_lab /opt/mcl/mcp_cybernet_lab
COPY plugins /opt/mcl-plugins
RUN uv pip install --no-deps --no-index --python /usr/bin/python3 --target /opt/mcl/site /opt/mcl-plugins/mcl_dummy_guard-0.1.0-py3-none-any.whl
ENV PYTHONPATH=/opt/mcl/site
RUN python3 /opt/mcl/guards.py check dummy <tree hash>
```

- **Any build backend, and the image fetches nothing.** The wheel is built on the host, where pip
  may download the plugin's build backend. The image installs the finished wheel with
  `--no-deps --no-index`. In the first round the image built the plugin from source, which worked
  offline only with `uv_build`; the dummy plugin now uses `setuptools`, and the live check passed with it.
- **The import path.** In the image the lab's modules are plain files in `/opt/mcl`, so
  `mcp_cybernet_lab.guards` does not exist there. A two-file package makes that name mean the very
  module the gateway imports as `guards`. A plugin therefore uses one import on the host and in the image.
- **The last line** loads the configured guard, applies the import rule and compares the code in
  the image with the code of the wheel. A missing, broken or different plugin fails the image
  build, not the run.
- **Refused at `mcl build`:** a folder that cannot be built (with the builder's own message); a
  configured plugin guard that no folder registers; and a configured guard whose code installed on
  the host is not the code of the wheel, because replay would then not run what the gateway runs.

Only the gateway image gets the plugin. The compose file gains nothing: same service, same
environment, same volumes. A plugin wheel must be pure Python; one with compiled parts built on
the host would not fit the image.

## Which code ran: the tree hash

A version number stays the same while the code changes. Each plugin guard is therefore identified
by a tree hash: the SHA-256 of the names and contents of the files of the top-level package its
class is in, compiled files left out, in the order of the names as text. It is taken over the same
files in three places: in the wheel (`compose.wheel_guards`), in the gateway image and on the host
(`guards.tree_sha256` over the installed package, however it was installed). `mcl build` refuses
unless the host and the wheel agree, and the image build refuses unless the image and the wheel agree.

| Where | What is recorded |
|---|---|
| `manifest.json`, `guard_plugin` | guard, package, version, tree hash; `null` for a built-in guard |
| `manifest.json`, `plugins` | for each folder of `gateway.plugins`: name, path, wheel file, package, version, and the tree hash of every guard it registers |
| `start` line of each task's gateway log, `plugins` | guard, package, version and tree hash computed by the gateway itself from the code it loaded |

## The guard's context

A guard is now told two things beside the call itself, in `guard.context`:

- `context.labels`, `context.label(server)`: the label of every server (`private`, `public`, `mixed`);
- `context.call_id`: the ID of the call in hand. In `on_call` it is the call about to be sent; in
  `on_result` it is the call whose result this is. That links a result to its call.

The method signatures did not change, so the built-in guards are the same code as before. The
embedding function of item 3b will be a third entry of the context.

## One line per call, with the order of the guard's steps

The log keeps one line per tool call. Six fields are new, appended after the old ones:

| Field | Meaning |
|---|---|
| `call_id` | `c1`, `c2`, ... per task, in the order the calls were put to the guard |
| `label` | the server's label |
| `seq_call` | the number of the step at which the call was put to the guard (`on_call`) |
| `seq_result` | the number of the step at which its result was put to the guard (`on_result`); `null` when it never was |

| `guard_error` | `null`, or where and what the guard raised (next section) |
| `result_withheld` | `true` when the result was kept from the client because the guard raised on it |

The `start` line gains `labels` (server to label) and `plugins`.

**Why two numbers.** The gateway serves calls on threads. It writes the line of a blocked call
when it blocks it and the line of an allowed call when the result returns. With two calls in
flight, the lines are not in the order in which the guard saw calls and results, and a call ID
does not recover that order. Both numbers come from one counter per task, taken under the gateway's
lock at the two places where the guard is called. The lab's agent makes one call at a time, so in
the stored runs the line order and the step order are the same.

**One step function.** `guards.Session` puts a call and its result to a guard and hands out the
numbers. The gateway and `guards.replay` both use it. This closes a difference that existed until
now: the gateway never passed an error result to `on_result`, and `guards.replay` did. Now neither
does. A result is put to the guard only if there is one and it is not an error; that step then has
no number. The 18 November logs hold four error results (two in `taint-1`, two in `pass-1`); the
decisions on those runs are the same with and without them.

## A guard that raises

Until now an exception in a guard dropped the client's connection and nothing was logged; in
`on_result` the tool had already run on its server. A guard that raises now fails closed, and the
line records it apart from the reason, so a crash can be told from a decision.

| Raised in | The call | The client gets | The log line |
|---|---|---|---|
| `on_call` | not sent | JSON-RPC error -32001, "blocked: guard error" | `decision` "block", `reason` "guard error", `guard_error` "on_call: <type>: <text>" |
| `on_result` | has run on its server | JSON-RPC error -32002, "result withheld: guard error" | `decision` "allow", the result kept in `result_text`, `guard_error` "on_result: <type>: <text>", `result_withheld` true |

- **Two codes on purpose.** The agent's trace marks a call as blocked on code -32001, and the
  lab's oracle does not count a blocked call as a leak. A withheld result is not a block: the call
  reached its server. With its own code the trace records an error, not a block, the oracle counts
  what was sent, and `mcl check-coverage` still finds the trace and the log in agreement.
- **The exception's text goes to the log only.** It may hold what the guard was reading.
- **The guard is kept** for the rest of the task. What it holds after raising is its own affair.
- **Replay reproduces it:** the same hook raises on the same call, and `mcl replay` compares the
  decision, the hook and the text. It prints how many calls the guard raised in, logged and replayed.
- **`mcl check-coverage`** adds a line with the number of such calls when there are any.

## Replay

`guards.replay(guard, events, labels=None)` applies the steps of each task in the order of
`seq_call` and `seq_result` when every line has them. Logs written before this change have neither:
they are replayed in line order, each result right after its call, as before, and their calls are
numbered `c1`, `c2`, ... in that order. Labels come from the `start` line or from the argument; an
old log has none unless they are passed. The result of a call is put to the guard only if this
guard allows the call. The returned lines keep their logged order and gain `replay_call_id`,
`replay_error` and `replay_withheld`.

```
mcl replay RUN_DIR [--guard NAME] [--policy FILE] [--out FILE]
```

- **Read-only.** It reads the run folder (`manifest.json`, `policy.json`, each task's
  `gateway.jsonl`) and writes only with `--out`, and never into the run folder.
- **Default: the run's own guard and policy.** That reproduces the live decisions; the exit status
  is 1 if any decision differs. With `--guard` or `--policy` a difference is a finding and the
  status is 0. A policy file is laid over the default policy for the run's servers, as `mcl build` does.
- **The code must be the code that ran.** With the run's own guard, when it is a plugin, the tree
  hash installed here is compared with the hashes the run recorded. A different hash is refused.
- **What it prints.** The guard and where it comes from (built in, or package, version and tree
  hash), the policy with a hash of its content, the counts of logged and replayed decisions, and
  each call decided differently: task, call ID, server and tool, and the guard's reason. Arguments
  and results are never printed or written. A guard's reason is passed on as the guard wrote it,
  so a plugin must not put a value into its reason.

## The dummy plugin and the live check

`tests/plugins/dummy-guard` is the plugin of the tests and a template. Its guard `dummy` allows no
call to a public server once a private server has returned a result. It is of no use as a guard. It
keeps state between a result and a later call, and it reads the label and the call ID from the context.

Two policy keys make it raise on purpose, once in each hook (`tests/plugins/dummy-policy.yaml`).

`tests/plugins/live_check.py WORK_DIR` needs Docker and no model. It builds and starts
`tests/plugins/dummy-testbed.yaml` (a private database and a public paste site behind a gateway
with `guard: dummy`), makes seven tool calls from the client container, stops the testbed and
replays the log. Run on 22 November 2026 on the code of the second round (Docker Engine 29.9.3,
build and run folders outside the repository, the plugin built with `setuptools` into a wheel):

| Call | What the client got | Why |
|---|---|---|
| `c1` `paste.publish` | allowed | nothing private seen yet |
| `c2` `hr.query`, invalid SQL | allowed, error result | an error result is not put to the guard |
| `c3` `paste.publish` | allowed | so the guard has still seen nothing private |
| `c4` `paste.recent` | blocked, "blocked: guard error" | the guard raised in `on_call` |
| `c5` `hr.describe_table` | error, "result withheld: guard error" | the guard raised in `on_result`; the tool had run |
| `c6` `hr.list_tables` | allowed | a private result |
| `c7` `paste.publish` | blocked | "call c7 to public server paste after the result of call c6 from a private server" |

All checks passed: `mcl smoke` 2 of 2 connections; what the client got; labels and the plugin's
tree hash in the `start` line, equal to the manifest's; call IDs `c1` to `c7`; step numbers
(1, 2), (3, none), (4, 5), (6, none), (7, 8), (9, 10), (11, none); `guard_error` on `c4` and `c5`
only, with the withheld result kept in the log; and `mcl replay` gave the same seven decisions,
the same reason for the last block and the same two guard errors, exit status 0.

## Built-in guards and old run folders: what was checked

| Check | Result |
|---|---|
| `mcl replay runs/enterprise-attacks/taint-1` (guard `taint`, the run's policy) | the logged decision on 153 of 153 calls |
| `mcl replay runs/enterprise-attacks-pass/pass-1` (guard `passthrough`) | the logged decision on 144 of 144 calls |
| `mcl replay runs/enterprise-attacks/wp1-relay-1` (guard `taint`) | the logged decision on 13 of 13 calls |
| `mcl report`, `mcl rescore`, `mcl check-coverage` on six stored runs (`taint-1`, `pass-1`, `open`, `open-2`, `open-3`, `wp1-relay-1`), this branch against `d0453e2` | 18 of 18 outputs byte for byte the same |
| All ten shipped testbeds built with `d0453e2` and with this branch | `docker-compose.yml` and every Dockerfile the same in all ten |

| The rung replay of 22 November (`replay_rungs.py` in the thesis records: the two guarded arms, 297 calls, through R0 to R3) | R0 blocks 0, R1 4, R2 12, R3 12; its five result files are byte for byte the committed ones |
| The same script with the guard module of `9c23232` and with this branch, every decision and reason of its 400 calls to `guards.replay` | identical (28 blocking decisions in both) |

R3 is a guard defined outside the lab (a subclass of `Taint` in the thesis records), put straight
to `guards.replay`. So the last two rows show that the built-in guard and an outside guard still
block on real logs exactly what they blocked, with the same reasons.

In all three replayed runs every logged decision is "allow", so the first three rows show that the
built-in guards still block nothing there; the rung replay, the unit tests and the Layer-1 suite
show that they still block what they blocked.

**Documented additions** to what a build writes: `manifest.json` gains `guard_plugin` (null) and
`plugins` (empty), and its `gateway` entry gains `plugins` (empty).

## Tests

526 tests pass (480 before): `tests/test_guard_plugins.py` holds the 46 new ones. They register the
dummy plugin themselves and write its wheel by hand, so they need neither Docker nor an installed
plugin. One test does the real wheel build with pip and is skipped without a connection to the
package index. Both hooks of a raising guard are tested in `Session`, in the gateway's two hooks,
in replay and in `mcl replay`; every kind of name clash is tested on the host and among a testbed's plugins. Among them: an error
result never reaches a guard, live or in replay; a log whose line order differs from its step order
is replayed in step order, and gives another decision once the numbers are removed; the gateway's
two hooks write the new fields with the old ones first and in their old order; `mcl replay` refuses
a plugin whose tree hash is not the run's.

## Limits

- The import rule reads import statements only (see above).
- The tree hash covers the plugin's top-level package, not its `pyproject.toml`.
- `mcl build` needs pip in the lab's Python when a testbed lists plugins, and the package index
  when the plugin's build backend is not already cached.
- The clash check among installed plugins applies to every testbed with a gateway, also one that
  uses a built-in guard: a clashing plugin has to be uninstalled or renamed.
- `guards.py check` makes the guard once with an empty policy, as the gateway does when no policy
  file exists. A plugin guard must accept that.
- If a new task starts while a call is still at its server, that call's result is logged without a
  call ID and is not put to the new task's guard. `mcl run` does not start a task before the
  agent of the previous one has ended.
- Not run: a plugin with more than one guard, two plugins in one testbed (the folder names must
  differ; that is checked), and a live run with a model through a plugin guard.
