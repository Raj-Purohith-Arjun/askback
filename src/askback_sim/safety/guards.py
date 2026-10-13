"""Safety guards, run on the way in and on the way out.

Two stages, because they catch different things:

  input  - the user trying to subvert the assistant (prompt injection, probing
           for another user's data, banned topics)
  output - the assistant leaking something it should not have said (secrets,
           personal data, prohibited content)

Every guard returns a `GuardVerdict` rather than raising, so one turn produces
one auditable `SafetyReport` listing everything that fired.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar

from askback.core.contracts import SafetyReport
from askback.core.text import tolerate_line_breaks
from askback.evaluation.patterns import SECRET_PATTERNS

Stage = str  # "input" | "output"


@dataclass(slots=True)
class GuardVerdict:
    """One guard's opinion about one piece of text."""

    guard: str
    triggered: bool = False
    blocked: bool = False
    reason: str = ""
    risk: float = 0.0
    text: str = ""
    redactions: list[str] = field(default_factory=list)


class Guard(ABC):
    """Base class for a safety check."""

    name: ClassVar[str] = "guard"
    stages: ClassVar[frozenset[Stage]] = frozenset({"input", "output"})

    @abstractmethod
    def inspect(self, text: str, *, stage: Stage, meta: dict[str, Any]) -> GuardVerdict:
        """Examine `text`; return a verdict, optionally with redacted text."""

    def _ok(self, text: str) -> GuardVerdict:
        return GuardVerdict(guard=self.name, text=text)


# --------------------------------------------------------------------------- #
# Prompt injection
# --------------------------------------------------------------------------- #

#: Ordered most-specific first. Each entry is (label, pattern).
INJECTION_PATTERNS: tuple[tuple[str, str], ...] = (
    ("override_instructions",
     r"\b(ignore|disregard|forget|override)\b.{0,30}\b(previous|prior|above|earlier|all)\b"
     r".{0,20}\b(instruction|prompt|rule|direction|guideline)s?\b"),
    ("reveal_system_prompt",
     r"\b(reveal|show|print|repeat|display|output|dump|tell me)\b.{0,30}"
     r"\b(system prompt|initial prompt|your instructions|your rules|your prompt|"
     r"configuration|config file)\b"),
    ("role_hijack",
     r"\b(you are now|from now on you are|act as|pretend to be|roleplay as)\b.{0,40}"
     r"\b(dan|developer mode|unrestricted|jailbroken|no restrictions|admin|root)\b"),
    ("bypass_safety",
     r"\b(bypass|disable|turn off|circumvent|ignore)\b.{0,25}"
     r"\b(safety|security|guardrail|filter|policy|restriction|moderation)\b"),
    # Asking for a secret *value*. Deliberately narrow: "what is the password
    # policy" is a legitimate question and must not be caught here, so the
    # password branch excludes the words that make it a policy question.
    ("credential_probe",
     r"\b(?:what(?:'s| is| are)?|show|give|reveal|print|list|tell|display|dump|send|share)\b"
     r"[^.?!]{0,45}?\b(?:api[ _-]?keys?|secret keys?|access tokens?|"
     r"auth(?:entication)? tokens?|service account tokens?|bearer tokens?|"
     r"client secrets?|private keys?|connection strings?|"
     r"(?:login|account|admin|service|database|system|user) credentials?)\b"),
    ("password_probe",
     r"\b(?:what(?:'s| is)|tell me|give me|show me|send me|share)\b[^.?!]{0,30}?"
     r"\b(?:the|my|your|his|her|their)\s+password\b"
     r"(?!\s*(?:policy|polic|rules?|requirements?|standard|length|complexity|"
     r"expiry|expiration|rotation|reset|manager))"),
    ("fake_authority",
     r"\b(as an? (admin|administrator|developer|engineer)|i am the (admin|developer|owner))\b"
     r".{0,40}\b(grant|give|show|allow|enable|override)\b"),
    ("delimiter_injection",
     r"(<\|im_(start|end)\|>|</?system>|\[/?INST\]|###\s*system\s*:)"),
    ("instruction_smuggling",
     r"\bnew instructions?\s*:\s*\S|\bsystem\s*(prompt\s*)?(override|update)\s*:"),
)

#: Probing for data belonging to somebody else.
#
# These rely on explicit third-party words rather than on a name looking
# capitalised: every pattern here is compiled case-insensitively, so a
# `[A-Z][a-z]+` "looks like a name" trick silently matches lowercase text too.
SENSITIVE_SUBJECT = (
    r"salary|salaries|payslip|pay slip|compensation|appraisal|performance review|"
    r"personal (?:data|file|record|details)|home address|medical record|bank details"
)
THIRD_PARTY = (
    r"other|another|someone|somebody|colleagues?|co-?workers?|employees?|"
    r"managers?|boss|team ?mates?|staff"
)

