"""Locked packages for a custom server: every wheel its image installs, with its hash.

A server folder (servers/<name>) may hold a file `wheels.lock`. `mcl build` then downloads exactly
those wheels on this machine, checks every hash, builds the server itself into a wheel here, and
the image installs all of it from the build context: the image build fetches nothing for the
server. Without the file a server is installed as before, by the image build.

    python -m mcp_cybernet_lab.serverlock servers/crm     write servers/crm/wheels.lock again
                                                          (a deliberate change: review and commit it)

The lock holds the dependencies of the server's pyproject.toml, resolved for the Python of the
server images on each processor type. Host only: nothing here is copied into an image.
"""

from __future__ import annotations

import platform
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from . import embedding

LOCK = "wheels.lock"
SERVER_PYTHON = ("3.11", "cp311")  # the python3 of the server images' base (Debian bookworm)
MACHINES = embedding.IMAGE_MACHINES


class LockError(Exception):
    pass


def dependencies(server: Path) -> list[str]:
    return tomllib.loads((server / "pyproject.toml").read_text(encoding="utf-8"))["project"].get("dependencies", [])


def write_lock(server: Path, log=print) -> Path:
    """Resolve the server's dependencies for every processor type and write its lock."""
    locked: dict[str, set[str]] = {}
    for machine in MACHINES:
        with tempfile.TemporaryDirectory() as tmp:
            r = subprocess.run(embedding.pip_download(Path(tmp), machine, dependencies(server), SERVER_PYTHON),
                               capture_output=True, text=True)
            if r.returncode != 0:
                raise LockError(f"pip download for {machine} failed:\n{(r.stderr or r.stdout).strip()[-1500:]}")
            for wheel in sorted(Path(tmp).glob("*.whl")):
                locked.setdefault(embedding.wheel_requirement(wheel.name), set()).add(embedding.file_sha256(wheel))
        log(f"{machine}: {len(locked)} packages")
    names = [r.split("==")[0] for r in locked]
    if len(names) != len(set(names)):
        raise LockError(f"a package resolves to different versions on different processors: {sorted(locked)}")
    lines = [f"# Every package the image of this server installs ({', '.join(dependencies(server))} and what it needs),",
             f"# with the SHA-256 of its wheel for each processor type ({', '.join(MACHINES)}), for Python "
             f"{SERVER_PYTHON[0]}.",
             f"# Written by:  python -m mcp_cybernet_lab.serverlock servers/{server.name}",
             "# Do not edit: write it again, review the change, commit it."]
    for requirement in sorted(locked):
        hashes = sorted(locked[requirement])
        lines.append(requirement + " \\")
        lines += [f"    --hash=sha256:{h}" + (" \\" if h != hashes[-1] else "") for h in hashes]
    (server / LOCK).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return server / LOCK


def download(lock: Path, dest: Path) -> dict[str, str]:
    """Exactly the wheels of the lock, for the server images' Python and this machine's processor.
    pip is told to require the locked hashes and to resolve nothing; then every file is checked
    against the lock again, and none may be missing. Returns file name -> SHA-256."""
    machine = platform.machine().lower().replace("amd64", "x86_64").replace("arm64", "aarch64")
    dest.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(embedding.pip_download(dest, machine, ["--require-hashes", "--no-deps", "-r", str(lock)],
                                              SERVER_PYTHON), capture_output=True, text=True)
    if r.returncode != 0:
        raise LockError(f"the packages of {lock} could not be downloaded ({sys.executable} -m pip download):\n"
                        f"{(r.stderr or r.stdout).strip()[-1500:]}")
    try:
        return embedding.check_wheels(dest, lock)
    except embedding.EmbeddingError as e:
        raise LockError(f"the packages downloaded are not those of {lock}: {e}") from None


if __name__ == "__main__":
    if len(sys.argv) != 2 or not (Path(sys.argv[1]) / "pyproject.toml").is_file():
        sys.exit(__doc__)
    print(f"wrote {write_lock(Path(sys.argv[1]))}")
