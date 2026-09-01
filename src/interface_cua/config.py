"""Minimal `.env` loading for the discovery path.

Deliberately not `python-dotenv`: this needs to read one file with one shape, and the repo is
scored partly on being easy to run. It is called only from the discovery path — replay must keep
working with no key and no `.env` at all.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_FILE = ".env"


def load_env(start: Path | None = None) -> dict[str, str]:
    """Load `KEY=value` pairs from the nearest `.env`, walking up from `start`.

    Existing environment variables win — an explicitly exported key is more specific than a file.
    Returns the names that were loaded, never the values.
    """

    path = _find(start or Path.cwd())
    if path is None:
        return {}
    loaded: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if name and name not in os.environ:
            os.environ[name] = value
            loaded[name] = value
    return loaded


def workspace_headers() -> dict[str, str]:
    """Headers a first-party client needs beyond the API key.

    An *identity-linked* API key must name the workspace it acts in, and the SDK only resolves
    `ANTHROPIC_WORKSPACE_ID` automatically on the federation path — for a plain client it has to be
    sent explicitly. Absent for ordinary keys, so this returns `{}` and changes nothing.
    """

    workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
    return {"anthropic-workspace-id": workspace} if workspace else {}


def _find(start: Path) -> Path | None:
    for directory in (start, *start.parents):
        candidate = directory / ENV_FILE
        if candidate.is_file():
            return candidate
    return None
