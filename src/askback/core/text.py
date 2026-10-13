"""Small text utilities shared by retrieval, agents and evaluators.

Kept in one place so scoring stays consistent across the framework: the
tokenizer used to index documents is the same one used to grade groundedness.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")

# Common words carry no signal for matching; dropping them sharpens every score.
STOPWORDS: frozenset[str] = frozenset(
    ["a", "an", "the", "and", "or", "but", "if", "then", "than", "that", "this", "these", "those", "of", "in", "on", "at", "to", "for", "from", "by", "with", "is", "are", "was", "were", "be", "been", "being", "do", "does", "did", "doing", "have", "has", "had", "having", "i", "you", "he", "she", "it", "we", "they", "me", "my", "your", "our", "their", "as", "not", "no", "so", "such", "can", "could", "should", "would", "will", "shall", "may", "might", "must", "about", "into", "over", "under", "again", "there", "here", "what", "which", "who", "whom", "when", "where", "why", "how", "all", "any", "both", "each", "few", "more", "most", "other", "some", "only", "own", "same", "too", "very", "just", "now", "please", "tell"]
)


def fold_plural(token: str) -> str:
    """Fold a simple English plural onto its singular form.

    Deliberately conservative: it collapses `claims`/`claim` and `policies`/
    `policy`, which is where lexical matching most often fails, and leaves
    `business`, `status` and `analysis` alone. It is not a stemmer, so verb
    forms (`submit` vs `submitted`) still differ. Replace this with a real
    stemmer or embeddings when retrieval quality needs it; every score in the
    framework goes through here, so the change stays consistent everywhere.
    """
    if len(token) > 3 and token.endswith("ies"):
        return f"{token[:-3]}y"
    if len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def tokenize(text: str, *, drop_stopwords: bool = True) -> list[str]:
    """Lowercase alphanumeric tokens: the single tokenizer for the whole system.

    Stopwords are removed before folding so common words are never mangled.
    """
    tokens = _TOKEN_RE.findall((text or "").lower())
    if drop_stopwords:
        tokens = [t for t in tokens if t not in STOPWORDS]
    return [fold_plural(t) for t in tokens]


def sentences(text: str) -> list[str]:
    """Split into sentences. Deliberately simple and dependency free."""
    return [s.strip() for s in _SENTENCE_RE.split(text or "") if s.strip()]


def normalize(text: str) -> str:
    """Collapse whitespace and casing, for forgiving substring comparisons."""
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def tolerate_line_breaks(pattern: str) -> str:
    r"""Rewrite `pattern` so each literal space matches any run of whitespace.

    A phrase written with single spaces - "so I can check", "policy manual",
    `api[_ -]?key` - stops matching the moment a reply is hard-wrapped between two of
    its words, and a detector that loses a signal that way is worse than one that never
    had it: nothing fails, the signal is simply absent, and the miss reads as the agent's
    wording rather than as our bug. Replies wrap at whatever width their renderer chose,
    so a space in a pattern means "whitespace here", never "exactly one space".

    `" "` becomes `\s+` outside a character class and `\s` inside one, and a trailing
    `?`, `*` or `+` is folded in (`" ?"` becomes `\s*`, not `\s+?`, which would make the
    space required). Two things are deliberately left alone: an escaped `\ `, which is
    how a pattern asks for one real space, and a space quantified by `{m,n}`, where the
    count is of spaces and rewriting it would change what is counted.
    """
    out: list[str] = []
    index = 0
    class_start = -1  # >= 0 while inside [...]
    while index < len(pattern):
        char = pattern[index]
        if char == "\\":
            out.append(pattern[index : index + 2])
            index += 2
            continue
        if class_start >= 0:
            # A `]` first in the class (after an optional `^`) is a literal, not the end.
            if char == "]" and index > class_start + (2 if pattern[class_start + 1 : class_start + 2] == "^" else 1):
                class_start = -1
            out.append("\\s" if char == " " else char)
            index += 1
            continue
        if char == "[":
            class_start = index
            out.append(char)
            index += 1
            continue
        if char != " ":
            out.append(char)
            index += 1
            continue
        rest = pattern[index:]
        run = len(rest) - len(rest.lstrip(" "))
        following = pattern[index + run : index + run + 1]
        if following in {"?", "*"}:
            out.append("\\s*")
            index += run + 1
        elif following == "+":
            out.append("\\s+")
            index += run + 1
        elif following == "{":
            out.append(" " * run)
            index += run
        else:
            out.append("\\s+")
            index += run
    return "".join(out)


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    """Set overlap in [0, 1]. Used where token order does not matter."""
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def coverage(claim_tokens: Iterable[str], source_tokens: Iterable[str]) -> float:
    """Fraction of claim tokens present in the source.

    Asymmetric on purpose: groundedness asks whether every word of the claim is
    backed by the source, not whether the two texts resemble each other.
    """
    claim = list(claim_tokens)
    if not claim:
        return 0.0
    source = set(source_tokens)
    return sum(1 for t in claim if t in source) / len(claim)


def cosine(a: Counter[str], b: Counter[str]) -> float:
    """Cosine similarity between two bag-of-words counters."""
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    dot = sum(a[t] * b[t] for t in common)
    if not dot:
        return 0.0
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb)
