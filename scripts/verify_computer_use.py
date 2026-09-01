"""Settle the one thing that could not be verified offline: the computer-use beta header.

The tool type `computer_20251124` is confirmed against the installed SDK. The matching beta header
is not in the SDK's `AnthropicBetaParam` literal, so this probes the live API with each candidate
and reports which one the API actually accepts.

Costs a handful of tokens: `max_tokens=1`, one word of input, no image.

    conda run -n codingtest python scripts/verify_computer_use.py
"""

from __future__ import annotations

import asyncio
import sys

from interface_cua.config import load_env, workspace_headers
from interface_cua.discovery.model import (
    COMPUTER_TOOL_NAME,
    COMPUTER_TOOL_TYPE,
    MODEL_ID,
    build_tools,
)

CANDIDATES = [
    ("computer-use-2025-11-24",),  # documented default
    ("computer-use-2025-01-24",),  # newest value present in the SDK literal
    (),                            # maybe the beta namespace alone is enough
]


async def probe(client, betas: tuple[str, ...]) -> tuple[bool, str]:
    try:
        await client.beta.messages.create(
            model=MODEL_ID,
            max_tokens=1,
            betas=list(betas),
            tools=build_tools((1280, 800)),
            messages=[{"role": "user", "content": "ok"}],
        )
        return True, "accepted"
    except Exception as exc:  # noqa: BLE001 - reporting every rejection is the point
        return False, f"{type(exc).__name__}: {str(exc)[:300]}"


async def main() -> int:
    load_env()
    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        print("anthropic SDK not installed", file=sys.stderr)
        return 2

    headers = workspace_headers()
    client = AsyncAnthropic(default_headers=headers)
    print(f"model={MODEL_ID}  tool={COMPUTER_TOOL_TYPE}  name={COMPUTER_TOOL_NAME}")
    print(f"workspace header: {'set' if headers else 'ABSENT'}\n")

    # Prove auth works at all before attributing any failure to the beta header.
    try:
        await client.models.retrieve(MODEL_ID)
        print(f"auth OK: {MODEL_ID} is reachable\n")
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        print(f"auth FAILED before any beta was tested:\n  {type(exc).__name__}: {text[:300]}\n")
        if not headers:
            print(
                "Set ANTHROPIC_WORKSPACE_ID in .env — an identity-linked key must name the\n"
                "workspace it acts in. List them with:\n"
                "  curl -s https://api.anthropic.com/v1/organizations/workspaces \\\n"
                "    -H \"x-api-key: $ANTHROPIC_API_KEY\" -H 'anthropic-version: 2023-06-01'\n"
                "Use the `id` field (wrkspc_...), NOT `compartment_id` (a bare UUID)."
            )
        elif "not found" in text.lower():
            print(
                "The workspace ID is well-formed but its backing compartment does not resolve for\n"
                "this key. Seen when the only workspace is the one auto-created for a Claude Code\n"
                "subscription: it lists via the Admin API but is not entitled to direct Messages\n"
                "API calls. Use a standard API key issued for an API-enabled workspace."
            )
        return 2

    accepted: list[tuple[str, ...]] = []
    for betas in CANDIDATES:
        label = ", ".join(betas) or "(no beta header)"
        ok, detail = await probe(client, betas)
        print(f"  {'PASS' if ok else 'FAIL'}  {label}\n        {detail}\n")
        if ok:
            accepted.append(betas)

    if not accepted:
        print("No candidate accepted — check the live computer-use docs for the current header.")
        return 1
    print(f"Use: betas={accepted[0]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
