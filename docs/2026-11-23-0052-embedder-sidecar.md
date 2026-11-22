# The embedder: a sidecar with a pinned model, `embed` for guards, and replay from recorded vectors (work package 3, item 3b)

23 November 2026, branch `wp3-embedder`. The second half of item 3b of the FlowGuard lab
requirements (revision 3.3). The first half, the candidate models and their calibration, is in
`docs/2026-11-22-2345-embedding-calibration.md`. Which model the lab should use is not decided
here: the model is a setting of a testbed, its default is none, and no shipped testbed sets it.

**Outcome.** A testbed can name one of the pinned embedding models. It then gets an `embedder`
service on a network that only the gateway shares, and guards get `context.embed(texts)`. Every
vector a guard was given is written to the task's `embeddings.jsonl`, keyed by the SHA-256 of the
text, and `mcl replay` reads it back, so a replay sees what the live run saw and needs no
embedder. `mcl check-isolation` covers the embedder from four sides. Without the setting, every
shipped testbed builds to the same compose file and Dockerfiles as before.

A second round, after review of the first (`9b6c86d`), added three things, each marked below: the
image's packages are locked with hashes, the gateway's wait for the embedder is short and a
testbed setting, and the isolation check was run on a testbed with both the LLM relay and the embedder.

## The setting

```yaml
gateway:
  guard: my-guard
  embedder: minilm-paraphrase    # none (default), e5-small, labse, bge-m3, minilm-paraphrase
  embed_timeout_s: 5             # optional: seconds the gateway waits for one text (default: the model's)
```

The name is one of `embedding.MODELS`, each pinned by revision and by the SHA-256 of every file.
An unknown name, an embedder without a gateway, and a testbed that already uses the id
`embedder` are refused when the file is loaded.

## The service

| | |
|---|---|
| Network | `net-embed`, internal, with the gateway and nobody else |
| Runs as | user `nobody`, all capabilities dropped, read-only file system, no environment, no volume |
| Holds | the model's files and two modules, `embedding.py` and `embedserver.py` |
| Answers | `GET /health` (model, revision, files with hashes) and `POST /embed` (one vector or one refusal per text) |
| Ready | a health check; `mcl up --wait` and the gateway wait until the model is loaded |

**What goes into the image comes from the build context; the image build fetches nothing but
its base image.**

- **The model's files.** `mcl build` takes them from the cache on the host (it fetches them at the
  pinned revision if they are not there), checks every hash, and links them into the embedder's
  own build context. The image build runs `embedding.py verify` again: a file that is not the
  pinned one fails the build. No weight is downloaded at run time; the container has no route out.
- **The packages** are wheels that `mcl build` downloads on the host for the image's Python and
  the image installs with `--no-index`. The first version let the image run `pip install` itself;
  on the machine of this work that failed, because TLS from inside containers is intercepted
  there. Wheels from the host need no network in the build, as with guard plugins.
- **The packages are locked with hashes** *(second round)*. `mcp_cybernet_lab/embedder.lock` lists
  every package of the image, the three pinned ones (numpy, onnxruntime, tokenizers, in the
  versions of the calibration) and the seventeen they depend on, each at one version with the
  SHA-256 of its wheel for x86_64 and for aarch64. `mcl build` downloads exactly these
  (`pip download --require-hashes --no-deps -r embedder.lock`), then checks file by file that each
  wheel is a locked package with a locked hash and that none is missing; the image installs them
  with `--require-hashes` again. A wheel that is not the locked one stops the build. The manifest
  records the lock's hash and the hash of every wheel that went in. Changing a package is a
  deliberate act: `python -m mcp_cybernet_lab.embedding lock` writes the file again, to be reviewed
  and committed. The install extra `embed` for the host pins the three packages only.
- **Its own context.** The files are in `build/<testbed>/embedder/`, not in the context the other
  images share, so no other image carries the model or the module.
- The module is not one of `CONTAINER_MODULES`: the standard-library images stay as they are.

