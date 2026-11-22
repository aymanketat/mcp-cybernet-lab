"""The embedder of a testbed: `context.embed` and its vector cache, the sidecar service, the build,
the gateway's side, replay from embeddings.jsonl, and the isolation plan. A stand-in model is used
throughout; one test at the end uses the smallest pinned model if its files are on this machine.
The live half is tests/embedder/live_check.py."""

from __future__ import annotations

import json
import sys
import threading
import types
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml

from mcp_cybernet_lab import compose, embedding, guards, isolation, runner
from mcp_cybernet_lab.cli import main
from mcp_cybernet_lab.loader import TestbedError, load_testbed

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "tests/plugins/dummy-guard"
TESTBED = ROOT / "tests/embedder/embed-testbed.yaml"
LABELS = {"hr": "private", "paste": "public"}
MODEL, REVISION = "minilm-paraphrase", embedding.MODELS["minilm-paraphrase"]["revision"]


def vector_of(text: str) -> tuple:
    """A stand-in for a model: two numbers that depend on the text, exactly representable as float32."""
    if len(text.split()) > 12:
        raise guards.TextTooLong(f"{len(text.split())} words; the stand-in reads 12")
    secret = 1.0 if "secret" in text else 0.0
    return (secret, 1.0 - secret, len(text) / 1024)


# --- the cache -----------------------------------------------------------------------------------
def test_a_context_without_an_embedder_refuses():
    with pytest.raises(guards.EmbedError, match="no embedder"):
        guards.Context().embed(["a text"])
    assert guards.cosine((1.0, 0.0), (1.0, 0.0)) == 1.0 and guards.cosine((1.0, 0.0), (0.0, 1.0)) == 0.0


def test_vectors_are_written_by_text_hash_and_read_back_exactly(tmp_path):
    path = tmp_path / "embeddings.jsonl"
    live = guards.EmbeddingCache(MODEL, REVISION, compute=vector_of, path=path)
    first = live.embed(["the secret plan", "an ordinary note"])
    assert live.embed(["the secret plan"]) == first[:1] and len(live.computed) == 2  # asked once per text
    lines = [json.loads(l) for l in path.read_text().splitlines()]
    assert [l["sha256"] for l in lines] == [guards.text_sha256("the secret plan"), guards.text_sha256("an ordinary note")]
    assert all(l["model"] == MODEL and l["revision"] == REVISION and l["dim"] == 3 for l in lines)
    assert "secret" not in path.read_text() and "ordinary" not in path.read_text()  # the text is never written
    replayed = guards.EmbeddingCache(MODEL, REVISION, known=guards.EmbeddingCache.read(path, MODEL, REVISION))
    assert replayed.embed(["an ordinary note", "the secret plan"]) == first[::-1]
    assert replayed.used == {l["sha256"] for l in lines} and not replayed.missing


def test_packed_vectors_are_exact():
    import struct
    values = struct.unpack("<4f", struct.pack("<4f", 0.1, -1 / 3, 1e-20, 123456.789))  # float32 values
    assert guards.unpack_vector(guards.pack_vector(values)) == values


def test_a_refusal_is_recorded_and_raised_again_with_the_same_words(tmp_path):
    path = tmp_path / "embeddings.jsonl"
    live = guards.EmbeddingCache(MODEL, REVISION, compute=vector_of, path=path)
    long_text = "word " * 20
    with pytest.raises(guards.TextTooLong, match="20 words; the stand-in reads 12") as first:
        live.embed([long_text])
    entry = json.loads(path.read_text())
    assert entry["error"] == "too_long" and "vector" not in entry
    replayed = guards.EmbeddingCache(MODEL, REVISION, known=guards.EmbeddingCache.read(path, MODEL, REVISION))
    with pytest.raises(guards.TextTooLong) as again:
        replayed.embed([long_text])
    assert str(again.value) == str(first.value)

    def down(text):
        raise guards.EmbedError("the embedder did not answer (TimeoutError)")

    live = guards.EmbeddingCache(MODEL, REVISION, compute=down, path=path)
    with pytest.raises(guards.EmbedError, match="did not answer"):
        live.embed(["a text"])
    assert json.loads(path.read_text().splitlines()[-1])["error"] == "unavailable"


