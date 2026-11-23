"""The CRM's data: customers and contracts, read from one JSON file. Standard library only.

The file is written by `mcl build` into the server's data folder (a server with `plant: crm`):

    {"customers": [{"customer_id", "name_ar", "name_en", "national_id", "record_ar"}],
     "contracts": [{"contract", "language", "company", "summary"}],
     "settings":  [{"name", "value"}]}

Without a file the CRM is empty.
"""

from __future__ import annotations

import json
from pathlib import Path


class NotFound(LookupError):
    """Shown to the agent as the tool's error."""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _data(self) -> dict:
        if not self.path.is_file():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def list_customers(self) -> list[dict]:
        return [{k: c[k] for k in ("customer_id", "name_en", "name_ar")} for c in self._data().get("customers", [])]

    def get_customer(self, customer_id: int) -> dict:
        for c in self._data().get("customers", []):
            if c["customer_id"] == customer_id:
                return c
        raise NotFound(f"no customer {customer_id}; list_customers shows the ids")

    def list_contracts(self) -> list[dict]:
        return [{k: c[k] for k in ("contract", "language", "company")} for c in self._data().get("contracts", [])]

    def get_contract(self, contract: int) -> dict:
        for c in self._data().get("contracts", []):
            if c["contract"] == contract:
                return c
        raise NotFound(f"no contract {contract}; list_contracts shows the numbers")

    def get_settings(self) -> list[dict]:
        return self._data().get("settings", [])
