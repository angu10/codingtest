"""Seeded synthetic records and deterministic fault cases for Meridian CU."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

#: Every SSN here is in the 666-xx-xxxx block, which the SSA has never issued and never will. The
#: data is therefore provably synthetic while still matching a real PII detector — which is the
#: point: a servicing console with no regulated data on screen would not exercise the controls that
#: exist to protect it.
_SEED_MEMBERS: dict[str, dict[str, Any]] = {
    "12345": {
        "name": "Jordan Rivera",
        "ssn": "666-41-2810",
        "date_of_birth": "1984-03-11",
        "accounts": [
            {"id": "sav-71", "kind": "Savings Account", "balance": "1250.00"},
            {"id": "chk-19", "kind": "Checking Account", "balance": "438.21"},
        ],
        "sub_accounts": [],
    },
    "58431": {
        "name": "Morgan Chen",
        "ssn": "666-19-4472",
        "date_of_birth": "1979-11-02",
        "accounts": [
            {"id": "sav-42", "kind": "Savings Account", "balance": "9876.54"},
            {"id": "chk-83", "kind": "Checking Account", "balance": "732.10"},
        ],
        "sub_accounts": [],
    },
    "55501": {
        "name": "Restricted Synthetic Record",
        "ssn": "666-55-0001",
        "date_of_birth": "1990-01-15",
        "accounts": [],
        "sub_accounts": [],
        "restricted": True,
    },
    "55502": {
        "name": "Taylor Delay",
        "ssn": "666-55-0002",
        "date_of_birth": "1988-07-23",
        "accounts": [{"id": "sav-52", "kind": "Savings Account", "balance": "600.00"}],
        "sub_accounts": [],
        "delay": True,
    },
    "55503": {
        "name": "Casey Session",
        "ssn": "666-55-0003",
        "date_of_birth": "1975-05-09",
        "accounts": [{"id": "sav-53", "kind": "Savings Account", "balance": "700.00"}],
        "sub_accounts": [],
        "session_expired": True,
    },
    "55504": {
        "name": "Avery Modal",
        "ssn": "666-55-0004",
        "date_of_birth": "1992-12-30",
        "accounts": [{"id": "sav-54", "kind": "Savings Account", "balance": "800.00"}],
        "sub_accounts": [],
        "maintenance_modal": True,
    },
    "55505": {
        "name": "Riley Ambiguous",
        "ssn": "666-55-0005",
        "date_of_birth": "1968-02-14",
        "accounts": [{"id": "sav-55", "kind": "Savings Account", "balance": "900.00"}],
        "sub_accounts": [],
        "ambiguous_create": True,
    },
    "55506": {
        "name": "Drew Entitlement",
        "ssn": "666-55-0006",
        "date_of_birth": "1983-09-27",
        "accounts": [{"id": "sav-56", "kind": "Savings Account", "balance": "1000.00"}],
        "sub_accounts": [],
        "authorization_denied": True,
    },
}

MEMBERS = deepcopy(_SEED_MEMBERS)


def reset_members() -> None:
    """Restore deterministic state between automated test runs."""

    MEMBERS.clear()
    MEMBERS.update(deepcopy(_SEED_MEMBERS))