def test_a_text_the_run_did_not_record_is_missing_and_another_models_lines_are_not_read(tmp_path):
    path = tmp_path / "embeddings.jsonl"
    guards.EmbeddingCache(MODEL, REVISION, compute=vector_of, path=path).embed(["one"])
    assert guards.EmbeddingCache.read(path, "labse", REVISION) == {} == guards.EmbeddingCache.read(path, MODEL, "0" * 40)
    assert guards.EmbeddingCache.read(tmp_path / "none.jsonl", MODEL, REVISION) == {}
    replayed = guards.EmbeddingCache(MODEL, REVISION, known=guards.EmbeddingCache.read(path, MODEL, REVISION))
    with pytest.raises(guards.EmbedError, match="no vector was recorded"):
        replayed.embed(["one", "two"])
    assert replayed.missing == {guards.text_sha256("two")} and not replayed.computed


# --- the sidecar ---------------------------------------------------------------------------------
class StandIn:
    """What embedserver needs of an Embedder."""
    name = MODEL
    spec = embedding.MODELS[MODEL]
    files = {"onnx/model.onnx": "0" * 64}

    def embed_one(self, text):
        import numpy as np
        try:
            return np.array(vector_of(text), dtype=np.float32)
        except guards.TextTooLong as e:
            raise embedding.TooLong(str(e)) from None


@pytest.fixture
def sidecar():
    pytest.importorskip("numpy")
    from mcp_cybernet_lab import embedserver
    server = embedserver.serve(StandIn(), 0, "127.0.0.1")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def post(url: str, body) -> tuple[int, dict]:
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_the_sidecar_names_its_model_and_embeds_one_text_at_a_time(sidecar):
    with urllib.request.urlopen(sidecar + "/health", timeout=10) as r:
        health = json.loads(r.read())
    assert (health["model"], health["revision"], health["max_tokens"]) == (MODEL, REVISION, 128)
    status, answer = post(sidecar + "/embed", {"texts": ["the secret plan", "word " * 20, "the secret plan"]})
    assert status == 200 and (answer["model"], answer["revision"]) == (MODEL, REVISION)
    first, refused, again = answer["results"]
    assert guards.unpack_vector(first["vector"]) == vector_of("the secret plan") and first == again
    assert refused == {"error": "too_long", "detail": "20 words; the stand-in reads 12"}
    assert post(sidecar + "/embed", {"text": "no list"})[0] == 400 and post(sidecar + "/other", {})[0] == 404


# --- the gateway's side --------------------------------------------------------------------------
@pytest.fixture
def gateway(monkeypatch, tmp_path, sidecar):
    """The gateway module as in its container, with the guard dummy-embed and the stand-in sidecar."""
    monkeypatch.syspath_prepend(str(PLUGIN / "src"))
    import mcl_dummy_guard
    monkeypatch.setenv("MCL_GATEWAY", json.dumps({
        "guard": "dummy-embed", "embedder": {"model": MODEL, "revision": REVISION, "url": sidecar},
        "servers": {"hr": {"label": "private", "domain": "corp"}, "paste": {"label": "public", "domain": "public"}}}))
    monkeypatch.setenv("MCL_POLICY_FILE", str(tmp_path / "none.json"))
    for name in ("gateway", "guards"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.syspath_prepend(str(ROOT / "mcp_cybernet_lab"))
    import gateway as module
    import guards as inner
    ep = types.SimpleNamespace(name="dummy-embed", load=lambda: mcl_dummy_guard.DummyEmbedGuard,
                               dist=types.SimpleNamespace(name="mcl-dummy-guard", version="0.1.0"))
    monkeypatch.setattr(inner, "plugin_entry_points", lambda: {"dummy-embed": ep})
    log = tmp_path / "t-r0" / "gateway.jsonl"
    log.parent.mkdir()
    module.load_guard()
    module.State.log, module.State.task = open(log, "w", encoding="utf-8"), "t-r0"
    module.new_task_embeddings(log)

    def call(server, request_id, text, answer=None):
        handler = types.SimpleNamespace(server_id=server, headers={})
        handler.own_call = lambda msg: module.Gateway.own_call(handler, msg)
        body = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                           "params": {"name": "x", "arguments": {"text": text}}}).encode()
        blocked = module.Gateway.before(handler, body)
        if blocked is not None:
            return json.loads(blocked)["error"]
        result = {"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": answer or "ok"}]}}
        withheld = module.Gateway.after(handler, body, 200, "application/json", json.dumps(result).encode())
        return json.loads(withheld)["error"] if withheld else None

    def read():
        module.State.log.flush()
        return [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()]

    yield types.SimpleNamespace(call=call, read=read, module=module, folder=log.parent)
    module.State.log.close()
    module.State.log = None


