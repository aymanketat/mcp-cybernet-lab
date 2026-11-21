# Guarded LLM egress — what was built and how it was verified

22 November 2026, on lab commit `9c23232`. Work package 1 of the FlowGuard lab requirements
(revision 3, 21 November 2026, in Ayman Ketat's thesis records).

## The problem

With a gateway, `mcl build` puts every client on the internal network `net-gateway` and nowhere else
(`client_service` and the `networks` block in `compose.py`). The client reaches the gateway and has no
route out, so its agent cannot call a hosted model: a guarded task run fails before its first model
call. The shipped `enterprise-guarded` and `enterprise-attacks` testbeds could therefore not be run as
built.

## Built

- **Schema** (`schema.py`): `llm.egress: relay | none`. Left out, it becomes `relay` when a gateway is
  configured and the model endpoint is a public HTTPS host (the hosted providers, or a `base_url` with a
  host name on port 443), and `none` otherwise. `relay` asked for where it cannot work is an error that
  says why (no gateway, no HTTPS endpoint on port 443, no domain with `internet: true`, …).
- **Relay** (`llmrelay.py`, standard library only): accepts TCP and copies it to one upstream fixed on
  its command line. It does not terminate TLS. With `--server-name` it forwards a connection only if
  its TLS ClientHello names that host. It logs one line per connection: counter, client address, byte
  totals, duration, error. It logs no payload and no name a client asked for.
- **Generator** (`compose.py`): for `relay`, two services running that file, `llm-relay` and
  `llm-egress`, from one image that holds the relay file and none of the other lab modules. They get
  no environment and no volume, run as user 65534 with every capability dropped and a read-only root
  file system, and publish no port.
- **Local models**: when `llm.base_url` points at a service of the testbed (vLLM, Ollama under
  `services:`), that service joins `net-gateway`, and no relay is built.
- **`mcl check-isolation T.yaml`** (`isolation.py`, `netprobe.py`): run in every client container of a
  running guarded testbed; spends no tokens. See the list below.
- **`mcl check-coverage RUN_DIR`** (`report.py`): every tool call in the agent traces of a guarded run
  must be in the gateway log, with the same server, tool, arguments, order and block flag.
- Smaller: `mcl run` refuses to start when a built relay container is not running; `mcl validate`
  names the relay; a warning when clients behind a gateway have no path to their model at all;
  `manifest.json` records `llm.egress`.

## Two points where the design differs from a single relay container

**1. Two containers.** The agent finds the relay because the relay carries the provider's host name
as a network alias on `net-gateway`. Docker's DNS answers an alias for every container on that
network, including the one that carries it. Measured with a container on both networks holding the
alias `api.deepseek.com`:

| Asked from | `api.deepseek.com` resolves to |
|---|---|
| the container that holds the alias | its own address on the gateway network |
| a container on the public network only | the provider's public address |
| a container on the gateway network only | the alias holder's address |

So one container cannot both hold the name and look up the provider. `llm-relay` holds the name and
forwards to `llm-egress`; `llm-egress` is outside `net-gateway`, so it resolves and reaches the
provider. `llm-relay` also has IP forwarding switched off: it sits on two networks and is not a router.

**2. The TLS server name is checked.** A relay that copies TCP to "the provider's address, port 443"
reaches more than the provider when that address belongs to a content-delivery network. Measured from
a container with no default route, through a plain TCP relay to `api.deepseek.com:443`, and through
`llm-relay`:

| TLS server name asked for | Plain TCP relay | `llm-relay` |
|---|---|---|
| `api.deepseek.com` | verified (`*.deepseek.com`) | verified (`*.deepseek.com`) |
| `aws.amazon.com` | **verified** (`aws.amazon.com`) | refused by the relay |
| `d1.awsstatic.com` | **verified** (`d1.awsstatic.com`) | refused by the relay |
| `example.com` | handshake failure (not on that network) | refused by the relay |

A check with `example.com` alone passes on the plain relay and hides this. The relay therefore reads
the server name in the ClientHello, which a client sends in clear before encryption starts, and
forwards only the provider's. The parser is strict (one complete ClientHello in one record, at most one
`server_name` extension with exactly one host name), so the relay and the server behind it cannot read
two different names in the same bytes. TLS still runs end to end between the agent and the provider.

## What changes in a build

All ten shipped testbeds were built twice into the same paths, with the code at `9c23232` and with
this change, and the two build folders compared file by file.

