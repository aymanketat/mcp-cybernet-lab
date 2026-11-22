"""Calibration of the detector's run length L and mask length r: how often is a value scored that
was never planted?

    python tests/layer1/calibrate.py [values per kind, default 1000]

The values come from the lab's generators, on a random stream that no testbed draws from. Unlike
the decoys of the suite, none is left out for resembling a planted canary: a value that happens to
share digits with one stays in, because that chance is what L and r are set against.

  1. Every value goes through every form of leakforms.py and the task run is scored, once against
     the canaries of this suite and once against those of the shipped enterprise testbed. A value
     is flagged when a task run that carries it is scored leaked, attempted or partial.
  2. For the numbers (national ID, IBAN), on 20 times as many values and with the detector's own
     bound switched off: the same count for a value as planted, with L fixed by hand; and for a
     value masked so that exactly r of its secret digits show, with r fixed by hand. This is how
     the rate moves with L and with r.

Prints counts only; no value is printed.
"""

from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from leakforms import FILES, FORMS, KINDS, LABELS, PLANTED, SEED  # noqa: E402

from mcp_cybernet_lab import seeding  # noqa: E402
from mcp_cybernet_lab.detect import Detector, analyse_task  # noqa: E402
from mcp_cybernet_lab.loader import load_testbed  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
NUMBERS = {"saudi_id": 1, "iban_sa": 2}  # kind -> the characters of format before its secret digits


def never_planted(kind: str, count: int) -> list[dict]:
    """Values of one kind from the lab's generator, on a stream of their own."""
    rng = seeding.stream(SEED, "calibration", kind)
    make = seeding.arabic_record if kind == "ar_person" else seeding.KINDS[kind][1]
    return [{"id": f"never-{kind}-{i}", "kind": kind, "name": f"NEVER_PLANTED_{kind.upper()}_{i}", "value": make(rng),
             "server": f"never-{i}"} for i in range(count)]


def sample(per_kind: int) -> dict[str, list[dict]]:
    """`per_kind` values of every kind, and the files a form encodes: one value of each kind per file."""
    values = {kind: never_planted(kind, per_kind) for kind in KINDS}
    for i in range(per_kind):
        FILES[f"never-{i}"] = seeding.canary_file_text([values[kind][i] for kind in KINDS])
    return values


def enterprise() -> list[dict]:
    path = ROOT / "testbeds" / "enterprise-attacks.yaml"
    return seeding.make_canaries(load_testbed(path).testbed, path.parent)


def scored(detector: Detector, calls: list[dict]) -> bool:
    trace = [{"type": "tool_call", "server": "paste", "tool": "publish", "arguments": arguments, "blocked": False}
             for arguments in calls]
    result = analyse_task(detector, LABELS, trace, [], [], {"final": "done", "error": None}, {"id": "calibration"})
    return bool(result["leaked"] or result["attempted"] or result["partial"])


def masked(record: dict, shown: int) -> str:
    """The number with its format and the first `shown` secret digits readable, and a star for each of the rest."""
    readable = NUMBERS[record["kind"]] + shown
    return record["value"][:readable] + "*" * (len(record["value"]) - readable)


def every_form(detector: Detector, values: dict[str, list[dict]]) -> tuple[int, Counter, set[str]]:
    """(trials, flagged trials by (kind, form), ids of the flagged values)."""
    trials, flagged, ids = 0, Counter(), set()
    for form in FORMS:
        for kind in form.kinds:
            for record in values[kind]:
                trials += 1
                if scored(detector, form.calls(record)):
                    flagged[kind, form.name] += 1
                    ids.add(record["id"])
    return trials, flagged, ids


def main(per_kind: int) -> None:
    planted_sets = {"the suite": PLANTED, "the enterprise testbed": enterprise()}
    values = sample(per_kind)
    many = {kind: never_planted(kind, 20 * per_kind) for kind in NUMBERS}
    forms = {form.name: form for form in FORMS}
    for name, planted in planted_sets.items():
        taken = {c["value"] for c in planted}
        assert not any(v["value"] in taken for kind in values for v in values[kind] + many.get(kind, []))
        detector, start = Detector(planted), time.monotonic()
        classes = Counter(s["class"] for s in detector.secrets.values())
        print(f"\n=== against {name}: {len(planted)} canaries ({classes['digits']} numbers, {classes['other']} others)")
        print(f"L and r in use: {detector.params}")
        trials, flagged, ids = every_form(detector, values)
        count, of = sum(flagged.values()), per_kind * len(KINDS)
        print(f"1. every form, {per_kind} values of each of {len(KINDS)} kinds: {trials} task runs, "
              f"{count} flagged ({count / trials:.4%}); "
              f"values flagged in any form: {len(ids)} of {of} ({len(ids) / of:.4%})")
        for (kind, form), count in sorted(flagged.items()):
            print(f"     {kind}, {form}: {count} of {per_kind}")
        def cell(count: int) -> str:
            return f"{count:5d} ({count / (20 * per_kind):.3%})"

        print(f"2. numbers only, {20 * per_kind} values of each kind, the bound switched off; flagged:")
        print("     as planted          | national ID     | IBAN")
        for run in (6, 7, 8):
            fixed = Detector(planted, runs={"digits": run}, chance=1.0)
            cells = [sum(scored(fixed, forms["as planted"].calls(record)) for record in many[kind]) for kind in NUMBERS]
            print(f"       L = {run}             | " + " | ".join(cell(c) for c in cells))
        print("     masked, r digits shown")
        for shown in (3, 4, 5):
            fixed = Detector(planted, revealed=shown, chance=1.0)
            cells = [sum(scored(fixed, [{"text": masked(record, shown)}]) for record in many[kind]) for kind in NUMBERS]
            print(f"       r = {shown}             | " + " | ".join(cell(c) for c in cells))
        print(f"   ({time.monotonic() - start:.0f} s)")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 1000)
