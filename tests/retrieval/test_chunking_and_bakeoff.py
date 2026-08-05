"""Chunking, the filter that cannot be widened, and the bake-off."""

from __future__ import annotations

from pathlib import Path

import pytest

from attestor.policy.tenants import Session
from attestor.retrieval import bakeoff, kb
from attestor.retrieval.chunking import ChunkingConfig, Strategy, chunk
from attestor.retrieval.kb import UnfilteredRetrieval

TEXT = """# ESRS E1 Climate change

## E1-6 Gross emissions

Gross Scope 1 emissions are direct emissions from owned or controlled sources.

They are reported before any deduction for removals or carbon credits.
"""


def _session(tenant: str = "helios") -> Session:
    return Session(
        tenant=tenant,
        subject="user-1",
        roles=frozenset({"role:preparer"}),
        period="2026",
        session_id="sess-01",
    )


@pytest.mark.parametrize("strategy", list(Strategy))
def test_chunking_is_deterministic(strategy: Strategy) -> None:
    config = ChunkingConfig(strategy=strategy)
    first = chunk(TEXT, document_id="D", config=config)
    second = chunk(TEXT, document_id="D", config=config)
    assert [c.id for c in first] == [c.id for c in second]
    assert first


def test_a_reboundaried_chunk_gets_a_different_id() -> None:
    """Otherwise a re-chunk could be mistaken for the index it replaced."""
    a = chunk(TEXT, document_id="D", config=ChunkingConfig(strategy=Strategy.FIXED))
    b = chunk(TEXT, document_id="D", config=ChunkingConfig(strategy=Strategy.PARAGRAPH))
    assert {c.id for c in a}.isdisjoint({c.id for c in b})


def test_hierarchical_chunks_carry_their_heading_path() -> None:
    """`§44(a)` means nothing without the article above it."""
    pieces = chunk(TEXT, document_id="D", config=ChunkingConfig(strategy=Strategy.HIERARCHICAL))
    assert any("ESRS E1 Climate change" in piece.text for piece in pieces)
    assert any(piece.heading_path for piece in pieces)


def test_paragraph_chunks_do_not_carry_headings() -> None:
    pieces = chunk(TEXT, document_id="D", config=ChunkingConfig(strategy=Strategy.PARAGRAPH))
    assert all("›" not in piece.text for piece in pieces)


# ── The filter ───────────────────────────────────────────────────────────────


def test_the_filter_comes_from_the_session() -> None:
    assert kb.metadata_filter(_session()) == {"tenant": "helios", "period": "2026"}


def test_a_caller_cannot_widen_the_filter() -> None:
    """Refused loudly, not ignored quietly — an ignored attack is an invisible one."""
    with pytest.raises(UnfilteredRetrieval, match="comes from the session"):
        kb.metadata_filter(_session(), extra={"tenant": "aegis"})


def test_a_caller_may_narrow_the_filter() -> None:
    applied = kb.metadata_filter(_session(), extra={"document_class": "utility_invoice"})
    assert applied["tenant"] == "helios"
    assert applied["document_class"] == "utility_invoice"


def test_evidence_retrieval_without_a_tenant_is_refused() -> None:
    class Blind:
        def search(self, **_kwargs):  # pragma: no cover — must not be reached
            raise AssertionError("the backend was reached without a filter")

    session = _session()
    object.__setattr__  # noqa: B018 — documents that Session is frozen; we build a stub below

    class Unscoped(Session):
        def retrieval_filter(self) -> dict[str, str]:
            return {"tenant": "", "period": self.period}

    unscoped = Unscoped(**session.model_dump())
    with pytest.raises(UnfilteredRetrieval):
        kb.retrieve(Blind(), query="x", session=unscoped, config=kb.EVIDENCE)


def test_regulatory_retrieval_needs_a_standard() -> None:
    """An ESRS engagement must not retrieve AI Act articles and cite them as if they applied."""

    class Blind:
        def search(self, **_kwargs):  # pragma: no cover
            raise AssertionError("reached")

    with pytest.raises(UnfilteredRetrieval, match="standard filter"):
        kb.retrieve(Blind(), query="x", session=_session(), config=kb.REGULATORY)


# ── Bake-off ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def result(request) -> bakeoff.BakeOff:
    return bakeoff.run(Path(request.config.rootpath), k=3)


def test_every_strategy_is_scored(result: bakeoff.BakeOff) -> None:
    assert {score.variant.id for score in result.scores} == {s.value for s in Strategy}


def test_no_strategy_splits_an_answer(result: bakeoff.BakeOff) -> None:
    """A chunking that cuts an answer in half fails regardless of how it ranks."""
    for score in result.scores:
        assert score.unanswerable == 0, score.variant.id


def test_the_winner_is_reproducible(request, result: bakeoff.BakeOff) -> None:
    again = bakeoff.run(Path(request.config.rootpath), k=3)
    assert result.winner.variant.id == again.winner.variant.id
    assert result.table() == again.table()


def test_the_table_admits_what_it_cannot_measure(result: bakeoff.BakeOff) -> None:
    """A fabricated number in a decision table gets quoted; an admitted gap gets filled."""
    table = result.table()
    assert "pending live capture" in table
    assert "provisional" in table


def test_hierarchical_costs_less_per_query_than_fixed(result: bakeoff.BakeOff) -> None:
    """Smaller, better-bounded chunks buy a narrower window at the same recall."""
    scores = {score.variant.id: score for score in result.scores}
    assert scores["hierarchical"].tokens_per_query < scores["fixed"].tokens_per_query
