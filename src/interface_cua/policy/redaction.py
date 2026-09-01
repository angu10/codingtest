"""Deterministic sensitive-field masking followed by residue PII scanning."""

from __future__ import annotations

import re
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol


@dataclass(frozen=True, slots=True)
class PIIFinding:
    kind: str
    start: int
    end: int


class PIIScanner(Protocol):
    def scan(self, text: str) -> list[PIIFinding]: ...


class RegexPIIScanner:
    """Small defense-in-depth scanner; deterministic field masking is the primary control."""

    _patterns: ClassVar[dict[str, re.Pattern[str]]] = {
        "ssn": re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"),
        "email": re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
        "phone": re.compile(r"(?<!\d)(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}(?!\d)"),
    }

    def scan(self, text: str) -> list[PIIFinding]:
        findings: list[PIIFinding] = []
        for kind, pattern in self._patterns.items():
            findings.extend(PIIFinding(kind, match.start(), match.end()) for match in pattern.finditer(text))
        return sorted(findings, key=lambda finding: finding.start)


@dataclass(frozen=True, slots=True)
class RedactionResult:
    value: Any
    schema_gaps: tuple[str, ...]


class Redactor:
    """Mask declared sensitive keys before scanning any remaining free text."""

    def __init__(
        self,
        sensitive_fields: AbstractSet[str],
        scanner: PIIScanner | None = None,
        sensitive_values: AbstractSet[str] = frozenset(),
    ) -> None:
        self.sensitive_fields = sensitive_fields
        self.scanner = scanner or RegexPIIScanner()
        # Masking by field name alone is not enough: a member id also travels inside a URL, a
        # page title, or a postcondition string, where no field is named after it. Callers that
        # know the run's actual input values pass them here so they are masked wherever they
        # appear — which is what invariant 7 requires of a log.
        self.sensitive_values = frozenset(v for v in sensitive_values if len(v) >= 3)

    def redact(self, value: Any) -> RedactionResult:
        masked = self._mask(value, field_name=None)
        gaps = tuple(sorted({finding.kind for finding in self.scanner.scan(_flatten_text(masked))}))
        return RedactionResult(masked, gaps)

    def _mask(self, value: Any, field_name: str | None) -> Any:
        if field_name in self.sensitive_fields:
            return _mask_sensitive(field_name, value)
        if isinstance(value, dict):
            return {key: self._mask(item, str(key)) for key, item in value.items()}
        if isinstance(value, list):
            return [self._mask(item, field_name) for item in value]
        if isinstance(value, tuple):
            return tuple(self._mask(item, field_name) for item in value)
        if isinstance(value, str):
            return self._mask_values(value)
        return value

    def _mask_values(self, text: str) -> str:
        for secret in self.sensitive_values:
            if secret in text:
                text = text.replace(secret, f"***{secret[-4:]}")
        return text


def _mask_sensitive(field_name: str, value: Any) -> str:
    lowered = field_name.lower()
    if "password" in lowered or "secret" in lowered:
        return "[redacted]"
    text = str(value)
    return f"***{text[-4:]}" if text else "[redacted]"


def _flatten_text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(f"{key} {_flatten_text(item)}" for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return " ".join(_flatten_text(item) for item in value)
    return str(value)
