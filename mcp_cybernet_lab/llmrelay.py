#!/usr/bin/env python3
"""The LLM relay: the one way out for a client behind the gateway, and it leads to the model only.

    llmrelay.py --upstream api.deepseek.com:443 --server-name api.deepseek.com [--listen 0.0.0.0:443]

With a gateway, every client sits on the internal network net-gateway and has no route out, so
its agent cannot call a hosted model. For `llm.egress: relay`, `mcl build` adds two containers
that run this file:

  llm-relay    on net-gateway, where it carries the provider's host name as a network alias, so
               the agent's HTTPS request to the provider lands here; forwards to llm-egress
  llm-egress   outside net-gateway, on a network with a route out; forwards to the provider

Two containers, because Docker's DNS answers an alias for every container on that network,
including the one that carries it: a container that holds the provider's name cannot look up the
provider's real address.

What a relay does. It accepts TCP connections and copies bytes to ONE upstream, fixed on its
command line; a client cannot choose another destination. It does not terminate TLS: the agent's
TLS session is with the provider and is verified by the agent against the provider's
certificate, and the API key travels inside that session. The relay holds no key and sees
ciphertext only.

With --server-name, a connection is forwarded only if it opens with a TLS ClientHello that names
that host (SNI, sent in clear before encryption starts). Providers sit behind content-delivery
networks whose addresses answer TLS for many sites; without this check a client could open a
verified TLS session with any of them through the relay.

What it logs, on standard output, one JSON line per connection: a counter, the client address,
byte totals in each direction, the duration and any error. No payload is logged, and none could
be read; nor is any name a client asked for. Standard library only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import socket
import sys
import time

CONNECT_TIMEOUT_S = 15
HELLO_TIMEOUT_S = 15  # a client must send its ClientHello within this time
RECORD_MAX = 2 ** 14  # largest TLS record a client may send before encryption
CHUNK = 65536


class Refused(Exception):
    """The connection did not open the way the relay requires. The message never quotes the client."""


def emit(**event) -> None:
    event["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    print(json.dumps(event), flush=True)


def _int(data: bytes) -> int:
    return int.from_bytes(data, "big")


def server_name(record: bytes) -> str | None:
    """The host a TLS client asks for (SNI), read from its first record. None if it names none.

    Strict on purpose: the record must hold exactly one complete ClientHello with at most one
    server_name extension naming exactly one host. Anything else raises Refused, so the relay
    and the server behind it cannot read two different names in the same bytes.
    """
    if len(record) < 9 or record[0] != 0x16 or record[5] != 0x01:
        raise Refused("not a TLS ClientHello")
    hello = record[9:]
    if _int(record[3:5]) != len(record) - 5 or _int(record[6:9]) != len(hello):
        raise Refused("the ClientHello does not fill exactly one TLS record")
    name, seen = None, False
    try:
        p = 2 + 32                               # client version, random
        p += 1 + hello[p]                        # session id
        p += 2 + _int(hello[p:p + 2])            # cipher suites
        p += 1 + hello[p]                        # compression methods
        if p == len(hello):                      # no extensions at all
            return None
        end = p + 2 + _int(hello[p:p + 2])
        p += 2
        if end != len(hello):
            raise Refused("malformed ClientHello")
        while p < end:
            kind, size = _int(hello[p:p + 2]), _int(hello[p + 2:p + 4])
            p += 4
            if p + size > end:
                raise Refused("malformed ClientHello")
            if kind == 0:                        # server_name: a list of (type, length, name)
                data = hello[p:p + size]
                entry = data[2:]
                if seen or len(entry) < 3 or _int(data[:2]) != len(entry) \
                        or entry[0] != 0 or _int(entry[1:3]) != len(entry) - 3:
                    raise Refused("malformed server name")
                seen, name = True, entry[3:].decode("ascii").lower()
            p += size
    except (IndexError, UnicodeDecodeError):
        raise Refused("malformed ClientHello") from None
    return name


async def read_record(reader: asyncio.StreamReader) -> bytes:
    """The first TLS record of a connection, and not one byte more."""
    head = await reader.readexactly(5)
    if head[0] != 0x16 or _int(head[3:5]) > RECORD_MAX:
        raise Refused("not a TLS handshake")
    return head + await reader.readexactly(_int(head[3:5]))


async def pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, totals: dict, key: str) -> None:
    """Copy one direction until its sender closes, then close that direction on the other side."""
    try:
        while data := await reader.read(CHUNK):
            totals[key] += len(data)
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except OSError:
        writer.close()  # one side broke: end the other direction too


class Relay:
    def __init__(self, upstream: str, name: str | None = None, log=emit):
        self.upstream = upstream
        self.host, port = upstream.rsplit(":", 1)
        self.port = int(port)
        self.name = name.lower() if name else None
        self.log = log
        self.count = 0

    async def handle(self, creader: asyncio.StreamReader, cwriter: asyncio.StreamWriter) -> None:
        self.count += 1
        n, t0 = self.count, time.monotonic()
        client = (cwriter.get_extra_info("peername") or ["?"])[0]
        totals = {"bytes_to_upstream": 0, "bytes_to_client": 0}
        error, uwriter = None, None
        try:
            first = b""
            if self.name:
                first = await asyncio.wait_for(read_record(creader), HELLO_TIMEOUT_S)
                if server_name(first) != self.name:
                    raise Refused(f"the TLS server name is not {self.name}")
            ureader, uwriter = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), CONNECT_TIMEOUT_S)
            for w in (cwriter, uwriter):  # so a peer that vanished is noticed eventually
                w.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            uwriter.write(first)
            totals["bytes_to_upstream"] += len(first)
            await asyncio.gather(pump(creader, uwriter, totals, "bytes_to_upstream"),
                                 pump(ureader, cwriter, totals, "bytes_to_client"))
        except Refused as e:
            error = f"refused: {e}"
        except asyncio.CancelledError:
            error = "relay stopped"
            raise
        except Exception as e:  # timeout, name resolution, connection errors: the type says enough
            error = type(e).__name__
        finally:
            for w in (cwriter, uwriter):
                if w is not None:
                    w.close()
            self.log(event="connection", n=n, client=client, upstream=self.upstream, error=error,
                     seconds=round(time.monotonic() - t0, 3), **totals)


async def serve(listen: str, relay: Relay) -> None:
    host, port = listen.rsplit(":", 1)
    server = await asyncio.start_server(relay.handle, host, int(port))
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    relay.log(event="start", listen=listen, upstream=relay.upstream, server_name=relay.name)
    await stop.wait()
    server.close()
    relay.log(event="stop", connections=relay.count)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="TCP relay to one fixed upstream (the model API of a guarded testbed).")
    p.add_argument("--upstream", required=True, help="host:port, the only destination")
    p.add_argument("--listen", default="0.0.0.0:443", help="address:port to accept on")
    p.add_argument("--server-name", help="forward only TLS sessions that name this host (SNI)")
    args = p.parse_args(argv)
    asyncio.run(serve(args.listen, Relay(args.upstream, args.server_name)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
