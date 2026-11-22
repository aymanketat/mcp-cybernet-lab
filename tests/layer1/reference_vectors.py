"""Vectors from each model's reference implementation (sentence-transformers), for the pipeline check.

Runs in its own environment, with torch and sentence-transformers; it imports nothing of the lab.

    python reference_vectors.py TEXTS.json REFERENCE.json

TEXTS.json is written by `calibrate_embedders.py texts`: the models (repository, revision, input
prefix) and the check texts. One text is encoded at a time, on the CPU, as the lab's module does.
"""
import json
import sys

from sentence_transformers import SentenceTransformer

job = json.load(open(sys.argv[1], encoding="utf-8"))
out = {"versions": {}, "vectors": {}, "tokens": {}, "max_seq_length": {}}
import sentence_transformers
import torch

out["versions"] = {"torch": torch.__version__, "sentence_transformers": sentence_transformers.__version__}
torch.set_num_threads(1)
for name, spec in job["models"].items():
    model = SentenceTransformer(spec["repo"], revision=spec["revision"], device="cpu")
    out["max_seq_length"][name] = model.max_seq_length
    texts = [spec["prefix"] + t for t in job["texts"]]
    out["tokens"][name] = [len(model.tokenizer(t)["input_ids"]) for t in texts]
    out["vectors"][name] = [model.encode([t], batch_size=1, normalize_embeddings=True)[0].tolist() for t in texts]
    print(name, "done", flush=True)
    del model
json.dump(out, open(sys.argv[2], "w", encoding="utf-8"))
