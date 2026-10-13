"""Convert v1 suite files (one message per case) to schema v2 (a list of turns).

    python scripts/migrate_v1_v2.py suites

Each case becomes a single-turn case. Identity stays on the case, the message
becomes the turn's `user`, and every expectation moves under the turn's
`expect`. Nothing is added or dropped, so a converted suite grades exactly as
it did. CSV and XLSX files are left alone: a row is one turn by definition.

YAML is rewritten as text, not re-dumped, because the comments in a suite file
are part of it: they say why a case exists and which failures are known gaps.
The rewrite is then parsed and compared with a plain conversion of the data.
If the two differ, or the file is not laid out the way the rewriter reads (a
block list of cases under a top-level `cases:`), the file is written from the
data instead, and the script says the comments were lost.

Standalone on purpose. It imports nothing from askback, so it still runs
after the loader stops reading v1, which is when people will need it.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SCHEMA_VERSION = 2

CASE_KEYS = frozenset(
    {"id", "suite", "description", "user_id", "tenant_id", "risk", "tags", "metadata"}
    # Kept by v1 as metadata, and by v2 the same way.
    | {"notes", "owner", "source", "author", "ticket"}
)
EXPECT_KEYS = frozenset(
    {
        "expected_route", "expected_intent", "expected_status", "expected_tools",
        "forbidden_tools", "expected_docs", "required_facts", "prohibited_content",
        "min_citations", "reference_answer", "must_refuse",
    }
)
#: Column names v1 accepted from spreadsheets. v2 has one name per field.
ALIASES = {
    "case_id": "id",
    "input": "message",
    "question": "message",
    "evidence": "expected_docs",
    "facts": "required_facts",
}

_KEY = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*):(?P<rest>\s.*)?$")
_CASES = re.compile(r"^(cases|tests):(?P<rest>\s*(#.*)?)$")


class Unconvertible(Exception):
    """The file cannot be converted without a decision from its author."""


class _Unsupported(Exception):
    """The text rewriter does not read this layout; convert from the data instead."""


# --------------------------------------------------------------------------- #
# Data conversion: the definition of what v1 -> v2 means
# --------------------------------------------------------------------------- #


def convert_case(row: dict[str, Any]) -> dict[str, Any]:
    case: dict[str, Any] = {}
    expect: dict[str, Any] = {}
    message: Any = None
    label = row.get("id") or row.get("case_id") or "?"

    for key, value in row.items():
        if value is None or value == "":
            continue  # v1 read an empty value as not written
        name = ALIASES.get(key, key)
        if name == "message":
            taken = message is not None
        elif name in CASE_KEYS:
            taken = name in case
        elif name in EXPECT_KEYS:
            taken = name in expect
        else:
            raise Unconvertible(
                f"case {label!r}: unknown key {key!r}. v1 ignored it and v2 refuses "
                "unknown keys. Delete it, or move it under `metadata`."
            )
        if taken:
            raise Unconvertible(
                f"case {label!r}: {key!r} repeats another column for {name!r}. Keep one."
            )
        if name == "message":
            message = value
        elif name in CASE_KEYS:
            case[name] = value
        else:
            expect[name] = value

    if "id" not in case or message is None:
        raise Unconvertible(f"case {label!r}: needs an id and a message.")
    turn: dict[str, Any] = {"user": message}
    if expect:
        turn["expect"] = expect
    return {**case, "turns": [turn]}


def convert_document(data: Any) -> dict[str, Any]:
    extras: dict[str, Any] = {}
    rows = data
    if isinstance(data, dict):
        rows = data.get("cases", data.get("tests", []))
        extras = {k: v for k, v in data.items() if k not in ("cases", "tests", "schema_version")}
        stray = sorted(k for k in extras if not str(k).startswith("_"))
        if stray:
            raise Unconvertible(
                f"unknown top-level key(s) {stray}. v2 allows schema_version, defaults, "
                "cases, and `_`-prefixed comments."
            )
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise Unconvertible("expected a list of case mappings, bare or under `cases:`.")
    return {"schema_version": SCHEMA_VERSION, **extras, "cases": [convert_case(r) for r in rows]}


# --------------------------------------------------------------------------- #
# YAML text rewrite: the same conversion, keeping comments and layout
# --------------------------------------------------------------------------- #


@dataclass
class _Entry:
    """One `key: value` of a case, with the lines that travel with it."""

    key: str
    rest: str
    lead: list[str] = field(default_factory=list)
    continuation: list[str] = field(default_factory=list)


@dataclass
class _Item:
    lead: list[str]
    entries: list[_Entry] = field(default_factory=list)


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _shift(lines: list[str], by: int) -> list[str]:
    return [" " * by + line if line.strip() else line for line in lines]


def rewrite_yaml(text: str) -> str:
    lines = text.split("\n")
    opening = next(((i, m) for i, line in enumerate(lines) if (m := _CASES.match(line))), None)
    if opening is None:
        raise _Unsupported("no top-level `cases:` block")
    start, header = opening

    items: list[_Item] = []
    pending: list[str] = []
    dash = -1
    for number, line in enumerate(lines[start + 1 :], start=start + 2):
        stripped = line.strip()
        # Held until the next line says whose they are: the next key's, or
        # more of a block scalar.
        if not stripped or stripped.startswith("#"):
            pending.append(line)
            continue
        indent = _indent_of(line)
        if dash < 0 and stripped.startswith("- "):
            dash = indent

        if indent == dash and stripped.startswith("- "):
            match = _KEY.match(stripped[2:])
            if not match:
                raise _Unsupported(f"line {number}: a case that does not open with `- key:`")
            items.append(_Item(lead=pending))
            items[-1].entries.append(_Entry(match["key"], match["rest"] or ""))
        elif items and indent == dash + 2 and (match := _KEY.match(stripped)):
            items[-1].entries.append(_Entry(match["key"], match["rest"] or "", lead=pending))
        elif items and indent > dash + 2:
            items[-1].entries[-1].continuation += [*pending, line]
        else:
            raise _Unsupported(f"line {number}: not part of a block list of cases")
        pending = []

    out = [*lines[:start], f"schema_version: {SCHEMA_VERSION}", "", "cases:" + header["rest"]]
    for item in items:
        out += _rewrite_item(item, dash)
    return "\n".join(out + pending)


def _rewrite_item(item: _Item, dash: int) -> list[str]:
    pad = " " * (dash + 2)
    case: list[tuple[str, _Entry]] = []
    expect: list[tuple[str, _Entry]] = []
    message: _Entry | None = None
    for entry in item.entries:
        name = ALIASES.get(entry.key, entry.key)
        if name == "message":
            message = entry
        elif name in CASE_KEYS:
            case.append((name, entry))
        else:
            expect.append((name, entry))
    if message is None or not case:
        raise _Unsupported("a case without a message or without identity keys")

    out = list(item.lead)
    for position, (name, entry) in enumerate(case):
        out += entry.lead
        out.append(f"{' ' * dash + '- ' if position == 0 else pad}{name}:{entry.rest}")
        out += entry.continuation
    # A comment above the message is about the case; it stays at case level.
    out += message.lead
    out.append(f"{pad}turns:")
    out.append(f"{pad}  - user:{message.rest}")
    out += _shift(message.continuation, 4)
    if expect:
        out.append(f"{pad}    expect:")
        for name, entry in expect:
            out += _shift(entry.lead, 6)
            out.append(f"{pad}      {name}:{entry.rest}")
            out += _shift(entry.continuation, 6)
    return out


# --------------------------------------------------------------------------- #
# Files
# --------------------------------------------------------------------------- #


def migrate_file(path: Path) -> str:
    """Convert one file in place. Returns what was done, for the log line."""
    text = path.read_text(encoding="utf-8")
    is_json = path.suffix.lower() == ".json"
    data = json.loads(text) if is_json else yaml.safe_load(text) or []

    declared = data.get("schema_version", 1) if isinstance(data, dict) else 1
    if declared == SCHEMA_VERSION:
        return "already v2, left alone"
    if declared != 1:
        raise Unconvertible(f"schema_version {declared!r}: this script converts v1 only.")
    converted = convert_document(data)

    note = ""
    if is_json:
        result = json.dumps(converted, indent=2, ensure_ascii=False) + "\n"
    else:
        try:
            result = rewrite_yaml(text)
            if yaml.safe_load(result) != converted:
                raise _Unsupported("the rewritten text does not parse to the converted data")
        except _Unsupported as exc:
            result = yaml.safe_dump(converted, sort_keys=False, allow_unicode=True, width=100)
            note = f" COMMENTS LOST: rewritten from data ({exc})"

    path.write_text(result, encoding="utf-8", newline="\n")
    return f"converted {len(converted['cases'])} case(s).{note}"


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: python scripts/migrate_v1_v2.py <suite file or directory>...", file=sys.stderr)
        return 2

    files: list[Path] = []
    for arg in argv:
        target = Path(arg)
        if not target.exists():
            print(f"{target}: not found", file=sys.stderr)
            return 2
        files += sorted(p for p in target.iterdir() if p.is_file()) if target.is_dir() else [target]

    failed = False
    for path in files:
        suffix = path.suffix.lower()
        if suffix in (".csv", ".xlsx"):
            print(f"{path}: tabular, one turn per row by definition; nothing to convert")
        elif suffix in (".yaml", ".yml", ".json"):
            try:
                print(f"{path}: {migrate_file(path)}")
            except Unconvertible as exc:
                failed = True
                print(f"{path}: NOT converted. {exc}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
