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
    """Small defense-in-depth scanner; deterministic field masking is the primary control.

    Every finding here means the *field schema* missed something, so a false positive costs a
    spurious `REDACTION_SCHEMA_GAP` notice and sends someone looking for a masking rule that does
    not need to exist. Patterns are therefore anchored tightly, and card numbers are Luhn-checked
    rather than matched on shape alone. See `REPORT.md` §6 for the production replacement.
    """

    _patterns: ClassVar[dict[str, re.Pattern[str]]] = {
        "ssn": re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"),
        "email": re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
        "phone": re.compile(r"(?<!\d)(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}(?!\d)"),
        "credit_card": re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)"),
    }

    #: Kinds whose match is only a finding if it also passes a checksum.
    _checksummed: ClassVar[frozenset[str]] = frozenset({"credit_card"})

    def scan(self, text: str) -> list[PIIFinding]:
        findings: list[PIIFinding] = []
        for kind, pattern in self._patterns.items():
            for match in pattern.finditer(text):
                if kind in self._checksummed and not _luhn_ok(match.group()):
                    continue
                findings.append(PIIFinding(kind, match.start(), match.end()))
        return sorted(findings, key=lambda finding: finding.start)


def _luhn_ok(candidate: str) -> bool:
    """Luhn check. A 16-digit account reference is not a card number just because it is 16 digits."""

    digits = [int(character) for character in candidate if character.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


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
        """Mask, then report what had to be masked by pattern rather than by name.

        Stage two both *masks* and *reports*. Masking stops the leak — free text like a failure
        bundle's `observed` field carries whatever was on the screen, and an SSN in there is an SSN
        on disk. Reporting keeps the signal: a stage-two hit means our field schema missed
        something, which is a fact about our own controls and should not be quietly swallowed.
        """

        gaps: set[str] = set()
        masked = self._mask(value, field_name=None, gaps=gaps)
        return RedactionResult(masked, tuple(sorted(gaps)))

    def _mask(self, value: Any, field_name: str | None, gaps: set[str]) -> Any:
        if field_name in self.sensitive_fields:
            return _mask_sensitive(field_name, value)
        if isinstance(value, dict):
            return {key: self._mask(item, str(key), gaps) for key, item in value.items()}
        if isinstance(value, list):
            return [self._mask(item, field_name, gaps) for item in value]
        if isinstance(value, tuple):
            return tuple(self._mask(item, field_name, gaps) for item in value)
        if isinstance(value, str):
            return self._mask_residue(self._mask_values(value), gaps)
        return value

    def _mask_values(self, text: str) -> str:
        # Through `_mask_span` rather than formatting inline: this is the third place the same
        # masking rule is applied, and it is the one that used to skip the length guard. A
        # four-character value came back as `***` plus the whole value.
        for secret in self.sensitive_values:
            if secret in text:
                text = text.replace(secret, _mask_span(secret))
        return text

    def _mask_residue(self, text: str, gaps: set[str]) -> str:
        findings = self.scanner.scan(text)
        if not findings:
            return text
        pieces: list[str] = []
        cursor = 0
        for finding in findings:
            if finding.start < cursor:
                continue  # overlaps one already masked
            gaps.add(finding.kind)
            pieces.append(text[cursor : finding.start])
            pieces.append(_mask_span(text[finding.start : finding.end]))
            cursor = finding.end
        pieces.append(text[cursor:])
        return "".join(pieces)


#: At or below this length, "the last four characters" *is* the value, and masking reveals it whole.
#: Five stays maskable on purpose: `***8431` for a five-digit member reference is the convention
#: invariant 7 states and the application itself renders.
_WHOLLY_REVEALED = 4


def _mask_sensitive(field_name: str, value: Any) -> str:
    lowered = field_name.lower()
    if "password" in lowered or "secret" in lowered:
        return "[redacted]"
    text = str(value)
    if len(text) <= _WHOLLY_REVEALED:
        return "[redacted]"
    return f"***{text[-4:]}"


def _mask_span(matched: str) -> str:
    """Mask a pattern hit the same way a declared field is masked, so logs read consistently."""

    return "[redacted]" if len(matched) <= _WHOLLY_REVEALED else f"***{matched[-4:]}"
