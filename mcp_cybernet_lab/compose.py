"""Compile a testbed into a build folder: Dockerfiles, docker-compose.yml, seeded data, manifest.

build/<testbed>/
  docker-compose.yml
  context/                 runtime files copied into every image (agent, gateway, proxies, servers)
  dockerfiles/<service>.Dockerfile
  data/<server>/           what each server sees at its mount point (canaries planted)
  data.pristine/           copy of data/, restored by `mcl reset`
  policy/current.json      the guard policy the gateway loads (when a gateway is configured)
  canaries.json            ground truth: every planted canary and where it lives
  decoys.json              values of the same kinds that are planted nowhere (canaries.decoys)
  manifest.json            what was built, from which file, with which seed

Run outputs go to runs/<testbed>/ (outside build/, so a rebuild never deletes results).
"""

from __future__ import annotations

import configparser
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from . import __version__, embedding, serverlock
from .guards import default_policy, files_sha256, guard_info, installed_guards, is_code, name_clashes
from .loader import Loaded, TestbedError, check_buildable
from .schema import EMBEDDER, LLM_EGRESS, LLM_RELAY, Client, Server, Service, Testbed
from .seeding import (CRM_FILE, PUBLIC_FILE, canary_file_text, crm_data, make_canaries, make_decoys,
                      public_reference_text, stream)

REPO = Path(__file__).resolve().parent.parent
PACKAGE = Path(__file__).resolve().parent
RUNTIME = REPO / "images" / "runtime"
CONTAINER_MODULES = ["probe.py", "mcpclient.py", "agent.py", "httpproxy.py", "authproxy.py",
                     "gateway.py", "guards.py", "sink.py"]
WHEEL_INSTALLER = "install_wheels.py"  # in images/runtime; copied into a build context only for a locked server
RELAY_MODULE = "llmrelay.py"  # copied only into the LLM relay image, and only when one is built
HTTP_PORT = 8080
LLM_PORT = 443  # the relay accepts and forwards HTTPS only
INNER_PORT = 8081  # where a server listens when an auth proxy sits in front of it
MCP_PROXY = "mcp-proxy==0.13.0"
BASE_IMAGE = "node:22-bookworm-slim"
UV_IMAGE = "ghcr.io/astral-sh/uv:0.9.5"
GATEWAY_NET = "net-gateway"
EMBED_NET = "net-embed"  # the gateway and the embedder, and nobody else
EMBEDDER_IMAGE = "python:3.12-slim-bookworm"
EMBEDDER_MODULES = ["embedding.py", "embedserver.py"]  # not standard library only: their own image


def network_name(domain: str) -> str:
    return f"net-{domain}"


def domain_token(tb: Testbed, domain: str) -> str:
    """Per-domain bearer token, derived from the seed (testbed secret, not a real credential)."""
    return "mcl_" + "".join(stream(tb.seed, "token", domain).choice("0123456789abcdef") for _ in range(32))


def needs_token(tb: Testbed, s: Server) -> bool:
    return s.transport == "http" and any(d.id == s.domain and d.auth == "token" for d in tb.domains)


def dockerfile(installs: list[Server], cmd: list[str], modules: list[str] = CONTAINER_MODULES,
               extra: list[str] = (), locked: frozenset[str] = frozenset()) -> str:
    """locked: ids of the servers whose packages lie in the build context as wheels (wheels/<id>),
    downloaded on the host from the server's lock. They are installed from there."""
    lines = [
        f"FROM {BASE_IMAGE}",
        "RUN apt-get update && apt-get install -y --no-install-recommends \\",
        "      python3 python3-venv git iproute2 ca-certificates \\",
        " && rm -rf /var/lib/apt/lists/*",
        f"COPY --from={UV_IMAGE} /uv /uvx /usr/local/bin/",
        "ENV UV_TOOL_DIR=/opt/uv-tools UV_TOOL_BIN_DIR=/usr/local/bin UV_PYTHON_PREFERENCE=only-system",
        f"RUN uv tool install {MCP_PROXY}",
        "COPY install_server.py entrypoint.sh /opt/mcl/",
        "RUN chmod +x /opt/mcl/*.sh /opt/mcl/*.py",
    ]
    for s in installs:
        if s.id in locked:  # mounted for this one step, so the wheels are not a layer of the image
            lines.append(f"COPY {WHEEL_INSTALLER} /opt/mcl/")
            lines.append(f"RUN --mount=type=bind,source=wheels/{s.id},target=/wheels "
                         f"python3 /opt/mcl/{WHEEL_INSTALLER} /wheels {s.id}")
            continue
        if s.runtime == "local":  # custom server: its source is copied into the build context
            lines.append(f"COPY servers/{s.id} /opt/mcp-src/{s.id}")
            source = f"/opt/mcp-src/{s.id}"
        else:
            source = s.package
        lines.append(f"RUN python3 /opt/mcl/install_server.py {s.runtime} {source} {s.version or '-'} {s.id}"
                     + "".join(f" '{w}'" for w in s.with_packages))
    # The lab's own Python files change most often: copy them last, to keep the layers above cached.
    lines.append("COPY " + " ".join(modules) + " /opt/mcl/")
    lines.extend(extra)
    lines.append('ENTRYPOINT ["/opt/mcl/entrypoint.sh"]')
    lines.append(f"CMD {json.dumps(cmd)}")
    return "\n".join(lines) + "\n"


