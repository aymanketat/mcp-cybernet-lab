"""The embedding module without any model: pinning, refusal of other files, and the calibration's
pair set and measures. What needs the models themselves is tests/layer1/calibrate_embedders.py."""

from __future__ import annotations

import hashlib
import io
import json
import re
import struct
import sys
from pathlib import Path

import pytest

from mcp_cybernet_lab import embedding

sys.path.insert(0, str(Path(__file__).resolve().parent / "layer1"))
import embedpairs  # noqa: E402


def test_every_model_is_pinned_by_revision_and_by_the_hash_of_every_file():
    assert set(embedding.MODELS) == {"e5-small", "labse", "bge-m3", "minilm-paraphrase"}
    for name, m in embedding.MODELS.items():
        assert re.fullmatch(r"[0-9a-f]{40}", m["revision"]), name
        assert {"onnx/model.onnx", "tokenizer.json"} <= set(m["files"]), name
        assert all(re.fullmatch(r"[0-9a-f]{64}", h or "") for h in m["files"].values()), name
        assert m["pooling"] in ("mean", "cls") and m["max_tokens"] > 0 and m["dim"] > 0
        assert m["dense"] is None or m["dense"] in m["files"]


@pytest.fixture
def tiny(monkeypatch, tmp_path):
    """A model of two small files, pinned by their hashes, in an empty cache."""
    files = {"onnx/model.onnx": b"weights", "tokenizer.json": b"{}"}
    spec = {"repo": "someone/tiny", "revision": "a" * 40, "pooling": "mean", "dense": None, "prefix": "",
            "max_tokens": 8, "dim": 2, "files": {p: hashlib.sha256(b).hexdigest() for p, b in files.items()}}
    monkeypatch.setitem(embedding.MODELS, "tiny", spec)
    served = dict(files)
    asked = []

    class Response(io.BytesIO):
        status = 200

    def urlopen(request, timeout=None):
        asked.append(request.full_url)
        data = served[request.full_url.split("/resolve/" + "a" * 40 + "/")[1]]
        start = int(request.headers.get("Range", "bytes=0-")[6:-1])
        response = Response(data[start:])
        response.status = 206 if start else 200
        breaks.append(None)
        if len(breaks) == break_at.get("call"):  # the line drops after three bytes
            response = Response(data[start:start + 3])
            response.status = 206 if start else 200
            real_read = response.read

            def read(n=-1):
                chunk = real_read(n)
                if not chunk:
                    raise TimeoutError("The read operation timed out")
                return chunk

            response.read = read
        return response

    breaks, break_at = [], {}

    monkeypatch.setattr(embedding.urllib.request, "urlopen", urlopen)
    return {"root": tmp_path, "served": served, "asked": asked, "break_at": break_at}


def test_fetch_takes_the_pinned_revision_and_verifies(tiny):
    folder = embedding.fetch("tiny", tiny["root"], log=lambda *_: None)
    assert folder == tiny["root"] / "tiny" / ("a" * 40)
    assert sorted(tiny["asked"]) == [f"https://huggingface.co/someone/tiny/resolve/{'a' * 40}/{p}"
                                     for p in ("onnx/model.onnx", "tokenizer.json")]
    assert embedding.verify("tiny", tiny["root"]) == embedding.MODELS["tiny"]["files"]
    tiny["asked"].clear()
    embedding.fetch("tiny", tiny["root"], log=lambda *_: None)
    assert tiny["asked"] == []  # nothing is fetched twice


def test_a_download_that_breaks_off_goes_on_from_where_it_stopped(tiny):
    tiny["break_at"]["call"] = 1  # the first request delivers three bytes, then times out
    embedding.fetch("tiny", tiny["root"], log=lambda *_: None)
    assert embedding.verify("tiny", tiny["root"]) == embedding.MODELS["tiny"]["files"]
    assert len(tiny["asked"]) == 3  # the broken file was asked for twice, the second time for the rest


def test_a_file_that_is_not_the_pinned_one_is_refused_and_not_kept(tiny):
    tiny["served"]["onnx/model.onnx"] = b"other weights"
    with pytest.raises(embedding.EmbeddingError, match="onnx/model.onnx .* has SHA-256 .* pinned is .*nothing was kept"):
        embedding.fetch("tiny", tiny["root"], log=lambda *_: None)
    assert not list(tiny["root"].rglob("model.onnx*"))


def test_verify_refuses_a_changed_or_missing_file(tiny):
    folder = embedding.fetch("tiny", tiny["root"], log=lambda *_: None)
    (folder / "tokenizer.json").write_bytes(b"{ }")
    with pytest.raises(embedding.EmbeddingError, match="tokenizer.json has SHA-256"):
        embedding.verify("tiny", tiny["root"])
    (folder / "tokenizer.json").unlink()
    with pytest.raises(embedding.EmbeddingError, match="is missing"):
        embedding.verify("tiny", tiny["root"])
    with pytest.raises(embedding.EmbeddingError, match="not one of"):
        embedding.verify("unknown", tiny["root"])