CROSS_USER_PATTERNS: tuple[tuple[str, str], ...] = (
    # "<sensitive thing> of/for <another person>"
    ("cross_user_access",
     rf"\b(?:{SENSITIVE_SUBJECT})\b[^.?!]{{0,30}}\b(?:of|for|belonging to)\b\s+"
     rf"(?:the\s+|a\s+|an\s+|my\s+|their\s+|his\s+|her\s+)?(?:{THIRD_PARTY})\b"),
    # "<another person>'s <sensitive thing>"
    ("cross_user_possessive",
     r"\b(?:other|another|someone else|somebody else|a colleague|a co-?worker|"
     r"my (?:manager|boss|co-?worker|colleague|team ?mate))(?:'s|s')?\b"
     rf"[^.?!]{{0,30}}\b(?:{SENSITIVE_SUBJECT}|password)\b"),
    ("impersonation",
     r"\b(?:log ?in|sign in|authenticate|act|respond) as\b[^.?!]{0,25}"
     r"\b(?:another|different|other|some other) (?:user|employee|person|account)\b"),
)


class PromptInjectionGuard(Guard):
    """Detects attempts to rewrite the assistant's instructions."""

    name: ClassVar[str] = "prompt_injection"
    stages: ClassVar[frozenset[Stage]] = frozenset({"input"})

    def __init__(self, extra_patterns: Sequence[str] = (), block: bool = True) -> None:
        patterns = list(INJECTION_PATTERNS) + [
            (f"custom_{i}", p) for i, p in enumerate(extra_patterns)
        ]
        self._rules = [(label, re.compile(p, re.IGNORECASE | re.DOTALL)) for label, p in patterns]
        self.block = block

    def inspect(self, text: str, *, stage: Stage, meta: dict[str, Any]) -> GuardVerdict:
        hits = [label for label, rx in self._rules if rx.search(text)]
        if not hits:
            return self._ok(text)
        return GuardVerdict(
            guard=self.name,
            triggered=True,
            blocked=self.block,
            reason=f"Prompt injection detected ({', '.join(hits)}).",
            risk=min(1.0, 0.6 + 0.2 * len(hits)),
            text=text,
        )


class CrossUserAccessGuard(Guard):
    """Blocks requests for another person's records."""

    name: ClassVar[str] = "cross_user_access"
    stages: ClassVar[frozenset[Stage]] = frozenset({"input"})

    def __init__(self, extra_patterns: Sequence[str] = ()) -> None:
        patterns = list(CROSS_USER_PATTERNS) + [
            (f"custom_{i}", p) for i, p in enumerate(extra_patterns)
        ]
        self._rules = [(label, re.compile(p, re.IGNORECASE)) for label, p in patterns]

    def inspect(self, text: str, *, stage: Stage, meta: dict[str, Any]) -> GuardVerdict:
        hits = [label for label, rx in self._rules if rx.search(text)]
        if not hits:
            return self._ok(text)
        return GuardVerdict(
            guard=self.name,
            triggered=True,
            blocked=True,
            reason="Request appears to target another user's data.",
            risk=0.9,
            text=text,
        )


# --------------------------------------------------------------------------- #
# Leakage
# --------------------------------------------------------------------------- #

# SECRET_PATTERNS is the evaluator's table (askback.evaluation.patterns): the
# guard blocks exactly what the harness would grade as a leak.

