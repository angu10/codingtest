"""Command-line entry point for deterministic replay.

This is the production-shaped way to invoke a capability: an artifact, typed arguments, and a
structured result on stdout. No model is constructed anywhere in this path — running it with
``ANTHROPIC_API_KEY`` unset is the point, not an accident.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from playwright.async_api import async_playwright

from interface_cua.handoff.lease import SessionLease
from interface_cua.observability.evidence import EvidenceWriter
from interface_cua.policy.engine import PolicyConfig, PolicyEngine
from interface_cua.replay.executor import ReplayExecutor
from interface_cua.schema.artifact import Capability, ValueType
from interface_cua.schema.result import ReplayResult
from interface_cua.surface.playwright_surface import VIEWPORT, PlaywrightSurface

#: Exit codes are part of the contract: a declared business outcome is a successful invocation of
#: the capability, not a failed one. Only states needing an operator are non-zero.
EXIT_CODES = {
    "success": 0,
    "business_outcome": 0,
    "needs_human": 2,
    "unknown_side_effect": 3,
    "validation_required": 4,
    "failure": 1,
}


def _parse_inputs(pairs: list[str], artifact: Capability) -> dict[str, object]:
    """Turn ``name=value`` pairs into arguments typed the way the artifact declares them."""

    declared = {spec.name: spec for spec in artifact.inputs}
    arguments: dict[str, object] = {}
    for pair in pairs:
        name, separator, raw = pair.partition("=")
        if not separator:
            raise SystemExit(f"--input expects name=value, got: {pair!r}")
        spec = declared.get(name)
        if spec is None:
            raise SystemExit(
                f"{name!r} is not an input of {artifact.capability.id}; "
                f"declared inputs are {sorted(declared)}"
            )
        if spec.type == ValueType.INTEGER:
            arguments[name] = int(raw)
        elif spec.type == ValueType.BOOLEAN:
            arguments[name] = raw.lower() in {"1", "true", "yes"}
        else:
            arguments[name] = raw
    return arguments


async def replay(args: argparse.Namespace) -> ReplayResult:
    artifact = Capability.from_yaml(args.artifact)
    arguments = _parse_inputs(args.input, artifact)
    run_id = args.run_id or f"replay_{datetime.now(UTC):%Y%m%dT%H%M%SZ}_{artifact.capability.id}"

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=not args.headed)
        page = await browser.new_page(viewport={"width": VIEWPORT[0], "height": VIEWPORT[1]})
        surface = PlaywrightSurface(page, SessionLease())

        evidence = None
        if args.evidence is not None:
            evidence = EvidenceWriter(
                args.evidence,
                run_id,
                sensitive_values=frozenset(str(v) for v in arguments.values()),
            )
            await surface.start_trace()

        await page.goto(args.base_url)
        executor = ReplayExecutor(
            surface,
            PolicyEngine(PolicyConfig(allowed_origins=frozenset({args.base_url.rstrip("/")}))),
            application_family=args.family,
            application_version=args.app_version,
            allow_draft=args.allow_draft,
            evidence=evidence,
        )
        try:
            result = await executor.execute(artifact, arguments)
        finally:
            await browser.close()
        if evidence is not None:
            print(f"evidence: {evidence.dir}", file=sys.stderr)
        return result


async def discover(args: argparse.Namespace) -> int:
    """Drive the app with a real model, then compile what it did into a draft artifact.

    This is the only command that needs an API key. Everything it produces — the artifact, the
    events, the screenshots — is then usable with no model at all.
    """

    from interface_cua.discovery.compiler import compile_run
    from interface_cua.discovery.model import MODEL_ID, ClaudeDiscoveryModel
    from interface_cua.discovery.orchestrator import DiscoveryOrchestrator, DiscoveryOutcome

    inputs = dict(pair.split("=", 1) for pair in args.input)
    run_id = args.run_id or f"disc_{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    evidence = EvidenceWriter(
        args.evidence, run_id, sensitive_values=frozenset(inputs.values())
    )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=not args.headed)
        page = await browser.new_page(viewport={"width": VIEWPORT[0], "height": VIEWPORT[1]})
        await page.goto(args.base_url)
        surface = PlaywrightSurface(page, SessionLease())
        await surface.start_trace()

        orchestrator = DiscoveryOrchestrator(
            surface=surface,
            page=page,
            model=ClaudeDiscoveryModel(viewport=VIEWPORT, goal=args.goal),
            policy=PolicyEngine(
                PolicyConfig(allowed_origins=frozenset({args.base_url.rstrip("/")}))
            ),
            evidence=evidence,
            goal=args.goal,
            max_steps=args.max_steps,
        )
        try:
            run = await orchestrator.run()
        finally:
            await surface.save_trace(evidence.dir / "trace.zip")
            await browser.close()

    print(f"outcome: {run.outcome.value}  steps: {len(run.steps)}  detail: {run.detail}")
    print(f"evidence: {evidence.dir}")
    if run.outcome is not DiscoveryOutcome.FINISHED:
        print("not compiling: only a finished run becomes a capability", file=sys.stderr)
        return 1

    artifact = compile_run(
        run,
        capability_id=args.capability_id,
        description=args.goal,
        inputs=inputs,
        application_family=args.family,
        application_version=args.app_version,
        entry_landmarks=args.landmark or ["Member Search"],
        model_id=MODEL_ID,
        operator=args.operator,
    )
    artifact.to_yaml(args.out)
    print(f"artifact: {args.out}  (draft — a human declares outcomes and risk before approval)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="interface-cua", description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    find = subcommands.add_parser("discover", help="Drive the app with a model and compile it.")
    find.add_argument("--goal", required=True)
    find.add_argument("--input", action="append", default=[], metavar="NAME=VALUE")
    find.add_argument("--out", type=Path, required=True, help="Where to write the artifact.")
    find.add_argument("--capability-id", default="discovered-capability")
    find.add_argument("--base-url", default="http://127.0.0.1:8000")
    find.add_argument("--family", default="meridian-cu")
    find.add_argument("--app-version", default="demo-v1")
    find.add_argument("--landmark", action="append", default=[])
    find.add_argument("--evidence", type=Path, default=Path("evidence"))
    find.add_argument("--run-id", default=None)
    find.add_argument("--operator", default="discovery-cli")
    find.add_argument("--max-steps", type=int, default=25)
    find.add_argument("--headed", action="store_true")
    find.set_defaults(handler=discover, discovery=True)

    run = subcommands.add_parser("replay", help="Replay a capability artifact deterministically.")
    run.add_argument("artifact", type=Path, help="Path to a capability YAML artifact.")
    run.add_argument(
        "--input",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="A capability input. Repeat for each one.",
    )
    run.add_argument("--base-url", default="http://127.0.0.1:8000")
    run.add_argument("--family", default="meridian-cu")
    run.add_argument("--app-version", default="demo-v1")
    run.add_argument(
        "--headed",
        action="store_true",
        help="Show the browser window — the same session a human would take over.",
    )
    run.add_argument(
        "--allow-draft",
        action="store_true",
        help="Replay an unapproved artifact. Development only; the gate exists for a reason.",
    )
    run.add_argument(
        "--evidence",
        type=Path,
        default=None,
        metavar="DIR",
        help="Write events.jsonl, screenshots, a Playwright trace, and a failure bundle under DIR.",
    )
    run.add_argument("--run-id", default=None, help="Name the run directory (default: timestamped).")
    return run.set_defaults(handler=replay) or parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    outcome = asyncio.run(args.handler(args))
    if getattr(args, "discovery", False):
        return int(outcome)
    json.dump(outcome.model_dump(mode="json"), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return EXIT_CODES[outcome.status]


if __name__ == "__main__":
    raise SystemExit(main())
