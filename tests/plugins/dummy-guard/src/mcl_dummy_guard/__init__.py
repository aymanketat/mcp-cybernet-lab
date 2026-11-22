"""A guard plugin used by the lab's tests: after a result from a private server, no call to a
public server is allowed for the rest of the task.

It is not a useful guard (it blocks harmless calls). It exists to exercise what a plugin gets:
the guard interface, state kept between a result and a later call, and the context, where it
reads the label of each server and the ID of the call in hand. It imports only the standard
library and mcp_cybernet_lab.guards, as every plugin must.
"""

from __future__ import annotations

from mcp_cybernet_lab.guards import Guard


class DummyGuard(Guard):
    name = "dummy"

    def reset(self) -> None:
        self.private_results: list[str] = []  # IDs of the calls whose results came from a private server

    def on_result(self, server, tool, arguments, text) -> None:
        if self.context.label(server) == "private":
            self.private_results.append(self.context.call_id)

    def on_call(self, server, tool, arguments) -> tuple[bool, str]:
        if self.context.label(server) == "public" and self.private_results:
            return False, (f"call {self.context.call_id} to public server {server} after the result of "
                           f"call {self.private_results[0]} from a private server")
        return True, ""
