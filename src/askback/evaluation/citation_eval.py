"""Do the sources an agent cites exist? A check against the map of the source document.

`rag.citation_support` asks whether a cited doc_id is among the chunks the agent itself
returned. That is self-consistency, and it cannot catch an invented chapter: an agent
that hallucinates a source hallucinates it consistently. This evaluator resolves a
citation against something the agent did not write - a manifest of the source document's
structure, kept by hand, roughly its contents page:

    name: Harborview Staff Handbook 2025
    aliases: [Harborview Staff Handbook, the staff handbook]
    total_pages: 300
    chapters:
      - number: 5
        title: Workplace Safety, Conduct & Corrective Action
        aliases: [Conduct Rules, Corrective Action Procedure]
        pages: [28, 45]

and reads the citations out of the reply's PROSE, because the agents this is for write
their sources into the answer text and return no citation field. It catches:

  - a chapter that does not exist
  - a page outside the chapter it is attributed to
  - a page beyond the end of the document
  - a real chapter cited under the wrong title
  - a document name the manifest does not know (a wrong year included)

WHAT THIS PROVES, AND WHAT IT DOES NOT. It proves the agent pointed somewhere real. It
does NOT prove the cited passage supports the claim: "Chapter 5, pages 32-33" resolves
whatever the sentence beside it says, and an agent that cites a real chapter for an
unrelated assertion passes. Necessary, not sufficient. Read a green result as "none of the
citations was invented", never as "the answer is sourced".

How citations are read, and where that is weak:
  - Hard wraps are undone first (`logical_lines`): a reply read as lines would otherwise
    have a title, or a document name, only when the renderer happened not to wrap there.
    A line that starts with a bullet, a number, a quote or a table pipe is the writer's
    own line and is kept, so a list stays a list.
  - A chapter is "Chapter N". Its title is the text after a colon or dash (hyphen, en-dash
    or em-dash alike: a document pipeline emits en-dashes, and a hyphen-only pattern
    silently misses them) or inside parentheses, cut at a page clause and at a trailing
    "of the <document>", and taken as a claimed title only when it is title-cased, so a
    colon introducing prose is not read as one. A title matches the manifest's title or an
    alias when the normalised text is equal or one contains the other, so a trimmed title
    passes and a different one does not.
  - A page is "page N", "pages N-M", "pages N and M", "p. N" or "pp. N-M".
  - A page is attributed to the chapter on its own line (the nearest one before it), else
    to the block's chapter when the block cites exactly one. When it cannot be attributed
    it is checked against the document's length only, and nothing more: a wrong guess
    would fail a correct answer. This means a page cited far from its chapter can go
    unchecked against that chapter's range.
  - A document name is found after "According to the", before ", page(s)", or after "of
    the". Only a name that looks like a document (manual, handbook, policy, guide ...) and
    is neither the manifest's document nor one of its chapters is reported unknown. This is
    a heuristic. The chapter and page checks are the precise part.

Opt-in per turn: `expect.verify_citations: true`. With no manifest configured the turn is
SKIPPED, visibly, and never passed. So is a reply from which no citation could be read:
nothing was verified, and that is not the same as everything having resolved. A citation
that does not resolve is CRITICAL, because a confident citation of something that does not
exist reads as more authoritative than no citation at all.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from ..core.contracts import ChatResponse, Severity
from ..core.errors import ConfigError
from ..core.registry import EVALUATORS
from ..datasets.schema import TestCase, Turn
from .base import Check, EvalResult, Evaluator

CITATION_EXISTENCE = "citation_existence"

# --------------------------------------------------------------------------- #
# The manifest
# --------------------------------------------------------------------------- #

_MANIFEST_KEYS = {"name", "aliases", "total_pages", "chapters"}
_CHAPTER_KEYS = {"number", "title", "aliases", "pages"}


@dataclass(frozen=True, slots=True)
class Chapter:
    number: int
    title: str
    aliases: tuple[str, ...]
    first_page: int
    last_page: int

    def names(self) -> list[str]:
        return [self.title, *self.aliases]


@dataclass(frozen=True, slots=True)
class Manifest:
    """A hand-kept structural map of the source document."""

    name: str
    aliases: tuple[str, ...]
    total_pages: int
    chapters: Mapping[int, Chapter] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Any, *, where: str = "manifest") -> Manifest:
        if not isinstance(data, Mapping):
            raise ConfigError(f"{where}: must be a mapping, got {type(data).__name__}")
        unknown = sorted(set(data) - _MANIFEST_KEYS)
        if unknown:
            raise ConfigError(f"{where}: unknown key(s) {unknown}; known: {sorted(_MANIFEST_KEYS)}")
        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"{where}: `name` is required")
        total = _int(data.get("total_pages"), f"{where}.total_pages", minimum=1)
        raw_chapters = data.get("chapters")
        if not isinstance(raw_chapters, list) or not raw_chapters:
            raise ConfigError(f"{where}: `chapters` must be a non-empty list")
        chapters: dict[int, Chapter] = {}
        for index, raw in enumerate(raw_chapters):
            at = f"{where}.chapters[{index}]"
            if not isinstance(raw, Mapping):
                raise ConfigError(f"{at}: must be a mapping")
            unknown = sorted(set(raw) - _CHAPTER_KEYS)
            if unknown:
                raise ConfigError(f"{at}: unknown key(s) {unknown}; known: {sorted(_CHAPTER_KEYS)}")
            number = _int(raw.get("number"), f"{at}.number", minimum=1)
            title = raw.get("title")
            if not isinstance(title, str) or not title.strip():
                raise ConfigError(f"{at}: `title` is required")
            pages = raw.get("pages")
            if not isinstance(pages, list) or len(pages) != 2:
                raise ConfigError(f"{at}.pages must be [first, last]")
            first = _int(pages[0], f"{at}.pages[0]", minimum=1)
            last = _int(pages[1], f"{at}.pages[1]", minimum=first)
            if last > total:
                raise ConfigError(f"{at}: pages end at {last}, past total_pages {total}")
            if number in chapters:
                raise ConfigError(f"{at}: chapter {number} is listed twice")
            chapters[number] = Chapter(number, title.strip(), _names(raw, "aliases", at), first, last)
        return cls(name.strip(), _names(data, "aliases", where), total, chapters)

    def knows_document(self, name: str) -> bool:
        return _norm(name) in {_norm(n) for n in (self.name, *self.aliases)}

    def chapter_named(self, name: str) -> bool:
        key = _norm(name)
        return any(key == _norm(n) for c in self.chapters.values() for n in c.names())


def load_manifest(path: str | Path) -> Manifest:
    """Read a manifest file. A missing or malformed one is a `ConfigError`, never an empty map."""
    target = Path(path)
    try:
        import yaml

        data = yaml.safe_load(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"citation manifest {target} could not be read: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"citation manifest {target} is not valid YAML: {exc}") from exc
    return Manifest.from_dict(data, where=str(target))


def _int(value: Any, where: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError(f"{where} must be a whole number >= {minimum}, got {value!r}")
    return value


def _names(data: Mapping[str, Any], key: str, where: str) -> tuple[str, ...]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ConfigError(f"{where}.{key} must be a list of names, got {value!r}")
    return tuple(v.strip() for v in value)


# --------------------------------------------------------------------------- #
# Reading citations out of prose
# --------------------------------------------------------------------------- #

# Hyphen, the unicode hyphens, figure dash, en-dash, em-dash and minus. Documents are
# produced by pipelines that emit en-dashes; a pattern with only "-" misses them silently.
_DASHES = "\\-\u2010\u2011\u2012\u2013\u2014\u2212"
_RANGE = rf"\s*[{_DASHES}]\s*"
_TITLE_SEPARATORS = ":\u2013\u2014\u2212\u2012\\-"

_CHAPTER = re.compile(r"\bchapter\s+(\d+)\b", re.IGNORECASE)
_PAGE = re.compile(
    rf"\b(?:pages?|pp?\.)\s*(\d+)(?:{_RANGE}(\d+)|\s*(?:,|and|&)\s*(\d+))?", re.IGNORECASE
)
_PAGE_CLAUSE = re.compile(r"[,;(]?\s*\b(?:on\s+)?(?:pages?|pp?\.)\s*\d", re.IGNORECASE)
_OF_THE = re.compile(r"\s+(?:of|in|from)\s+the\s+", re.IGNORECASE)
#: A run of capitalised words (and years), allowing "of/and/&/for" between them.
_NAME = (
    r"[A-Z][\w&'\u2019.-]*(?:\s+(?:(?:of|and|&|for)\s+)?(?:[A-Z][\w&'\u2019-]*|\d{4}))*"
)
# The lead-in is case-insensitive, the name is not: "According to the Harborview Staff Handbook".
_ACCORDING = re.compile(rf"(?i:\baccording to (?:the )?)({_NAME})")
_OF_THE_NAME = re.compile(rf"(?i:\b(?:of|in|from|per|see) the )({_NAME})")
_DOC_WORD = re.compile(
    r"\b(?:manual|handbook|guide|guidelines|policy|policies|code|regulations|rulebook)\b",
    re.IGNORECASE,
)


def _norm(text: str) -> str:
    text = text.lower().replace("&", " and ").replace("\u2019", "'")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[4:] if text.startswith("the ") else text


@dataclass(frozen=True, slots=True)
class ChapterRef:
    number: int
    title: str | None
    line: int


@dataclass(frozen=True, slots=True)
class PageRef:
    first: int
    last: int
    line: int
    #: The chapter this page is attributed to, when there is a defensible one.
    chapter: int | None


@dataclass(frozen=True, slots=True)
class DocumentRef:
    name: str


@dataclass(frozen=True, slots=True)
class Citations:
    chapters: list[ChapterRef]
    pages: list[PageRef]
    documents: list[DocumentRef]

    def __bool__(self) -> bool:
        return bool(self.chapters or self.pages or self.documents)


#: What starts a new line on purpose: a bullet, a number, a quote, a table row, a heading.
_STRUCTURAL_LINE = re.compile(r"\s*(?:[-*•+>|#]|\d+[.)])(?:\s|$)")


def logical_lines(reply: str) -> list[str]:
    """The reply's lines with hard wraps undone, blank lines kept as block separators.

    A citation is read within a line, so where the renderer wrapped decided whether
    "Chapter 5:\\nWorkplace Safety" had a title at all and whether the document name
    before ",\\npages 32-33" was seen. Neither is the agent's doing. A line that begins
    with a bullet, a number, a quote or a table pipe is a line the writer meant, so it
    stays one: that is what keeps the items of a list from being read as one sentence,
    and with them the refusal to pin a page on one of two chapters.
    """
    out: list[str] = []
    for raw in reply.splitlines():
        if not raw.strip():
            out.append("")
            continue
        if out and out[-1] and not _STRUCTURAL_LINE.match(raw):
            out[-1] = f"{out[-1]} {raw.strip()}"
            continue
        out.append(raw)
    return out


def parse_citations(reply: str) -> Citations:
    """Every chapter, page and document reference in `reply`, with pages attributed."""
    lines = logical_lines(reply)
    chapters: list[ChapterRef] = []
    pages: list[PageRef] = []
    documents: list[DocumentRef] = []
    block_of = _blocks(lines)
    per_line: list[list[tuple[int, int, int]]] = []  # (position, chapter number) per line

    for index, line in enumerate(lines):
        found: list[tuple[int, int, int]] = []
        spans: list[tuple[int, int]] = []  # text that is a chapter reference, not a document
        for match in _CHAPTER.finditer(line):
            title, title_end, tail_doc = _read_title(line, match.end())
            chapters.append(ChapterRef(int(match.group(1)), title, index))
            found.append((match.start(), int(match.group(1)), index))
            spans.append((match.start(), title_end))
            if tail_doc:
                documents.append(DocumentRef(tail_doc))
        per_line.append(found)
        documents.extend(_documents_in(line, spans))

    for index, line in enumerate(lines):
        for match in _PAGE.finditer(line):
            first = int(match.group(1))
            if match.group(2) is not None:
                span = (first, int(match.group(2)))
            elif match.group(3) is not None:
                span = (min(first, int(match.group(3))), max(first, int(match.group(3))))
            else:
                span = (first, first)
            pages.append(
                PageRef(*span, line=index, chapter=_attribute(match.start(), index, per_line, block_of))
            )
    return Citations(chapters, pages, _unique(documents))


def _blocks(lines: Sequence[str]) -> list[range]:
    """For each line, the contiguous run of non-blank lines it sits in."""
    blocks: list[range] = [range(0)] * len(lines)
    start = 0
    for index in range(len(lines) + 1):
        if index == len(lines) or not lines[index].strip():
            for member in range(start, index):
                blocks[member] = range(start, index)
            start = index + 1
    return blocks


def _attribute(
    position: int, line: int, per_line: list[list[tuple[int, int, int]]], block_of: list[range]
) -> int | None:
    """Which chapter a page belongs to: the nearest before it on its line, else the block's only one."""
    before = [number for pos, number, _ in per_line[line] if pos < position]
    if before:
        return before[-1]
    if per_line[line]:
        return per_line[line][0][1] if len(per_line[line]) == 1 else None
    in_block = {number for member in block_of[line] for _, number, _ in per_line[member]}
    return next(iter(in_block)) if len(in_block) == 1 else None


