"""Unit tests for the guarded LLM egress: the `llm.egress` field, the relay containers in the
generated compose file, and the relay itself. No Docker and no network needed: the relay is
exercised on 127.0.0.1 with made-up bytes."""

from __future__ import annotations

import ast
import asyncio
import copy
import json
import ssl
from pathlib import Path

import pytest
import yaml

from mcp_cybernet_lab import llmrelay
from mcp_cybernet_lab.compose import build
from mcp_cybernet_lab.loader import TestbedError, load_testbed
from mcp_cybernet_lab.runner import Runner
from mcp_cybernet_lab.schema import LLM_ENDPOINTS

ROOT = Path(__file__).resolve().parent.parent
TESTBEDS = sorted((ROOT / "testbeds").glob("*.yaml"))
RELAYS = {"llm-relay", "llm-egress"}
HOST = "api.deepseek.com"

GUARDED = {
    "testbed": "g",
    "defaults": {"transport": "http"},
    "domains": [{"id": "corp"}, {"id": "public", "internet": True}],
    "llm": {"provider": "deepseek", "model": "deepseek-flash", "api_key_env": "DEEPSEEK_API_KEY"},
    "servers": [
        {"id": "a", "package": "pkg-a", "runtime": "python", "version": "1", "domain": "corp"},
        {"id": "b", "package": "pkg-b", "runtime": "node", "version": "1", "domain": "public", "label": "public"},
    ],
    "services": [{"id": "hook", "builtin": "webhook-sink", "domain": "public", "label": "public"}],
    "clients": [{"id": "agent", "servers": ["a", "b"]}],
    "gateway": {"guard": "passthrough"},
}
MODEL_SERVICE = {"llm": {"provider": "vllm", "model": "m", "base_url": "http://vllm:8000/v1"},
                 "services": [{"id": "vllm", "image": "vllm/vllm-openai:v0.6.0", "domain": "corp"}]}


def load(tmp_path, **changes):
    data = copy.deepcopy(GUARDED) | copy.deepcopy(changes)
    p = tmp_path / "tb.yaml"
    p.write_text(yaml.safe_dump(data))
    return load_testbed(p)


def compose_of(loaded, out) -> dict:
    return yaml.safe_load((build(loaded, out) / "docker-compose.yml").read_text())


# --- the field and its default -----------------------------------------------------------------
@pytest.mark.parametrize("changes, egress", [
    ({}, "relay"),                                                             # gateway, hosted model
    ({"gateway": {"guard": "none"}}, "none"),                                  # no gateway: as before
    ({"llm": {"provider": "none"}}, "none"),
    ({"llm": {"provider": "ollama", "model": "m"}}, "none"),                   # a model on the host, http
    (MODEL_SERVICE, "none"),                                                   # a model in the testbed
    ({"llm": {"provider": "openai", "model": "m", "base_url": "https://llm.example.org/v1"}}, "relay"),
    ({"llm": {"provider": "openai", "model": "m", "base_url": "https://llm.example.org:8443/v1"}}, "none"),
    ({"llm": {"provider": "openai", "model": "m", "base_url": "https://192.0.2.7/v1"}}, "none"),
    ({"domains": [{"id": "corp"}, {"id": "public"}]}, "none"),                 # no way out for a relay
    ({"llm": GUARDED["llm"] | {"egress": "none"}}, "none"),                    # asked for: kept
    ({"llm": GUARDED["llm"] | {"egress": "relay"}}, "relay"),
])
def test_egress_default(tmp_path, changes, egress):
    assert load(tmp_path, **changes).testbed.llm.egress == egress


def test_egress_default_never_takes_an_id_the_testbed_uses(tmp_path):
    servers = GUARDED["servers"] + [{"id": "llm-relay", "package": "p", "runtime": "python", "version": "1",
                                     "domain": "corp"}]
    loaded = load(tmp_path, servers=servers, clients=[{"id": "agent", "servers": ["a", "llm-relay"]}])
    assert loaded.testbed.llm.egress == "none"


