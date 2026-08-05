"""The retrieval bake-off: a one-way door decided with numbers, and an honest gap.

Chunking strategy and embedding model are the two decisions in a RAG system that are
expensive to revisit — changing either means re-embedding the corpus and invalidating every
evaluation ever quoted from it. So they are chosen once, on a sample, with measurements
committed beside the decision.

**What is measured here, and what is not.**

Chunking is a pure function of text. Where a boundary falls, how much context a chunk
carries, how many tokens that costs — all computable on a laptop, exactly, every time. So the
chunking comparison below is *real*: it runs a deterministic BM25 retriever over the actual
corpus and reports what it actually found.

Embedding-model choice is not computable offline. Comparing Titan against Cohere requires
embedding the corpus with both, which requires the estate. Rather than inventing rankings and
presenting them as a comparison, that dimension is reported as **pending live capture** and
`scripts/seed_recordings.py` will stamp it when the estate is stood up. A fabricated number
in a decision table is worse than an admitted gap, because the gap gets filled and the
fabrication gets quoted.

Three metrics, because they disagree in useful ways:

``recall@k``
    Did the answer make it into the window at all? If not, nothing downstream can recover —
    reranking, a bigger model and a better prompt are all irrelevant.

``MRR``
    How far down was it? A correct passage at rank 8 competes with seven wrong ones, and a
    model forced to choose usually chooses badly.

``tokens/query``
    What a wider window costs on every single retrieval. `hierarchical` buys recall by
    prefixing each chunk with its heading path and pays for it forever; the number that
    decides whether that trade is worth taking belongs in the table.

Relevance is defined by document plus an anchor phrase rather than by chunk id, because chunk
ids differ per strategy — that is the whole point of comparing strategies. A retrieved chunk
counts when it comes from the right document and actually contains the answer.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from attestor.retrieval.chunking import Chunk, ChunkingConfig, Strategy, chunk

_TOKEN = re.compile(r"[a-z0-9§().-]+")


def tokenise(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


@dataclass(frozen=True, slots=True)
class GoldenQuestion:
    id: str
    question: str
    document_id: str
    #: A phrase that must appear in a chunk for it to actually answer the question. Anchors
    #: rather than ids, so the same golden set scores every chunking strategy.
    anchors: tuple[str, ...]
    standard: str = "ESRS"

    def is_relevant(self, piece: Chunk) -> bool:
        if piece.document_id != self.document_id:
            return False
        body = " ".join(piece.text.lower().split())
        return any(" ".join(anchor.lower().split()) in body for anchor in self.anchors)


class BM25:
    """A small, deterministic lexical retriever.

    It stands in for the vector index while the estate is down. It is not a simulation of
    one — it is a different retriever with different failure modes, and it is used only for
    the dimension where that does not matter: whether a *chunking* strategy keeps the answer
    findable and intact. A strategy that splits an answer across two chunks hurts BM25 and a
    vector index alike.
    """

    def __init__(self, chunks: list[Chunk], *, k1: float = 1.5, b: float = 0.75) -> None:
        self._chunks = chunks
        self._k1 = k1
        self._b = b
        self._tokens = [tokenise(piece.text) for piece in chunks]
        self._lengths = [len(tokens) for tokens in self._tokens]
        self._average = sum(self._lengths) / len(self._lengths) if self._lengths else 0.0
        self._frequencies = [Counter(tokens) for tokens in self._tokens]
        document_frequency: Counter[str] = Counter()
        for tokens in self._tokens:
            document_frequency.update(set(tokens))
        total = len(chunks) or 1
        self._idf = {
            term: math.log(1 + (total - count + 0.5) / (count + 0.5))
            for term, count in document_frequency.items()
        }

    def search(self, query: str, *, top_k: int) -> list[Chunk]:
        terms = tokenise(query)
        scored: list[tuple[float, int]] = []
        for index, frequencies in enumerate(self._frequencies):
            score = 0.0
            for term in terms:
                if term not in frequencies:
                    continue
                frequency = frequencies[term]
                length_norm = (
                    1
                    - self._b
                    + self._b * (self._lengths[index] / self._average if self._average else 1)
                )
                score += self._idf.get(term, 0.0) * (
                    frequency * (self._k1 + 1) / (frequency + self._k1 * length_norm)
                )
            if score > 0:
                scored.append((score, index))
        # Ties break on ordinal so the ranking is stable across runs and machines.
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        return [self._chunks[index] for _, index in scored[:top_k]]


@dataclass(frozen=True, slots=True)
class Variant:
    strategy: Strategy
    #: Recorded only. Not varied offline — see the module docstring.
    embedding_model: str = "pending-live-capture"

    @property
    def id(self) -> str:
        return self.strategy.value


@dataclass(slots=True)
class VariantScore:
    variant: Variant
    recall_at_k: float = 0.0
    mrr: float = 0.0
    tokens_per_query: float = 0.0
    chunks: int = 0
    questions: int = 0
    #: Questions where no chunk in the whole corpus contained the answer intact. A chunking
    #: strategy that splits an answer scores zero here regardless of ranking, which is
    #: exactly the failure the comparison exists to expose.
    unanswerable: int = 0

    def row(self) -> str:
        return (
            f"| `{self.variant.id}` | {self.recall_at_k:.3f} | {self.mrr:.3f} | "
            f"{self.tokens_per_query:.0f} | {self.chunks} | {self.unanswerable} |"
        )


@dataclass(slots=True)
class BakeOff:
    scores: list[VariantScore] = field(default_factory=list)
    k: int = 8

    @property
    def winner(self) -> VariantScore | None:
        """Recall first, then MRR, then cost.

        Recall leads because a passage that never entered the window cannot be recovered by
        anything downstream; MRR and tokens are both recoverable with money.
        """
        if not self.scores:
            return None
        return max(
            self.scores,
            key=lambda s: (round(s.recall_at_k, 3), round(s.mrr, 3), -s.tokens_per_query),
        )

    def table(self) -> str:
        lines = [
            f"| chunking | recall@{self.k} | MRR | tokens/query | chunks | answer split |",
            "|---|---:|---:|---:|---:|---:|",
            *(score.row() for score in sorted(self.scores, key=lambda s: s.variant.id)),
        ]
        best = self.winner
        if best:
            lines += [
                "",
                f"**Chosen:** `{best.variant.id}` — recall {best.recall_at_k:.3f}, "
                f"MRR {best.mrr:.3f}, {best.tokens_per_query:.0f} tokens/query.",
                "",
                "**Embedding model:** pending live capture. Comparing Titan against Cohere "
                "requires embedding the corpus with both, which requires the estate. That "
                "row is deliberately absent rather than invented.",
                "",
                f"**Sample:** {best.questions} question(s) over {best.chunks} chunk(s). That "
                "is enough to expose a strategy that splits answers and to price the token "
                "difference; it is not enough to settle a one-way door. The decision is "
                "provisional until it is re-run against the full corpus.",
            ]
        return "\n".join(lines)


def load_golden(path: Path | str) -> tuple[GoldenQuestion, ...]:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return tuple(
        GoldenQuestion(
            id=entry["id"],
            question=entry["question"],
            document_id=entry["document_id"],
            anchors=tuple(entry["anchors"]),
            standard=entry.get("standard", "ESRS"),
        )
        for entry in payload.get("questions", [])
    )


def load_corpus(path: Path | str) -> dict[str, str]:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return {entry["document_id"]: entry["text"] for entry in payload.get("documents", [])}


def score_variant(
    variant: Variant,
    *,
    questions: tuple[GoldenQuestion, ...],
    corpus: dict[str, str],
    k: int = 8,
) -> VariantScore:
    config = ChunkingConfig(strategy=variant.strategy)
    chunks = [
        piece
        for document_id, text in sorted(corpus.items())
        for piece in chunk(text, document_id=document_id, config=config)
    ]
    index = BM25(chunks)
    average_tokens = sum(piece.tokens for piece in chunks) / len(chunks) if chunks else 0.0

    recalls: list[float] = []
    reciprocals: list[float] = []
    unanswerable = 0

    for question in questions:
        if not any(question.is_relevant(piece) for piece in chunks):
            unanswerable += 1
            recalls.append(0.0)
            reciprocals.append(0.0)
            continue
        ranked = index.search(question.question, top_k=k)
        hit_positions = [
            position
            for position, piece in enumerate(ranked, start=1)
            if question.is_relevant(piece)
        ]
        recalls.append(1.0 if hit_positions else 0.0)
        reciprocals.append(1.0 / hit_positions[0] if hit_positions else 0.0)

    return VariantScore(
        variant=variant,
        recall_at_k=sum(recalls) / len(recalls) if recalls else 0.0,
        mrr=sum(reciprocals) / len(reciprocals) if reciprocals else 0.0,
        tokens_per_query=average_tokens * k,
        chunks=len(chunks),
        questions=len(questions),
        unanswerable=unanswerable,
    )


def run(root: Path | str = ".", *, k: int = 8) -> BakeOff:
    root = Path(root)
    directory = root / "evals" / "retrieval"
    questions = load_golden(directory / "golden.yaml")
    corpus = load_corpus(directory / "corpus.yaml")
    result = BakeOff(k=k)
    for strategy in Strategy:
        result.scores.append(
            score_variant(Variant(strategy=strategy), questions=questions, corpus=corpus, k=k)
        )
    return result
