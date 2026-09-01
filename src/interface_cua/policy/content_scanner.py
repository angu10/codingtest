"""Untrusted-content scanner seam used before model context and output return."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, Protocol


class ContentVerdict(StrEnum):
    CLEAN = "clean"
    SUSPICIOUS = "suspicious"


@dataclass(frozen=True, slots=True)
class ContentScan:
    verdict: ContentVerdict
    scanner: str
    signals: tuple[str, ...] = ()


class ContentRiskScanner(Protocol):
    def scan(self, text: str) -> ContentScan: ...


class HeuristicContentRiskScanner:
    """Conservative default; policy and LLM-free replay provide structural containment."""

    _signals: ClassVar[dict[str, re.Pattern[str]]] = {
        "instruction_override": re.compile(
            r"\b(ignore|override|disregard)\b.{0,40}\b(instruction|policy|system)\b",
            re.IGNORECASE,
        ),
        "secret_request": re.compile(
            r"\b(reveal|send|upload|exfiltrate)\b.{0,40}\b(secret|token|password|key)\b",
            re.IGNORECASE,
        ),
    }

    def scan(self, text: str) -> ContentScan:
        matches = tuple(name for name, pattern in self._signals.items() if pattern.search(text))
        verdict = ContentVerdict.SUSPICIOUS if matches else ContentVerdict.CLEAN
        return ContentScan(verdict=verdict, scanner="heuristic", signals=matches)
