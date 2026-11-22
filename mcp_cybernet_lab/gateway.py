#!/usr/bin/env python3
"""The gateway: every MCP request from a client passes here, is logged, and is checked by a guard.

    gateway.py serve                      (container command)
    gateway.py ctl start <log path>       start a task: reset the guard, reload the policy, log here
    gateway.py ctl stop                   end the task (log closed)

Clients reach server <id> at http://gateway:8080/<id>/mcp. The gateway forwards to
http://<id>:8080/mcp, keeping the client's headers (so per-domain tokens still apply).
A blocked tool call is answered with a JSON-RPC error, code -32001, and never reaches the server.

An allowed call that gets no answer from its server (the server is down, or breaks off) also has
its line: decision "allow", `upstream_error` with what went wrong, no result, seq_result null. The
client gets HTTP 502. So every call that was put to the guard is in the log, whatever became of it.

A result that returns after the next task has started belongs to no guard any more: its line is
written into the log that is open, without call ID and step numbers, and replay does not apply it.

A guard that raises fails closed, and the line says so in `guard_error` (null otherwise). Raised
on the call: the call is blocked, with the reason "guard error". Raised on the result: the tool has
run, the decision stays "allow", the result is kept in the log and withheld from the client
(`result_withheld` true), which gets a JSON-RPC error, code -32002. That code is not the code of a
block: the call did reach its server, and the agent's trace must not record it as blocked.

Control runs on 127.0.0.1:9090, inside the container only: agents and servers cannot reach it.
Configuration: MCL_GATEWAY = {"guard": ..., "servers": {id: {"label", "domain"}}},
policy file /policy/current.json.

The log has one line per tool call, written when the call is blocked or when its result returns.
Each carries the server's label, a call ID (c1, c2, ... per task) and the numbers of its two guard
steps, seq_call and seq_result (see guards.Session): calls are served on threads, so the order of
the lines is not always the order in which the guard saw calls and results. seq_result is null
when no result was put to the guard: a blocked call, an error result, no answer.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from guards import Context, Session, guard_info, make_guard  # noqa: E402
from httpproxy import Proxy, jsonrpc_error, parse_messages, serve  # noqa: E402
from mcpclient import result_text  # noqa: E402

CONFIG = json.loads(os.environ.get("MCL_GATEWAY", '{"guard": "passthrough", "servers": {}}'))
POLICY_FILE = Path(os.environ.get("MCL_POLICY_FILE", "/policy/current.json"))
BLOCKED = -32001
WITHHELD = -32002


class State:
    lock = threading.Lock()
    guard = None
    session = None  # the guard steps of the current task, numbered
    plugins: list = []  # where the guard comes from, when it is a plugin
    log = None
    task = None
    t0 = time.monotonic()


def load_guard():
    policy = json.loads(POLICY_FILE.read_text()) if POLICY_FILE.exists() else {}
    State.guard = make_guard(CONFIG["guard"], policy, Context(labels()))
    State.session = Session(State.guard)
    info = guard_info(CONFIG["guard"])
    State.plugins = [info] if info else []
    return policy.get("version")


def labels() -> dict:
    return {server: spec.get("label") for server, spec in CONFIG["servers"].items()}


def write(**event):
    if State.log:
        event.update(t=round(time.monotonic() - State.t0, 3), task=State.task)
        State.log.write(json.dumps(event, ensure_ascii=False) + "\n")
        State.log.flush()


class Gateway(Proxy):
    def route(self):
        parts = self.path.split("/", 2)  # "", "<server>", "mcp..."
        if len(parts) < 3 or parts[1] not in CONFIG["servers"]:
            return None
        self.server_id = parts[1]
        return parts[1], 8080, "/" + parts[2]

    def before(self, body):
        try:
            msg = json.loads(body)
        except json.JSONDecodeError:
            return None
        if not isinstance(msg, dict) or msg.get("method") != "tools/call":
            if isinstance(msg, dict) and msg.get("method"):
                write(type="message", server=self.server_id, method=msg["method"])
            return None
        params = msg.get("params") or {}
        tool, args = params.get("name"), params.get("arguments") or {}
        with State.lock:
            step = State.session.call(self.server_id, tool, args)
            # One handler serves one request: the call waits here for its own result. The task's
            # session is kept with it, so that a result which returns after the next task has
            # started is recognised, whatever its request ID, and is not put to the new guard.
            self.waiting = (tool, args, step, State.session)
        if step["allowed"]:
            return None
        reason = step["reason"]
        write(type="tool_call", server=self.server_id, tool=tool, arguments=args,
              decision="block", reason=reason, result_text=None, call_id=step["call_id"],
              label=CONFIG["servers"][self.server_id].get("label"), seq_call=step["seq_call"], seq_result=None,
              guard_error=step["error"], result_withheld=False)
        # What the guard raised goes to the log only: the text of an exception may hold what it was reading.
        return jsonrpc_error(msg.get("id"), BLOCKED, f"blocked: {reason}", {"blocked": True})

    def own_call(self, msg):
        """(tool, arguments, the step of this request's call), under the lock. The step is None
        when a new task has started since the call was put to the guard: that guard is gone."""
        tool, args, step, session = getattr(self, "waiting", None) or (None, {}, None, None)
        if step is None or session is not State.session:
            params = msg.get("params") or {}
            return params.get("name"), params.get("arguments") or {}, None
        return tool, args, step

    def failed(self, body, error):
        """An allowed call that got no response: log it, so that its seq_call is in the log."""
        try:
            msg = json.loads(body)
        except json.JSONDecodeError:
            return
        if not isinstance(msg, dict) or msg.get("method") != "tools/call":
            return
        with State.lock:
            tool, args, step = self.own_call(msg)
        write(type="tool_call", server=self.server_id, tool=tool, arguments=args, decision="allow",
              reason="", result_text=None, is_error=None, http_status=502,
              call_id=step and step["call_id"], label=CONFIG["servers"][self.server_id].get("label"),
              seq_call=step and step["seq_call"], seq_result=None, guard_error=None, result_withheld=False,
              upstream_error=error)

    def after(self, body, status, ctype, resp):
        try:
            msg = json.loads(body)
        except json.JSONDecodeError:
            return None
        if not isinstance(msg, dict) or msg.get("method") != "tools/call":
            return None
        with State.lock:
            tool, args, step = self.own_call(msg)
            text, is_error = None, None
            for m in parse_messages(resp, ctype):
                if m.get("id") == msg.get("id"):
                    if "result" in m:
                        text, is_error = result_text(m["result"]), bool(m["result"].get("isError"))
                    elif "error" in m:
                        text, is_error = f"error: {m['error'].get('message')}", True
            seen = State.session.result(step["call_id"], self.server_id, tool, args, text, is_error) \
                if step else {"seq_result": None, "error": None}
        write(type="tool_call", server=self.server_id, tool=tool, arguments=args, decision="allow",
              reason="", result_text=text, is_error=is_error, http_status=status,
              call_id=step and step["call_id"], label=CONFIG["servers"][self.server_id].get("label"),
              seq_call=step and step["seq_call"], seq_result=seen["seq_result"],
              guard_error=seen["error"], result_withheld=bool(seen["error"]))
        if seen["error"]:
            return jsonrpc_error(msg.get("id"), WITHHELD, "result withheld: guard error", {"withheld": True})
        return None


class Control(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        cmd = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        with State.lock:
            if State.log:
                State.log.close()
                State.log = None
            if self.path == "/start":
                version = load_guard()
                path = Path(cmd["log"])
                path.parent.mkdir(parents=True, exist_ok=True)
                State.log, State.task, State.t0 = open(path, "w", encoding="utf-8"), cmd.get("task"), time.monotonic()
                write(type="start", guard=CONFIG["guard"], policy_version=version,
                      labels=labels(), plugins=State.plugins)
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def ctl(args):
    if args[0] == "start":
        data = {"log": args[1], "task": args[2] if len(args) > 2 else None}
        path = "/start"
    else:
        data, path = {}, "/stop"
    req = urllib.request.Request(f"http://127.0.0.1:9090{path}", data=json.dumps(data).encode(), method="POST")
    print(urllib.request.urlopen(req, timeout=10).read().decode())


def main():
    if sys.argv[1:2] == ["ctl"]:
        return ctl(sys.argv[2:])
    load_guard()
    threading.Thread(target=serve(Control, 9090, "127.0.0.1").serve_forever, daemon=True).start()
    print(f"mcl-gateway: guard {CONFIG['guard']}, servers {sorted(CONFIG['servers'])}", flush=True)
    serve(Gateway, 8080).serve_forever()


if __name__ == "__main__":
    main()