def netem_env(s: Server) -> dict[str, str]:
    n = s.network
    if n.is_default:
        return {}
    env = {}
    if n.delay_ms or n.jitter_ms:
        env["MCL_DELAY_MS"] = str(n.delay_ms)
        env["MCL_JITTER_MS"] = str(n.jitter_ms)
    if n.loss:
        env["MCL_LOSS_PCT"] = f"{n.loss * 100:g}"
    return env


def llm_env(tb: Testbed) -> dict[str, str]:
    # `egress` is about the network around the agent, not a setting of the model: left out, so
    # this value is the same as before the field existed.
    env = {"MCL_LLM": json.dumps(tb.llm.model_dump(exclude={"egress"}))}
    if tb.llm.api_key_env:
        env[tb.llm.api_key_env] = f"${{{tb.llm.api_key_env}:-}}"  # read from the host at `up`
    return env


def server_command(tb: Testbed, s: Server) -> list[str]:
    """The stdio server, exposed over Streamable HTTP by mcp-proxy (behind the auth proxy if needed)."""
    if needs_token(tb, s):
        return ["python3", "/opt/mcl/authproxy.py", "--", "mcp-proxy", "--host", "127.0.0.1",
                "--port", str(INNER_PORT), "--pass-environment", "--", f"mcp-{s.id}", *s.args]
    return ["mcp-proxy", "--host", "0.0.0.0", "--port", str(HTTP_PORT), "--pass-environment",
            "--", f"mcp-{s.id}", *s.args]


def server_service(tb: Testbed, s: Server) -> dict:
    env = {**s.env, **netem_env(s)}
    if needs_token(tb, s):
        env["MCL_TOKEN"] = domain_token(tb, s.domain)
    if "MCL_TRANSFORM" in s.env:  # transformer servers call the testbed's LLM
        env.update(llm_env(tb))
    svc = {
        "build": {"context": "context", "dockerfile": f"../dockerfiles/{s.id}.Dockerfile"},
        "image": f"mcl-{tb.testbed}-{s.id}",
        "command": server_command(tb, s),
        "environment": env,
        "networks": [network_name(s.domain)],
        "labels": {"mcl.role": "server", "mcl.label": s.label, "mcl.transport": "http"},
    }
    if s.has_volume:
        svc["volumes"] = [f"./data/{s.id}:{s.mount}"]
    if env.keys() & {"MCL_DELAY_MS", "MCL_LOSS_PCT"}:
        svc["cap_add"] = ["NET_ADMIN"]
    if not env:
        del svc["environment"]
    return svc


def client_endpoints(tb: Testbed, c: Client) -> dict:
    """How the client reaches each of its servers (MCL_SERVERS)."""
    via_gateway = tb.gateway.guard != "none"
    out = {}
    for s in (tb.server(x) for x in c.servers):
        if s.transport == "stdio":
            out[s.id] = {"transport": "stdio", "command": [f"mcp-{s.id}", *s.args], "env": s.env}
            continue
        url = (f"http://gateway:{HTTP_PORT}/{s.id}/mcp" if via_gateway else f"http://{s.id}:{HTTP_PORT}/mcp")
        spec = {"transport": "http", "url": url}
        if needs_token(tb, s):
            spec["headers"] = {"Authorization": f"Bearer {domain_token(tb, s.domain)}"}
        out[s.id] = spec
    return out