def _read_title(line: str, after: int) -> tuple[str | None, int, str | None]:
    """The title claimed after "Chapter N", where it ends, and a trailing document name."""
    tail = line[after:]
    stripped = tail.lstrip()
    offset = after + (len(tail) - len(stripped))
    raw: str | None = None
    if stripped[:1] and stripped[0] in _TITLE_SEPARATORS and not stripped[1:2].isdigit():
        raw = stripped[1:]
        offset += 1
    elif stripped.startswith("(") and ")" in stripped:
        raw = stripped[1 : stripped.index(")")]
        offset += 1
    if raw is None:
        return None, after, None
    clause = _PAGE_CLAUSE.search(raw)
    if clause:
        raw = raw[: clause.start()]
    sentence = re.search(r"\.(?:\s|$)", raw)
    if sentence:
        raw = raw[: sentence.start()]
    document: str | None = None
    parts = list(_OF_THE.finditer(raw))
    if parts:
        candidate = raw[parts[-1].end() :].strip(" .:,;")
        if _DOC_WORD.search(candidate) and candidate[:1].isupper():
            document, raw = candidate, raw[: parts[-1].start()]
    title = raw.strip(" .:,;\u2013\u2014-")
    end = offset + len(raw)
    if document is not None:
        end = len(line)
    if not _looks_like_a_title(title):
        return None, end, document
    return (title or None), end, document


