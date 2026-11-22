#!/usr/bin/env python3
"""What a container of a guarded testbed can and cannot reach. The inside half of `mcl check-isolation`.

Mostly run in the client containers. With an embedder in the testbed it is also run, with another
role in its plan, in every server (which must not reach the embedder), in the embedder (which must
have no way out) and in the gateway (which must reach it).

`mcl check-isolation` sends this file to `python3 -` in each client container, so it is part of
no image, and describes the testbed in the environment variable MCL_NETPROBE (JSON, written by
isolation.plan). Prints one JSON line per check: {"name", "ok", "detail"}.

No tokens are spent: the model API is only asked for its model list. The API key is read from
the container's own environment and is never printed. Standard library only.
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import sys
import urllib.error
import urllib.request

PLAN = json.loads(os.environ.get("MCL_NETPROBE") or "{}")
MODEL = PLAN.get("model")
KEY = os.environ.get((MODEL or {}).get("key_env") or "", "")
FAILED = 0


def check(name: str, ok: bool, detail) -> None:
    global FAILED
    FAILED += not ok
    detail = str(detail)
    if KEY:
        detail = detail.replace(KEY, "[key]")
    print(json.dumps({"name": name, "ok": bool(ok), "detail": detail[:240]}), flush=True)


def why(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"


def default_routes(table: str) -> list[str]:
    """Interfaces that carry a default route, from the text of /proc/net/route."""
    rows = [line.split() for line in table.splitlines()[1:]]
    return [r[0] for r in rows if len(r) > 7 and r[1] == "00000000" and r[7] == "00000000"]


def resolve(name: str) -> tuple[list[str], str]:
    try:
        found = sorted({a[4][0] for a in socket.getaddrinfo(name, None, socket.AF_INET)})
        return found, f"resolves to {found}"
    except OSError as e:
        return [], why(e)


def connect(host: str, port: int, timeout: float = 4) -> tuple[str, str]:
    """('open' | 'refused' | 'unreachable', detail). 'refused' still means the address answered."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return "open", "connected"
    except ConnectionRefusedError as e:
        return "refused", why(e)
    except OSError as e:
        return "unreachable", why(e)


def tls(host: str, port: int, name: str) -> tuple[bool, str, str]:
    """(verified, detail, peer address) for a TLS handshake to host:port that asks for `name`."""
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=10) as s, \
                ctx.wrap_socket(s, server_hostname=name) as t:
            cert = t.getpeercert()
            issuer = dict(x[0] for x in cert["issuer"])
            names = [v for k, v in cert.get("subjectAltName", ()) if k == "DNS"]
            return True, (f"peer {t.getpeername()[0]}; {t.version()}; issuer CN={issuer.get('commonName')}, "
                          f"O={issuer.get('organizationName')}; certificate names {names[:3]}"), t.getpeername()[0]
    except (OSError, ValueError) as e:
        return False, why(e), ""


def model_api() -> tuple[bool, str]:
    """GET <endpoint>/models: answers 200 and lists the testbed's model. Asks for no completion."""
    headers = {"Accept": "application/json"}
    if KEY:
        headers["Authorization"] = f"Bearer {KEY}"
        if MODEL.get("provider") == "anthropic":  # its model list wants the key in its own header
            headers |= {"x-api-key": KEY, "anthropic-version": "2023-06-01"}
    try:
        with urllib.request.urlopen(urllib.request.Request(MODEL["url"], headers=headers), timeout=20) as r:
            body = json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:  # the body may echo the key: report the status only
        hint = f"; is {MODEL.get('key_env')} set for `mcl up`?" if e.code in (401, 403) else ""
        return False, f"HTTP {e.code}{hint}"
    except (OSError, ValueError) as e:
        return False, why(e)
    ids = [m.get("id") for m in body.get("data", []) if isinstance(m, dict)] if isinstance(body, dict) else []
    listed = MODEL["name"] in ids
    return listed or not ids, f"HTTP 200; {len(ids)} models listed; '{MODEL['name']}' listed: {listed}"


def closed_targets() -> None:
    """Every target of PLAN["closed"] neither resolves nor answers at its addresses."""
    for target in PLAN["closed"]:
        found, detail = resolve(target["host"])
        check(f"{target['what']} `{target['host']}` does not resolve", not found, detail)
        if not target["addresses"]:
            check(f"{target['what']} `{target['host']}` by address is unreachable", False,
                  "its container has no address (not running?), so nothing was tested")
        for address in target["addresses"]:
            state, detail = connect(address, target["port"])
            check(f"{target['what']} `{target['host']}` by address ({address}:{target['port']}) is unreachable",
                  state == "unreachable", detail)