def client_service(tb: Testbed, c: Client, runs: Path) -> dict:
    servers = [tb.server(x) for x in c.servers]
    local = [s for s in servers if s.transport == "stdio"]
    remote = [s for s in servers if s.transport == "http"]
    mounts = {}
    for s in local:  # stdio servers run inside the client container, so their data is mounted here
        if s.has_volume:
            m = s.mount
            if m in mounts:
                raise TestbedError(f"client '{c.id}': servers '{mounts[m]}' and '{s.id}' both mount {m}")
            mounts[m] = s.id
    env = {"MCL_SERVERS": json.dumps(client_endpoints(tb, c)), **llm_env(tb)}
    for s in local:  # stdio transformers need the LLM too; keys already passed by llm_env
        env.update({k: v for k, v in s.env.items() if k.startswith("MCL_") and k not in env})
    svc = {
        "build": {"context": "context", "dockerfile": f"../dockerfiles/{c.id}.Dockerfile"},
        "image": f"mcl-{tb.testbed}-{c.id}",
        "command": ["sleep", "infinity"],  # tasks are started by `mcl run` with docker exec
        "environment": env,
        "labels": {"mcl.role": "client", "mcl.harness": c.harness},
        "init": True,
        "volumes": [f"{runs}:/runs"] + [f"./data/{sid}:{m}" for m, sid in mounts.items()],
    }
    if tb.gateway.guard != "none":
        svc["networks"] = [GATEWAY_NET]
    else:
        nets = sorted({network_name(s.domain) for s in remote})
        if nets:
            svc["networks"] = nets
    if c.replicas > 1:
        svc["deploy"] = {"replicas": c.replicas}
    return svc


ENTRY_POINTS = "mcp_cybernet_lab.guards"
PLUGIN_SITE = "/opt/mcl/site"  # where guard plugins are installed in the gateway image
# In the image the lab's modules are plain files in /opt/mcl. This makes the import a plugin uses,
# `mcp_cybernet_lab.guards`, name the very module the gateway runs (/opt/mcl/guards.py).
GUARDS_SHIM = "import sys\n\nimport guards\n\nsys.modules[__name__] = guards\n"


def lock_of(tb_dir: Path, s: Server) -> Path | None:
    """The lock of a custom server's packages (servers/<name>/wheels.lock), if the server has one."""
    lock = tb_dir / s.path / serverlock.LOCK if s.runtime == "local" else None
    return lock if lock is not None and lock.is_file() else None


def server_wheels(tb_dir: Path, s: Server, lock: Path, out: Path) -> dict:
    """Put everything the image of a locked server installs into the build context (wheels/<id>):
    the wheels of the lock, downloaded here with every hash checked, the lock, the server itself
    built into a wheel here, and server.json naming that wheel. Returns what the manifest records."""
    dest = out / "context" / "wheels" / s.id
    try:
        wheels = serverlock.download(lock, dest)
    except serverlock.LockError as e:
        raise TestbedError(f"server '{s.id}': {e}") from None
    shutil.copy(lock, dest / serverlock.LOCK)
    own = build_wheel(tb_dir / s.path, dest, what=f"server '{s.id}'")
    package = tomllib.loads((tb_dir / s.path / "pyproject.toml").read_text(encoding="utf-8"))["project"]["name"]
    (dest / "server.json").write_text(json.dumps({"wheel": own.name, "package": package}), encoding="utf-8")
    return {"lock_sha256": embedding.file_sha256(lock), "wheels": wheels}


def build_wheel(source: Path, dest: Path, what: str = "gateway.plugins") -> Path:
    """Build a plugin folder into a wheel, on this machine, with whatever build backend it names.

    The folder is copied first, so the build leaves nothing in it. pip may fetch the build backend
    here; the image then installs the finished wheel and fetches nothing.
    """
    with tempfile.TemporaryDirectory() as tmp:
        copy, made = Path(tmp) / source.name, Path(tmp) / "wheel"
        shutil.copytree(source, copy, ignore=shutil.ignore_patterns(
            "__pycache__", "*.egg-info", "build", "dist", ".venv", ".git"))
        r = subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--quiet", "--wheel-dir", str(made),
                            str(copy)], capture_output=True, text=True)
        wheels = sorted(made.glob("*.whl")) if made.is_dir() else []
        if r.returncode != 0 or len(wheels) != 1:
            raise TestbedError(f"{what}: '{source}' could not be built into a wheel "
                               f"({sys.executable} -m pip wheel):\n{(r.stderr or r.stdout).strip()[-1500:]}")
        dest.mkdir(parents=True, exist_ok=True)
        return Path(shutil.copy(wheels[0], dest / wheels[0].name))


def wheel_guards(wheel: Path) -> dict:
    """What a plugin wheel holds: {"package", "version", "guards": [{"guard", "tree_sha256"}]}.

    The tree hash is taken over the files of the top-level package the guard class is in, exactly
    as guards.tree_sha256 takes it over the installed package (in the image and on the host).
    """
    with zipfile.ZipFile(wheel) as z:
        names = z.namelist()
        meta = next(n for n in names if n.endswith(".dist-info/METADATA"))
        fields = dict(line.split(": ", 1) for line in z.read(meta).decode("utf-8").split("\n\n")[0].splitlines()
                      if ": " in line and line.split(": ", 1)[0] in ("Name", "Version"))
        eps = configparser.ConfigParser(delimiters=("=",))
        eps.optionxform = str
        entry = next((n for n in names if n.endswith(".dist-info/entry_points.txt")), None)
        if entry:
            eps.read_string(z.read(entry).decode("utf-8"))
        found = []
        for guard, target in (eps[ENTRY_POINTS].items() if eps.has_section(ENTRY_POINTS) else []):
            top = target.split(":")[0].strip().split(".")[0]
            files = {n[len(top) + 1:]: z.read(n) for n in names
                     if n.startswith(top + "/") and not n.endswith("/") and is_code(n)}
            if f"{top}.py" in names:
                files = {f"{top}.py": z.read(f"{top}.py")}
            found.append({"guard": guard, "tree_sha256": files_sha256(files)})
    return {"package": fields.get("Name"), "version": fields.get("Version"), "guards": found}