@pytest.mark.parametrize("changes, message", [
    ({"gateway": {"guard": "none"}}, "needs a gateway"),
    ({"llm": {"provider": "none"}}, "needs an llm endpoint"),
    ({"llm": {"provider": "ollama", "model": "m"}}, "on port 443"),
    ({"llm": {"provider": "openai", "model": "m", "base_url": "https://192.0.2.7/v1"}}, "host name"),
    (MODEL_SERVICE, "service of this testbed"),
    ({"domains": [{"id": "corp"}, {"id": "public"}]}, "internet: true"),
    ({"clients": [{"id": "llm-egress", "servers": ["a"]}]}, "needs the ids"),
])
def test_egress_relay_is_refused_where_it_cannot_work(tmp_path, changes, message):
    changes = copy.deepcopy(changes)
    changes["llm"] = changes.get("llm", GUARDED["llm"]) | {"egress": "relay"}
    with pytest.raises(TestbedError, match=message):
        load(tmp_path, **changes)


def test_shipped_testbeds_get_a_relay_only_behind_a_gateway():
    for path in TESTBEDS:
        tb = load_testbed(path).testbed
        hosted = tb.llm.provider in ("openai", "deepseek", "anthropic")
        assert tb.llm.egress == ("relay" if tb.gateway.guard != "none" and hosted else "none"), path.name


def test_a_guarded_client_without_a_path_to_its_model_gets_a_warning(tmp_path):
    def about_the_path(**changes):
        return [w for w in load(tmp_path, **changes).warnings if "no path to the model" in w]

    assert "on port 443" in about_the_path(llm={"provider": "ollama", "model": "m"})[0]
    assert not about_the_path()                                              # the relay is the path
    assert not about_the_path(**MODEL_SERVICE)                               # the model service is
    assert not about_the_path(llm=GUARDED["llm"] | {"egress": "none"})       # asked for: no comment
    assert not about_the_path(gateway={"guard": "none"})


def test_endpoint_table_is_the_one_the_containers_use():
    for file in ("mcp_cybernet_lab/agent.py", "servers/transformer/transformer/server.py"):
        tree = ast.parse((ROOT / file).read_text())
        table = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                     and getattr(n.targets[0], "id", None) == "ENDPOINTS")
        assert table == LLM_ENDPOINTS, file


# --- the generated compose file ----------------------------------------------------------------
def test_relay_containers_in_compose(tmp_path):
    out = tmp_path / "out"
    c = compose_of(load(tmp_path), out)
    s = c["services"]
    assert set(s) == {"a", "b", "agent", "gateway", "hook"} | RELAYS
    relay, egress = s["llm-relay"], s["llm-egress"]
    # The agent finds the provider's name on its own network; only llm-egress looks up the real one.
    assert relay["networks"] == {"net-gateway": {"aliases": [HOST]}, "net-public": {}}
    assert egress["networks"] == ["net-public"]
    assert relay["command"][-4:] == ["--upstream", "llm-egress:443", "--server-name", HOST]
    assert egress["command"][-4:] == ["--upstream", f"{HOST}:443", "--server-name", HOST]
    for svc in (relay, egress):  # no key, no run data, no privileges
        assert "environment" not in svc and "volumes" not in svc and "ports" not in svc
        assert svc["user"] == "65534:65534" and svc["cap_drop"] == ["ALL"] and svc["read_only"] is True
    assert relay["sysctls"]["net.ipv4.ip_forward"] == 0
    # The client is where it was: on the internal gateway network only.
    assert s["agent"]["networks"] == ["net-gateway"] and c["networks"]["net-gateway"] == {"internal": True}
    assert "DEEPSEEK_API_KEY" in s["agent"]["environment"]
    df = (out / "dockerfiles/llm-relay.Dockerfile").read_text()
    assert "COPY llmrelay.py /opt/mcl/" in df and "agent.py" not in df
    assert (out / "context/llmrelay.py").read_text() == (ROOT / "mcp_cybernet_lab/llmrelay.py").read_text()
    assert json.loads((out / "manifest.json").read_text())["llm"]["egress"] == "relay"