def no_way_out(who: str) -> None:
    """No default route, and no outside host by name or by address."""
    outside = PLAN["outside"]
    with open("/proc/net/route", encoding="ascii") as f:
        routes = default_routes(f.read())
    check(f"no default route in the {who}", not routes, f"default route on {routes}" if routes else "none")
    found, detail = resolve(outside["name"])
    check(f"{outside['name']} does not resolve", not found, detail)
    for address in filter(None, (outside.get("address"), outside["public_address"])):
        state, detail = connect(address, 443)
        check(f"an outside address ({address}:443) is unreachable", state == "unreachable", detail)


def embedder_answers() -> None:
    """The gateway's side: the embedder answers, and is the model of this testbed."""
    want = PLAN["embedder"]
    try:
        with urllib.request.urlopen(want["url"] + "/health", timeout=10) as r:
            got = json.loads(r.read())
        same = (got.get("model"), got.get("revision")) == (want["model"], want["revision"])
        check("the embedder answers the gateway with the testbed's model", same,
              f"{got.get('model')} at {str(got.get('revision'))[:12]}, {len(got.get('files') or {})} files verified")
    except (OSError, ValueError) as e:
        check("the embedder answers the gateway with the testbed's model", False, why(e))


def main() -> int:
    role = PLAN.get("role", "client")
    if role == "server":       # a server must not reach the embedder
        closed_targets()
        return 1 if FAILED else 0
    if role == "embedder":     # the embedder has no way out
        no_way_out("embedder")
        return 1 if FAILED else 0
    if role == "gateway":      # and only the gateway reaches it
        embedder_answers()
        return 1 if FAILED else 0
    relay = PLAN.get("relay")
    outside = PLAN["outside"]

    with open("/proc/net/route", encoding="ascii") as f:
        routes = default_routes(f.read())
    check("no default route in the client", not routes, f"default route on {routes}" if routes else "none")

    # --- the model must answer ---------------------------------------------------------
    if MODEL and relay:
        host, port = MODEL["host"], MODEL["port"]
        found, detail = resolve(host)
        check(f"{host} resolves to the relay", bool(found) and set(found) <= set(relay["addresses"]),
              f"{detail} (relay {relay['addresses']})")
        ok, detail, peer = tls(host, port, host)
        check(f"TLS to {host} verifies end to end through the relay", ok and peer in relay["addresses"], detail)
    if MODEL:
        ok, detail = model_api()
        check("model API answers (GET /models with the key, no tokens)", ok, detail)

    # --- nothing else may answer ---------------------------------------------------------
    name = outside["name"]
    found, detail = resolve(name)
    check(f"{name} does not resolve", not found, detail)
    if outside.get("address"):
        state, detail = connect(outside["address"], 443)
        check(f"{name} by address ({outside['address']}:443) is unreachable", state == "unreachable", detail)
    else:
        check(f"{name} by address is unreachable", False, "the host could not resolve it, so nothing was tested")
    state, detail = connect(outside["public_address"], 443)
    check(f"a public address ({outside['public_address']}:443) is unreachable", state == "unreachable", detail)
    try:
        urllib.request.urlopen(f"https://{name}/", timeout=6)
        check(f"https://{name}/ does not answer", False, "answered")
    except (OSError, ValueError) as e:
        check(f"https://{name}/ does not answer", True, why(e))
    closed_targets()

    # --- the relay offers one port and one destination ---------------------------------
    for address in (relay or {}).get("addresses", []):
        for port in (80, 8080):
            state, detail = connect(address, port)
            check(f"relay port {port} is closed", state != "open", detail)
        ok, detail, _ = tls(address, MODEL["port"], name)
        check(f"relay cannot be used to reach another host (TLS for {name} via the relay)", not ok,
              "handshake verified" if ok else detail)

    # --- the gateway is still the client's only MCP path ---------------------------------
    try:
        urllib.request.urlopen(PLAN["gateway"], timeout=6)
        check("gateway reachable", True, "HTTP 200")
    except urllib.error.HTTPError as e:
        check("gateway reachable", True, f"HTTP {e.code} for a bare GET")
    except (OSError, ValueError) as e:
        check("gateway reachable", False, why(e))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
