"""The test-case schema: one conversation, whatever file format it came from.

A case is who is asking plus the turns they send. Expectations hang off the
turn, not the case: "the reply must cite POLICY-1" is a claim about one reply,
and a three-turn conversation has three replies to make claims about.

    schema_version: 2
    defaults:                    # merged under every evaluated turn's `expect`
      expected_route: policy_agent
    cases:
      - id: return-window
        tags: [policy, returns]
        turns:
          - user: How many days do I have to return an item?
            expect:
              required_facts: ["30 days"]

Every expectation is optional, and evaluators skip the checks a turn does not
ask for. A turn with `evaluate: false` is setup: it is sent, never graded.

A case may declare `samples: N` to be run N times, each in a fresh session, and
`min_passing_samples: K` for how many must pass (default: all). One draw from a
non-deterministic agent is not a measurement; a K above N is refused at load.

A turn may carry a `clarification` block, and a case a `persona`, from schema
version 3. They say what to do when the agent answers with a question instead of
an answer; see `evaluation/clarification.py`. A v2 file that uses them is refused,
not ignored, so a file never changes meaning under the loader that reads it.

The nested form is strict where the flat one was loose. An unknown key is
refused rather than ignored, because a misspelt expectation is a check that
silently never runs.

A tabular file (CSV, XLSX) has one row per case, so one turn per case. Its flat
columns are read by `TestCase.from_row`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from ..core.errors import DatasetError

#: The newest suite-file version this module reads. `scripts/migrate_v1_v2.py` still writes 2.
SCHEMA_VERSION = 3

#: Versions the loader reads. 3 only adds optional keys, so a v2 file loads unchanged.
READABLE_VERSIONS = (2, 3)

CLARIFICATION_POLICIES = ("allow", "forbid", "require")
#: What happens when scripted answers run out: fall through to the case's persona
#: (a case without one has nothing to say), or stop at once. There is deliberately
#: no option to grade the question as if it were the answer.
ON_EXHAUSTED = ("persona", "gap")
DEFAULT_MAX_ROUNDS = 2
DEFAULT_UNKNOWN_RESPONSE = "I don't have that information."

#: Keys accepted in a dataset row but stored in `metadata` rather than a field.
_PASSTHROUGH = {"notes", "owner", "source", "author", "ticket"}

_CASE_KEYS = {
    "id", "suite", "description", "user_id", "tenant_id", "risk", "tags", "metadata", "turns",
    "samples", "min_passing_samples",
}
_TURN_KEYS = {"user", "expect", "evaluate"}
_V3_CASE_KEYS = {"persona"}
_V3_TURN_KEYS = {"clarification"}
_CLARIFICATION_KEYS = {"policy", "max_rounds", "answers", "on_exhausted"}
_PERSONA_KEYS = {"goal", "facts", "style", "unknown_response"}


def _as_list(value: Any) -> list[str]:
    """Accept a list, a comma-separated string, or nothing."""
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [part.strip() for part in str(value).split(",") if part.strip()]


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _as_count(value: Any, *, name: str, where: str, minimum: int | None = None) -> int:
    """Read a sample count from YAML (int), a CSV cell (str) or XLSX (float).

    Strict where `_as_bool` is loose: `samples: "three"` or `samples: true`
    silently read as 1 would turn a flake measurement into a single draw.
    """
    if isinstance(value, bool):
        raise DatasetError(f"{where}: `{name}` must be a whole number, got {value!r}")
    number: int | None = None
    if isinstance(value, float) and value.is_integer():
        number = int(value)
    elif isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.strip().isdecimal():
        number = int(value)
    if number is None or (minimum is not None and number < minimum):
        raise DatasetError(f"{where}: `{name}` must be a whole number, got {value!r}")
    return number


def _as_strict_bool(value: Any, *, name: str, where: str, default: bool) -> bool:
    """Strict where `_as_bool` is loose: a typo must not read as `false`.

    `expected_handoff: flase` read as False would switch off the evidence check
    on an escalation case, silently.
    """
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "false"}:
        return text == "true"
    raise DatasetError(f"{where}: `{name}` must be true or false, got {value!r}")


def _as_str(value: Any) -> str:
    return "" if value is None else str(value)


def _present(data: Mapping[str, Any]) -> dict[str, Any]:
    """Drop empty cells, so a blank spreadsheet column reads as not written."""
    return {k: v for k, v in data.items() if v is not None and v != ""}


@dataclass(slots=True)
class Expectations:
    """What one reply must satisfy. An empty field is an expectation not made."""

    # --- routing and behaviour ---------------------------------------------- #
    expected_route: str = ""
    expected_intent: str = ""
    expected_status: str = ""
    expected_tools: list[str] = field(default_factory=list)
    forbidden_tools: list[str] = field(default_factory=list)

    # --- content ------------------------------------------------------------ #
    #: Doc ids that retrieval must surface. This is the "evidence" column.
    expected_docs: list[str] = field(default_factory=list)
    #: Substrings that must appear in the reply. Wrap in /slashes/ for regex.
    required_facts: list[str] = field(default_factory=list)
    prohibited_content: list[str] = field(default_factory=list)
    min_citations: int = 0
    #: Resolve the chapters, pages and documents the reply cites against the citation
    #: manifest. Opt-in; with no manifest configured the turn is skipped, never passed.
    verify_citations: bool = False
    #: Free-text ideal answer, used by the LLM-as-a-judge evaluator.
    reference_answer: str = ""

    # --- safety ------------------------------------------------------------- #
    must_refuse: bool = False
    #: Only read when `expected_status` is escalated: the turn must show a ticket
    #: reference or queue name. False for an escalation that is a status change only.
    expected_handoff: bool = True

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, where: str) -> Expectations:
        unknown = sorted(set(data) - EXPECTATION_FIELDS)
        if unknown:
            raise DatasetError(
                f"{where}: unknown expectation(s) {unknown}. "
                f"Known: {', '.join(sorted(EXPECTATION_FIELDS))}"
            )
        return cls(
            expected_route=_as_str(data.get("expected_route")),
            expected_intent=_as_str(data.get("expected_intent")),
            expected_status=_as_str(data.get("expected_status")),
            expected_tools=_as_list(data.get("expected_tools")),
            forbidden_tools=_as_list(data.get("forbidden_tools")),
            expected_docs=_as_list(data.get("expected_docs")),
            required_facts=_as_list(data.get("required_facts")),
            prohibited_content=_as_list(data.get("prohibited_content")),
            min_citations=int(data.get("min_citations") or 0),
            verify_citations=_as_strict_bool(
                data.get("verify_citations"), name="verify_citations", where=where, default=False
            ),
            reference_answer=_as_str(data.get("reference_answer")),
            must_refuse=_as_bool(data.get("must_refuse")),
            expected_handoff=_as_strict_bool(
                data.get("expected_handoff"), name="expected_handoff", where=where, default=True
            ),
        )


EXPECTATION_FIELDS = frozenset(f.name for f in fields(Expectations))


@dataclass(slots=True)
class ClarificationSpec:
    """What to do when this turn's reply is a question rather than an answer.

    `policy` is the assertion: `forbid` fails a turn that asked (it was
    answerable), `require` fails one that answered (it was ambiguous), `allow`
    passes either way and scores lower for every round it took. `max_rounds` is
    how many times we answer before giving up; 0 means do not answer at all,
    which is how `forbid` and `require` assert on the first reply.
    """

    policy: str = "allow"
    max_rounds: int = DEFAULT_MAX_ROUNDS
    #: Scripted replies, used in order, one per question.
    answers: list[str] = field(default_factory=list)
    on_exhausted: str = "persona"

    @classmethod
    def from_dict(cls, data: Any, *, where: str) -> ClarificationSpec:
        if not isinstance(data, Mapping):
            raise DatasetError(f"{where}: `clarification` must be a mapping, got {data!r}")
        unknown = sorted(set(data) - _CLARIFICATION_KEYS)
        if unknown:
            raise DatasetError(
                f"{where}: unknown clarification key(s) {unknown}. "
                f"Known: {', '.join(sorted(_CLARIFICATION_KEYS))}"
            )
        policy = str(data.get("policy", "allow")).strip().lower()
        if policy not in CLARIFICATION_POLICIES:
            raise DatasetError(
                f"{where}: clarification.policy must be one of {', '.join(CLARIFICATION_POLICIES)}, "
                f"got {data.get('policy')!r}"
            )
        on_exhausted = str(data.get("on_exhausted", "persona")).strip().lower()
        if on_exhausted not in ON_EXHAUSTED:
            raise DatasetError(
                f"{where}: clarification.on_exhausted must be one of {', '.join(ON_EXHAUSTED)}, "
                f"got {data.get('on_exhausted')!r}"
            )
        # `forbid` never answers: there is nothing to say to a question it must not ask.
        default_rounds = 0 if policy == "forbid" else DEFAULT_MAX_ROUNDS
        rounds = (
            _as_count(data["max_rounds"], name="clarification.max_rounds", where=where, minimum=0)
            if "max_rounds" in data
            else default_rounds
        )
        answers = data.get("answers") or []
        if not isinstance(answers, list) or not all(
            isinstance(a, str) and a.strip() for a in answers
        ):
            raise DatasetError(f"{where}: clarification.answers must be a list of non-empty strings.")
        if policy == "forbid" and (rounds or answers):
            raise DatasetError(
                f"{where}: clarification policy `forbid` never answers; drop `answers` and "
                "`max_rounds`."
            )
        return cls(policy, rounds, [a.strip() for a in answers], on_exhausted)


@dataclass(slots=True)
class Persona:
    """Who the simulated user is, as a CLOSED sheet of facts.

    The persona may state a fact on `facts` and nothing else. Anything the agent
    asks beyond it is declined with `unknown_response`, because a persona free to
    invent an answer makes every later assertion a measurement of the persona.
    """

    goal: str
    facts: dict[str, str]
    style: str = ""
    unknown_response: str = DEFAULT_UNKNOWN_RESPONSE

    @classmethod
    def from_dict(cls, data: Any, *, where: str) -> Persona:
        if not isinstance(data, Mapping):
            raise DatasetError(f"{where}: `persona` must be a mapping, got {data!r}")
        unknown = sorted(set(data) - _PERSONA_KEYS)
        if unknown:
            raise DatasetError(
                f"{where}: unknown persona key(s) {unknown}. Known: {', '.join(sorted(_PERSONA_KEYS))}"
            )
        goal = data.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            raise DatasetError(f"{where}: persona needs a non-empty `goal`.")
        facts = data.get("facts")
        if not isinstance(facts, Mapping) or not facts:
            raise DatasetError(f"{where}: persona needs `facts`, a non-empty mapping of name -> value.")
        sheet: dict[str, str] = {}
        for key, value in facts.items():
            if not isinstance(key, str) or not key.strip() or value is None or str(value).strip() == "":
                raise DatasetError(f"{where}: persona.facts.{key} needs a non-empty name and value.")
            sheet[key.strip()] = str(value).strip()
        refusal = data.get("unknown_response", DEFAULT_UNKNOWN_RESPONSE)
        if not isinstance(refusal, str) or not refusal.strip():
            raise DatasetError(f"{where}: persona.unknown_response must be non-empty text.")
        return cls(goal.strip(), sheet, _as_str(data.get("style")).strip(), refusal.strip())


@dataclass(slots=True)
class Turn:
    """One user message and what its reply must satisfy."""

    user: str
    expect: Expectations = field(default_factory=Expectations)
    #: False for a setup turn: sent to build the conversation, never graded.
    evaluate: bool = True
    #: None means the turn says nothing about questions; the runner still looks for one.
    clarification: ClarificationSpec | None = None

    @classmethod
    def from_dict(
        cls, data: Any, *, defaults: Mapping[str, Any], where: str, clarifications: bool = True
    ) -> Turn:
        """Build a turn, with the suite's `defaults` under its own `expect`.

        Merged by key, and a key the turn wrote always wins, including when it
        wrote an empty value: `expected_route: null` opts one turn out of a
        default route. A default must never stand in for something the author
        did say.
        """
        if not isinstance(data, Mapping):
            raise DatasetError(f"{where}: must be a mapping with a `user` message, got {data!r}")
        allowed = _TURN_KEYS | (_V3_TURN_KEYS if clarifications else set())
        unknown = sorted(set(data) - allowed)
        if unknown:
            hint = (
                " `clarification` needs schema_version: 3."
                if not clarifications and "clarification" in unknown
                else ""
            )
            raise DatasetError(
                f"{where}: unknown key(s) {unknown}. A turn has {', '.join(sorted(allowed))}.{hint}"
            )
        spec = (
            ClarificationSpec.from_dict(data["clarification"], where=where)
            if data.get("clarification") is not None
            else None
        )

        user = data.get("user")
        if not isinstance(user, str) or not user.strip():
            raise DatasetError(f"{where}: needs a non-empty `user` message.")
        evaluate = data.get("evaluate", True)
        if not isinstance(evaluate, bool):
            raise DatasetError(f"{where}: `evaluate` must be true or false, got {evaluate!r}")
        own = data.get("expect")
        if own is not None and not isinstance(own, Mapping):
            raise DatasetError(f"{where}: `expect` must be a mapping, got {own!r}")

        if not evaluate:
            if own:
                raise DatasetError(
                    f"{where}: has `expect` and `evaluate: false`. A setup turn is never "
                    "graded, so those expectations would be dropped without a word. "
                    "Remove one of the two."
                )
            return cls(user=user.strip(), evaluate=False, clarification=spec)

        return cls(
            user=user.strip(),
            expect=Expectations.from_dict({**defaults, **(own or {})}, where=f"{where}: expect"),
            clarification=spec,
        )


@dataclass(slots=True)
class TestCase:
    """One evaluated conversation: who is asking, and the turns they send."""

    #: Stops pytest trying to collect this dataclass as a test class.
    __test__ = False

    # --- identity ---------------------------------------------------------- #
    id: str
    turns: list[Turn]
    suite: str = "golden"
    description: str = ""

    # --- who is asking (drives tenant isolation checks) -------------------- #
    user_id: str = "test_user"
    tenant_id: str = "default"

    #: low | medium | high. Weights the case in the safety report.
    risk: str = "low"
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    # --- non-determinism ---------------------------------------------------- #
    #: How many times the whole conversation is run, each in a fresh session.
    samples: int = 1
    #: How many of those runs must pass. None means all of them.
    min_passing_samples: int | None = None

    #: The simulated user that answers the agent's questions. None: scripted answers only.
    persona: Persona | None = None

    def __post_init__(self) -> None:
        if not self.turns:
            raise DatasetError(f"case {self.id!r}: needs at least one turn.")
        if not any(turn.evaluate for turn in self.turns):
            raise DatasetError(
                f"case {self.id!r}: every turn has `evaluate: false`, so the case would "
                "run and assert nothing."
            )
        if self.samples < 1:
            raise DatasetError(f"case {self.id!r}: `samples` must be at least 1, got {self.samples}.")
        if self.min_passing_samples is not None and not 1 <= self.min_passing_samples <= self.samples:
            raise DatasetError(
                f"case {self.id!r}: `min_passing_samples` is {self.min_passing_samples} but "
                f"`samples` is {self.samples}. It must be between 1 and `samples`, or the "
                "case could never pass (or never fail)."
            )

    @property
    def required_samples(self) -> int:
        """The K of the K-of-N gate: passing samples this case needs."""
        return self.samples if self.min_passing_samples is None else self.min_passing_samples

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
        *,
        suite: str = "golden",
        defaults: Mapping[str, Any] | None = None,
        default_samples: int = 1,
        schema_version: int = SCHEMA_VERSION,
    ) -> TestCase:
        """Build a case from one entry of a suite file of `schema_version` (2 or 3)."""
        row = _present(data)
        case_id = str(row.get("id") or "").strip()
        if not case_id:
            raise DatasetError(f"Test case needs an 'id': {data!r}")
        where = f"case {case_id!r}"

        v3 = schema_version >= 3
        unknown = sorted(set(row) - _CASE_KEYS - _PASSTHROUGH - (_V3_CASE_KEYS if v3 else set()))
        if unknown:
            misplaced = [k for k in unknown if k in EXPECTATION_FIELDS or k == "message"]
            hint = (
                f" {misplaced} belong on a turn: `turns: [{{user: ..., expect: {{...}}}}]`."
                if misplaced
                else ""
            )
            if not v3 and "persona" in unknown:
                hint += " `persona` needs schema_version: 3."
            raise DatasetError(f"{where}: unknown key(s) {unknown}.{hint}")

        turns = row.get("turns")
        if not isinstance(turns, list) or not turns:
            raise DatasetError(f"{where}: needs `turns`, a list of at least one turn.")
        return cls(
            id=case_id,
            turns=[
                Turn.from_dict(
                    turn, defaults=defaults or {}, where=f"{where} turn {number}", clarifications=v3
                )
                for number, turn in enumerate(turns, start=1)
            ],
            persona=Persona.from_dict(row["persona"], where=where) if "persona" in row else None,
            **_identity(row, suite, default_samples, where),
        )

    @classmethod
    def from_row(
        cls, data: Mapping[str, Any], *, suite: str = "golden", default_samples: int = 1
    ) -> TestCase:
        """Build a one-turn case from a flat row, tolerating loose spreadsheet input."""
        row = _present(data)
        case_id = str(row.get("id") or row.get("case_id") or "").strip()
        message = str(row.get("message") or row.get("input") or row.get("question") or "").strip()
        if "turns" in row:
            raise DatasetError(
                f"case {case_id!r}: has `turns`, but a flat row is one turn by definition "
                "(a CSV or XLSX row, or a suite file with no schema_version). Multi-turn "
                f"cases go in a YAML or JSON suite that declares schema_version: {SCHEMA_VERSION}."
            )
        if not case_id or not message:
            raise DatasetError(f"Test case needs 'id' and 'message': {data!r}")

        expect = {name: row[name] for name in EXPECTATION_FIELDS if name in row}
        # Spreadsheets in the wild call these columns something else.
        expect["expected_docs"] = row.get("expected_docs") or row.get("evidence")
        expect["required_facts"] = row.get("required_facts") or row.get("facts")

        return cls(
            id=case_id,
            turns=[Turn(user=message, expect=Expectations.from_dict(expect, where=f"case {case_id!r}"))],
            **_identity(row, suite, default_samples, f"case {case_id!r}"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _identity(
    row: Mapping[str, Any], suite: str, default_samples: int, where: str
) -> dict[str, Any]:
    """The case-level fields both shapes share. `row` has no empty values.

    A case that does not say `samples` gets the config's `default_samples`, so a
    `min_passing_samples` written against a larger N than the default is refused
    at load rather than quietly never reachable.
    """
    samples = (
        _as_count(row["samples"], name="samples", where=where)
        if "samples" in row
        else default_samples
    )
    min_passing = (
        _as_count(row["min_passing_samples"], name="min_passing_samples", where=where)
        if "min_passing_samples" in row
        else None
    )
    metadata = dict(row.get("metadata") or {})
    metadata.update({k: row[k] for k in _PASSTHROUGH if k in row})
    return {
        "suite": str(row.get("suite") or suite),
        "description": str(row.get("description", "")),
        "user_id": str(row.get("user_id", "test_user")),
        "tenant_id": str(row.get("tenant_id", "default")),
        "risk": str(row.get("risk", "low")).lower(),
        "tags": _as_list(row.get("tags")),
        "metadata": metadata,
        "samples": samples,
        "min_passing_samples": min_passing,
    }
