"""Calibration of the candidate embedding models on English-Arabic pairs (work package 3, item 3b).

It produces a table for a decision that is not taken here: no model is chosen, and none goes into
a testbed. The pair set is embedpairs.pair_set(); the measures are below and in the lab note, and
both were committed before any score was computed.

    python tests/layer1/calibrate_embedders.py texts TEXTS.json
        write the check texts and the pinned models, for reference_vectors.py (its own environment)
    python tests/layer1/calibrate_embedders.py check REFERENCE.json
        the pipeline check: each model's vectors against its reference implementation, cosine >= 0.999
    python tests/layer1/calibrate_embedders.py run OUT_DIR [--held-out RUN_DIR ...] [--models M ...]
        embed the pair set with each model and write OUT_DIR/results.json and OUT_DIR/table.md

Measures, per model, with score = cosine of the two vectors of a comparison:
  - recall at zero decoy matches: the share of positives scoring above every decoy;
  - recall at 0.1 %: the share of positives above the threshold that at most 0.1 % of decoys exceed;
  - the score gap: median positive minus median decoy, and lowest positive minus highest decoy;
  each against easy decoys and against hard decoys separately, for fact records and person records
  separately; recall against easy decoys also by language direction and by what the sink did.
  Comparisons that reuse the same texts are not independent trials, so a bound counts distinct
  decoy texts: with none of n distinct texts matching, the rate for new text of that sort is below
  about 3/n (95 %): 1.5 % for the 200 ordinary lines, 6 % for the 50 records of a kind. Decoys are
  therefore also counted per unit: an ordinary line, or a decoy record, matches if it exceeds the
  threshold against any source.
  - milliseconds per text (median, one thread, this machine), the token limit, texts refused as
    too long, and the size of the pinned files.
  - held out, numbers only: the real translations in stored runs (--held-out), text against its
    translation, scored against the easy-decoy thresholds of fact records and of person records
    (they translate a name and a national ID). Not used for anything else.
  - the same text embedded in two fresh processes gives the same bytes.
"""

from __future__ import annotations

import collections
import hashlib
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import embedpairs  # noqa: E402

from mcp_cybernet_lab import embedding  # noqa: E402

CHECK = 0.999  # the least cosine with the reference implementation
RATE = 0.001   # "at 0.1 %"


def check_texts() -> list[str]:
    """Twenty texts of the pair set: both languages, both digit scripts, short and long."""
    texts = embedpairs.pair_set()["texts"]
    ids = ["fact00/fs-en/ascii", "fact00/fs-en-fields/ascii", "fact00/fs-ar/ascii", "fact00/fs-ar/ar",
           "fact01/fa-close/ascii", "fact01/fa-reordered/arabic-indic", "fact02/fa-note/ascii",
           "fact03/fa-three-facts/arabic-indic", "fact04/fe-close/ascii", "fact05/fe-note-translit/ascii",
           "fact06/fe-three-facts/ascii", "fact07/fe-reordered/ascii", "person00/ps-ar/ar",
           "person01/pa-sentence/ascii", "person02/pa-fields/arabic-indic", "person03/pe-short/ascii",
           "person04/pe-sentence/ascii", "ordinary/ea-010", "ordinary/ee-047", "ordinary/ea-084"]
    return [texts[i] for i in ids]


def cosine(a, b) -> float:
    return float(a @ b)  # both of unit length


def cmd_texts(out: Path) -> int:
    models = {n: {"repo": m["repo"], "revision": m["revision"], "prefix": m["prefix"]}
              for n, m in embedding.MODELS.items()}
    out.write_text(json.dumps({"models": models, "texts": check_texts()}, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {out}: {len(check_texts())} texts, {len(models)} models")
    return 0


def cmd_check(reference: Path, names: list[str]) -> int:
    import numpy as np
    ref, texts, failed = json.loads(reference.read_text(encoding="utf-8")), check_texts(), 0
    print(f"reference: {ref['versions']}")
    for name in names:
        e = embedding.Embedder(name)
        scores, skipped, tokens_differ = [], 0, 0
        for text, vector, tokens in zip(texts, ref["vectors"][name], ref["tokens"][name]):
            tokens_differ += e.token_count(text) != tokens
            try:
                scores.append(cosine(e.embed_one(text), np.array(vector, dtype=np.float32)))
            except embedding.TooLong:
                skipped += 1
        ok = min(scores) >= CHECK and not tokens_differ
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name}: lowest cosine with the reference {min(scores):.6f} over "
              f"{len(scores)} texts (median {statistics.median(scores):.6f}); token counts differing {tokens_differ}; "
              f"refused as too long {skipped}; reference reads {ref['max_seq_length'][name]} tokens, "
              f"here {embedding.MODELS[name]['max_tokens']}")
    return 1 if failed else 0


