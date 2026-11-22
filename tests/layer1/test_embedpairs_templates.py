"""The wording for the embedding calibration (embedpairs.py): every template can be filled, states
the facts it says it states, and the reading sheet in the repository is the one the code writes."""

from __future__ import annotations

import re
from pathlib import Path

import embedpairs as ep
import pytest

from mcp_cybernet_lab import seeding

RNG = seeding.stream(20261122, "embedpairs", "test")
FACTS = [seeding.fact_record(RNG) for _ in range(30)]
PEOPLE = [seeding.arabic_record(RNG) for _ in range(30)]
ALL_FACT = {**ep.FACT_SOURCES, **ep.FACT_SINKS}
ALL_PERSON = {**ep.PERSON_SOURCES, **ep.PERSON_SINKS}


@pytest.mark.parametrize("key", ALL_FACT)
@pytest.mark.parametrize("arabic_digits", [False, True])
def test_a_fact_template_states_the_facts_it_names_and_no_other(key, arabic_digits):
    language, states, _ = ALL_FACT[key]
    for record in FACTS:
        text = ep.render(ALL_FACT[key], record, arabic_digits)
        fields = ep.fact_fields(record, language, arabic_digits)
        for fact, value in fields.items():
            assert (value in text) == (fact in states.split()), (key, fact)
        assert "{" not in text


@pytest.mark.parametrize("key", ALL_PERSON)
@pytest.mark.parametrize("arabic_digits", [False, True])
def test_a_person_template_states_name_and_number(key, arabic_digits):
    language = ALL_PERSON[key][0]
    for record in PEOPLE:
        text = ep.render(ALL_PERSON[key], record, arabic_digits)
        fields = ep.person_fields(record, language, arabic_digits)
        assert fields["name"] in text and fields["nid"] in text and "{" not in text


def test_the_person_source_is_the_labs_own_record():
    for record in PEOPLE:
        assert ep.render(ep.PERSON_SOURCES["ps-ar"], record, arabic_digits=True) == record


def test_languages_and_scripts_are_what_the_template_says():
    for record in FACTS:
        for key, template in ep.FACT_SINKS.items():
            arabic = bool(re.search(r"[؀-ۿ]", ep.render(template, record).replace(record["ref"], "")))
            assert arabic == (template[0] == "ar"), key
        assert not re.search(r"[0-9]", ep.render(ep.FACT_SINKS["fa-close"], record, True).replace(record["ref"], ""))
    for key, (language, text) in ep.EASY_DECOYS.items():
        assert bool(re.search(r"[؀-ۿ]", text)) == (language == "ar"), key
        assert not re.search(r"\d", text), key  # ordinary traffic here carries no number at all


def test_every_name_has_its_latin_form():
    assert len(ep.EN_FIRST) == len(seeding.AR_FIRST) and len(ep.EN_LAST) == len(seeding.AR_LAST)
    assert len(ep.AR_MONTHS) == len(ep.EN_MONTHS) == 12


def test_the_reading_sheet_in_the_repository_is_current():
    sheet = Path(ep.__file__).with_name("embedpairs-reading-sheet.md")
    assert sheet.read_text(encoding="utf-8") == ep.reading_sheet()
