"""Chunking strategies, and the harness that decides between them with numbers.

Chunking is a **one-way door**. Re-chunking a corpus means re-embedding it, re-indexing it,
and re-running every evaluation that was ever quoted from it. So the choice is made once,
slowly, on a sample, with measurements — and the measurements are committed next to the
decision so a reader can disagree with the conclusion rather than with the vibe.

Four strategies, and the differences are about *where a boundary falls*, not about size:

``fixed``
    Character windows with overlap. Cheap, predictable, and it cuts a sentence in half
    roughly whenever it feels like it. The baseline everything else has to beat.

``sentence``
    Windows that end on sentence boundaries. Almost free, and it removes the single most
    common retrieval failure — a chunk that begins mid-clause and reads as nonsense.

``paragraph``
    One chunk per paragraph, merged until a floor is reached. Follows the author's own
    structure, which in a regulation is a real signal: paragraph breaks in ESRS separate
    *requirements*, and a chunk that spans two of them retrieves for both and satisfies
    neither.

``hierarchical``
    Paragraph chunks that carry their heading path as a prefix. Costs tokens on every chunk
    and buys disambiguation: "§44(a)" means nothing without "ESRS E1-6 Gross GHG emissions"
    above it, and a query mentioning Scope 1 will otherwise match the wrong article.

Everything here is deterministic and pure. Same input, same chunks, every time — which is
what lets the bake-off be replayed from a recording instead of re-run against a live index.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_PARAGRAPH = re.compile(r"\n\s*\n")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


class Strategy(StrEnum):
    FIXED = "fixed"
    SENTENCE = "sentence"
    PARAGRAPH = "paragraph"
    HIERARCHICAL = "hierarchical"


@dataclass(frozen=True, slots=True)
class Chunk:
    text: str
    #: Ordinal within the document. Part of the id, so a re-chunk that shifts boundaries
    #: produces different ids and cannot be mistaken for the previous index.
    ordinal: int
    document_id: str
    heading_path: tuple[str, ...] = ()
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def id(self) -> str:
        digest = hashlib.sha256(
            f"{self.document_id}|{self.ordinal}|{self.text}".encode()
        ).hexdigest()
        return f"ev:{digest[:8]}"

    @property
    def tokens(self) -> int:
        """A crude token estimate. Good enough to compare strategies; never used for billing."""
        return max(1, len(self.text) // 4)


@dataclass(frozen=True, slots=True)
class ChunkingConfig:
    strategy: Strategy = Strategy.HIERARCHICAL
    max_characters: int = 1200
    overlap: int = 150
    #: Merge paragraphs upward until they reach this, so a one-line paragraph is not a chunk.
    min_characters: int = 300

    def describe(self) -> str:
        return (
            f"{self.strategy.value}(max={self.max_characters}, "
            f"overlap={self.overlap}, min={self.min_characters})"
        )


def chunk(
    text: str,
    *,
    document_id: str,
    config: ChunkingConfig,
    metadata: dict[str, str] | None = None,
) -> tuple[Chunk, ...]:
    strategies: dict[
        Strategy, Callable[[str, ChunkingConfig], Iterator[tuple[str, tuple[str, ...]]]]
    ] = {
        Strategy.FIXED: _fixed,
        Strategy.SENTENCE: _sentence,
        Strategy.PARAGRAPH: _paragraph,
        Strategy.HIERARCHICAL: _hierarchical,
    }
    produced = strategies[config.strategy](text, config)
    return tuple(
        Chunk(
            text=body,
            ordinal=index,
            document_id=document_id,
            heading_path=path,
            metadata=dict(metadata or {}),
        )
        for index, (body, path) in enumerate(produced)
    )


def _fixed(text: str, config: ChunkingConfig) -> Iterator[tuple[str, tuple[str, ...]]]:
    stride = max(1, config.max_characters - config.overlap)
    body = " ".join(text.split())
    for start in range(0, len(body), stride):
        window = body[start : start + config.max_characters]
        if window.strip():
            yield window.strip(), ()
        if start + config.max_characters >= len(body):
            break


def _sentence(text: str, config: ChunkingConfig) -> Iterator[tuple[str, tuple[str, ...]]]:
    sentences = [s.strip() for s in _SENTENCE_END.split(" ".join(text.split())) if s.strip()]
    current: list[str] = []
    length = 0
    for sentence in sentences:
        if current and length + len(sentence) > config.max_characters:
            yield " ".join(current), ()
            # Overlap by carrying the last sentence forward: a retrieved chunk that starts
            # with the previous sentence's conclusion reads as continuous prose.
            current = current[-1:] if config.overlap else []
            length = sum(len(part) for part in current)
        current.append(sentence)
        length += len(sentence)
    if current:
        yield " ".join(current), ()


def _paragraph(text: str, config: ChunkingConfig) -> Iterator[tuple[str, tuple[str, ...]]]:
    for body, _ in _paragraph_with_headings(text, config, carry_headings=False):
        yield body, ()


def _hierarchical(text: str, config: ChunkingConfig) -> Iterator[tuple[str, tuple[str, ...]]]:
    yield from _paragraph_with_headings(text, config, carry_headings=True)


def _paragraph_with_headings(
    text: str, config: ChunkingConfig, *, carry_headings: bool
) -> Iterator[tuple[str, tuple[str, ...]]]:
    headings: list[str] = []
    pending: list[str] = []
    pending_path: tuple[str, ...] = ()

    def flush() -> Iterator[tuple[str, tuple[str, ...]]]:
        nonlocal pending, pending_path
        if pending:
            body = "\n\n".join(pending)
            if carry_headings and pending_path:
                body = " › ".join(pending_path) + "\n\n" + body
            yield body, pending_path
            pending = []
            pending_path = ()

    for block in _PARAGRAPH.split(text):
        stripped = block.strip()
        if not stripped:
            continue
        heading = _HEADING.match(stripped)
        if heading:
            yield from flush()
            depth = len(heading.group(1))
            headings[:] = [*headings[: depth - 1], heading.group(2).strip()]
            continue
        if not pending:
            pending_path = tuple(headings)
        pending.append(stripped)
        if sum(len(part) for part in pending) >= config.min_characters:
            yield from flush()
    yield from flush()
