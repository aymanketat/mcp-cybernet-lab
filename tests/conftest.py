"""Settings for every test.

A build writes 3,000 decoys by default, which takes over a second. The tests build testbeds many
times, so they ask for a few decoys instead; a test marked `full_decoys` builds with the number a
testbed sets.
"""

from __future__ import annotations

import pytest

from mcp_cybernet_lab import compose, seeding

FEW = 60


@pytest.fixture(autouse=True)
def few_decoys(request, monkeypatch):
    if "full_decoys" in request.keywords:
        return
    full = seeding.make_decoys

    def few(tb, canaries):
        fewer = tb.canaries.model_copy(update={"decoys": min(tb.canaries.decoys, FEW)})
        return full(tb.model_copy(update={"canaries": fewer}), canaries)

    monkeypatch.setattr(compose, "make_decoys", few)


@pytest.fixture(autouse=True)
def no_wheel_downloads(request, monkeypatch):
    """A server with a lock has its wheels downloaded by every build. The tests build many times and
    must run without a network, so they skip the download and the wheel build; a test marked
    `full_wheels` does both."""
    if "full_wheels" in request.keywords:
        return

    def listed(tb_dir, s, lock, out):
        from mcp_cybernet_lab import embedding
        (out / "context" / "wheels" / s.id).mkdir(parents=True, exist_ok=True)
        return {"lock_sha256": embedding.file_sha256(lock), "wheels": {}}

    monkeypatch.setattr(compose, "server_wheels", listed)



@pytest.fixture(scope="session")
def v2(tmp_path_factory):
    """enterprise-v2 as `mcl build` writes it, once for all tests: with the few decoys the tests ask
    for and without the download of wheels. {"out", "canaries", "decoys", "manifest", "labels", "sheet"}."""
    import json
    from pathlib import Path

    from mcp_cybernet_lab import embedding
    from mcp_cybernet_lab.loader import load_testbed

    def few(tb, canaries):
        fewer = tb.canaries.model_copy(update={"decoys": FEW})
        return seeding.make_decoys(tb.model_copy(update={"canaries": fewer}), canaries)

    def listed(tb_dir, s, lock, out):
        (out / "context" / "wheels" / s.id).mkdir(parents=True, exist_ok=True)
        return {"lock_sha256": embedding.file_sha256(lock), "wheels": {}}

    kept = compose.make_decoys, compose.server_wheels
    compose.make_decoys, compose.server_wheels = few, listed
    try:
        root = Path(__file__).resolve().parent.parent
        out = compose.build(load_testbed(root / "testbeds" / "enterprise-v2.yaml"), tmp_path_factory.mktemp("v2") / "out")
    finally:
        compose.make_decoys, compose.server_wheels = kept
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    return {"out": out, "manifest": manifest, "labels": {s["id"]: s["label"] for s in manifest["servers"]},
            "canaries": json.loads((out / "canaries.json").read_text(encoding="utf-8")),
            "decoys": json.loads((out / "decoys.json").read_text(encoding="utf-8")),
            "sheet": (out / "data" / "partners" / "public-reference.md").read_text(encoding="utf-8")}


def pytest_configure(config):
    config.addinivalue_line("markers", "full_decoys: build with the number of decoys the testbed sets")
    config.addinivalue_line("markers", "full_wheels: download and build the wheels of locked servers (needs a network)")