| Testbeds | `docker-compose.yml`, Dockerfiles, `context/` | `manifest.json` |
|---|---|---|
| the 8 without a gateway | identical, byte for byte | new key `llm.egress: none` |
| `enterprise-guarded`, `enterprise-attacks` | compose: 52 lines added at the end of `services:` (the two relay services), none removed or changed; new files `dockerfiles/llm-relay.Dockerfile` and `context/llmrelay.py`; every other file identical | new key `llm.egress: relay` |

Every `manifest.json` also differs in its build time and commit, and that of `enterprise-attacks` in the
folder recorded for its attack servers, because the two builds came from two checkouts. The `MCL_LLM`
value passed to agents and transformers is unchanged: it does not carry `egress`.

## What `mcl check-isolation` checks

Inside each client container, with the expected outcome:

1. no default route;
2. the provider's host name resolves to `llm-relay`; TLS to it verifies against the provider's
   certificate, and the TCP peer is the relay; the model API answers `GET /models` with status 200 and
   lists the testbed's model (with a local model service: only this request);
3. `example.com` does not resolve, is unreachable by its address, and `https://example.com/` does not
   answer; a public address (`1.1.1.1:443`) is unreachable;
4. every server and service of the testbed, of any label, and `llm-egress`: the name does not
   resolve, and the address is unreachable. A refused connection counts as a failure here, because it
   shows a route to that address;
5. the relay's ports 80 and 8080 are closed, and a TLS session for another name through the relay
   fails;
6. the gateway answers.

Addresses are read from the running containers on the host. The script is sent to the container on
standard input, so no image contains it. The API key is read from the container's environment and
never printed; on an error status, only the status is reported.

## Verified (Ubuntu 26.04 on WSL 2, Docker Desktop 4.99.0, engine 29.9.3, Compose 5.7.0)

| Check | Result |
|---|---|
| Unit tests (`pytest`) | 108 passed: the 37 existing and 71 new |
| Builds at `9c23232` and with this change, all 10 testbeds | as in the table above |
| `enterprise-attacks`: `up --wait`, `smoke` | 14 containers running; 9/9 connections |
| `enterprise-attacks`: `check-isolation`, with the API key set for `mcl up` | **34 of 34**, exit status 0. TLS to the provider verified end to end through both relay containers; `GET /models` answered HTTP 200 and listed the testbed's model |
| `enterprise-guarded`: the same three commands | 5/5 connections; **26 of 26**, exit status 0 |
| Both testbeds again, with no API key in the environment | 33 of 34 and 25 of 26, exit status 1: `GET /models` answers HTTP 401 and the check says to set the key for `mcl up`. Every other check is unchanged |
| The relay containers, inspected while running | user 65534; no effective capability; no mount; no published port; no key variable in the environment, also when the agent has one; `ip_forward` 0 in `llm-relay` |
| Relay logs after the checks | one line per connection, both containers agree on byte totals; the refused connection is logged without the name it asked for |
| Local model: `enterprise-guarded` with a scripted stand-in for a model server as a service (0 tokens) | the service is on `net-corp` and `net-gateway`, no relay built; `check-isolation` 19 of 19; `mcl run`, 2 task runs: 10 tool calls, 2 blocked by the taint guard |
| `check-coverage` on that run | 10 calls in the traces, 10 in the gateway logs, 0 task runs differ, 2 block flags agree |
| `check-coverage` on two earlier guarded runs of 50 task runs each (passthrough, taint) | 144 of 144 and 153 of 153 calls; 0 task runs differ |
| `enterprise-attacks` with the hosted model: one pass of `tasks/attacks.yaml`, 5 task runs, protocol and prediction committed beforehand (thesis records, 22 November 2026) | 5 of 5 completed, 0 agent errors, 73,239 tokens. 16 model replies and 16 connections in each relay container, 0 errors, same sizes connection for connection. `check-isolation` 34 of 34 before the run and after it |
| `check-coverage` on that run | 13 calls in the traces, 13 in the gateway logs, 0 task runs differ |
| `down` | no container or network left |

## Not verified here

- **Other hosted providers.** Only `deepseek` was run. For `openai` and `anthropic` the relay and the
  checks take the same path, but the model-list request of `check-isolation` (for `anthropic`, with its
  own key header) has not been tried against them.
- **vLLM and Ollama themselves.** The local-model path was run with a scripted stand-in that answers
  `/v1/models` and `/v1/chat/completions`.

## Limits

- The relay checks the name in the TLS handshake, not the HTTP request inside the session, which it
  cannot see. Whether a request for another site is served inside a session opened for the provider
  is decided by the provider's network.
- A client that hides the server name (Encrypted Client Hello) is refused. The lab's agent and
  transformer use Python's standard library, which sends it in clear.
- The relay serves clients behind the gateway. A transformer server placed in a domain without
  `internet: true` still has no path to the model, as before.