def test_a_guard_embeds_through_the_gateway_and_the_vectors_are_kept_next_to_the_log(gateway):
    assert gateway.call("paste", 1, "an ordinary note") is None            # nothing private yet: nothing embedded
    assert gateway.call("hr", 2, "who", answer="the secret plan") is None   # a private result: embedded
    blocked = gateway.call("paste", 3, "the secret plan, shared")
    assert blocked["code"] == -32001 and "is similar (1.000) to the result of call c2" in blocked["message"]
    assert gateway.call("paste", 4, "an ordinary note") is None
    withheld = gateway.call("hr", 5, "all", answer="word " * 20)            # too long for the model: fails closed
    assert withheld == {"code": -32002, "message": "result withheld: guard error", "data": {"withheld": True}}
    lines = gateway.read()
    assert lines[-1]["guard_error"] == "on_result: TextTooLong: 20 words; the stand-in reads 12"
    recorded = [json.loads(l) for l in (gateway.folder / "embeddings.jsonl").read_text().splitlines()]
    assert [r["sha256"] for r in recorded] == [guards.text_sha256(t) for t in (
        "the secret plan", "the secret plan, shared", "an ordinary note", "word " * 20)]
    assert recorded[-1]["error"] == "too_long"
    # replay from the file alone, no sidecar: the same decisions, reasons and failure
    import mcl_dummy_guard
    known = guards.EmbeddingCache.read(gateway.folder / "embeddings.jsonl", MODEL, REVISION)
    guard = mcl_dummy_guard.DummyEmbedGuard({})
    again = guards.replay(guard, lines, LABELS, guards.EmbeddingCache(MODEL, REVISION, known=known).embed)
    assert [(e["replay_allowed"], e["replay_reason"], e["replay_error"]) for e in again] == [
        (e["decision"] == "allow", e["reason"], e["guard_error"]) for e in lines]


def test_the_gateway_refuses_an_embedder_that_is_not_the_testbeds_model_or_does_not_answer(gateway, monkeypatch):
    module = gateway.module
    assert module.ask_embedder("the secret plan") == vector_of("the secret plan")
    with pytest.raises(sys.modules["guards"].TextTooLong, match="20 words"):
        module.ask_embedder("word " * 20)
    monkeypatch.setitem(module.CONFIG["embedder"], "revision", "f" * 40)
    with pytest.raises(sys.modules["guards"].EmbedError, match="not the model of this testbed"):
        module.ask_embedder("a text")
    monkeypatch.setitem(module.CONFIG["embedder"], "url", "http://127.0.0.1:9")
    with pytest.raises(sys.modules["guards"].EmbedError, match="did not answer"):
        module.ask_embedder("a text")


def test_a_stuck_embedder_holds_a_call_for_the_timeout_and_no_longer(gateway, monkeypatch):
    import socket
    import time
    listener = socket.socket()  # accepts the connection and never answers
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    module = gateway.module
    monkeypatch.setitem(module.CONFIG["embedder"], "url", f"http://127.0.0.1:{listener.getsockname()[1]}")
    monkeypatch.setitem(module.CONFIG["embedder"], "timeout_s", 0.3)
    started = time.monotonic()
    assert gateway.call("hr", 1, "who", answer="the secret plan")["code"] == -32002  # the guard raised: failed closed
    assert time.monotonic() - started < 3
    assert gateway.read()[-1]["guard_error"] == "on_result: EmbedError: the embedder did not answer (TimeoutError)"
    listener.close()


def test_the_start_line_names_the_embedding_model(gateway):
    module = gateway.module
    module.write(type="start", guard="dummy-embed", policy_version=None, labels=module.labels(),
                 plugins=module.State.plugins,
                 embedder={k: module.CONFIG["embedder"][k] for k in ("model", "revision")})
    assert gateway.read()[-1]["embedder"] == {"model": MODEL, "revision": REVISION}


