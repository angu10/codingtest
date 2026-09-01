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

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, async_playwright

from interface_cua.handoff.lease import SessionLease
from interface_cua.observability.evidence import EvidenceWriter
from interface_cua.policy.engine import PolicyConfig, PolicyEngine
from interface_cua.policy.redaction import Redactor
from interface_cua.replay.executor import ReplayExecutor
from interface_cua.schema.artifact import Capability, ValueType
from interface_cua.schema.result import (
    NeedsHumanResult,
    ReplayResult,
    ValidationCheck,
    ValidationRequiredResult,
)
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

        # Every other terminal state in this system is typed and explains itself; an unreachable
        # application must not be the one exception that arrives as a stack trace.
        try:
            await page.goto(args.base_url)
        except PlaywrightError as exc:
            await browser.close()
            return ValidationRequiredResult(
                check=ValidationCheck.ENTRY_ROUTE,
                reason="the target application is not reachable",
                expected={"base_url": args.base_url},
                observed={"error": str(exc).splitlines()[0]},
            )

        executor = ReplayExecutor(
            surface,
            PolicyEngine(PolicyConfig(allowed_origins=frozenset({args.base_url.rstrip("/")}))),
            application_family=args.family,
            application_version=args.app_version,
            allow_draft=args.allow_draft,
            evidence=evidence,
        )
        try:
            result = await executor.execute(
                artifact, arguments, confirmed_steps=frozenset(args.confirm)
            )
            if result.status == "needs_human" and args.handoff:
                result = await _handoff(
                    artifact, arguments, result, surface, page, executor, evidence, args
                )
        finally:
            await browser.close()
        if evidence is not None:
            print(f"evidence: {evidence.dir}", file=sys.stderr)
        return result


async def _handoff(
    artifact: Capability,
    arguments: dict[str, object],
    result: NeedsHumanResult,
    surface: PlaywrightSurface,
    page: Page,
    executor: ReplayExecutor,
    evidence: EvidenceWriter | None,
    args: argparse.Namespace,
) -> ReplayResult:
    """Escalate to a human on the same live session, then resume if they say so.

    The operator drives the actual browser window — same cookies, same session — so `--headed` is
    what makes this usable. The console only shows why we stopped and takes the decision.
    """

    import uvicorn

    from interface_cua.handoff.console import CONSOLE_PORT, build_console
    from interface_cua.handoff.intervention import HandoffCoordinator

    if evidence is None:
        evidence = EvidenceWriter(Path("evidence"), f"handoff_{artifact.capability.id}")

    coordinator = HandoffCoordinator(
        lease=surface.lease,
        surface=surface,
        page=page,
        evidence=evidence,
        sensitive_fields=frozenset(spec.name for spec in artifact.inputs if spec.sensitive),
    )
    await coordinator.raise_request(
        run_id=evidence.run_id,
        capability_id=artifact.capability.id,
        step_id=result.step,
        reason=result.reason,
    )

    server = uvicorn.Server(
        uvicorn.Config(
            build_console(coordinator), host="127.0.0.1", port=CONSOLE_PORT, log_level="warning"
        )
    )
    serving = asyncio.create_task(server.serve())
    print(
        f"\nescalated at step {result.step}: {result.reason}\n"
        f"operator console: http://127.0.0.1:{CONSOLE_PORT}\n"
        f"take over the browser window, then choose Resume or Abort.\n",
        file=sys.stderr,
    )
    try:
        await coordinator.decided.wait()
    finally:
        server.should_exit = True
        await serving

    request = coordinator.request
    for action in request.human_actions if request else []:
        print(f"  human: {action.describe()}", file=sys.stderr)

    if coordinator.decision != "resume":
        return result
    coordinator.confirm_resume()
    # The resumed step re-checks its own precondition; the operator clicking Resume is not
    # sufficient authority to continue.
    return await executor.execute(artifact, arguments, resume_from=result.step)


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
        entry_landmarks=args.landmark,
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
    # Defaults point at the bundled demo app for convenience; every one is overridable, and
    # nothing under src/ reads them.
    find.add_argument("--base-url", default="http://127.0.0.1:8000")
    find.add_argument("--family", default="meridian-cu")
    find.add_argument("--app-version", default="demo-v1")
    find.add_argument(
        "--landmark",
        action="append",
        required=True,
        help="Text that must be visible on the entry screen. Repeatable. App-specific by nature.",
    )
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
    run.add_argument(
        "--confirm",
        action="append",
        default=[],
        metavar="STEP_ID",
        help="Confirm a consequential step by name. Standing in for a human saying yes.",
    )
    run.add_argument(
        "--handoff",
        action="store_true",
        help="On needs_human, open the operator console and wait for a decision. Use with --headed.",
    )
    return run.set_defaults(handler=replay) or parser


def _printable(
    outcome: ReplayResult, artifact: Capability | None, arguments: dict[str, object]
) -> dict[str, object]:
    """The result as it goes to stdout, through the same redactor everything else uses.

    stdout is an egress point like any other: it lands in a terminal, a CI log, or whatever called
    us. The whole payload is redacted, not just `outputs` — a failure's `observed` field carries
    whatever was on the screen, which is exactly where regulated data turns up uninvited. A caller
    that genuinely needs a value reads it from the returned object; what gets *printed* is masked,
    because printing is the part nobody controls.
    """

    payload = outcome.model_dump(mode="json")
    if artifact is None:
        return payload
    declared = {item.name for item in artifact.inputs if item.sensitive}
    declared |= {item.name for item in artifact.outputs if item.sensitive}
    redactor = Redactor(
        declared, sensitive_values=frozenset(str(value) for value in arguments.values())
    )
    return redactor.redact(payload).value


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    outcome = asyncio.run(args.handler(args))
    if getattr(args, "discovery", False):
        return int(outcome)
    artifact = Capability.from_yaml(args.artifact) if hasattr(args, "artifact") else None
    arguments = _parse_inputs(args.input, artifact) if artifact is not None else {}
    json.dump(_printable(outcome, artifact, arguments), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return EXIT_CODES[outcome.status]


if __name__ == "__main__":
    raise SystemExit(main())
