"""The generator of fact records (seeding.fact_record): deterministic, well formed, in two languages."""

from __future__ import annotations

import re
from datetime import date

from mcp_cybernet_lab import seeding


def records(n: int, seed: int = 20261122) -> list[dict]:
    rng = seeding.stream(seed, "fact-record", "test")
    return [seeding.fact_record(rng) for _ in range(n)]


def test_the_same_seed_gives_the_same_records_and_another_seed_others():
    assert records(20) == records(20)
    assert records(20) != records(20, seed=1)


def test_a_record_has_five_well_formed_facts():
    for r in records(200):
        assert set(r) == {"ref", "entity", "amount", "date", "city"}
        assert re.fullmatch(r"CTR-[A-Z0-9]{4}-[0-9]{5}", r["ref"])
        assert 12_000 <= r["amount"] < 9_800_000 and r["amount"] % 25 == 0
        date.fromisoformat(r["date"])
        assert re.fullmatch(r"[A-Za-z]+ [A-Za-z]+ [A-Za-z]+", r["entity"]["en"])
        assert r["entity"]["ar"].startswith("شركة ") and not re.search(r"[A-Za-z0-9]", r["entity"]["ar"])
        assert (r["city"]["en"], r["city"]["ar"]) in seeding.FACT_CITIES


def test_the_two_forms_of_a_name_are_the_same_three_words():
    english = {name: i for words in (seeding.FACT_PLACES, seeding.FACT_EMBLEMS, seeding.FACT_TRADES)
               for i, (name, _) in enumerate(words)}
    for r in records(200):
        place, emblem, trade = r["entity"]["en"].split()
        assert r["entity"]["ar"] == (f"شركة {seeding.FACT_EMBLEMS[english[emblem]][1]} "
                                     f"{seeding.FACT_PLACES[english[place]][1]} {seeding.FACT_TRADES[english[trade]][1]}")


def test_the_generator_changes_no_planted_canary():
    """Fact records have their own stream: the canaries of a testbed are what they were."""
    from pathlib import Path

    from mcp_cybernet_lab.loader import load_testbed
    root = Path(__file__).resolve().parent.parent
    loaded = load_testbed(root / "testbeds/toy-network.yaml")
    before = seeding.make_canaries(loaded.testbed, loaded.path.parent)
    records(50)
    assert seeding.make_canaries(loaded.testbed, loaded.path.parent) == before