# --- the build -----------------------------------------------------------------------------------
@pytest.fixture
def no_download(monkeypatch, tmp_path):
    """The model's files as if they were in the cache: small files, and no network."""
    def fetch(name, root=None, log=print):
        folder = tmp_path / "cache" / name / embedding.MODELS[name]["revision"]
        for path in embedding.MODELS[name]["files"]:
            (folder / path).parent.mkdir(parents=True, exist_ok=True)
            (folder / path).write_text(path)
        return folder

    monkeypatch.setattr(embedding, "fetch", fetch)
    # one small "wheel", and a lock that holds exactly it
    import hashlib
    lock = tmp_path / "embedder.lock"
    lock.write_text("numpy==0 \\\n    --hash=sha256:" + hashlib.sha256(b"wheel").hexdigest() + "\n")
    monkeypatch.setattr(embedding, "LOCK", lock)
    monkeypatch.setattr(compose, "download_wheels", lambda dest: (dest / "numpy-0-py3-none-any.whl").write_bytes(b"wheel"))
    monkeypatch.syspath_prepend(str(PLUGIN / "src"))
    import mcl_dummy_guard
    eps = {name: types.SimpleNamespace(name=name, load=lambda cls=cls: cls,
                                       dist=types.SimpleNamespace(name="mcl-dummy-guard", version="0.1.0"))
           for name, cls in (("dummy", mcl_dummy_guard.DummyGuard), ("dummy-embed", mcl_dummy_guard.DummyEmbedGuard))}
    monkeypatch.setattr(guards, "plugin_entry_points", lambda: eps)
    monkeypatch.setattr(guards, "installed_entry_points", lambda: list(eps.values()))
    sys.path.insert(0, str(ROOT / "tests"))
    from test_guard_plugins import make_wheel
    both = {"dummy": "mcl_dummy_guard:DummyGuard", "dummy-embed": "mcl_dummy_guard:DummyEmbedGuard"}
    monkeypatch.setattr(compose, "build_wheel", lambda source, dest: make_wheel(source, dest, entry_points=both))


def test_an_embedder_is_a_service_of_its_own_on_a_network_only_the_gateway_shares(tmp_path, no_download):
    out = compose.build(load_testbed(TESTBED), tmp_path / "b", tmp_path / "runs")
    built = yaml.safe_load((out / "docker-compose.yml").read_text())
    service = built["services"]["embedder"]
    assert service == {"build": {"context": "embedder", "dockerfile": "../dockerfiles/embedder.Dockerfile"},
                       "image": "mcl-embed-check-embedder", "networks": ["net-embed"],
                       "labels": {"mcl.role": "embedder", "mcl.embedder": MODEL},
                       "user": "65534:65534", "cap_drop": ["ALL"], "read_only": True,
                       "healthcheck": service["healthcheck"]}
    assert "/health" in service["healthcheck"]["test"][-1]
    assert built["services"]["gateway"]["depends_on"] == {"embedder": {"condition": "service_healthy"}}
    assert "environment" not in service and "volumes" not in service
    assert built["networks"]["net-embed"] == {"internal": True}
    on_embed = sorted(name for name, s in built["services"].items() if "net-embed" in s.get("networks", []))
    assert on_embed == ["embedder", "gateway"]
    config = json.loads(built["services"]["gateway"]["environment"]["MCL_GATEWAY"])
    assert config["embedder"] == {"model": MODEL, "revision": REVISION, "url": "http://embedder:8080", "timeout_s": 5}
    dockerfile = (out / "dockerfiles/embedder.Dockerfile").read_text().splitlines()
    assert dockerfile == [
        "FROM python:3.12-slim-bookworm",
        "RUN --mount=type=bind,source=wheels,target=/wheels pip install --no-cache-dir --no-index "
        "--find-links /wheels --require-hashes -r /wheels/embedder.lock",
        "COPY embedding.py embedserver.py /opt/mcl/", "COPY models /models", "ENV MCL_EMBEDDING_MODELS=/models",
        f"RUN python3 /opt/mcl/embedding.py verify {MODEL}",
        f'CMD ["python3", "/opt/mcl/embedserver.py", "{MODEL}"]']
    files = sorted(p.relative_to(out / "embedder").as_posix() for p in (out / "embedder").rglob("*") if p.is_file())
    assert files == ["embedding.py", "embedserver.py", f"models/{MODEL}/{REVISION}/onnx/model.onnx",
                     f"models/{MODEL}/{REVISION}/tokenizer.json", "wheels/embedder.lock",
                     "wheels/numpy-0-py3-none-any.whl"]
    assert not (out / "context" / "embedding.py").exists()  # the other images do not get the module
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["embedder"]["revision"] == REVISION and manifest["embedder"]["files"] == embedding.MODELS[MODEL]["files"]
    assert manifest["embedder"]["max_tokens"] == 128
    import hashlib
    assert manifest["embedder"]["wheels"] == {"numpy-0-py3-none-any.whl": hashlib.sha256(b"wheel").hexdigest()}
    assert manifest["embedder"]["lock_sha256"] == embedding.file_sha256(embedding.LOCK)