def thresholds(decoys: list[float]) -> tuple[float, float]:
    """(above every decoy, above all but 0.1 % of the decoys)"""
    ranked = sorted(decoys, reverse=True)
    return ranked[0], ranked[int(RATE * len(ranked))]


def measure(positives: list[float], decoys: list[float], units: list[str]) -> dict:
    """units: for each decoy comparison, the decoy text it reuses (an ordinary line) or the record
    its text was filled with. A unit matches if any of its comparisons exceeds the threshold."""
    zero, rate = thresholds(decoys)
    best: dict[str, float] = {}
    for score, unit in zip(decoys, units):
        best[unit] = max(score, best.get(unit, -1.0))
    return {"positives": len(positives), "decoys": len(decoys), "decoy_units": len(best),
            "units_matching_at_zero": sum(s > zero for s in best.values()),
            "units_matching_at_rate": sum(s > rate for s in best.values()),
            "bound_units_at_zero": 3 / len(best),
            "recall_at_zero": sum(s > zero for s in positives) / len(positives),
            "recall_at_rate": sum(s > rate for s in positives) / len(positives),
            "threshold_zero": zero, "threshold_rate": rate,
            "median_positive": statistics.median(positives), "median_decoy": statistics.median(decoys),
            "gap_medians": statistics.median(positives) - statistics.median(decoys),
            "margin": min(positives) - zero}


def held_out(run_dirs: list[Path]) -> list[tuple[str, str]]:
    """(text given to the translator, its translation) for every translator call of stored runs."""
    out = []
    for run in run_dirs:
        for trace in sorted(run.glob("*/trace.jsonl")):
            for line in trace.read_text(encoding="utf-8").splitlines():
                e = json.loads(line)
                if (e.get("type") == "tool_call" and e.get("tool") == "translate" and not e.get("is_error")
                        and not e.get("blocked") and e.get("result_text") and (e.get("arguments") or {}).get("text")):
                    out.append((e["arguments"]["text"], e["result_text"]))
    return out


def same_bytes_in_two_processes(name: str) -> bool:
    code = ("import sys, hashlib; sys.path.insert(0, sys.argv[1]); import calibrate_embedders as c; "
            "e = c.embedding.Embedder(sys.argv[2]); h = hashlib.sha256(); "
            "[h.update(e.embed_one(t).tobytes()) for t in c.check_texts() if e.token_count(t) <= e.spec['max_tokens']]; "
            "print(h.hexdigest())")
    runs = [subprocess.run([sys.executable, "-c", code, str(Path(__file__).resolve().parent), name],
                           capture_output=True, text=True).stdout.strip() for _ in range(2)]
    return bool(runs[0]) and runs[0] == runs[1]


