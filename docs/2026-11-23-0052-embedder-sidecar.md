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

## The setting

```yaml
gateway:
  guard: my-guard
  embedder: minilm-paraphrase    # none (default), e5-small, labse, bge-m3, minilm-paraphrase
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
- **The packages** (numpy, onnxruntime, tokenizers, in the versions of the calibration) are wheels
  that `mcl build` downloads on the host for the image's Python and installs with `--no-index`.
  The first version let the image run `pip install` itself; on the machine of this work that
  failed, because TLS from inside containers is intercepted there. Wheels from the host need no
  network in the build, as with guard plugins. The manifest lists the wheels that went in.
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
| the embedder | no default route; an outside host neither resolves nor answers by address |
| the gateway | the embedder answers and names the testbed's model and revision |

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

630 tests pass. `tests/test_embedder.py` holds 23: the cache (exact round trip, refusals recorded
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
- **Packages one level down are not pinned.** The three packages are pinned; what they depend on
  is resolved when the wheels are downloaded. The manifest records which wheels went in.
- **`mcl build` with an embedder** needs pip, the package index, and the model's files (fetched
  once, 0.5 to 2.3 GB). Without the setting it needs none of these (tested).
- **Windows and long texts are a guard's business.** `embed` refuses a long text; cutting a
  payload into windows is not built here, and the calibration did not test it.
- **The lock.** A slow model (`bge-m3`: about 0.3 s per text) holds up every call while a guard embeds.
- **Not run:** an embedder together with the LLM relay in one testbed, the three larger models as
  images, and a live run with a language model through an embedding guard.
