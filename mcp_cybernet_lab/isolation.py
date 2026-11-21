"""`mcl check-isolation`: what the clients of a guarded testbed can and cannot reach.

With a gateway, every client sits on the internal network net-gateway. The checks run inside each
client container (netprobe.py, sent to it on standard input) and must find that

  - the client has no default route;
  - the model answers: through the LLM relay, with TLS verified end to end against the provider's
    certificate (`llm.egress: relay`), or from the testbed's own model service;
  - nothing else answers: an outside host, by name and by address, and every server and service
    of the testbed, by name and by address (the client reaches them through the gateway only);
  - the relay has one open port and leads to one host;
  - the gateway is reachable.

No tokens are spent: the model API is only asked for its model list. Run it after `mcl up` and
before `mcl run`; it exits with status 1 if any check has another outcome.
"""

from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from .compose import GATEWAY_NET, HTTP_PORT, LLM_PORT
from .loader import Loaded, TestbedError
from .schema import LLM_EGRESS, LLM_RELAY, Testbed

OUTSIDE = "example.com"  # reserved for documentation (RFC 2606): an outside host that is safe to name
PUBLIC_ADDRESS = "1.1.1.1"
PROBE = Path(__file__).with_name("netprobe.py")


def addresses(build: Path) -> dict[str, dict[str, list[str]]]:
    """service -> network (named as in the compose file) -> addresses of its running containers."""
    compose = ["docker", "compose", "-f", str(build / "docker-compose.yml")]
    ids = subprocess.run([*compose, "ps", "-q"], capture_output=True, text=True).stdout.split()
    if not ids:
        return {}
    fmt = ('{{index .Config.Labels "com.docker.compose.service"}}\t'
           '{{index .Config.Labels "com.docker.compose.project"}}\t{{json .NetworkSettings.Networks}}')
    r = subprocess.run(["docker", "inspect", "-f", fmt, *ids], capture_output=True, text=True)
    out: dict[str, dict[str, list[str]]] = {}
    for line in r.stdout.splitlines():
        service, project, networks = line.split("\t", 2)
        for net, spec in json.loads(networks).items():
            out.setdefault(service, {}).setdefault(net.removeprefix(f"{project}_"), []).append(spec["IPAddress"])
    return out


def plan(tb: Testbed, addr: dict[str, dict[str, list[str]]], outside_address: str | None) -> dict:
    """What netprobe.py must find in a client container of this testbed, as plain data.

    addr: the result of addresses(). outside_address: an address of OUTSIDE, looked up on the
    host, because the client must not be able to look it up itself.
    """
    def closed(what: str, sid: str, port: int) -> dict:
        return {"what": what, "host": sid, "port": port,
                "addresses": sorted(a for found in addr.get(sid, {}).values() for a in found)}

    model, relay = None, None
    if tb.llm.provider != "none" and tb.llm.model and tb.llm.endpoint:
        url = urlsplit(tb.llm.endpoint)
        model = {"url": tb.llm.endpoint.rstrip("/") + "/models", "host": url.hostname,
                 "port": url.port or (443 if url.scheme == "https" else 80), "name": tb.llm.model,
                 "provider": tb.llm.provider, "key_env": tb.llm.api_key_env}
    targets = [closed(f"{s.label} server", s.id, HTTP_PORT) for s in tb.servers if s.transport == "http"]
    targets += [closed(f"{v.label} service", v.id, HTTP_PORT if v.builtin else 80)
                for v in tb.services if v.id != tb.llm_service]  # the model service is meant to answer
    if tb.llm.egress == "relay":
        relay = {"addresses": sorted(addr.get(LLM_RELAY, {}).get(GATEWAY_NET, []))}
        targets.append(closed("relay exit", LLM_EGRESS, LLM_PORT))
    return {"gateway": f"http://gateway:{HTTP_PORT}/", "model": model, "relay": relay, "closed": targets,
            "outside": {"name": OUTSIDE, "address": outside_address, "public_address": PUBLIC_ADDRESS}}


def check(loaded: Loaded, build: Path) -> list[dict]:
    """Run the probe in every client container: one {"client", "name", "ok", "detail"} per check."""
    tb = loaded.testbed
    if tb.gateway.guard == "none":
        raise TestbedError("check-isolation is for a testbed with a gateway; "
                           "without one, clients sit on the domain networks")
    addr = addresses(build)
    missing = sorted(({c.id for c in tb.clients} | {"gateway"}) - set(addr))
    if missing:
        raise TestbedError(f"containers not running: {missing}; run `mcl up` first")
    try:
        outside = socket.getaddrinfo(OUTSIDE, 443, socket.AF_INET)[0][4][0]
    except OSError:
        outside = None
    env = "MCL_NETPROBE=" + json.dumps(plan(tb, addr, outside))
    results = []
    for c in tb.clients:
        for i in range(1, c.replicas + 1):
            r = subprocess.run(["docker", "compose", "-f", str(build / "docker-compose.yml"), "exec", "-T",
                                "--index", str(i), "-e", env, c.id, "python3", "-"],
                               input=PROBE.read_text(encoding="utf-8"), capture_output=True, text=True)
            found = [json.loads(line) for line in r.stdout.splitlines() if line.startswith("{")]
            if r.returncode not in (0, 1) or not found:  # the probe itself failed: say so, as a failed check
                tail = (r.stderr or r.stdout).strip().splitlines()[-1:]
                found.append({"name": "the probe ran to its end", "ok": False,
                              "detail": tail[0] if tail else "no output"})
            results += [{"client": c.id if c.replicas == 1 else f"{c.id}[{i}]", **f} for f in found]
    return results


def render(results: list[dict]) -> str:
    lines, client = [], None
    for r in results:
        if r["client"] != client:
            client = r["client"]
            lines.append(f"client {client}")
        lines.append(f"{'PASS' if r['ok'] else 'FAIL'}  {r['name']}: {r['detail']}")
    lines.append(f"{sum(r['ok'] for r in results)}/{len(results)} checks have the expected outcome")
    return "\n".join(lines)
