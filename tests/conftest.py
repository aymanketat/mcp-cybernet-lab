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


def pytest_configure(config):
    config.addinivalue_line("markers", "full_decoys: build with the number of decoys the testbed sets")
    config.addinivalue_line("markers", "full_wheels: download and build the wheels of locked servers (needs a network)")