def build_plugins(loaded: Loaded, out: Path, guard_plugin: dict | None) -> list[dict]:
    """Build every folder of gateway.plugins into a wheel in the build context. One entry per plugin.

    Refused: a guard name that two of them register, or that is a built-in guard; a configured
    plugin guard that none of them registers; and a configured guard whose code installed on this
    machine (which `mcl replay` will load) is not the code of the wheel (which the gateway will run).
    """
    tb, plugins = loaded.testbed, []
    for p in tb.gateway.plugins:
        source = (loaded.path.parent / p).resolve()
        wheel = build_wheel(source, out / "context" / "plugins")
        plugins.append({"name": source.name, "path": str(source), "wheel": wheel.name} | wheel_guards(wheel))
    # A listed plugin may clash with nothing: not with a built-in name, another listed plugin, or
    # a plugin installed on this machine (replay would not know which one the gateway ran).
    listed = [(g["guard"], p["package"]) for p in plugins for g in p["guards"]]
    clashes = name_clashes(listed + installed_guards(), "in gateway.plugins or installed on this machine")
    clashes = [sentence for name, sentence in clashes.items() if name in {g for g, _ in listed}]
    if clashes:
        raise TestbedError("a plugin in gateway.plugins registers a guard name that does not mean one thing:\n"
                           + "\n".join(f"  - {c}" for c in clashes))
    if guard_plugin:
        name = guard_plugin["guard"]
        built = [g["tree_sha256"] for p in plugins for g in p["guards"] if g["guard"] == name]
        if not built:
            raise TestbedError(f"gateway.guard '{name}' is not registered by any folder in gateway.plugins "
                               f"(they register: {sorted(g['guard'] for p in plugins for g in p['guards']) or 'nothing'})")
        if built != [guard_plugin["tree_sha256"]]:
            raise TestbedError(
                f"gateway.guard '{name}': the code installed on this machine ({guard_plugin['package']} "
                f"{guard_plugin['version']}, tree hash {guard_plugin['tree_sha256']}) is not the code in "
                f"gateway.plugins (tree hash {built[0]}). Replay would not run what the gateway runs: "
                f"install the folder here (pip install -e <folder>)")
    return plugins


def gateway_plugin_lines(tb: Testbed, wheels: list[str], info: dict | None) -> list[str]:
    """Dockerfile lines that put guard plugins into the gateway image. None without plugins.

    The wheels were built on the host (build_wheel). The image installs them without dependencies
    and without a package index, so its build fetches nothing for a plugin, whatever the plugin's
    build backend. A plugin that needs more than the standard library fails the last step, which
    loads the configured guard and compares its code with the wheel's (the tree hash).
    """
    if not wheels:
        return []
    return ["COPY mcp_cybernet_lab /opt/mcl/mcp_cybernet_lab",
            "COPY plugins /opt/mcl-plugins",
            f"RUN uv pip install --no-deps --no-index --python /usr/bin/python3 --target {PLUGIN_SITE} "
            + " ".join(f"/opt/mcl-plugins/{w}" for w in wheels),
            f"ENV PYTHONPATH={PLUGIN_SITE}",
            f"RUN python3 /opt/mcl/guards.py check {tb.gateway.guard}" + (f" {info['tree_sha256']}" if info else "")]


def embedder_spec(tb: Testbed) -> dict | None:
    """The testbed's embedding model as the gateway and the manifest name it; None without one."""
    name = tb.gateway.embedder
    if name == "none":
        return None
    model = embedding.MODELS[name]
    return {"model": name, "repo": model["repo"], "revision": model["revision"], "dim": model["dim"],
            "max_tokens": model["max_tokens"], "files": dict(model["files"]),
            # How long the gateway waits for one text; calls wait meanwhile, so it is kept short.
            "timeout_s": tb.gateway.embed_timeout_s or model["timeout_s"]}


