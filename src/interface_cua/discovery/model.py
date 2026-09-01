"""The model seam.

`DiscoveryModel` is a protocol so the loop in `orchestrator.py` never imports the Anthropic SDK.
That matters twice: it keeps the discovery loop testable without a key, and it makes the *only*
place a model can influence this system a single injected object — which is what makes invariant 1
checkable rather than merely asserted.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from interface_cua.config import load_env, workspace_headers

# --- Verified against the installed anthropic SDK (0.125.0) -----------------------------------
# `types/beta/beta_tool_computer_use_20251124_param.py` declares the tool as:
#   type: Literal["computer_20251124"], name: Literal["computer"],
#   display_width_px / display_height_px required.
# It lives under `types/beta/`, so it must be sent through `client.beta.messages`.
COMPUTER_TOOL_TYPE = "computer_20251124"
COMPUTER_TOOL_NAME = "computer"

# --- Verified against the live API, not just the SDK -------------------------------------------
# The SDK's `AnthropicBetaParam` literal (types/anthropic_beta_param.py) does *not* list this
# value — it stops at `computer-use-2025-01-24` — so the SDK alone could not confirm it. Probed
# directly instead (`scripts/verify_computer_use.py`) against claude-opus-5:
#
#   computer-use-2025-11-24  -> accepted
#   computer-use-2025-01-24  -> 400, "Input tag 'computer_20251124' does not match any of the
#                                     expected tags: bash_20250124, browser_toolset_20260801, ..."
#   (no beta header)         -> 400
#
# The tool version and the beta header are coupled: the older beta rejects the newer tool. Still
# a constructor argument so a future version bump is one override, not a code change.
DEFAULT_COMPUTER_USE_BETA = "computer-use-2025-11-24"

MODEL_ID = "claude-opus-5"


class DiscoveryConfigurationError(RuntimeError):
    """Raised when the model rejects the request shape rather than the task."""


class DiscoveryRefusal(RuntimeError):
    """The model declined the request. Never retried automatically."""


@dataclass(frozen=True, slots=True)
class Observation:
    """What the model is shown for one turn."""

    screenshot_png: bytes
    url: str
    #: Redacted, scanned page text. Never raw page content straight from the surface.
    page_text: str


@dataclass(frozen=True, slots=True)
class ModelAction:
    """One action the model proposed. Coordinates are discovery-only; artifacts store semantics."""

    kind: Literal["click", "type", "key", "scroll", "screenshot", "extract", "finish", "escalate"]
    tool_use_id: str
    x: int | None = None
    y: int | None = None
    text: str | None = None
    #: For `extract`: the output name the model is naming.
    output_name: str | None = None
    reason: str | None = None
    raw_input: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ModelTurn:
    actions: tuple[ModelAction, ...]
    #: The model's own summary of why — this is the "why" in the event log (plan §8).
    rationale_summary: str | None
    stop_reason: str | None


class DiscoveryModel(Protocol):
    async def propose(self, observation: Observation) -> ModelTurn: ...

    def record_results(self, results: list[dict[str, Any]]) -> None: ...


EXTRACT_OUTPUT_TOOL: dict[str, Any] = {
    "name": "extract_output",
    "description": (
        "Record a value visible on screen as a typed output of this capability. "
        "Call this when you can see a value the goal asked you to return."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Output name, e.g. current_balance."},
            "value": {"type": "string", "description": "The value exactly as displayed."},
            "region": {
                "type": "string",
                "description": "Short description of where on screen it appeared.",
            },
        },
        "required": ["name", "value"],
    },
}

FINISH_TOOL: dict[str, Any] = {
    "name": "finish",
    "description": "The goal has been reached. Call this instead of acting further.",
    "input_schema": {
        "type": "object",
        "properties": {"reason": {"type": "string"}},
        "required": ["reason"],
    },
}

ESCALATE_TOOL: dict[str, Any] = {
    "name": "escalate",
    "description": (
        "Stop and hand control to a human. Call this when the screen is unexpected, when you "
        "would be guessing, or when continuing could have a side effect you cannot undo."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"reason": {"type": "string"}},
        "required": ["reason"],
    },
}


def build_tools(viewport: tuple[int, int]) -> list[dict[str, Any]]:
    """Computer-use primitives plus the three custom tools from plan §5."""

    width, height = viewport
    return [
        {
            "type": COMPUTER_TOOL_TYPE,
            "name": COMPUTER_TOOL_NAME,
            # Set to exactly the viewport so model coordinates map 1:1 to image pixels.
            "display_width_px": width,
            "display_height_px": height,
        },
        EXTRACT_OUTPUT_TOOL,
        FINISH_TOOL,
        ESCALATE_TOOL,
    ]


SYSTEM_PROMPT = """\
You are discovering how to complete a task in a bank servicing console, so the steps can be \
recorded as a reusable capability.

Work one action at a time. After each action you will be shown a fresh screenshot.