#: Words a title may carry in lower case; everything else in one must be capitalised.
_TITLE_CONNECTORS = frozenset({"of", "and", "for", "the", "to", "in", "or", "a", "an", "with", "on"})


def _looks_like_a_title(text: str) -> bool:
    """Is the text after "Chapter N:" a title, or a sentence that happens to follow a colon?

    It matters because of where the colon can be: "Chapter 5:\\nStaff must not remove
    safety equipment" is a lead-in to prose, and reading it as a claimed title would fail
    a correct answer for naming a chapter wrongly - the worst kind of false positive this
    evaluator can produce. A title is title-cased ("Career Progression Policy"); prose is
    not. The cost is that a manifest whose titles are in sentence case has its claimed
    titles left unchecked: a miss, which invariant 1 prefers to a wrong verdict.
    """
    words = [w for w in re.split(r"[\s,/]+", text) if w.strip(" .:;-")]
    if not words:
        return False
    return all(
        w.lower() in _TITLE_CONNECTORS or not w[:1].islower()
        for w in words
    )


def _documents_in(line: str, spans: list[tuple[int, int]]) -> list[DocumentRef]:
    """Document-looking names the line cites, leaving out chapter titles."""
    names: list[str] = []
    for match in _ACCORDING.finditer(line):
        names.append(match.group(1))
    for match in _OF_THE_NAME.finditer(line):
        names.append(match.group(1))
    for page in _PAGE.finditer(line):
        trailing = re.search(rf"({_NAME})\s*,?\s*$", line[: page.start()])
        if trailing:
            names.append(trailing.group(1))
    kept: list[DocumentRef] = []
    for name in names:
        start = line.find(name)
        if any(a <= start < b for a, b in spans):
            continue
        kept.append(DocumentRef(name.strip(" .:,;")))
    return kept


