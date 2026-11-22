# Guard plugins, richer gateway events and `mcl replay` (work package 3, items 3a and 3c)

22 November 2026, branch `wp3-extension-points`. The branch starts from a merge (`d0453e2`) of
`wp1-llm-egress` (`4f2414a`) and `wp2-oracle-v2` (`ab745fc`), because later packages need both. Work
package 3 of the FlowGuard lab requirements (revision 3.2, 22 November 2026, in Ayman Ketat's thesis
records): item 3a (guard plugins) and item 3c (richer gateway events). Item 3b (the embedding
sidecar) is not in this change; it follows on its own branch.

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
requires = ["uv_build>=0.9.5,<0.10"]
build-backend = "uv_build"

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
- **Built-in names win.** A plugin cannot replace `passthrough` or `taint`.
- **Interface.** `reset`, `on_call` and `on_result`, unchanged. Subclassing `Guard` is the simple
  way; a class with those three methods also works.
- **Installed on the host too.** `mcl build` checks the name against the built-in guards and the
  plugins installed on the host, and replay loads the plugin there: `pip install -e <folder>`.
  A plugin guard whose folder is missing from `gateway.plugins` is refused at build.

`gateway.guard` is now any name of the lab's ID form. Unknown names fail at `mcl build`, not in the
schema. `mcphunt` and `fides` are refused with the same message as before unless a plugin registers them.

## How a plugin gets into the gateway image

Only when `gateway.plugins` is not empty, `mcl build` copies each folder to `context/plugins/<name>/`
and adds five lines to the gateway's Dockerfile:

```
COPY mcp_cybernet_lab /opt/mcl/mcp_cybernet_lab
COPY plugins /opt/mcl-plugins
RUN uv pip install --no-deps --no-index --python /usr/bin/python3 --target /opt/mcl/site /opt/mcl-plugins/dummy-guard
ENV PYTHONPATH=/opt/mcl/site
RUN python3 /opt/mcl/guards.py check dummy <tree hash>
```

- **Nothing is fetched.** `--no-deps --no-index` installs the plugin without its dependencies and
  without a package index. One consequence, measured: the plugin's build backend must be one uv
  carries itself. With `uv_build` in a version range that holds the image's uv (0.9.5) the install
  works offline; with `setuptools` it fails, because the backend cannot be downloaded.
- **The import path.** In the image the lab's modules are plain files in `/opt/mcl`, so
  `mcp_cybernet_lab.guards` does not exist there. A two-file package makes that name mean the very
  module the gateway imports as `guards`. A plugin therefore uses one import on the host and in the image.
- **The last line** loads the configured guard, applies the import rule and compares the code in
  the image with the code installed on the host. A missing, broken or different plugin fails the
  image build, not the run. Checked by hand: a wrong hash, a `setuptools` plugin and a plugin that
  imports `yaml` each stop the build.

Only the gateway image gets the plugin. The compose file gains nothing: same service, same
environment, same volumes.

## Which code ran: the tree hash

A version number stays the same while the code changes. Each plugin guard is therefore identified
by a tree hash: the SHA-256 of the names and contents of the files of its top-level package,
compiled files left out. The same code gives the same hash however it was installed (editable on
the host, copied into the image).

| Where | What is recorded |
|---|---|
| `manifest.json`, `guard_plugin` | guard, package, version, tree hash of the code on the host at build; `null` for a built-in guard |
| `manifest.json`, `plugins` | name, path and source hash of each folder in `gateway.plugins` |
| `start` line of each task's gateway log, `plugins` | guard, package, version and tree hash computed by the gateway itself from the code it loaded |

## The guard's context

A guard is now told two things beside the call itself, in `guard.context`:

- `context.labels`, `context.label(server)`: the label of every server (`private`, `public`, `mixed`);
- `context.call_id`: the ID of the call in hand. In `on_call` it is the call about to be sent; in
  `on_result` it is the call whose result this is. That links a result to its call.

The method signatures did not change, so the built-in guards are the same code as before. The
embedding function of item 3b will be a third entry of the context.

## One line per call, with the order of the guard's steps

The log keeps one line per tool call. Four fields are new, appended after the old ones:

| Field | Meaning |
|---|---|
| `call_id` | `c1`, `c2`, ... per task, in the order the calls were put to the guard |
| `label` | the server's label |
| `seq_call` | the number of the step at which the call was put to the guard (`on_call`) |
| `seq_result` | the number of the step at which its result was put to the guard (`on_result`); `null` when it never was |

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

## Replay

`guards.replay(guard, events, labels=None)` applies the steps of each task in the order of
`seq_call` and `seq_result` when every line has them. Logs written before this change have neither:
they are replayed in line order, each result right after its call, as before, and their calls are
numbered `c1`, `c2`, ... in that order. Labels come from the `start` line or from the argument; an
old log has none unless they are passed. The result of a call is put to the guard only if this
guard allows the call. The returned lines keep their logged order and gain `replay_call_id`.

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

`tests/plugins/live_check.py WORK_DIR` needs Docker and no model. It builds and starts
`tests/plugins/dummy-testbed.yaml` (a private database and a public paste site behind a gateway
with `guard: dummy`), makes five tool calls from the client container, stops the testbed and
replays the log. Run on 22 November 2026 (Docker Engine 29.9.3, build and run folders outside the
repository):

| Call | Live decision | Why |
|---|---|---|
| `c1` `paste.publish` | allowed | nothing private seen yet |
| `c2` `hr.query`, invalid SQL | allowed, error result | an error result is not put to the guard |
| `c3` `paste.publish` | allowed | so the guard has still seen nothing private |
| `c4` `hr.list_tables` | allowed | a private result |
| `c5` `paste.publish` | blocked | "call c5 to public server paste after the result of call c4 from a private server" |

All checks passed: `mcl smoke` 2 of 2 connections; the five decisions; labels and the plugin's
tree hash in the `start` line, equal to the manifest's; call IDs `c1` to `c5`; step numbers
(1, 2), (3, none), (4, 5), (6, 7), (8, none); and `mcl replay` gave the same five decisions with
the same reason for the block, exit status 0.

## Built-in guards and old run folders: what was checked

| Check | Result |
|---|---|
| `mcl replay runs/enterprise-attacks/taint-1` (guard `taint`, the run's policy) | the logged decision on 153 of 153 calls |
| `mcl replay runs/enterprise-attacks-pass/pass-1` (guard `passthrough`) | the logged decision on 144 of 144 calls |
| `mcl replay runs/enterprise-attacks/wp1-relay-1` (guard `taint`) | the logged decision on 13 of 13 calls |
| `mcl report`, `mcl rescore`, `mcl check-coverage` on six stored runs (`taint-1`, `pass-1`, `open`, `open-2`, `open-3`, `wp1-relay-1`), this branch against `d0453e2` | 18 of 18 outputs byte for byte the same |
| All ten shipped testbeds built with `d0453e2` and with this branch | `docker-compose.yml` and every Dockerfile the same in all ten |

In all three replayed runs every logged decision is "allow", so the first three rows show that the
built-in guards still block nothing there; that they still block what they blocked is shown by the
unit tests and the Layer-1 suite, which pass unchanged.

**Documented additions** to what a build writes: `manifest.json` gains `guard_plugin` (null) and
`plugins` (empty), and its `gateway` entry gains `plugins` (empty).

## Tests

508 tests pass (480 before): `tests/test_guard_plugins.py` holds the 28 new ones. They register the
dummy plugin themselves, so they need neither Docker nor an installed plugin. Among them: an error
result never reaches a guard, live or in replay; a log whose line order differs from its step order
is replayed in step order, and gives another decision once the numbers are removed; the gateway's
two hooks write the new fields with the old ones first and in their old order; `mcl replay` refuses
a plugin whose tree hash is not the run's.

## Limits

- The import rule reads import statements only (see above).
- The tree hash covers the plugin's top-level package, not its `pyproject.toml`.
- `guards.py check` makes the guard once with an empty policy, as the gateway does when no policy
  file exists. A plugin guard must accept that.
- If a new task starts while a call is still at its server, that call's result is logged without a
  call ID and is not put to the new task's guard. `mcl run` does not start a task before the
  agent of the previous one has ended.
- Not run: a plugin with more than one guard, two plugins in one testbed (the folder names must
  differ; that is checked), and a live run with a model through a plugin guard.