@pytest.mark.parametrize("path", TESTBEDS, ids=lambda p: p.stem)
def test_relay_is_only_an_addition_to_a_shipped_testbed(path, tmp_path):
    """Built with and without the relay, a testbed differs by the two relay containers alone."""
    loaded = load_testbed(path)
    out = tmp_path / loaded.testbed.testbed
    with_default = (build(loaded, out) / "docker-compose.yml").read_text()
    files = {f.relative_to(out): f.read_bytes() for f in (out / "dockerfiles").iterdir()}
    loaded.testbed.llm.egress = "none"
    without = (build(loaded, out) / "docker-compose.yml").read_text()
    if RELAYS & set(yaml.safe_load(with_default)["services"]):
        a, b = yaml.safe_load(with_default), yaml.safe_load(without)
        assert set(a["services"]) - set(b["services"]) == RELAYS
        assert {k: v for k, v in a["services"].items() if k not in RELAYS} == b["services"]
        assert a["networks"] == b["networks"]
        head, tail = without.split("\nnetworks:\n")  # the relays come last, after every service
        assert with_default.startswith(head + "\n") and with_default.endswith("\nnetworks:\n" + tail)
        assert files.pop(Path("dockerfiles/llm-relay.Dockerfile"))
    else:
        assert with_default == without
        assert not (out / "context/llmrelay.py").exists()
    assert files == {f.relative_to(out): f.read_bytes() for f in (out / "dockerfiles").iterdir()}


@pytest.mark.parametrize("path", TESTBEDS, ids=lambda p: p.stem)
def test_model_settings_passed_to_containers_are_unchanged(path, tmp_path):
    loaded = load_testbed(path)
    c = compose_of(loaded, tmp_path / "out")
    seen = [json.loads(s["environment"]["MCL_LLM"]) for s in c["services"].values()
            if "MCL_LLM" in s.get("environment", {})]
    assert seen and all(set(m) == {"provider", "model", "base_url", "temperature", "max_steps", "api_key_env"}
                        for m in seen)


def test_model_service_joins_the_gateway_network(tmp_path):
    c = compose_of(load(tmp_path, **MODEL_SERVICE), tmp_path / "out")
    assert c["services"]["vllm"]["networks"] == ["net-corp", "net-gateway"]
    assert not RELAYS & set(c["services"])
    c = compose_of(load(tmp_path, gateway={"guard": "none"}, **MODEL_SERVICE), tmp_path / "out")
    assert c["services"]["vllm"]["networks"] == ["net-corp"]  # no gateway: as before


def test_run_needs_the_relay_containers(tmp_path):
    loaded = load(tmp_path)
    runner = Runner(loaded, build(loaded, tmp_path / "out"))
    runner.running = lambda: {"agent", "gateway"}
    with pytest.raises(TestbedError, match="llm-egress.*llm-relay"):
        runner.run([{"id": "t", "prompt": "p"}])


# --- the relay ---------------------------------------------------------------------------------
def client_hello(name: str | None) -> bytes:
    """The first bytes a real TLS client sends, taken from Python's own TLS stack."""
    ctx = ssl.create_default_context()
    if name is None:
        ctx.check_hostname = False
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    tls = ctx.wrap_bio(incoming, outgoing, server_hostname=name)
    with pytest.raises(ssl.SSLWantReadError):
        tls.do_handshake()
    return outgoing.read()


def made_up_hello(extensions: bytes) -> bytes:
    body = b"\x03\x03" + bytes(32) + b"\x00" + b"\x00\x02\x13\x01" + b"\x01\x00"
    body += len(extensions).to_bytes(2, "big") + extensions
    handshake = b"\x01" + len(body).to_bytes(3, "big") + body
    return b"\x16\x03\x01" + len(handshake).to_bytes(2, "big") + handshake


def sni(*names: bytes) -> bytes:
    entries = b"".join(b"\x00" + len(n).to_bytes(2, "big") + n for n in names)
    data = len(entries).to_bytes(2, "big") + entries
    return b"\x00\x00" + len(data).to_bytes(2, "big") + data


def test_server_name_of_a_real_client_hello():
    assert llmrelay.server_name(client_hello(HOST)) == HOST
    assert llmrelay.server_name(client_hello(None)) is None
    assert llmrelay.server_name(made_up_hello(sni(b"API.DeepSeek.com"))) == HOST
    assert llmrelay.server_name(made_up_hello(b"")) is None