def _unique(documents: list[DocumentRef]) -> list[DocumentRef]:
    seen: set[str] = set()
    out: list[DocumentRef] = []
    for doc in documents:
        key = _norm(doc.name)
        if key not in seen:
            seen.add(key)
            out.append(doc)
    return out


# --------------------------------------------------------------------------- #
# Resolving them
# --------------------------------------------------------------------------- #


def _title_matches(claimed: str, chapter: Chapter) -> bool:
    claim = _norm(claimed)
    for name in chapter.names():
        known = _norm(name)
        if claim == known:
            return True
        shorter, longer = sorted((claim, known), key=len)
        # A trimmed title passes; one word in common does not.
        if len(shorter.split()) >= 2 and shorter in longer:
            return True
    return False


@dataclass(frozen=True, slots=True)
class Resolution:
    """What each kind of citation came to. A kind with no citations has no entry."""

    chapter: list[str] | None = None
    title: list[str] | None = None
    page: list[str] | None = None
    document: list[str] | None = None


def resolve(citations: Citations, manifest: Manifest) -> Resolution:
    """The problems found, by kind. An empty list means every citation of that kind resolved."""
    chapter_problems: list[str] = []
    title_problems: list[str] = []
    numbers = sorted(manifest.chapters)
    for ref in citations.chapters:
        chapter = manifest.chapters.get(ref.number)
        if chapter is None:
            chapter_problems.append(
                f"Chapter {ref.number} does not exist ({manifest.name} has chapters "
                f"{numbers[0]}-{numbers[-1]})"
            )
        elif ref.title is not None and not _title_matches(ref.title, chapter):
            title_problems.append(
                f"Chapter {ref.number} is {chapter.title!r}, not {ref.title!r}"
            )

    page_problems: list[str] = []
    for page in citations.pages:
        label = f"page {page.first}" if page.first == page.last else f"pages {page.first}-{page.last}"
        if page.first > page.last or page.first < 1:
            page_problems.append(f"{label} is not a valid page range")
            continue
        if page.last > manifest.total_pages:
            page_problems.append(f"{label} is beyond the document, which has {manifest.total_pages} pages")
            continue
        chapter = manifest.chapters.get(page.chapter) if page.chapter is not None else None
        if chapter is not None and not (chapter.first_page <= page.first and page.last <= chapter.last_page):
            page_problems.append(
                f"{label} is outside Chapter {chapter.number}, which is pages "
                f"{chapter.first_page}-{chapter.last_page}"
            )

    document_problems: list[str] = []
    for doc in citations.documents:
        if manifest.knows_document(doc.name) or manifest.chapter_named(doc.name):
            continue
        if _DOC_WORD.search(doc.name):
            document_problems.append(f"{doc.name!r} is not {manifest.name!r} or a known alias")
    documents_seen = [d for d in citations.documents if _DOC_WORD.search(d.name) or manifest.knows_document(d.name)]

    return Resolution(
        chapter=chapter_problems if citations.chapters else None,
        title=title_problems if any(c.title for c in citations.chapters) else None,
        page=page_problems if citations.pages else None,
        document=document_problems if documents_seen else None,
    )