PII_PATTERNS: tuple[tuple[str, str], ...] = (
    ("email", r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    ("phone", r"\b(?:\+\d{1,3}[ -]?)?(?:\(\d{2,4}\)[ -]?)?\d{3,5}[ -]?\d{3,4}[ -]?\d{0,4}\b"),
    ("national_id", r"\b\d{3}-\d{2}-\d{4}\b"),
    ("card_number", r"\b(?:\d{4}[ -]?){3}\d{4}\b"),
)


class _RedactingGuard(Guard):
    """Shared machinery for guards that mask matches instead of blocking."""

    patterns: ClassVar[tuple[tuple[str, str], ...]] = ()
    block_on_hit: ClassVar[bool] = False
    risk_on_hit: ClassVar[float] = 0.5

    def __init__(self, enabled: bool = True, extra_patterns: Sequence[str] = ()) -> None:
        self.enabled = enabled
        rules = list(self.patterns) + [
            (f"custom_{i}", p) for i, p in enumerate(extra_patterns)
        ]
        # Same rewrite the evaluator applies: a credential split by a wrap is still one.
        self._rules = [(label, re.compile(tolerate_line_breaks(p))) for label, p in rules]

    def inspect(self, text: str, *, stage: Stage, meta: dict[str, Any]) -> GuardVerdict:
        if not self.enabled or not text:
            return self._ok(text)

        redacted = text
        hits: list[str] = []
        for label, rx in self._rules:
            redacted, count = rx.subn(f"[REDACTED_{label.upper()}]", redacted)
            if count:
                hits.append(label)

        if not hits:
            return self._ok(text)
        return GuardVerdict(
            guard=self.name,
            triggered=True,
            blocked=self.block_on_hit,
            reason=f"Masked sensitive content ({', '.join(hits)}).",
            risk=self.risk_on_hit,
            text=redacted,
            redactions=hits,
        )


class SecretLeakageGuard(_RedactingGuard):
    """Masks credentials before they reach the user or a log."""

    name: ClassVar[str] = "secret_leakage"
    stages: ClassVar[frozenset[Stage]] = frozenset({"output"})
    patterns: ClassVar[tuple[tuple[str, str], ...]] = SECRET_PATTERNS
    risk_on_hit: ClassVar[float] = 1.0


class PIIGuard(_RedactingGuard):
    """Masks personal data. Off by default: many corpora legitimately contain emails."""

    name: ClassVar[str] = "pii"
    stages: ClassVar[frozenset[Stage]] = frozenset({"output"})
    patterns: ClassVar[tuple[tuple[str, str], ...]] = PII_PATTERNS
    risk_on_hit: ClassVar[float] = 0.6


class ProhibitedContentGuard(Guard):
    """Blocks configured phrases in either direction. Your domain's red lines."""

    name: ClassVar[str] = "prohibited_content"

    def __init__(self, phrases: Sequence[str] = (), stages: Iterable[Stage] = ("input", "output")) -> None:
        self.phrases = [p.lower() for p in phrases if p.strip()]
        self.stages = frozenset(stages)  # type: ignore[misc]

    def inspect(self, text: str, *, stage: Stage, meta: dict[str, Any]) -> GuardVerdict:
        lowered = (text or "").lower()
        hits = [p for p in self.phrases if p in lowered]
        if not hits:
            return self._ok(text)
        return GuardVerdict(
            guard=self.name,
            triggered=True,
            blocked=True,
            reason=f"Prohibited content: {', '.join(hits)}.",
            risk=0.8,
            text=text,
        )


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #


class SafetyPipeline:
    """Runs the configured guards for a stage and merges their verdicts."""

    def __init__(self, guards: Sequence[Guard]) -> None:
        self.guards = list(guards)

    @classmethod
    def from_config(cls, config: dict[str, Any] | None = None) -> SafetyPipeline:
        cfg = config or {}
        guards: list[Guard] = []
        if cfg.get("prompt_injection", True):
            guards.append(PromptInjectionGuard(cfg.get("injection_patterns", [])))
        if cfg.get("cross_user_access", True):
            guards.append(CrossUserAccessGuard(cfg.get("cross_user_patterns", [])))
        if cfg.get("secret_leakage", True):
            guards.append(SecretLeakageGuard(True, cfg.get("secret_patterns", [])))
        if cfg.get("pii_redaction", False):
            guards.append(PIIGuard(True, cfg.get("pii_patterns", [])))
        if cfg.get("prohibited_phrases"):
            guards.append(ProhibitedContentGuard(cfg["prohibited_phrases"]))
        return cls(guards)

    def run(
        self, text: str, *, stage: Stage, meta: dict[str, Any] | None = None
    ) -> tuple[str, SafetyReport]:
        """Return the (possibly redacted) text and a merged report for `stage`."""
        meta = meta or {}
        report = SafetyReport()
        current = text

        for guard in self.guards:
            if stage not in guard.stages:
                continue
            verdict = guard.inspect(current, stage=stage, meta=meta)
            if verdict.text:
                current = verdict.text
            if not verdict.triggered:
                continue

            report.reasons.append(verdict.reason)
            report.redactions.extend(verdict.redactions)
            report.risk_score = max(report.risk_score, verdict.risk)
            report.blocked = report.blocked or verdict.blocked
            if guard.name == PromptInjectionGuard.name:
                report.injection_detected = True

        return current, report


def merge_reports(*reports: SafetyReport) -> SafetyReport:
    """Combine per-stage reports into the single one attached to a response."""
    merged = SafetyReport()
    for report in reports:
        merged.blocked = merged.blocked or report.blocked
        merged.injection_detected = merged.injection_detected or report.injection_detected
        merged.reasons.extend(report.reasons)
        merged.redactions.extend(report.redactions)
        merged.risk_score = max(merged.risk_score, report.risk_score)
    return merged