Rules:
- Only act on what you can see. Do not assume a control exists because it usually would.
- Call `extract_output` the moment a value the goal asks for is visible.
- Call `finish` when the goal is reached. Do not continue past it.
- Call `escalate` rather than guessing, and rather than taking any action whose effect you could \
not undo.
- Do not submit anything that creates, modifies, or deletes a record unless the goal says to.
"""


class ClaudeDiscoveryModel:
    """`DiscoveryModel` backed by the Anthropic API.

    Deliberately narrow: it turns an observation into proposed actions and nothing else. It does
    not touch the surface, evaluate policy, or decide when to stop — those belong to the
    orchestrator, below the model.
    """

    def __init__(
        self,
        *,
        viewport: tuple[int, int],
        goal: str,
        client: Any | None = None,
        model: str = MODEL_ID,
        betas: tuple[str, ...] = (DEFAULT_COMPUTER_USE_BETA,),
        effort: str = "high",
        max_tokens: int = 16_000,
    ) -> None:
        if client is None:
            load_env()  # discovery path only — replay never reaches here
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise DiscoveryConfigurationError(
                    "ANTHROPIC_API_KEY is not set and no .env provided it. "
                    "Discovery needs a model; replay does not."
                )
            from anthropic import AsyncAnthropic  # imported lazily: replay must never need it

            client = AsyncAnthropic(default_headers=workspace_headers())
        self.client = client
        self.model = model
        self.betas = list(betas)
        self.effort = effort
        self.max_tokens = max_tokens
        self.tools = build_tools(viewport)
        self.goal = goal
        self.messages: list[dict[str, Any]] = []

    async def propose(self, observation: Observation) -> ModelTurn:
        self.messages.append({"role": "user", "content": self._observation_content(observation)})
        message = await self._request()

        if message.stop_reason == "refusal":
            # Checked before reading content — on a refusal `content` may be empty or partial.
            raise DiscoveryRefusal(f"model declined the request: {message.stop_reason}")

        # Echo the assistant turn back verbatim, including thinking blocks. Editing or dropping
        # them breaks the next turn.
        self.messages.append({"role": "assistant", "content": message.content})
        return ModelTurn(
            actions=tuple(_actions_from(message.content)),
            rationale_summary=_rationale_from(message.content),
            stop_reason=message.stop_reason,
        )

    def record_results(self, results: list[dict[str, Any]]) -> None:
        """Feed tool results back as the next user turn."""

        if results:
            self.messages.append({"role": "user", "content": results})

    async def _request(self) -> Any:
        try:
            async with self.client.beta.messages.stream(
                model=self.model,
                max_tokens=self.max_tokens,
                betas=self.betas,
                system=SYSTEM_PROMPT + f"\n\nGoal: {self.goal}",
                tools=self.tools,
                # `summarized` is required for `rationale_summary` to carry anything: the default
                # is `omitted`, which returns thinking blocks with empty text.
                thinking={"type": "adaptive", "display": "summarized"},
                output_config={"effort": self.effort},
                messages=self.messages,
            ) as stream:
                return await stream.get_final_message()
        except Exception as exc:
            raise _configuration_error(exc) from exc

    def _observation_content(self, observation: Observation) -> list[dict[str, Any]]:
        import base64

        return [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.standard_b64encode(observation.screenshot_png).decode(),
                },
            },
            {"type": "text", "text": f"Current URL: {observation.url}"},
        ]


def _configuration_error(exc: Exception) -> Exception:
    """Turn a request-shape rejection into an actionable error rather than a stack trace."""

    text = str(exc)
    if "beta" in text.lower() or COMPUTER_TOOL_TYPE in text:
        return DiscoveryConfigurationError(
            f"The API rejected the computer-use request shape: {text}\n"
            f"The tool type {COMPUTER_TOOL_TYPE!r} is verified against the installed SDK. The beta "
            f"header default {DEFAULT_COMPUTER_USE_BETA!r} could not be verified offline — pass "
            f"`betas=(...)` to ClaudeDiscoveryModel with the value from the live computer-use docs."
        )
    return exc


_ACTION_ALIASES = {
    "left_click": "click",
    "click": "click",
    "type": "type",
    "key": "key",
    "scroll": "scroll",
    "screenshot": "screenshot",
}


def _actions_from(content: Any) -> list[ModelAction]:
    actions: list[ModelAction] = []
    for block in content:
        if getattr(block, "type", None) != "tool_use":
            continue
        payload = dict(block.input or {})
        if block.name == COMPUTER_TOOL_NAME:
            kind = _ACTION_ALIASES.get(str(payload.get("action")))
            if kind is None:
                continue
            coordinate = payload.get("coordinate") or [None, None]
            actions.append(
                ModelAction(
                    kind=kind,  # type: ignore[arg-type]
                    tool_use_id=block.id,
                    x=coordinate[0],
                    y=coordinate[1],
                    text=payload.get("text"),
                    raw_input=payload,
                )
            )
        elif block.name == "extract_output":
            actions.append(
                ModelAction(
                    kind="extract",
                    tool_use_id=block.id,
                    output_name=payload.get("name"),
                    text=payload.get("value"),
                    raw_input=payload,
                )
            )
        elif block.name in {"finish", "escalate"}:
            actions.append(
                ModelAction(
                    kind=block.name,  # type: ignore[arg-type]
                    tool_use_id=block.id,
                    reason=payload.get("reason"),
                    raw_input=payload,
                )
            )
    return actions


def _rationale_from(content: Any) -> str | None:
    """The model's own words for why it acted — text first, thinking summary as fallback."""

    for attribute in ("text", "thinking"):
        parts = [
            getattr(block, attribute)
            for block in content
            if getattr(block, "type", None) == ("text" if attribute == "text" else "thinking")
            and getattr(block, attribute, None)
        ]
        if parts:
            return " ".join(parts).strip()[:500]
    return None