def embedder_dockerfile(name: str) -> str:
    """The embedder image: the pinned packages, the lab's two modules, and the model's files.

    Everything comes from the build context: the image build fetches nothing but its base image.
    The packages are the wheels of the lock (embedder.lock), which `mcl build` downloaded on the
    host with every hash checked; pip checks the hashes again when it installs them. The model's
    files were checked against their pinned hashes there, and the image build checks them again,
    so a file that is not the pinned one fails the build. The container then runs without network
    access, as an unprivileged user, on a read-only file system.
    """
    return "\n".join([
        f"FROM {EMBEDDER_IMAGE}",
        # The wheels are mounted for this one step, so they are not a layer of the image.
        "RUN --mount=type=bind,source=wheels,target=/wheels pip install --no-cache-dir --no-index "
        f"--find-links /wheels --require-hashes -r /wheels/{embedding.LOCK.name}",
        "COPY " + " ".join(EMBEDDER_MODULES) + " /opt/mcl/",
        "COPY models /models",
        "ENV MCL_EMBEDDING_MODELS=/models",
        f"RUN python3 /opt/mcl/embedding.py verify {name}",
        f'CMD ["python3", "/opt/mcl/embedserver.py", "{name}"]',
    ]) + "\n"


def download_wheels(dest: Path) -> None:
    """Exactly the wheels of the lock, for the embedder image's Python and this machine's
    processor, downloaded here. pip is told to require the locked hashes and to resolve nothing."""
    import platform
    machine = platform.machine().lower().replace("amd64", "x86_64").replace("arm64", "aarch64")
    r = subprocess.run(embedding.pip_download(dest, machine, ["--require-hashes", "--no-deps",
                                                              "-r", str(embedding.LOCK)]),
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise TestbedError(f"gateway.embedder: the packages of the embedder image could not be downloaded "
                           f"({sys.executable} -m pip download):\n{(r.stderr or r.stdout).strip()[-1500:]}")


def embedder_context(name: str, out: Path) -> None:
    """Put the model's files, the two modules and the packages' wheels into the embedder's own build context.

    The files are taken from the cache on this machine (fetched now if they are not there yet) and
    verified against their pinned hashes first. Linked where the file system allows, else copied.
    """
    try:
        source = embedding.fetch(name, log=lambda line: print(line, flush=True))
    except embedding.EmbeddingError as e:
        raise TestbedError(f"gateway.embedder: {e}") from None
    context = out / "embedder"
    target = context / "models" / name / embedding.MODELS[name]["revision"]
    for path in embedding.MODELS[name]["files"]:
        (target / path).parent.mkdir(parents=True, exist_ok=True)
        try:
            (target / path).hardlink_to(source / path)
        except OSError:
            shutil.copy(source / path, target / path)
    for m in EMBEDDER_MODULES:
        shutil.copy(PACKAGE / m, context / m)
    (context / "wheels").mkdir()
    download_wheels(context / "wheels")
    try:  # whatever fetched them: each file is a locked package with a locked hash, and none is missing
        embedding.check_wheels(context / "wheels")
    except embedding.EmbeddingError as e:
        raise TestbedError(f"gateway.embedder: the packages of the embedder image are not those of "
                           f"{embedding.LOCK.name}: {e}") from None
    shutil.copy(embedding.LOCK, context / "wheels" / embedding.LOCK.name)


def embedder_service(tb: Testbed) -> dict:
    """No environment, no volume, one network: it holds no key and sees no run data, only texts."""
    return {
        "build": {"context": "embedder", "dockerfile": "../dockerfiles/embedder.Dockerfile"},
        "image": f"mcl-{tb.testbed}-{EMBEDDER}",
        "networks": [EMBED_NET],
        "labels": {"mcl.role": "embedder", "mcl.embedder": tb.gateway.embedder},
        "user": "65534:65534",  # nobody
        "cap_drop": ["ALL"],
        "read_only": True,
        # Loading a model takes seconds: `up --wait` and the gateway wait until it answers.
        "healthcheck": {"test": ["CMD", "python3", "-c", "import urllib.request; urllib.request.urlopen("
                                 f"'http://127.0.0.1:{HTTP_PORT}/health', timeout=3)"],
                        "interval": "3s", "timeout": "5s", "retries": 60, "start_period": "5s"},
    }


def gateway_service(tb: Testbed, runs: Path) -> dict:
    http = [s for s in tb.servers if s.transport == "http"]
    config = {"guard": tb.gateway.guard,
              "servers": {s.id: {"label": s.label, "domain": s.domain}
                          | ({"tool_labels": s.tool_labels} if s.tool_labels else {}) for s in http}}
    spec = embedder_spec(tb)
    if spec:  # only then: without an embedder the gateway's configuration is what it was
        config["embedder"] = {"model": spec["model"], "revision": spec["revision"],
                              "url": f"http://{EMBEDDER}:{HTTP_PORT}", "timeout_s": spec["timeout_s"]}
    return ({"depends_on": {EMBEDDER: {"condition": "service_healthy"}}} if spec else {}) | {
        "build": {"context": "context", "dockerfile": "../dockerfiles/gateway.Dockerfile"},
        "image": f"mcl-{tb.testbed}-gateway",
        "command": ["python3", "/opt/mcl/gateway.py", "serve"],
        "environment": {"MCL_GATEWAY": json.dumps(config)},
        "volumes": ["./policy:/policy", f"{runs}:/runs"],
        "networks": sorted({network_name(s.domain) for s in http} | {GATEWAY_NET} | ({EMBED_NET} if spec else set())),
        "labels": {"mcl.role": "gateway", "mcl.guard": tb.gateway.guard},
    }


def llm_relay_services(tb: Testbed) -> dict[str, dict]:
    """The two containers that carry a guarded client's HTTPS requests to a hosted model.

    llm-relay holds the provider's host name as an alias on the gateway network and forwards to
    llm-egress, which is outside that network and so can look up the provider (see llmrelay.py).
    Neither gets an environment or a volume: they hold no key and see no run data.
    """
    host = urlsplit(tb.llm.endpoint).hostname
    way_out = network_name(next(d.id for d in tb.domains if d.internet))

    def relay(upstream: str, networks, sysctls: dict) -> dict:
        return {
            "build": {"context": "context", "dockerfile": f"../dockerfiles/{LLM_RELAY}.Dockerfile"},
            "image": f"mcl-{tb.testbed}-{LLM_RELAY}",
            "command": ["python3", f"/opt/mcl/{RELAY_MODULE}", "--listen", f"0.0.0.0:{LLM_PORT}",
                        "--upstream", upstream, "--server-name", host],
            "networks": networks,
            "labels": {"mcl.role": "llm-relay"},
            "user": "65534:65534",  # nobody
            "cap_drop": ["ALL"],
            "read_only": True,
            # An unprivileged user may listen on 443; and the container on two networks is no router.
            "sysctls": {"net.ipv4.ip_unprivileged_port_start": 0, **sysctls},
        }

    return {
        LLM_RELAY: relay(f"{LLM_EGRESS}:{LLM_PORT}", {GATEWAY_NET: {"aliases": [host]}, way_out: {}},
                         {"net.ipv4.ip_forward": 0}),
        LLM_EGRESS: relay(f"{host}:{LLM_PORT}", [way_out], {}),
    }


def service_service(tb: Testbed, v: Service, runs: Path) -> dict:
    svc = {"networks": [network_name(v.domain)],
           "labels": {"mcl.role": "service", "mcl.label": v.label}}
    if tb.gateway.guard != "none" and v.id == tb.llm_service:
        svc["networks"].append(GATEWAY_NET)  # the model server: clients behind the gateway call it
    if v.image:
        svc["image"] = v.image
    else:  # builtin webhook-sink: records every request in runs/<testbed>/_sinks/<id>.jsonl
        svc |= {"build": {"context": "context", "dockerfile": "../dockerfiles/sink.Dockerfile"},
                "image": f"mcl-{tb.testbed}-sink",
                "command": ["python3", "/opt/mcl/sink.py", f"/runs/_sinks/{v.id}.jsonl"],
                "volumes": [f"{runs}:/runs"]}
    if v.env:
        svc["environment"] = dict(v.env)
    return svc


def tree_sha256(root: Path) -> str:
    """Hash of every file under a folder (names and contents), to identify a custom server's code."""
    h = hashlib.sha256()
    for f in sorted(p for p in root.rglob("*") if p.is_file()):
        h.update(str(f.relative_to(root)).encode() + b"\0" + f.read_bytes())
    return h.hexdigest()


def _git_commit() -> str:
    try:
        out = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(REPO), "status", "--porcelain"],
                               capture_output=True, text=True).stdout.strip()
        return out + ("-dirty" if dirty else "")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def initial_policy(loaded: Loaded) -> dict:
    tb = loaded.testbed
    servers = {s.id: {"label": s.label, "tool_labels": s.tool_labels} for s in tb.servers}
    policy = default_policy(servers)
    if tb.gateway.policy:
        f = loaded.path.parent / tb.gateway.policy
        if not f.is_file():
            raise TestbedError(f"gateway.policy '{tb.gateway.policy}' not found next to {loaded.path.name}")
        policy.update(yaml.safe_load(f.read_text()) or {})
        problems = unknown_policy_names(policy["sources"] + policy["sinks"], set(servers))
        if problems:
            raise TestbedError("gateway.policy: " + "; ".join(problems))
    return policy


