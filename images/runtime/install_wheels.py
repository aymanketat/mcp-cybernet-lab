#!/usr/bin/env python3
"""Install a custom MCP server from wheels in the build context and expose it as /usr/local/bin/mcp-<id>.

Run at image build time:  install_wheels.py <folder> <id>

<folder> holds the server built into a wheel, the wheels of everything it needs, their lock
(wheels.lock, with hashes) and server.json, which names the server's wheel and package.
`mcl build` put them there, downloaded and checked on the host (see serverlock.py). Everything is
installed from that folder with the hashes required; nothing is fetched.

The result is what install_server.py leaves for any other server: `mcp-<id> <args>` starts it.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

folder, sid = Path(sys.argv[1]), sys.argv[2]
prefix = Path("/opt/mcp") / sid
prefix.mkdir(parents=True, exist_ok=True)
wrapper = Path("/usr/local/bin") / f"mcp-{sid}"


def run(*cmd):
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


named = json.loads((folder / "server.json").read_text())
package = named["package"]
run("uv", "venv", "--quiet", str(prefix / "venv"))
py = str(prefix / "venv" / "bin" / "python")
offline = ["--quiet", "--python", py, "--offline", "--no-index", "--find-links", str(folder)]
run("uv", "pip", "install", *offline, "--require-hashes", "-r", str(folder / "wheels.lock"))
run("uv", "pip", "install", *offline, "--no-deps", str(folder / named["wheel"]))
probe = (
    "import importlib.metadata as m, json;"
    f"d = m.distribution({package!r});"
    "eps = [e.name for e in d.entry_points if e.group == 'console_scripts'];"
    "print(json.dumps({'eps': eps, 'version': d.version}))"
)
info = json.loads(subprocess.check_output([py, "-c", probe], text=True))
if not info["eps"]:
    sys.exit(f"{package}: no console script")
# Prefer the script named like the package; otherwise the first one.
name = package if package in info["eps"] else info["eps"][0]
wrapper.write_text(f'#!/bin/sh\nexec "{prefix / "venv" / "bin" / name}" "$@"\n')
wrapper.chmod(0o755)
# Record what was actually installed; mcp-cybernet-lab copies these into the run manifest.
(prefix / "installed.json").write_text(json.dumps({"id": sid, "package": package, "version": info["version"]}))
print(f"installed {package} {info['version']} as {wrapper}")
os._exit(0)