@pytest.mark.parametrize("record", [
    b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n",                 # not TLS
    client_hello(HOST)[:-7],                                        # cut short
    client_hello(HOST) + b"\x00",                                   # more than the record
    made_up_hello(sni(HOST.encode()) + sni(b"example.com")),        # two server_name extensions
    made_up_hello(sni(HOST.encode(), b"example.com")),              # two names in one
    made_up_hello(sni(HOST.encode())[:-1] + b"\xff"),               # not ASCII
    made_up_hello(sni(HOST.encode()) + b"\x00\x17\x00\x09"),        # an extension longer than the hello
], ids=["http", "truncated", "trailing", "two-extensions", "two-names", "not-ascii", "overlong"])
def test_server_name_refuses_what_is_not_one_plain_client_hello(record):
    with pytest.raises(llmrelay.Refused):
        llmrelay.server_name(record)


def relay_exchange(first: bytes, more: bytes = b"", name: str | None = HOST, upstream_up: bool = True):
    """Send bytes through a relay on 127.0.0.1 to a made-up upstream. -> (reply, upstream saw, log)"""
    async def scenario():
        seen, log = [], []

        async def upstream(reader, writer):
            seen.append(await reader.read(len(first) + len(more)))
            writer.write(b"REPLY:" + seen[-1][-len(more):] if more else b"REPLY")
            await writer.drain()
            writer.close()

        up = await asyncio.start_server(upstream, "127.0.0.1", 0)
        port = up.sockets[0].getsockname()[1]
        if not upstream_up:
            up.close()
            await up.wait_closed()
        relay = llmrelay.Relay(f"127.0.0.1:{port}", name, log=lambda **event: log.append(event))
        front = await asyncio.start_server(relay.handle, "127.0.0.1", 0)
        reader, writer = await asyncio.open_connection("127.0.0.1", front.sockets[0].getsockname()[1])
        writer.write(first + more)
        await writer.drain()
        reply = await asyncio.wait_for(reader.read(), 10)  # until the relay closes
        writer.close()
        for _ in range(100):  # the relay logs when the connection has ended
            if log:
                break
            await asyncio.sleep(0.02)
        front.close()
        up.close()
        return reply, b"".join(seen), log

    return asyncio.run(scenario())


def test_relay_passes_bytes_unchanged_and_logs_no_content():
    hello, secret = client_hello(HOST), b"PAYLOAD-NEVER-IN-THE-LOG-0123456789"
    reply, seen, log = relay_exchange(hello, secret)
    assert seen == hello + secret and reply == b"REPLY:" + secret
    (event,) = log
    assert set(event) == {"event", "n", "client", "upstream", "error", "seconds",
                          "bytes_to_upstream", "bytes_to_client"}
    assert event["error"] is None and event["client"] == "127.0.0.1"
    assert event["bytes_to_upstream"] == len(hello + secret) and event["bytes_to_client"] == len(reply)
    assert secret.decode() not in json.dumps(log)


@pytest.mark.parametrize("first", [client_hello("example.com"), client_hello(None),
                                   b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n" + bytes(600)],
                         ids=["another-name", "no-name", "not-tls"])
def test_relay_forwards_nothing_that_does_not_name_its_host(first):
    reply, seen, log = relay_exchange(first)
    assert reply == b"" and seen == b""  # closed, and the upstream was never contacted
    (event,) = log
    assert event["error"].startswith("refused:") and event["bytes_to_upstream"] == 0
    assert "example.com" not in json.dumps(log)  # what a client asks for is never logged


def test_relay_without_a_name_is_a_plain_tcp_relay_and_reports_a_dead_upstream():
    reply, seen, log = relay_exchange(b"any bytes", name=None)
    assert seen == b"any bytes" and reply == b"REPLY" and log[0]["error"] is None
    reply, seen, log = relay_exchange(client_hello(HOST), upstream_up=False)
    assert reply == b"" and log[0]["error"] == "ConnectionRefusedError"