## `embed` for guards

`guard.context.embed(texts)` returns one vector per text: a tuple of floats of unit length.
`guards.cosine(a, b)` compares two. The guard does no I/O itself; the gateway backs the function
live and a cache backs it in replay.

| Case | What the guard gets |
|---|---|
| A text within the model's token limit | its vector |
| A text over the limit | `TextTooLong`, never a vector of a shortened text |
| The embedder does not answer, or is not the testbed's model | `EmbedError` |
| No embedder in the testbed | `EmbedError` |

A guard that lets either exception escape fails closed, as any guard that raises does: the call
is blocked, or the result withheld, and the log line has `guard_error`. A guard embeds inside its
hooks, which run under the gateway's lock, so other calls wait while a text is embedded.

**How long they wait** *(second round)*. The gateway waited up to 120 s for the embedder; a stuck
embedder held every call that long. The wait is now sized to the largest request a guard can
send, one text at the model's token limit, measured on one thread of the machine of the calibration:

| Model | Token limit in the lab | One text at that limit | Default wait |
|---|---|---|---|
| `minilm-paraphrase` | 128 | 0.09 s | 5 s |
| `e5-small` | 512 | 0.33 to 0.36 s | 5 s |
| `labse` | 256 | 0.57 to 0.63 s | 5 s |
| `bge-m3` | 512 (the model reads 8,192) | 4.3 to 5.9 s | 30 s |

- After the wait the guard gets `EmbedError` ("the embedder did not answer") and fails closed
  unless it handles it; the refusal is recorded, so replay reproduces it.
- `gateway.embed_timeout_s` sets another value for a testbed (above 0, at most 600).
- **`bge-m3` is bounded at 512 tokens in the lab.** At 2,048 tokens one text took 24 to 33 s, and
  at its full 8,192 tokens the process was killed for lack of memory on a machine with 8 GB. The
  calibration is not affected: no text of its pair set comes near any limit. The calibration note
  gives 8,192 as this model's limit; that was the reference's figure, and this bound replaces it.

## `embeddings.jsonl`

One file per task, next to `gateway.jsonl`. One line per distinct text the guard embedded:

```
{"sha256": "<of the text>", "model": "minilm-paraphrase", "revision": "<40 hex>", "dim": 384, "vector": "<float32, little-endian, base64>"}
{"sha256": "<of the text>", "model": "...", "revision": "...", "error": "too_long", "detail": "3226 tokens; minilm-paraphrase reads 128"}
```

The text is never written. A refusal is recorded too, with its words, so a guard that failed on
a text live fails in replay with the same `guard_error`. The `start` line of the gateway log and
`manifest.json` name the model and its revision.

## Replay

```
mcl replay RUN_DIR [--exact] [--embedder MODEL] [--embed-cache FILE]
```

- **From the run's vectors.** Replay reads every `embeddings.jsonl` of the run and gives the guard
  the vectors recorded there. No embedder runs.
- **`--exact`** is for reproducing a live run: a text the run recorded no vector for is an error,
  and nothing is computed.
- **Without `--exact`,** a text that is not in the run is computed on the host by the same module
  from the same pinned files (`pip install -e .[embed]`), marked `"computed_at": "replay"`, and
  kept in a cache outside the run folder (next to the model files, or `--embed-cache`). This is
  for Layer 1, for the calibration, and for replaying a guard over runs made without an embedder
  (`--embedder MODEL`). A cache inside the run folder is refused.
- A run made with another revision of the model than this version of the lab pins is refused.
- The report says how many texts had the run's vector and how many were computed here.

## `mcl check-isolation`

With an embedder the check probes more containers, with the probe it already uses:

| From | Must find |
|---|---|
| each client | the embedder does not resolve and does not answer at its address |
| each server | the same |
| the embedder | no default route; an outside host neither resolves nor answers by address; with an LLM relay, neither relay container resolves or answers *(second round)* |
| the gateway | the embedder answers and names the testbed's model and revision |

