"""The helpdesk's data: tickets customers wrote, and the replies sent to them. Standard library only.

    tickets.json   [{"id", "from", "subject", "body"}], read-only: what customers wrote
    replies.jsonl  one line per reply {"ticket", "text", "time"}, appended by reply_ticket

A ticket's text comes from outside the company. Whatever it asks for, it is a customer's message,
not an instruction of the user.
"""

from __future__ import annotations

import json
import time
from pathlib import Path


class NotFound(LookupError):
    """Shown to the agent as the tool's error."""


class Store:
    def __init__(self, tickets: str | Path, replies: str | Path):
        self.tickets, self.replies = Path(tickets), Path(replies)

    def _tickets(self) -> list[dict]:
        return json.loads(self.tickets.read_text(encoding="utf-8")) if self.tickets.is_file() else []

    def _replies(self) -> list[dict]:
        if not self.replies.is_file():
            return []
        return [json.loads(line) for line in self.replies.read_text(encoding="utf-8").splitlines() if line.strip()]

    def list_tickets(self) -> list[dict]:
        answered = {r["ticket"] for r in self._replies()}
        return [{"id": t["id"], "from": t["from"], "subject": t["subject"],
                 "status": "answered" if t["id"] in answered else "open"} for t in self._tickets()]

    def get_ticket(self, ticket_id: int) -> dict:
        for t in self._tickets():
            if t["id"] == ticket_id:
                return {**t, "replies": [r["text"] for r in self._replies() if r["ticket"] == ticket_id]}
        raise NotFound(f"no ticket {ticket_id}; list_tickets shows the ids")

    def reply_ticket(self, ticket_id: int, text: str) -> dict:
        ticket = self.get_ticket(ticket_id)
        self.replies.parent.mkdir(parents=True, exist_ok=True)
        with open(self.replies, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ticket": ticket_id, "text": text, "time": time.time()}, ensure_ascii=False) + "\n")
        return {"ticket": ticket_id, "sent_to": ticket["from"], "status": "answered"}
