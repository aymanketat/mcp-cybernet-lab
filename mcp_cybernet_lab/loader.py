"""Read a testbed YAML file into a validated `Testbed`."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from .guards import GUARDS, UnknownGuard, installed_clashes
from .guards import check as check_guard
from .schema import Testbed


class TestbedError(Exception):
    """A testbed file that cannot be used, with a message meant for the person who wrote it."""

    __test__ = False  # the name starts with "Test"; tell pytest it is not a test class


@dataclass
class Loaded:
    testbed: Testbed
    path: Path  # the YAML file
    sha256: str  # hash of the file bytes, recorded in the manifest
    warnings: list[str]


def load_testbed(path: str | Path) -> Loaded:
    path = Path(path).resolve()
    if not path.is_file():
        raise TestbedError(f"{path}: file not found")
    raw = path.read_bytes()
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise TestbedError(f"{path}: not valid YAML: {e}") from e
    if not isinstance(data, dict):
        raise TestbedError(f"{path}: the top level must be a mapping")
    try:
        tb = Testbed.model_validate(data)
    except ValidationError as e:
        lines = [f"{path}: {e.error_count()} problem(s)"]
        for err in e.errors():
            where = ".".join(str(p) for p in err["loc"]) or "(top level)"
            lines.append(f"  - {where}: {err['msg']}")
        raise TestbedError("\n".join(lines)) from None

    warnings = []
    for s in tb.servers:
        if s.runtime == "local":
            src = path.parent / s.path
            if not (src / "pyproject.toml").is_file():
                raise TestbedError(f"server '{s.id}': '{s.path}' must be a folder with a pyproject.toml")
        elif s.version is None:
            warnings.append(f"server '{s.id}': no version pinned; the build is not reproducible")
        if s.extra_canaries and not (path.parent / s.extra_canaries).is_file():
            raise TestbedError(f"server '{s.id}': extra_canaries '{s.extra_canaries}' not found")
        if s.data and s.data.source and not (path.parent / s.data.source).is_dir():
            raise TestbedError(f"server '{s.id}': data folder '{s.data.source}' not found next to {path.name}")
    for p in tb.gateway.plugins:
        if not (path.parent / p / "pyproject.toml").is_file():
            raise TestbedError(f"gateway.plugins: '{p}' must be a folder with a pyproject.toml, "
                               f"relative to {path.name}")
    names = [(path.parent / p).resolve().name for p in tb.gateway.plugins]
    if len(set(names)) < len(names):
        raise TestbedError(f"gateway.plugins: two folders have the same name ({sorted(names)})")
    if tb.gateway.plugins and tb.gateway.guard == "none":
        raise TestbedError("gateway.plugins needs a gateway (gateway.guard is none)")
    if tb.gateway.guard != "none":  # other clashes among installed plugins do not touch this testbed
        warnings.extend(f"{sentence}; this testbed does not use the name"
                        for name, sentence in installed_clashes().items() if name != tb.gateway.guard)
    if tb.llm.api_key_env and not os.environ.get(tb.llm.api_key_env):
        warnings.append(f"llm: environment variable {tb.llm.api_key_env} is not set")
    asked = isinstance(data.get("llm"), dict) and data["llm"].get("egress") is not None
    if (tb.gateway.guard != "none" and tb.llm.provider != "none" and tb.llm.egress == "none"
            and not tb.llm_service and not asked):
        warnings.append(f"llm: clients behind the gateway have no path to the model: egress relay "
                        f"{tb.llm_relay_obstacle()}, and the endpoint is not a service of this testbed")
    return Loaded(tb, path, hashlib.sha256(raw).hexdigest(), warnings)


def check_buildable(tb: Testbed) -> None:
    """Refuse values that the schema names but this version does not implement."""
    todo = []
    for d in tb.domains:
        if d.auth == "oauth":
            todo.append(f"domain '{d.id}': auth 'oauth' (use 'token'; OAuth 2.1 is not implemented)")
    name = tb.gateway.guard
    if name != "none" and name in installed_clashes():  # the name this testbed uses is ambiguous
        raise TestbedError(f"gateway.guard: {installed_clashes()[name]}. Uninstall or rename a plugin")
    if name != "none" and name not in GUARDS:
        try:
            check_guard(name)
        except UnknownGuard as e:
            if name not in ("mcphunt", "fides"):
                raise TestbedError(f"gateway.guard: {e}. Install the plugin on this machine as well "
                                   f"(pip install -e <its folder>): `mcl build` and replay load it here") from None
            todo.append(f"gateway.guard '{name}' (external guard; integrate its code as a guard plug-in)")
        except (ValueError, ImportError) as e:
            raise TestbedError(f"gateway.guard: {e}") from None
        else:
            if not tb.gateway.plugins:
                raise TestbedError(f"gateway.guard '{name}' comes from a plugin: list the plugin's folder in "
                                   f"gateway.plugins, so that the gateway image gets it")
    for c in tb.clients:
        if c.harness in ("mcphunt", "langgraph"):
            todo.append(f"client '{c.id}': harness '{c.harness}' (only 'agent' and 'none' are built in)")
    if todo:
        raise TestbedError("not implemented in this version:\n" + "\n".join(f"  - {t}" for t in todo))