def test_a_wheel_that_is_not_the_locked_one_stops_the_build(tmp_path, no_download, monkeypatch):
    monkeypatch.setattr(compose, "download_wheels", lambda dest: (dest / "numpy-0-py3-none-any.whl").write_bytes(b"other"))
    with pytest.raises(TestbedError, match="are not those of embedder.lock: numpy-0-py3-none-any.whl: not a package "
                                           "of the lock, or not with a locked hash"):
        compose.build(load_testbed(TESTBED), tmp_path / "b", tmp_path / "runs")


def test_the_timeout_is_the_models_unless_the_testbed_sets_one(tmp_path, no_download):
    out = compose.build(load_testbed(edited(tmp_path, lambda d: d["gateway"].update(embed_timeout_s=1.5))),
                        tmp_path / "b", tmp_path / "runs")
    built = yaml.safe_load((out / "docker-compose.yml").read_text())
    assert json.loads(built["services"]["gateway"]["environment"]["MCL_GATEWAY"])["embedder"]["timeout_s"] == 1.5
    assert json.loads((out / "manifest.json").read_text())["embedder"]["timeout_s"] == 1.5
    for bad in (0, -1, 601):
        with pytest.raises(TestbedError, match="embed_timeout_s"):
            load_testbed(edited(tmp_path, lambda d: d["gateway"].update(embed_timeout_s=bad)))
    assert {name: m["timeout_s"] for name, m in embedding.MODELS.items()} == {
        "e5-small": 5, "labse": 5, "bge-m3": 30, "minilm-paraphrase": 5}
    assert embedding.MODELS["bge-m3"]["max_tokens"] == 512  # the lab's bound, not the model's 8,192