### With the LLM relay and the embedder together (second round)

`testbeds/enterprise-guarded.yaml` with `embedder: minilm-paraphrase` added, started on
23 November 2026 without an API key. No tokens: the check only asks the provider for its model list.

| From | Checks | Result |
|---|---|---|
| the agent | no default route; the provider's name resolves to the relay; no outside host, server, service, relay exit or embedder by name or address; the relay has one open port and carries no other host; the gateway answers | 26 of 26 |
| the agent | TLS to the provider verifies end to end through the relay; the model API answers | 0 of 2 (below) |
| each of the five servers | the embedder neither resolves nor answers | 10 of 10 |
| the embedder | no default route, no outside host, and neither relay container by name or address | 9 of 9 |
| the gateway | the embedder answers with the testbed's model | 1 of 1 |

46 of 48. The two that fail are the same fact: **the certificate the agent is shown for the
provider is not the provider's.** Read without trusting it, in a plain container and in the agent
through the relay, the certificate for `api.deepseek.com` (and for `pypi.org`, `huggingface.co`,
`registry.npmjs.org`, `example.com`) is issued by "Norton Web/Mail Shield Root" and does not verify.
On the WSL host the same names present their real issuers (Amazon, GlobalSign, Google Trust
Services, Cloudflare) and verify. So an antivirus product on this machine intercepts TLS from
containers, and the relay's guarantee holds as built: the agent verifies the provider's
certificate itself, sees a certificate it does not trust, and sends nothing, the key included. No
container was made to trust the interceptor; that would hand it the key. With the interception
on, a guarded run with a hosted model cannot start on this machine. The embedder is not affected:
its image is built from the host's downloads and it makes no connection.

**Run again later on 23 November, after HTTPS scanning was switched off in the antivirus product
on the host** (no change in the lab, no key, no tokens): 47 of 48.

- In a plain container the five names present their real issuers and verify; `api.deepseek.com`
  is issued by "Amazon RSA 2048 S24", as on the host.
- In the agent, through the relay: "TLS to api.deepseek.com verifies end to end through the
  relay", TLS 1.3, issuer CN=Amazon RSA 2048 S24, certificate names `*.deepseek.com`.
- The other 46 checks are as before, the nine of the embedder included.
- The one check that fails is the model list, with HTTP 401: the run was made without an API key.
  It is no longer a TLS failure. With the key it is the check that passed in work package 1.

**And once more with the API key** (23 November, the key in the environment for `mcl up` only; no
tokens: the provider is asked for its model list and nothing else): 48 of 48.

- "model API answers (GET /models with the key, no tokens)": HTTP 200, 2 models listed, the
  testbed's model `deepseek-flash` among them.
- "TLS to api.deepseek.com verifies end to end through the relay": TLS 1.3, issuer CN=Amazon RSA
  2048 S24, certificate names `*.deepseek.com`.
- The 46 other checks as before. The key is in no output of the check.

So with an embedder and the LLM relay in one testbed, the agent reaches its model and the gateway
and nothing else, no server reaches the embedder, and the embedder reaches nothing, the relay included.

## The live check

`tests/embedder/live_check.py WORK_DIR [--rebuild]` needs Docker and no language model. Testbed
`tests/embedder/embed-testbed.yaml`: a private database and a public paste site, the guard
`dummy-embed` of the test plugin (a public call is blocked when its text is as similar as 0.8 to an
earlier private result), and `minilm-paraphrase`, the smallest pinned model, which reads 128
tokens. Run on 23 November 2026 with `--rebuild`, Docker Engine 29.9.3:

| Call | What the client got | Why |
|---|---|---|
| `c1` `paste.publish` | allowed | nothing private seen yet |
| `c2` `hr.query`, three department names | allowed | a private result, embedded |
| `c3` `paste.publish` of that result | blocked | "is similar (0.995) to the result of call c2 from a private server" |
| `c4` `paste.publish`, an ordinary line | allowed | not similar |
| `c5` `hr.query`, every employee | error, "result withheld: guard error" | `TextTooLong: 3226 tokens; minilm-paraphrase reads 128`: the guard raised and failed closed |