def unknown_policy_names(names: list[str], servers: set[str]) -> list[str]:
    """What is wrong with the sources and sinks of a policy, as sentences; none when all are known.

    A name is known when it is a server of the testbed, or `server/tool` for a server of the testbed
    (guards.tool_key: how a single tool is listed, and how the default policy lists a tool that is
    labelled public on a server that is not). The tool's own name is not checked here: a build has no
    list of the tools a server offers, which the server gives when it runs. A server id holds no
    `/`, so the server is what stands before the first one.
    """
    plain = [n for n in names if "/" not in n and n not in servers]
    tools = [n for n in names if "/" in n and n.split("/", 1)[0] not in servers]
    empty = [n for n in names if "/" in n and n.split("/", 1)[0] in servers and not n.split("/", 1)[1]]
    return ([f"unknown servers {plain}"] if plain else []) \
        + ([f"tools on unknown servers {tools}"] if tools else []) \
        + ([f"{empty} name no tool (a single tool is listed as server/tool)"] if empty else [])


def build(loaded: Loaded, out: Path, runs: Path | None = None) -> Path:
    tb = loaded.testbed
    check_buildable(tb)
    out = out.resolve()
    runs = (runs or out.parent.parent / "runs" / tb.testbed).resolve()
    (runs / "_sinks").mkdir(parents=True, exist_ok=True)
    if out.exists():
        shutil.rmtree(out)
    (out / "dockerfiles").mkdir(parents=True)
    # install_wheels.py is for servers with a lock only: it is copied further down, when there is one,
    # so that a testbed without such a server has the build context it always had.
    shutil.copytree(RUNTIME, out / "context", ignore=shutil.ignore_patterns(WHEEL_INSTALLER))
    for m in CONTAINER_MODULES:
        shutil.copy(PACKAGE / m, out / "context" / m)
    for s in tb.servers:
        if s.runtime == "local":
            shutil.copytree(loaded.path.parent / s.path, out / "context" / "servers" / s.id,
                            ignore=shutil.ignore_patterns("__pycache__", "*.egg-info", "build", "*.db"))

    # Data and canaries.
    canaries = make_canaries(tb, loaded.path.parent)
    decoys = make_decoys(tb, canaries)
    for s in tb.servers:
        if not s.has_volume:
            continue
        d = out / "data" / s.id
        if s.data and s.data.source:
            shutil.copytree(loaded.path.parent / s.data.source, d)
        d.mkdir(parents=True, exist_ok=True)
        mine = [r for r in canaries if r["server"] == s.id and r["file"]]
        if mine and s.plant == "crm":
            (d / CRM_FILE).write_text(json.dumps(crm_data(mine), indent=1, ensure_ascii=False), encoding="utf-8")
        elif mine:
            (d / tb.canaries.file).write_text(canary_file_text(mine), encoding="utf-8")
        placed = [r for r in decoys if r["placed"] and r["server"] == s.id]
        if placed:
            (d / PUBLIC_FILE).write_text(public_reference_text(placed), encoding="utf-8")
    if (out / "data").exists():
        shutil.copytree(out / "data", out / "data.pristine")
    (out / "canaries.json").write_text(json.dumps(canaries, indent=1, ensure_ascii=False), encoding="utf-8")
    if decoys:
        (out / "decoys.json").write_text(json.dumps(decoys, indent=1, ensure_ascii=False), encoding="utf-8")

    # Containers.
    plugins: list[dict] = []
    guard_plugin = guard_info(tb.gateway.guard) if tb.gateway.guard != "none" else None
    services = {}
    # Custom servers with a lock: their packages are downloaded here and installed from the context.
    locked = {s.id: server_wheels(loaded.path.parent, s, lock, out)
              for s in tb.servers if (lock := lock_of(loaded.path.parent, s))}
    if locked:
        shutil.copy(RUNTIME / WHEEL_INSTALLER, out / "context" / WHEEL_INSTALLER)
    for s in tb.servers:
        if s.transport == "http":
            (out / "dockerfiles" / f"{s.id}.Dockerfile").write_text(
                dockerfile([s], server_command(tb, s), locked=frozenset(locked)))
            services[s.id] = server_service(tb, s)
    for c in tb.clients:
        local = [tb.server(x) for x in c.servers if tb.server(x).transport == "stdio"]
        (out / "dockerfiles" / f"{c.id}.Dockerfile").write_text(
            dockerfile(local, ["sleep", "infinity"], locked=frozenset(locked)))
        services[c.id] = client_service(tb, c, runs)
    if tb.gateway.guard != "none":
        (out / "policy").mkdir()
        (out / "policy" / "current.json").write_text(json.dumps(initial_policy(loaded), indent=1))
        plugins = build_plugins(loaded, out, guard_plugin)
        if plugins:
            shim = out / "context" / "mcp_cybernet_lab"
            shim.mkdir()
            (shim / "__init__.py").write_text("")
            (shim / "guards.py").write_text(GUARDS_SHIM)
        (out / "dockerfiles" / "gateway.Dockerfile").write_text(dockerfile(
            [], ["python3", "/opt/mcl/gateway.py", "serve"],
            extra=gateway_plugin_lines(tb, [p["wheel"] for p in plugins], guard_plugin)))
        services["gateway"] = gateway_service(tb, runs)
    if tb.gateway.embedder != "none":
        embedder_context(tb.gateway.embedder, out)
        (out / "dockerfiles" / "embedder.Dockerfile").write_text(embedder_dockerfile(tb.gateway.embedder))
        services[EMBEDDER] = embedder_service(tb)
    if any(v.builtin for v in tb.services):
        (out / "dockerfiles" / "sink.Dockerfile").write_text(dockerfile([], ["python3", "/opt/mcl/sink.py"]))
    for v in tb.services:
        services[v.id] = service_service(tb, v, runs)
    if tb.llm.egress == "relay":
        shutil.copy(PACKAGE / RELAY_MODULE, out / "context" / RELAY_MODULE)
        (out / "dockerfiles" / f"{LLM_RELAY}.Dockerfile").write_text(
            dockerfile([], ["python3", f"/opt/mcl/{RELAY_MODULE}", "--help"], modules=[RELAY_MODULE]))
        services |= llm_relay_services(tb)

    used = {n for svc in services.values() for n in svc.get("networks", [])}
    networks = {network_name(d.id): {"internal": not d.internet} for d in tb.domains
                if network_name(d.id) in used}
    if GATEWAY_NET in used:
        networks[GATEWAY_NET] = {"internal": True}  # clients reach only the gateway
    if EMBED_NET in used:
        networks[EMBED_NET] = {"internal": True}  # the embedder has no way out
    compose = {"name": f"mcl-{tb.testbed}", "services": services}
    if networks:
        compose["networks"] = networks
    (out / "docker-compose.yml").write_text(
        f"# Generated by mcp-cybernet-lab {__version__} from {loaded.path.name}. Do not edit; edit the YAML and rebuild.\n"
        + yaml.safe_dump(compose, sort_keys=False, allow_unicode=True)
    )

    manifest = {
        "testbed": tb.testbed,
        "source": str(loaded.path),
        "source_sha256": loaded.sha256,
        "seed": tb.seed,
        "version": __version__,
        "commit": _git_commit(),
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "base_images": [BASE_IMAGE, UV_IMAGE],
        "mcp_proxy": MCP_PROXY,
        "runs": str(runs),
        "llm": tb.llm.model_dump(),
        "gateway": tb.gateway.model_dump(),
        # A plugin guard: its package, version and the tree hash of its code. The wheel in the
        # build context, the gateway image and this machine hold the same code, or the build
        # fails. null = a built-in guard. plugins: every wheel built from gateway.plugins.
        "guard_plugin": guard_plugin,
        "plugins": plugins,
        # The embedding model of the testbed, pinned: revision and the SHA-256 of every file. null = none.
        "embedder": (embedder_spec(tb) or {}) | {
            "lock_sha256": embedding.file_sha256(embedding.LOCK),
            "wheels": {w.name: embedding.file_sha256(w) for w in sorted((out / "embedder" / "wheels").glob("*.whl"))},
        } if embedder_spec(tb) else None,
        "domains": [d.model_dump() for d in tb.domains],
        "servers": [{"id": s.id, "package": s.package, "path": s.path,
                     "source_sha256": tree_sha256(out / "context" / "servers" / s.id) if s.path else None,
                     "runtime": s.runtime,
                     "requested_version": s.version, "transport": s.transport,
                     "domain": s.domain, "label": s.label,
                     "attack": s.env.get("MCL_ATTACK"), "transform": s.env.get("MCL_TRANSFORM")}
                    # A server with a lock: the hash of the lock and of every wheel its image installs.
                    | ({"locked": locked[s.id]} if s.id in locked else {})
                    # A server that labels single tools: the map, so that a stored run is scored by it.
                    | ({"tool_labels": s.tool_labels} if s.tool_labels else {})
                    for s in tb.servers],
        "services": [{"id": v.id, "image": v.image, "builtin": v.builtin, "domain": v.domain, "label": v.label}
                     for v in tb.services],
        "canaries": len(canaries),
        # What a run of this build is scored against: decoy ids hold within one build only, so a run
        # names its lists by their hashes.
        "canaries_sha256": embedding.file_sha256(out / "canaries.json"),
        "decoys_sha256": embedding.file_sha256(out / "decoys.json") if (out / "decoys.json").is_file() else None,
        "warnings": loaded.warnings,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
    return out
