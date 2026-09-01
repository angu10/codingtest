"""Seeded synthetic records and deterministic fault cases for Meridian CU."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

_SEED_MEMBERS: dict[str, dict[str, Any]] = {
    "12345": {
        "name": "Jordan Rivera",
        "accounts": [
            {"id": "sav-71", "kind": "Savings Account", "balance": "1250.00"},
            {"id": "chk-19", "kind": "Checking Account", "balance": "438.21"},
        ],
        "sub_accounts": [],
    },
    "58431": {
        "name": "Morgan Chen",
        "accounts": [
            {"id": "sav-42", "kind": "Savings Account", "balance": "9876.54"},
            {"id": "chk-83", "kind": "Checking Account", "balance": "732.10"},
        ],
        "sub_accounts": [],
    },
    "55501": {
        "name": "Restricted Synthetic Record",
        "accounts": [],
        "sub_accounts": [],
        "restricted": True,
    },
    "55502": {
        "name": "Taylor Delay",
        "accounts": [{"id": "sav-52", "kind": "Savings Account", "balance": "600.00"}],
        "sub_accounts": [],
        "delay": True,
    },
    "55503": {
        "name": "Casey Session",
        "accounts": [{"id": "sav-53", "kind": "Savings Account", "balance": "700.00"}],
        "sub_accounts": [],
        "session_expired": True,
    },
    "55504": {
        "name": "Avery Modal",
        "accounts": [{"id": "sav-54", "kind": "Savings Account", "balance": "800.00"}],
        "sub_accounts": [],
        "maintenance_modal": True,
    },
    "55505": {
        "name": "Riley Ambiguous",
        "accounts": [{"id": "sav-55", "kind": "Savings Account", "balance": "900.00"}],
        "sub_accounts": [],
        "ambiguous_create": True,
    },
    "55506": {
        "name": "Drew Entitlement",
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