| Check | Result |
|---|---|
| `mcl check-isolation` | 21 of 21: the agent and both servers cannot reach the embedder, it has no way out, the gateway reaches it |
| The same five calls after `docker compose restart embedder` | the same outcomes; every text has the same vector as before, byte for byte |
| The same after the embedder image was built again without the build cache | the same outcomes and the same bytes |
| `mcl replay --exact`, every container stopped | 15 of 15 calls: the same decisions, the same reason for the block, the same guard error |
| The run copied without its `embeddings.jsonl`, replayed without `--exact` | 4 texts computed on the host, the same 15 decisions; nothing written into the run folder |
| Host against container | the 4 host vectors are byte for byte the container's (same onnxruntime, different Python: 3.14 and 3.12) |

So the acceptance points of revision 3.3 that belong to 3b hold: the same text gives the same
vector on every run on this machine; replay needs no running sidecar; `mcl check-isolation`
covers the embedder.

**Image size,** which the calibration could not give: for `minilm-paraphrase` the layers add up to
about 0.8 GB (base image about 0.12 GB, packages 0.21 GB, model files 0.48 GB). `docker image
inspect` reports 1.38 GB on this machine, whose image store counts differently. For the other
models the image is larger by the difference in their files: about 0.8 GB for `e5-small`, 2.2 GB
for `labse`, 2.6 GB for `bge-m3` by layers. Only the `minilm-paraphrase` image was built.

## Unchanged without the setting

| Check | Result |
|---|---|
| All ten shipped testbeds, built with `d0453e2` and with this branch | the same `docker-compose.yml` and Dockerfiles |
| `mcl report`, `mcl rescore`, `mcl check-coverage` on six stored runs | 18 of 18 outputs byte for byte the same |
| `mcl replay --exact` of `taint-1` and `pass-1` | 153 of 153 and 144 of 144 |

Documented additions: `manifest.json` gains `embedder` (null), its `gateway` entry gains
`embedder` ("none"), and the `start` line of a gateway log gains `embedder` (null).

## Tests

637 tests pass. `tests/test_embedder.py` holds 30 (seven from the second round: the lock and the
file-by-file check of the wheels, a wheel that is not the locked one, the wait and its setting, a
stuck embedder, the relay in the embedder's plan): the cache (exact round trip, refusals recorded
and raised again, a missing text), the sidecar over HTTP with a stand-in model, the gateway's side
(a guard embeds, the vectors are written, replay from the file gives the same decisions; an
embedder that is another model or does not answer is refused), the build, the isolation plan, and
`mcl replay` with `--exact`, with the host fallback and with a model named for a run that had
none. One test uses the real `minilm-paraphrase` when its files are on the machine: two sessions
give the same bytes, and a long text is refused. The test plugin now registers two guards.

## Limits

- **One machine.** Byte-identical vectors were seen across a restart, a rebuild, and between the
  container and the host of this machine. Across processors they are not guaranteed; the run's
  file is what makes a replay exact.
- **The lock is for Python 3.12 on x86_64 and aarch64 Linux,** the embedder image's Python. The
  base image itself is named by tag (`python:3.12-slim-bookworm`), not by digest.
- **`mcl build` with an embedder** needs pip, the package index, and the model's files (fetched
  once, 0.5 to 2.3 GB). Without the setting it needs none of these (tested).
- **Windows and long texts are a guard's business.** `embed` refuses a long text; cutting a
  payload into windows is not built here, and the calibration did not test it.
- **The gateway's lock.** Calls wait while a guard embeds, now at most the wait above per text. A
  request that timed out is still being computed in the embedder, so the next one waits behind it.
- **Not run:** the three larger models as images, and a live run with a language model through an
  embedding guard. (An embedder together with the LLM relay was started and checked for isolation,
  above; no task was run on it.)
