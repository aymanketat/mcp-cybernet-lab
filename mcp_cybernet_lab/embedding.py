"""Sentence embeddings from pinned multilingual models, on the CPU, the same vector for the same text.

One module for every place that embeds: the embedder sidecar of a testbed, the Layer-1 calibration,
and a replay on the host that needs a vector no run recorded. It is not copied into the lab's
standard-library images (it needs numpy, onnxruntime and tokenizers: `pip install -e .[embed]`).

A model is pinned by its repository revision and the SHA-256 of every file it uses (MODELS). `fetch`
downloads those files once and refuses a file whose hash differs; `Embedder` checks the hashes
again before it loads anything, and never touches the network.

What makes a vector reproducible on one machine: one thread, one text per inference (so a vector
never depends on the texts it was batched with, or on padding), no truncation. A text longer than
the model's token limit is refused (TooLong), not cut: a guard must know that it did not look at
all of a text. Across different CPUs the last bits of a vector are not guaranteed; the cache of a
run is what makes its replay exact.

Each model's pipeline is that of its reference implementation (sentence-transformers), which the
calibration checks vector by vector before it scores anything:

  e5-small            "query: " before every text, mean over tokens, unit length
  labse               the first token, then the model's Dense layer with tanh, unit length
  bge-m3              the first token, unit length
  minilm-paraphrase   mean over tokens (the reference stops there; unit length is added here,
                      which does not change a cosine)

    python -m mcp_cybernet_lab.embedding fetch [MODEL ...]     download and verify the pinned files
    python -m mcp_cybernet_lab.embedding verify [MODEL ...]    verify files already downloaded
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import sys
import urllib.request
from pathlib import Path

HUB = "https://huggingface.co"
# What the module needs beside the standard library, in the versions the calibration was run with.
# The same list is the install extra `embed` and what the embedder image installs.
REQUIREMENTS = ["numpy==2.5.5", "onnxruntime==1.32.0", "tokenizers==0.23.3"]
RETRIES = 8  # attempts per file; each goes on from the bytes already there
MODELS = {
    "e5-small": {
        "repo": "intfloat/multilingual-e5-small", "revision": "614241f622f53c4eeff9890bdc4f31cfecc418b3",
        "files": {"onnx/model.onnx": "ca456c06b3a9505ddfd9131408916dd79290368331e7d76bb621f1cba6bc8665",
                  "tokenizer.json": "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39"},
        "pooling": "mean", "dense": None, "prefix": "query: ", "max_tokens": 512, "dim": 384,
    },
    "labse": {
        "repo": "sentence-transformers/LaBSE", "revision": "836121a0533e5664b21c7aacc5d22951f2b8b25b",
        "files": {"onnx/model.onnx": "2a48ae86b3ef337ea47916915386180fe97ecbb73f0797b6f6b5550bb05c4979",
                  "tokenizer.json": "d09046654dc5151fc313613f16768b6ecc1e5e3bddbc9c8aca209a5f23fe3cd3",
                  "2_Dense/model.safetensors": "f866c945fa2147ce4940c8ead0b4b20af4217e0f4664bd35f41c2a0967c4fe77"},
        "pooling": "cls", "dense": "2_Dense/model.safetensors", "prefix": "", "max_tokens": 256, "dim": 768,
    },
    "bge-m3": {
        "repo": "BAAI/bge-m3", "revision": "5617a9f61b028005a4858fdac845db406aefb181",
        "files": {"onnx/model.onnx": "f84251230831afb359ab26d9fd37d5936d4d9bb5d1d5410e66442f630f24435b",
                  "onnx/model.onnx_data": "1eebfb28493f67bba03ce0ef64bfdc7fc5a3bd9d7493f818bb1d78cd798416b4",
                  "tokenizer.json": "21106b6d7dab2952c1d496fb21d5dc9db75c28ed361a05f5020bbba27810dd08"},
        "pooling": "cls", "dense": None, "prefix": "", "max_tokens": 8192, "dim": 1024,
    },
    "minilm-paraphrase": {
        "repo": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        "revision": "e8f8c211226b894fcb81acc59f3b34ba3efd5f42",
        "files": {"onnx/model.onnx": "10f7a088420252b26caf819236ca2c9d2987afd0fc06fec7553b542a5655a05a",
                  "tokenizer.json": "2c3387be76557bd40970cec13153b3bbf80407865484b209e655e5e4729076b8"},
        "pooling": "mean", "dense": None, "prefix": "", "max_tokens": 128, "dim": 384,
    },
}


class EmbeddingError(Exception):
    """A model that cannot be used: unknown, not downloaded, or a file that is not the pinned one."""


class TooLong(ValueError):
    """A text with more tokens than the model reads. It is refused, not cut."""


def default_root() -> Path:
    return Path(os.environ.get("MCL_EMBEDDING_MODELS") or Path.home() / ".cache" / "mcl-embedding")


def model_dir(name: str, root: Path | None = None) -> Path:
    if name not in MODELS:
        raise EmbeddingError(f"embedding model '{name}': not one of {', '.join(MODELS)}")
    return (root or default_root()) / name / MODELS[name]["revision"]


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def verify(name: str, root: Path | None = None) -> dict[str, str]:
    """Check every file of a model against its pinned hash. Returns path -> hash; raises on any
    missing or different file."""
    folder, found = model_dir(name, root), {}
    for path, pinned in MODELS[name]["files"].items():
        f = folder / path
        if not f.is_file():
            raise EmbeddingError(f"embedding model '{name}': {f} is missing "
                                 f"(python -m mcp_cybernet_lab.embedding fetch {name})")
        found[path] = file_sha256(f)
        if found[path] != pinned:
            raise EmbeddingError(f"embedding model '{name}': {path} has SHA-256 {found[path]}, pinned is {pinned}")
    return found


def fetch(name: str, root: Path | None = None, log=print) -> Path:
    """Download the pinned files of a model at its pinned revision, each checked against its hash."""
    spec, folder = MODELS[name], model_dir(name, root)
    for path, pinned in spec["files"].items():
        target = folder / path
        if target.is_file() and file_sha256(target) == pinned:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        url = f"{HUB}/{spec['repo']}/resolve/{spec['revision']}/{path}"
        log(f"{name}: fetching {path}")
        for attempt in range(1, RETRIES + 1):  # a large file over a slow line: go on where it broke off
            have = part.stat().st_size if part.is_file() else 0
            request = urllib.request.Request(url, headers={"User-Agent": "mcl-embedding",
                                                           **({"Range": f"bytes={have}-"} if have else {})})
            try:
                with urllib.request.urlopen(request, timeout=120) as r:
                    resumed = have and getattr(r, "status", 200) == 206
                    with open(part, "ab" if resumed else "wb") as out:
                        while chunk := r.read(1 << 20):
                            out.write(chunk)
                break
            except OSError as e:  # timeouts and broken connections; HTTP errors too
                if attempt == RETRIES or getattr(e, "code", None) in (401, 403, 404):
                    raise EmbeddingError(f"embedding model '{name}': {path} could not be fetched: {e}") from None
                log(f"{name}: {path} broke off ({type(e).__name__}); trying again from where it stopped")
        got = file_sha256(part)  # of the whole file as it now lies on disk, however many parts it came in
        if got != pinned:
            part.unlink()
            raise EmbeddingError(f"embedding model '{name}': {path} at revision {spec['revision']} has SHA-256 "
                                 f"{got}, pinned is {pinned}; nothing was kept")
        part.replace(target)
    verify(name, root)
    return folder


def read_safetensors(path: Path) -> dict:
    """Tensors of a safetensors file as numpy arrays (float32 only: all this module needs)."""
    import numpy as np
    data = path.read_bytes()
    (n,) = struct.unpack("<Q", data[:8])
    header = json.loads(data[8:8 + n])
    out = {}
    for key, t in header.items():
        if key == "__metadata__":
            continue
        if t["dtype"] != "F32":
            raise EmbeddingError(f"{path}: tensor {key} has dtype {t['dtype']}, expected F32")
        a, b = t["data_offsets"]
        out[key] = np.frombuffer(data[8 + n + a:8 + n + b], dtype="<f4").reshape(t["shape"])
    return out


class Embedder:
    """embed(texts) -> one float32 vector of unit length per text, for one pinned model."""

    def __init__(self, name: str, root: Path | None = None):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.name, self.spec, self.np = name, MODELS.get(name), np
        folder = model_dir(name, root)
        self.files = verify(name, root)  # nothing is loaded that is not the pinned file
        self.tokenizer = Tokenizer.from_file(str(folder / "tokenizer.json"))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        self.session = ort.InferenceSession(str(folder / "onnx/model.onnx"), sess_options=options,
                                            providers=["CPUExecutionProvider"])
        self.inputs = [i.name for i in self.session.get_inputs()]
        # The output with one vector per token. Everything after it is done here, the same for every model.
        tokens = [o.name for o in self.session.get_outputs() if len(o.shape) == 3]
        if not tokens:
            raise EmbeddingError(f"embedding model '{name}': the ONNX file has no per-token output "
                                 f"({[o.name for o in self.session.get_outputs()]})")
        self.output = tokens[0]
        self.dense = read_safetensors(folder / self.spec["dense"]) if self.spec["dense"] else None

    def token_count(self, text: str) -> int:
        """Tokens the model reads for this text, with its prefix and its special tokens."""
        return len(self.tokenizer.encode(self.spec["prefix"] + text).ids)

    def embed_one(self, text: str):
        np = self.np
        encoding = self.tokenizer.encode(self.spec["prefix"] + text)
        if len(encoding.ids) > self.spec["max_tokens"]:
            raise TooLong(f"{len(encoding.ids)} tokens; {self.name} reads {self.spec['max_tokens']}")
        feed = {"input_ids": np.array([encoding.ids], dtype=np.int64),
                "attention_mask": np.ones((1, len(encoding.ids)), dtype=np.int64),
                "token_type_ids": np.zeros((1, len(encoding.ids)), dtype=np.int64)}
        hidden = self.session.run([self.output], {k: v for k, v in feed.items() if k in self.inputs})[0][0]
        vector = hidden[0] if self.spec["pooling"] == "cls" else hidden.mean(axis=0)
        if self.dense is not None:
            vector = np.tanh(self.dense["linear.weight"] @ vector + self.dense["linear.bias"])
        vector = vector.astype(np.float32)
        return vector / np.linalg.norm(vector)

    def embed(self, texts: list[str]) -> list:
        return [self.embed_one(t) for t in texts]


def main(argv: list[str]) -> int:
    if len(argv) < 1 or argv[0] not in ("fetch", "verify"):
        print(__doc__.strip().split("\n\n")[-1])
        return 2
    try:
        for name in argv[1:] or list(MODELS):
            if argv[0] == "fetch":
                fetch(name)
            hashes = verify(name)
            print(f"{name}: {MODELS[name]['repo']} at {MODELS[name]['revision'][:12]}, {len(hashes)} files verified, "
                  f"{sum((model_dir(name) / p).stat().st_size for p in hashes) / 1e6:.0f} MB in {model_dir(name)}")
    except EmbeddingError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