def test_without_an_embedder_nothing_of_it_is_built(tmp_path, monkeypatch):
    monkeypatch.setattr(embedding, "fetch", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no fetch")))
    out = compose.build(load_testbed(ROOT / "testbeds/enterprise-guarded.yaml"), tmp_path / "b", tmp_path / "runs")
    built = yaml.safe_load((out / "docker-compose.yml").read_text())
    assert "embedder" not in built["services"] and "net-embed" not in built["networks"]
    assert "depends_on" not in built["services"]["gateway"]
    assert "embedder" not in json.loads(built["services"]["gateway"]["environment"]["MCL_GATEWAY"])
    assert not (out / "embedder").exists() and not (out / "dockerfiles/embedder.Dockerfile").exists()
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["embedder"] is None and manifest["gateway"]["embedder"] == "none"


def test_the_install_extra_is_what_the_image_installs():
    text = (ROOT / "pyproject.toml").read_text()
    assert "embed = " + json.dumps(embedding.REQUIREMENTS) in text


def edited(tmp_path, change) -> Path:
    data = yaml.safe_load(TESTBED.read_text())
    for s in data["servers"]:
        s["path"] = str((TESTBED.parent / s["path"]).resolve())
    data["gateway"]["plugins"] = [str(PLUGIN)]
    data["gateway"]["policy"] = str(TESTBED.parent / "embed-policy.yaml")
    change(data)
    f = tmp_path / "t.yaml"
    f.write_text(yaml.safe_dump(data))
    return f


@pytest.mark.parametrize("change, says", [
    (lambda d: d["gateway"].update(embedder="word2vec"), "gateway.embedder 'word2vec': not a pinned embedding model"),
    (lambda d: d.update(gateway={"embedder": MODEL}), "gateway.embedder needs a gateway"),
    (lambda d: d["servers"][1].update(id="embedder") or d["clients"][0].update(servers=["hr", "embedder"]),
     "the id 'embedder' is used by this testbed"),
], ids=["unknown-model", "no-gateway", "id-taken"])
def test_an_embedder_that_cannot_be_built_is_refused(tmp_path, change, says):
    with pytest.raises(TestbedError, match=says):
        load_testbed(edited(tmp_path, change))


# --- the isolation plan --------------------------------------------------------------------------
def test_isolation_covers_the_embedder_from_four_sides():
    tb = load_testbed(TESTBED).testbed
    addr = {"embedder": {"net-embed": ["10.0.7.2"]}, "gateway": {"net-embed": ["10.0.7.3"], "net-gateway": ["10.0.9.3"]},
            "agent": {"net-gateway": ["10.0.9.2"]}, "hr": {"net-corp": ["10.0.1.2"]}, "paste": {"net-public": ["10.0.2.2"]}}
    client = isolation.plan(tb, addr, "93.184.216.34")
    assert {"what": "embedder", "host": "embedder", "port": 8080, "addresses": ["10.0.7.2"]} in client["closed"]
    plans = isolation.embedder_plans(tb, addr, "93.184.216.34")
    assert [(service, role) for service, role, _ in plans] == [
        ("hr", "server"), ("paste", "server"), ("embedder", "embedder"), ("gateway", "gateway")]
    assert plans[0][2] == {"role": "server", "closed": [
        {"what": "embedder", "host": "embedder", "port": 8080, "addresses": ["10.0.7.2"]}]}
    assert plans[2][2]["outside"]["address"] == "93.184.216.34"
    assert plans[3][2]["embedder"] == {"url": "http://embedder:8080", "model": MODEL, "revision": REVISION}
    assert plans[2][2]["closed"] == []  # no relay in this testbed
    assert isolation.embedder_plans(load_testbed(ROOT / "testbeds/enterprise-guarded.yaml").testbed, addr, None) == []


def test_with_a_relay_the_embedder_is_probed_for_any_path_to_it(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "x")
    data = yaml.safe_load((ROOT / "testbeds/enterprise-guarded.yaml").read_text())
    data["gateway"]["embedder"] = MODEL
    f = ROOT / "testbeds" / "tmp-relay-embedder.yaml"
    try:
        f.write_text(yaml.safe_dump(data))
        tb = load_testbed(f).testbed
    finally:
        f.unlink()
    assert tb.llm.egress == "relay"
    addr = {"embedder": {"net-embed": ["10.0.7.2"]}, "llm-relay": {"net-gateway": ["10.0.9.5"], "net-public": ["10.0.2.5"]},
            "llm-egress": {"net-public": ["10.0.2.6"]}}
    plans = dict((service, plan) for service, _, plan in isolation.embedder_plans(tb, addr, None))
    assert plans["embedder"]["closed"] == [
        {"what": "relay", "host": "llm-relay", "port": 443, "addresses": ["10.0.2.5", "10.0.9.5"]},
        {"what": "relay exit", "host": "llm-egress", "port": 443, "addresses": ["10.0.2.6"]}]


def test_the_isolation_report_names_each_container_by_its_role():
    text = isolation.render([
        {"client": "agent", "name": "gateway reachable", "ok": True, "detail": "HTTP 404"},
        {"client": "hr", "role": "server", "name": "embedder `embedder` does not resolve", "ok": True, "detail": "x"},
        {"client": "embedder", "role": "embedder", "name": "no default route in the embedder", "ok": True, "detail": "none"},
        {"client": "gateway", "role": "gateway", "name": "the embedder answers", "ok": False, "detail": "refused"}])
    assert text.splitlines() == [
        "client agent", "PASS  gateway reachable: HTTP 404", "server hr", "PASS  embedder `embedder` does not resolve: x",
        "embedder", "PASS  no default route in the embedder: none", "gateway", "FAIL  the embedder answers: refused",
        "3/4 checks have the expected outcome"]


# --- replay --------------------------------------------------------------------------------------
def line(server, call_id, seq_call, seq_result, text, result="ok", decision="allow", reason="", **more) -> dict:
    return {"type": "tool_call", "server": server, "tool": "x", "arguments": {"text": text}, "decision": decision,
            "reason": reason, "result_text": result if decision == "allow" else None, "is_error": False,
            "task": "t-r0", "call_id": call_id, "seq_call": seq_call, "seq_result": seq_result,
            "guard_error": None, "result_withheld": False} | more


@pytest.fixture
def run(tmp_path, no_download):
    """A run folder as a live run with the guard dummy-embed and an embedder leaves it."""
    folder = tmp_path / "runs/embed-check/live"
    (folder / "t-r0").mkdir(parents=True)
    sha = guards.tree_sha256(PLUGIN / "src/mcl_dummy_guard")
    (folder / "manifest.json").write_text(json.dumps({
        "testbed": "embed-check", "gateway": {"guard": "dummy-embed"},
        "guard_plugin": {"guard": "dummy-embed", "package": "mcl-dummy-guard", "version": "0.1.0", "tree_sha256": sha},
        "embedder": {"model": MODEL, "revision": REVISION},
        "servers": [{"id": s, "label": label} for s, label in LABELS.items()]}))
    (folder / "policy.json").write_text(json.dumps({"version": 1, "similar_at": 0.8}))
    reason = "call c2 to public server paste is similar (1.000) to the result of call c1 from a private server"
    log = [line("hr", "c1", 1, 2, "who", result="the secret plan"),
           line("paste", "c2", 3, None, "the secret plan, shared", decision="block", reason=reason),
           line("paste", "c3", 4, 5, "an ordinary note")]
    (folder / "t-r0/gateway.jsonl").write_text("".join(json.dumps(e) + "\n" for e in log))
    live = guards.EmbeddingCache(MODEL, REVISION, compute=vector_of, path=folder / "t-r0/embeddings.jsonl")
    live.embed(["the secret plan", "the secret plan, shared", "an ordinary note"])
    return folder


def test_mcl_replay_exact_reproduces_a_run_from_its_vectors_alone(run, monkeypatch, capsys):
    monkeypatch.setattr(runner, "host_embedder", lambda model: (_ for _ in ()).throw(AssertionError("no model here")))
    before = sorted((p, p.stat().st_mtime_ns) for p in run.rglob("*"))
    assert main(["replay", str(run), "--exact"]) == 0
    text = capsys.readouterr().out
    assert "same decision as logged: 3 of 3" in text
    assert (f"embedding model {MODEL} at {REVISION[:12]}: 3 texts with the vector the run recorded (3 in the run), "
            f"0 computed here; --exact: nothing may be computed") in text
    assert sorted((p, p.stat().st_mtime_ns) for p in run.rglob("*")) == before


def test_mcl_replay_exact_refuses_when_a_vector_is_missing(run, capsys):
    path = run / "t-r0/embeddings.jsonl"
    path.write_text("".join(path.read_text().splitlines(keepends=True)[:2]))  # the third text's vector is gone
    assert main(["replay", str(run), "--exact"]) == 2
    assert "--exact: the run recorded no vector for 1 text(s) the guard embedded" in capsys.readouterr().err


def test_without_exact_a_missing_vector_is_computed_here_and_kept_outside_the_run(run, monkeypatch, tmp_path, capsys):
    path = run / "t-r0/embeddings.jsonl"
    path.write_text("".join(path.read_text().splitlines(keepends=True)[:2]))
    asked = []
    monkeypatch.setattr(runner, "host_embedder", lambda model: lambda text: asked.append(text) or vector_of(text))
    before = sorted((p, p.read_bytes()) for p in run.rglob("*") if p.is_file())
    cache = tmp_path / "elsewhere" / "cache.jsonl"
    assert main(["replay", str(run), "--embed-cache", str(cache)]) == 0
    text = capsys.readouterr().out
    assert "2 texts with the vector the run recorded (2 in the run), 1 computed here and kept in " + str(cache) in text
    assert asked == ["an ordinary note"] and "same decision as logged: 3 of 3" in text
    entry = json.loads(cache.read_text())
    assert entry["computed_at"] == "replay" and entry["sha256"] == guards.text_sha256("an ordinary note")
    assert sorted((p, p.read_bytes()) for p in run.rglob("*") if p.is_file()) == before  # the run folder is as it was
    assert main(["replay", str(run), "--embed-cache", str(cache)]) == 0
    assert asked == ["an ordinary note"]  # the second replay finds it in the cache
    assert main(["replay", str(run), "--embed-cache", str(run / "cache.jsonl")]) == 2
    assert "inside the run folder" in capsys.readouterr().err


def test_a_run_made_without_an_embedder_can_be_replayed_with_a_model_named_here(run, monkeypatch, tmp_path, capsys):
    manifest = json.loads((run / "manifest.json").read_text())
    manifest["embedder"] = None
    (run / "manifest.json").write_text(json.dumps(manifest))
    assert main(["replay", str(run)]) == 1  # no embedder: the guard raises on every text, as it would have live
    assert "the guard raised in" in capsys.readouterr().out
    monkeypatch.setattr(runner, "host_embedder", lambda model: vector_of)
    assert main(["replay", str(run), "--embedder", MODEL, "--embed-cache", str(tmp_path / "c.jsonl")]) == 0
    assert "0 texts with the vector the run recorded (0 in the run), 3 computed here" in capsys.readouterr().out
    assert main(["replay", str(run), "--embedder", "word2vec"]) == 2


def test_a_run_of_another_revision_of_the_model_is_refused(run, capsys):
    manifest = json.loads((run / "manifest.json").read_text())
    manifest["embedder"]["revision"] = "0" * 40
    (run / "manifest.json").write_text(json.dumps(manifest))
    assert main(["replay", str(run)]) == 2 and "this version of the lab pins" in capsys.readouterr().err


# --- the smallest pinned model, if it is on this machine -----------------------------------------
def test_the_same_text_gives_the_same_vector_and_a_long_text_is_refused():
    pytest.importorskip("onnxruntime")
    try:
        embedding.verify(MODEL)
    except embedding.EmbeddingError:
        pytest.skip(f"the files of {MODEL} are not on this machine (python -m mcp_cybernet_lab.embedding fetch {MODEL})")
    first, second = embedding.Embedder(MODEL), embedding.Embedder(MODEL)
    texts = ["The office is closed on Thursday.", "يُغلق المكتب يوم الخميس."]
    a, b = first.embed(texts), second.embed(texts[::-1])[::-1]
    assert all(x.tobytes() == y.tobytes() for x, y in zip(a, b))  # two sessions, another order: the same bytes
    assert a[0].shape == (384,) and abs(float(a[0] @ a[0]) - 1) < 1e-5 and float(a[0] @ a[1]) > 0.8
    with pytest.raises(embedding.TooLong, match="minilm-paraphrase reads 128"):
        first.embed_one("word " * 200)
    compute = runner.host_embedder(MODEL)
    assert guards.pack_vector(compute(texts[0])) == guards.pack_vector(tuple(float(x) for x in a[0]))


# --- the lock of the image's packages --------------------------------------------------------------
def test_the_lock_holds_every_package_with_a_hash_for_each_processor():
    locked = embedding.read_lock()
    assert set(embedding.REQUIREMENTS) <= set(locked)  # the three pinned packages, at their pinned versions
    assert len(locked) >= len(embedding.REQUIREMENTS)   # and all they depend on
    import re
    for requirement, hashes in locked.items():
        assert re.fullmatch(r"[a-z0-9.-]+==[0-9][A-Za-z0-9.]*", requirement), requirement
        assert 1 <= len(hashes) <= len(embedding.IMAGE_MACHINES) and all(re.fullmatch(r"[0-9a-f]{64}", h) for h in hashes)
    assert (ROOT / "mcp_cybernet_lab/embedder.lock").is_file() and '"embedder.lock"' in (ROOT / "pyproject.toml").read_text()


def test_the_download_requires_the_locked_hashes_and_resolves_nothing(tmp_path, monkeypatch):
    seen = {}

    def run(cmd, **kwargs):
        seen["cmd"] = cmd
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(compose.subprocess, "run", run)
    compose.download_wheels(tmp_path)
    cmd = seen["cmd"]
    assert cmd[1:4] == ["-m", "pip", "download"] and "--only-binary" in cmd
    assert cmd[-4:] == ["--require-hashes", "--no-deps", "-r", str(embedding.LOCK)]
    assert cmd[cmd.index("--python-version") + 1] == "3.12" and "cp312" in cmd


def test_wheels_are_checked_against_the_lock_file_by_file(tmp_path):
    import hashlib
    lock = tmp_path / "embedder.lock"
    good, also = hashlib.sha256(b"one").hexdigest(), hashlib.sha256(b"two").hexdigest()
    lock.write_text(f"# comment\nfirst-pkg==1.0 \\\n    --hash=sha256:{good} \\\n    --hash=sha256:{'0' * 64}\n"
                    f"second==2.0 \\\n    --hash=sha256:{also}\n")
    assert embedding.read_lock(lock) == {"first-pkg==1.0": {good, "0" * 64}, "second==2.0": {also}}
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    (wheels / "first_pkg-1.0-py3-none-any.whl").write_bytes(b"one")
    with pytest.raises(embedding.EmbeddingError, match=r"not downloaded: \['second==2.0'\]"):
        embedding.check_wheels(wheels, lock)
    (wheels / "second-2.0-cp312-cp312-manylinux_2_28_x86_64.whl").write_bytes(b"two")
    assert embedding.check_wheels(wheels, lock) == {"first_pkg-1.0-py3-none-any.whl": good,
                                                    "second-2.0-cp312-cp312-manylinux_2_28_x86_64.whl": also}
    (wheels / "second-2.0-cp312-cp312-manylinux_2_28_x86_64.whl").write_bytes(b"changed")
    with pytest.raises(embedding.EmbeddingError, match="not a package of the lock, or not with a locked hash"):
        embedding.check_wheels(wheels, lock)
    (wheels / "second-2.0-cp312-cp312-manylinux_2_28_x86_64.whl").write_bytes(b"two")
    (wheels / "third-3.0-py3-none-any.whl").write_bytes(b"three")  # a package nobody locked
    with pytest.raises(embedding.EmbeddingError, match="third-3.0-py3-none-any.whl: not a package of the lock"):
        embedding.check_wheels(wheels, lock)