def calibrate(name: str, pair_set: dict, translations: list[tuple[str, str]]) -> dict:
    e = embedding.Embedder(name)
    vectors, refused, times = {}, [], []
    for text_id, text in pair_set["texts"].items():
        t0 = time.perf_counter()
        try:
            vectors[text_id] = e.embed_one(text)
            times.append((time.perf_counter() - t0) * 1000)
        except embedding.TooLong:
            refused.append(text_id)
    scores = collections.defaultdict(list)   # (kind, class) -> scores
    units = collections.defaultdict(list)    # (kind, class) -> the decoy line or decoy record of each score
    detail = collections.defaultdict(list)   # (kind, "direction" | "family", value) -> positive scores
    dropped = collections.Counter()
    for p in pair_set["pairs"]:
        if p["source"] not in vectors or p["sink"] not in vectors:
            dropped[p["class"]] += 1
            continue
        s = cosine(vectors[p["source"]], vectors[p["sink"]])
        scores[(p["kind"], p["class"])].append(s)
        units[(p["kind"], p["class"])].append(p["sink"] if p["class"] == "easy" else p["sink"].split("/")[0])
        if p["class"] == "positive":
            detail[(p["kind"], "direction", f"{p['source_lang']} to {p['sink_lang']}")].append(s)
            detail[(p["kind"], "family", p["family"])].append(s)
            detail[(p["kind"], "digits", p["digits"])].append(s)
    result = {"model": name, "repo": e.spec["repo"], "revision": e.spec["revision"], "dim": e.spec["dim"],
              "token_limit": e.spec["max_tokens"], "texts": len(pair_set["texts"]), "refused_too_long": len(refused),
              "refused_by_template": dict(collections.Counter(t.split("/")[1] for t in refused)),
              "pairs_dropped": dict(dropped), "ms_per_text_median": statistics.median(times),
              "ms_per_text_p95": sorted(times)[int(0.95 * len(times))],
              "files_mb": sum((embedding.model_dir(name) / f).stat().st_size for f in e.spec["files"]) / 1e6,
              "same_bytes_in_two_processes": same_bytes_in_two_processes(name), "measures": {}, "detail": {}}
    for kind in ("fact", "person"):
        for decoy in ("easy", "hard"):
            result["measures"][f"{kind}/{decoy}"] = measure(scores[(kind, "positive")], scores[(kind, decoy)],
                                                             units[(kind, decoy)])
        zero, rate = thresholds(scores[(kind, "easy")])
        for (k, by, value), s in sorted(detail.items()):
            if k == kind:
                result["detail"][f"{kind}/{by}/{value}"] = {
                    "positives": len(s), "recall_at_zero": sum(x > zero for x in s) / len(s),
                    "recall_at_rate": sum(x > rate for x in s) / len(s), "median": statistics.median(s)}
    # Held out: real translations, scored against the easy-decoy thresholds of both kinds of record.
    real, too_long = [], 0
    for text, translation in translations:
        try:
            real.append(cosine(e.embed_one(text), e.embed_one(translation)))
        except embedding.TooLong:
            too_long += 1
    result["held_out"] = {"translations": len(translations), "refused_too_long": too_long}
    if real:
        result["held_out"] |= {"lowest": min(real), "median": statistics.median(real)}
        for kind in ("fact", "person"):
            zero, rate = thresholds(scores[(kind, "easy")])
            result["held_out"] |= {f"above_{kind}_threshold_zero": sum(s > zero for s in real) / len(real),
                                   f"above_{kind}_threshold_rate": sum(s > rate for s in real) / len(real)}
    return result


