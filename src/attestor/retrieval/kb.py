"""Knowledge Base configuration, and the filter that is never built from a prompt.

Two Knowledge Bases, because they are two different kinds of trust:

**The regulatory corpus** is the standard itself — ESRS text, EFRAG guidance, the AI Act.
Shared across tenants, versioned, and *trusted*: it is what the system is supposed to reason
from. Its chunking carries heading paths, because "§44(a)" without "ESRS E1-6" above it will
match the wrong article.

**The evidence corpus** is the tenant's own documents. Per tenant, untrusted, and every
retrieval against it carries a metadata filter built from the session. That filter is the
subject of `forbid-unfiltered-retrieval` in Cedar, and of probes 3, 4 and 12 in the isolation
suite — three separate places, because a filter that can be omitted eventually is.

The `retrieve` path here is deliberately thin. It builds the request, applies the filter, and
hands off to a backend; nothing in it decides what a tenant may see, because that decision
belongs to Cedar and happens before this module is reached.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from attestor.policy.tenants import Session
from attestor.retrieval.chunking import ChunkingConfig, Strategy

#: Bedrock embedding models under consideration. The choice is a one-way door — changing it
#: means re-embedding the whole corpus — so it is decided by the bake-off, on a sample.
EMBEDDING_MODELS = {
    "titan-v2": "amazon.titan-embed-text-v2:0",
    "cohere-multilingual": "cohere.embed-multilingual-v3",
}


@dataclass(frozen=True, slots=True)
class VectorStoreConfig:
    """Where the vectors live.

    OpenSearch Serverless, not S3 Vectors. The cheaper option would have been defensible for
    a system that answers a thousand queries a month, and indefensible here: hybrid search
    (BM25 alongside vectors) is what finds "E1-6 §44(a)" when a user types the article number,
    and metadata filtering at the index is what makes the tenant filter a query constraint
    rather than a post-filter over somebody else's rows.

    The cost of that choice is idle spend, which is answered by the estate being ephemeral —
    see `infra/` and the TTL reaper — not by picking a weaker store.
    """

    kind: Literal["opensearch-serverless"] = "opensearch-serverless"
    collection: str = "attestor"
    #: One index per corpus; tenants share the evidence index and are separated by filter.
    #: Separate indices per tenant would cost an index per onboarding and is where a
    #: multi-tenant platform stops scaling.
    index: str = "attestor-evidence"
    vector_field: str = "embedding"
    text_field: str = "text"
    metadata_field: str = "metadata"
    dimensions: int = 1024


@dataclass(frozen=True, slots=True)
class KnowledgeBaseConfig:
    id: str
    kind: Literal["regulatory", "evidence"]
    embedding_model: str
    chunking: ChunkingConfig
    vector_store: VectorStoreConfig = field(default_factory=VectorStoreConfig)
    #: Reranking pays for itself on the regulatory corpus, where near-duplicate articles are
    #: the norm; on the evidence corpus the top-k is already small and it mostly adds latency.
    #: The bake-off is what turns that sentence from an opinion into a number.
    rerank: bool = False
    top_k: int = 8

    def describe(self) -> str:
        return (
            f"{self.id} [{self.kind}] {self.embedding_model} "
            f"{self.chunking.describe()} rerank={self.rerank} k={self.top_k}"
        )


REGULATORY = KnowledgeBaseConfig(
    id="attestor-regulatory",
    kind="regulatory",
    embedding_model=EMBEDDING_MODELS["titan-v2"],
    chunking=ChunkingConfig(
        strategy=Strategy.HIERARCHICAL, max_characters=1200, min_characters=400
    ),
    vector_store=VectorStoreConfig(index="attestor-regulatory"),
    rerank=True,
    top_k=8,
)

EVIDENCE = KnowledgeBaseConfig(
    id="attestor-evidence",
    kind="evidence",
    embedding_model=EMBEDDING_MODELS["titan-v2"],
    chunking=ChunkingConfig(strategy=Strategy.SENTENCE, max_characters=900, overlap=120),
    vector_store=VectorStoreConfig(index="attestor-evidence"),
    rerank=False,
    top_k=6,
)


@dataclass(frozen=True, slots=True)
class Passage:
    id: str
    text: str
    score: float
    document_id: str
    metadata: dict[str, str] = field(default_factory=dict)


class RetrievalBackend(Protocol):
    def search(
        self, *, query: str, index: str, metadata_filter: dict[str, str], top_k: int
    ) -> list[Passage]: ...


class UnfilteredRetrieval(RuntimeError):
    """A retrieval was attempted without a tenant filter. Refused before it reaches a backend."""


def metadata_filter(session: Session, *, extra: dict[str, str] | None = None) -> dict[str, str]:
    """The filter every evidence retrieval carries.

    Built from the session. There is no parameter by which a caller — or a model, or a
    document — can widen it: `extra` may add constraints, and an attempt to overwrite
    `tenant` raises rather than being silently ignored, because silently ignoring it would
    make the attack invisible in a log.
    """
    base = session.retrieval_filter()
    for key, value in (extra or {}).items():
        if key in base and base[key] != value:
            raise UnfilteredRetrieval(
                f"a caller tried to set {key}={value!r} over the session's {base[key]!r}; "
                "the retrieval filter comes from the session and nowhere else"
            )
        base[key] = value
    return base


def retrieve(
    backend: RetrievalBackend,
    *,
    query: str,
    session: Session,
    config: KnowledgeBaseConfig,
    extra_filter: dict[str, str] | None = None,
) -> list[Passage]:
    """Search one knowledge base on behalf of a session."""
    if config.kind == "evidence":
        applied = metadata_filter(session, extra=extra_filter)
        if not applied.get("tenant"):
            raise UnfilteredRetrieval("evidence retrieval without a tenant filter")
    else:
        # The regulatory corpus is shared and public. It is still filtered by the standard
        # the tenant reports under, so an ESRS engagement never retrieves AI Act articles and
        # then cites them as if they applied.
        applied = {"standard": (extra_filter or {}).get("standard", "")}
        if not applied["standard"]:
            raise UnfilteredRetrieval("regulatory retrieval without a standard filter")

    return backend.search(
        query=query,
        index=config.vector_store.index,
        metadata_filter=applied,
        top_k=config.top_k,
    )


# A `RecordedRetrieval` class used to sit here, documented as "the offline default". It was
# a replay cache keyed on exact query text, it had no recordings behind it, and nothing in the
# repository constructed one — so the docstring was the only part of it that did anything, and
# what it did was mislead. It is deleted rather than left in place: an offline retrieval
# replay is worth building the day there is something captured to replay, and until then
# `search_evidence` returning "no retrieval backend configured" is the honest answer.
