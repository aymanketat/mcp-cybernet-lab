"""A guard plugin used by the lab's tests: after a result from a private server, no call to a
public server is allowed for the rest of the task.

It is not a useful guard (it blocks harmless calls). It exists to exercise what a plugin gets:
the guard interface, state kept between a result and a later call, and the context, where it
reads the label of each server and the ID of the call in hand. It imports only the standard
library and mcp_cybernet_lab.guards, as every plugin must.

Two policy keys make it fail on purpose, to test what the gateway does with a guard that raises:
`raise_in_on_call` and `raise_in_on_result`, each a list of tool names.

A second guard, `dummy-embed`, exercises `context.embed`: it blocks a call to a public server whose
text is similar to a result a private server returned. It is no more useful than the first (the
calibration shows what similarity of whole texts cannot tell apart); it exists to test the path.
"""

from __future__ import annotations

from mcp_cybernet_lab.guards import Guard, cosine


class DummyGuard(Guard):
    name = "dummy"

    def reset(self) -> None:
        self.private_results: list[str] = []  # IDs of the calls whose results came from a private server

    def on_result(self, server, tool, arguments, text) -> None:
        if tool in self.policy.get("raise_in_on_result", []):
            raise RuntimeError(f"dummy guard told to fail on the result of {tool}")
        if self.context.label(server) == "private":
            self.private_results.append(self.context.call_id)

    def on_call(self, server, tool, arguments) -> tuple[bool, str]:
        if tool in self.policy.get("raise_in_on_call", []):
            raise RuntimeError(f"dummy guard told to fail on a call of {tool}")
        if self.context.label(server) == "public" and self.private_results:
            return False, (f"call {self.context.call_id} to public server {server} after the result of "
                           f"call {self.private_results[0]} from a private server")
        return True, ""


class DummyEmbedGuard(Guard):
    """Blocks a public call whose text is as similar to an earlier private result as the policy's
    `similar_at` (a cosine, 0.8 if not set). It catches nothing: a text the model refuses as too
    long, or an embedder that does not answer, makes the guard raise, and so fail closed."""
    name = "dummy-embed"

    def reset(self) -> None:
        self.private: list[tuple] = []  # (call ID, vector) of every result from a private server

    def on_result(self, server, tool, arguments, text) -> None:
        if self.context.label(server) == "private":
            self.private.append((self.context.call_id, self.context.embed([text])[0]))

    def on_call(self, server, tool, arguments) -> tuple[bool, str]:
        if self.context.label(server) != "public" or not self.private:
            return True, ""
        text = " ".join(str(v) for v in arguments.values())
        vector = self.context.embed([text])[0]
        score, call_id = max((cosine(vector, seen), earlier) for earlier, seen in self.private)
        if score >= self.policy.get("similar_at", 0.8):
            return False, (f"call {self.context.call_id} to public server {server} is similar ({score:.3f}) "
                           f"to the result of call {call_id} from a private server")
        return True, ""
