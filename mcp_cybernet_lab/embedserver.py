#!/usr/bin/env python3
"""The embedder sidecar of a testbed: one pinned embedding model behind a small HTTP service.

    embedserver.py MODEL            (container command; the model's files are in the image)

It sits on an internal network that only the gateway is on. It has no way out and needs none: the
weights were put into the image when it was built, and embedding.py never uses the network.

    GET  /health   {"model", "repo", "revision", "dim", "max_tokens", "files": {path: sha256}}
    POST /embed    {"texts": [...]}  ->  {"model", "revision", "results": [...]}, one per text:
                   {"vector": <float32, little-endian, base64>, "dim"}  or  {"error": "too_long", "detail"}

One text is embedded at a time, on one thread, so a vector does not depend on what else is asked.
"""

from __future__ import annotations

import base64
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from mcp_cybernet_lab import embedding  # on the host, as part of the package
except ImportError:  # in the image: a plain file next to this one
    sys.path.insert(0, str(Path(__file__).parent))
    import embedding

PORT = 8080


def serve(embedder, port: int = PORT, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    """A server around an Embedder (anything with name, spec, files, embed_one)."""
    lock = threading.Lock()
    spec = embedder.spec
    health = {"model": embedder.name, "repo": spec["repo"], "revision": spec["revision"], "dim": spec["dim"],
              "max_tokens": spec["max_tokens"], "files": embedder.files}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def answer(self, status: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.answer(200, health) if self.path == "/health" else self.answer(404, {"error": "unknown path"})

        def do_POST(self):
            if self.path != "/embed":
                return self.answer(404, {"error": "unknown path"})
            try:
                texts = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))["texts"]
                assert isinstance(texts, list) and all(isinstance(t, str) for t in texts)
            except (ValueError, KeyError, TypeError, AssertionError):
                return self.answer(400, {"error": 'expected {"texts": [strings]}'})
            results = []
            for text in texts:
                with lock:
                    try:
                        vector = embedder.embed_one(text)
                        results.append({"dim": len(vector),
                                        "vector": base64.b64encode(vector.astype("<f4").tobytes()).decode("ascii")})
                    except embedding.TooLong as e:
                        results.append({"error": "too_long", "detail": str(e)})
            self.answer(200, {"model": embedder.name, "revision": spec["revision"], "results": results})

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    embedder = embedding.Embedder(sys.argv[1])
    print(f"mcl-embedder: {embedder.name}, {embedder.spec['repo']} at {embedder.spec['revision'][:12]}, "
          f"{len(embedder.files)} files verified", flush=True)
    serve(embedder).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