def test_safetensors_are_read_without_torch(tmp_path):
    np = pytest.importorskip("numpy")
    weight, bias = np.arange(6, dtype="<f4").reshape(2, 3), np.array([0.5, -0.5], dtype="<f4")
    header = json.dumps({"__metadata__": {"format": "pt"},
                         "linear.weight": {"dtype": "F32", "shape": [2, 3], "data_offsets": [0, 24]},
                         "linear.bias": {"dtype": "F32", "shape": [2], "data_offsets": [24, 32]}}).encode()
    f = tmp_path / "model.safetensors"
    f.write_bytes(struct.pack("<Q", len(header)) + header + weight.tobytes() + bias.tobytes())
    tensors = embedding.read_safetensors(f)
    assert (tensors["linear.weight"] == weight).all() and (tensors["linear.bias"] == bias).all()


def test_the_module_is_not_one_of_the_standard_library_images_modules():
    from mcp_cybernet_lab import compose
    assert "embedding.py" not in compose.CONTAINER_MODULES


# --- the pair set and the measures ---------------------------------------------------------------
def test_the_pair_set_is_fixed_by_its_seed():
    a, b = embedpairs.pair_set(), embedpairs.pair_set()
    assert a == b and embedpairs.pair_set(seed=1)["texts"] != a["texts"]
    assert len(a["texts"]) == 1900 and len(a["pairs"]) == 60400


def test_the_pair_set_has_enough_decoy_comparisons_of_each_class():
    import collections
    pairs = embedpairs.pair_set()["pairs"]
    count = collections.Counter((p["kind"], p["class"]) for p in pairs)
    assert count == {("fact", "positive"): 3400, ("fact", "hard"): 3400, ("fact", "easy"): 40000,
                     ("person", "positive"): 600, ("person", "hard"): 3000, ("person", "easy"): 10000}
    assert all(count[(kind, decoy)] >= 3000 for kind in ("fact", "person") for decoy in ("easy", "hard"))
    easy = {p["sink"] for p in pairs if p["class"] == "easy"}
    assert len(easy) == 200  # each source meets two hundred different ordinary lines


def test_a_positive_states_the_record_of_its_source_and_a_hard_decoy_another_record():
    ps = embedpairs.pair_set()
    for p in ps["pairs"]:
        same_record = p["source"].split("/")[0] == p["sink"].split("/")[0]
        assert same_record == (p["class"] == "positive")
        if p["class"] == "hard":
            assert p["sink"].split("/")[0][:-2] == p["source"].split("/")[0][:-2]  # the same kind of record
            assert ps["texts"][p["sink"]] != ps["texts"][p["source"]]
    companies = {t.split(":")[1].split(" will be paid")[0] for i, t in ps["texts"].items() if i.endswith("/fs-en/ascii")}
    assert len(companies) == 50  # fifty different companies


def test_thresholds_and_recall():
    sys.modules.pop("calibrate_embedders", None)
    import calibrate_embedders as c
    decoys = [i / 10000 for i in range(5000)]            # 0.0000 ... 0.4999
    zero, rate = c.thresholds(decoys)
    assert zero == 0.4999 and rate == 0.4994             # five of 5,000 decoys (0.1 %) lie above the second
    assert sum(d > rate for d in decoys) == 5 and sum(d > zero for d in decoys) == 0
    units = [f"line{i % 200}" for i in range(5000)]      # 5,000 comparisons reuse 200 decoy texts
    m = c.measure([0.3, 0.49945, 0.6, 0.9], decoys, units)
    assert (m["recall_at_zero"], m["recall_at_rate"]) == (0.5, 0.75)
    assert round(m["margin"], 4) == round(0.3 - 0.4999, 4)
    # the bound counts the distinct texts, not the comparisons: 3/200, not 3/5000
    assert (m["decoy_units"], m["bound_units_at_zero"]) == (200, 3 / 200)
    # a text matches if any of its comparisons does: none above every decoy, five above the 0.1 % threshold
    assert (m["units_matching_at_zero"], m["units_matching_at_rate"]) == (0, 5)


def test_the_check_texts_are_twenty_texts_of_the_pair_set_in_both_languages():
    import calibrate_embedders as c
    texts = c.check_texts()
    assert len(texts) == len(set(texts)) == 20
    arabic = sum(bool(re.search("[ء-ي]", t)) for t in texts)
    assert 8 <= arabic <= 14