# --------------------------------------------------------------------------- #
# The evaluator
# --------------------------------------------------------------------------- #


def unconfigured_result() -> EvalResult:
    """A turn asked for citation verification and there is no manifest: skipped, and visibly so."""
    return _skipped(
        "citations_resolve",
        "verify_citations is set, but no citation manifest is configured, so nothing was "
        "verified. Set evaluation.citations.manifest to a manifest file.",
    )


def _skipped(name: str, detail: str) -> EvalResult:
    check = Evaluator.check(name, False, detail=detail, expected="a citation to verify")
    return EvalResult(evaluator=CITATION_EXISTENCE, score=0.0, passed=False, checks=[check], skipped=True)


@EVALUATORS.register(CITATION_EXISTENCE)
class CitationExistenceEvaluator(Evaluator):
    """Resolves the citations in a reply against the document manifest. See the module docstring."""

    name: ClassVar[str] = CITATION_EXISTENCE
    description: ClassVar[str] = "Cited chapters, pages and documents exist in the source manifest."

    def __init__(self, *, manifest: Manifest | None = None, **options: Any) -> None:
        super().__init__(**options)
        self.manifest = manifest

    def evaluate(self, case: TestCase, turn: Turn, response: ChatResponse) -> EvalResult:
        # Opt-in: a turn that did not ask has nothing for this to say.
        if not turn.expect.verify_citations:
            return EvalResult(evaluator=self.name, score=1.0, passed=True, skipped=True)
        if self.manifest is None:
            return unconfigured_result()

        citations = parse_citations(response.reply)
        if not citations:
            return _skipped(
                "citations_found",
                "no chapter, page or document citation could be read from the reply, so "
                "none was verified. That is not the same as every citation being real.",
            )
        found = resolve(citations, self.manifest)
        checks = [
            Check(
                name=name,
                passed=not problems,
                score=0.0 if problems else 1.0,
                detail="; ".join(problems) if problems else f"every cited {kind} exists.",
                severity=Severity.CRITICAL,
                expected=f"cited {kind} exists in {self.manifest.name}",
                actual=problems or "ok",
            )
            for name, kind, problems in (
                ("citation_chapter", "chapter", found.chapter),
                ("citation_title", "chapter title", found.title),
                ("citation_page", "page", found.page),
                ("citation_document", "document", found.document),
            )
            if problems is not None
        ]
        return self.build(checks)


__all__ = [
    "CITATION_EXISTENCE",
    "Chapter",
    "CitationExistenceEvaluator",
    "Citations",
    "Manifest",
    "Resolution",
    "load_manifest",
    "logical_lines",
    "parse_citations",
    "resolve",
    "unconfigured_result",
]