def table(results: list[dict], machine: str) -> str:
    def pct(x: float) -> str:
        return f"{100 * x:.1f} %"

    out = [f"Machine: {machine}. One thread, one text per inference.", ""]
    first = results[0]["measures"]
    for kind in ("fact", "person"):
        for decoy in ("easy", "hard"):
            m0 = first[f"{kind}/{decoy}"]
            out += [f"### {kind.capitalize()} records against {decoy} decoys "
                    f"({m0['positives']} positives, {m0['decoys']} decoy comparisons, before texts refused as too long)", "",
                    f"Distinct decoy {'lines' if decoy == 'easy' else 'records'}: {m0['decoy_units']}; none matching "
                    f"bounds the rate for new ones at about {100 * m0['bound_units_at_zero']:.1f} % (95 %).", "",
                    "| Model | Recall at zero decoy matches | Recall at 0.1 % of comparisons | "
                    f"Decoy {'lines' if decoy == 'easy' else 'records'} matching at that 0.1 % threshold | "
                    "Median positive | Median decoy | Highest decoy | Lowest positive minus highest decoy | "
                    "Decoy comparisons |", "|---|---|---|---|---|---|---|---|---|"]
            for r in results:
                m = r["measures"][f"{kind}/{decoy}"]
                out.append(f"| {r['model']} | {pct(m['recall_at_zero'])} | {pct(m['recall_at_rate'])} | "
                           f"{m['units_matching_at_rate']} of {m['decoy_units']} | "
                           f"{m['median_positive']:.3f} | {m['median_decoy']:.3f} | {m['threshold_zero']:.3f} | "
                           f"{m['margin']:+.3f} | {m['decoys']} |")
            out.append("")
    for kind in ("fact", "person"):
        keys = [k for k in results[0]["detail"] if k.startswith(kind + "/")]
        out += [f"### {kind.capitalize()} records: recall at zero easy-decoy matches, by direction, by what the sink did, by digits", "",
                "| Positives of | n | " + " | ".join(r["model"] for r in results) + " |",
                "|---|---|" + "---|" * len(results)]
        for k in keys:
            out.append(f"| {k.split('/', 1)[1].replace('/', ': ')} | {results[0]['detail'][k]['positives']} | "
                       + " | ".join(pct(r["detail"][k]["recall_at_zero"]) if k in r["detail"] else "none left"
                                    for r in results) + " |")
        out.append("")
    out += ["### Cost, limits and the held-out translations", "",
            "| Model | Dimensions | Token limit | Texts refused as too long | ms per text, median (95th percentile) | "
            "Pinned files | Same bytes in two processes |", "|---|---|---|---|---|---|---|"]
    for r in results:
        out.append(f"| {r['model']} | {r['dim']} | {r['token_limit']} | {r['refused_too_long']} of {r['texts']} | "
                   f"{r['ms_per_text_median']:.0f} ({r['ms_per_text_p95']:.0f}) | {r['files_mb']:.0f} MB | "
                   f"{'yes' if r['same_bytes_in_two_processes'] else 'NO'} |")
    if any("lowest" in r["held_out"] for r in results):
        out += ["", "### Held out: the real translations of stored runs (a text against its translation)", "",
                "Share scoring above the easy-decoy thresholds of each kind of record. Not used to set anything.", "",
                "| Model | Translations scored (refused as too long) | Above the fact threshold: zero matches | "
                "fact: 0.1 % | Above the person threshold: zero matches | person: 0.1 % | Lowest, median score |",
                "|---|---|---|---|---|---|---|"]
        for r in results:
            h = r["held_out"]
            if "lowest" in h:
                out.append(f"| {r['model']} | {h['translations'] - h['refused_too_long']} ({h['refused_too_long']}) | "
                           f"{pct(h['above_fact_threshold_zero'])} | {pct(h['above_fact_threshold_rate'])} | "
                           f"{pct(h['above_person_threshold_zero'])} | {pct(h['above_person_threshold_rate'])} | "
                           f"{h['lowest']:.3f}, {h['median']:.3f} |")
    return "\n".join(out) + "\n"


def cmd_run(out: Path, run_dirs: list[Path], names: list[str]) -> int:
    pair_set = embedpairs.pair_set()
    key = hashlib.sha256(json.dumps(pair_set, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    translations = held_out(run_dirs)
    cpu = next((l.split(":", 1)[1].strip() for l in Path("/proc/cpuinfo").read_text().splitlines()
                if l.startswith("model name")), "unknown CPU")
    out.mkdir(parents=True, exist_ok=True)
    results = []
    for name in names:
        t0 = time.time()
        results.append(calibrate(name, pair_set, translations))
        print(f"{name}: done in {time.time() - t0:.0f} s", flush=True)
        (out / "results.json").write_text(json.dumps(
            {"pair_set_sha256": key, "texts": len(pair_set["texts"]), "pairs": len(pair_set["pairs"]),
             "held_out_runs": [r.name for r in run_dirs], "machine": cpu, "results": results}, indent=1))
    (out / "table.md").write_text(table(results, cpu), encoding="utf-8")
    print(f"pair set {key[:16]}: {len(pair_set['texts'])} texts, {len(pair_set['pairs'])} comparisons; "
          f"wrote {out / 'results.json'} and {out / 'table.md'} (numbers only, no text)")
    return 0


def main(argv: list[str]) -> int:
    def option(flag: str) -> list[str]:
        if flag not in argv:
            return []
        rest = argv[argv.index(flag) + 1:]
        return rest[:next((i for i, a in enumerate(rest) if a.startswith("--")), len(rest))]

    names = option("--models") or list(embedding.MODELS)
    if argv[:1] == ["texts"] and len(argv) == 2:
        return cmd_texts(Path(argv[1]))
    if argv[:1] == ["check"] and len(argv) >= 2:
        return cmd_check(Path(argv[1]), names)
    if argv[:1] == ["run"] and len(argv) >= 2:
        return cmd_run(Path(argv[1]), [Path(p) for p in option("--held-out")], names)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
