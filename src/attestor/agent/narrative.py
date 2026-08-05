"""Where model-authored prose comes from — and the one place that decides.

The resolver takes a `NarrativeProvider` callable. Until now nothing built one outside a
test: the Lambda wired `None`, so every narrative datapoint abstained with
`E_METHOD_UNAVAILABLE` and blocked the report, while the CLI passed a hand-written paragraph
that rendered into a real DOCX. Neither path ever reached a model, and one of them shipped
invented prose under a tenant's name.

So the choice is made here, once, by the same switch that chooses the query backend:

    ATTESTOR_BACKEND=recorded  (default) → RecordedNarrativeProvider
    ATTESTOR_BACKEND=athena              → BedrockNarrativeProvider

That symmetry is the point. An offline run replays *captured* drafts exactly as it replays
captured query results, and a live run talks to Bedrock through the same resolver. There is
no third option in which someone types a paragraph into Python.

`RecordedNarrativeProvider` carries the same staleness discipline as `RecordedBackend`: a
draft is keyed by the digest of the prompt that produced it, so editing a prompt without
re-capturing raises rather than replaying prose the current prompt would never have written.
A recording that does not exist is not substituted for — the provider raises, the resolver
turns that into an abstention, and the report says so.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import yaml

from attestor.contracts.model import DatapointContract
from attestor.datapoints.resolver import NarrativeDraft, ResolutionContext
from attestor.policy.tenants import Session

NARRATIVES_FILE = "narratives.yaml"


class NarrativeUnavailable(RuntimeError):
    """No draft could be produced. Always becomes an abstention upstream, never prose."""


class StaleNarrativeDraft(NarrativeUnavailable):
    """A recorded draft was captured against a different version of its prompt.

    Deliberately fatal, for the same reason `StaleRecording` is: a replay that answers with
    the previous prompt's output is a suite that passes while the thing it tests has changed.
    """


def prompt_digest(text: str) -> str:
    """Hash of the prompt text, whitespace-normalised so reformatting is not a re-capture."""
    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()


def read_prompt(prompts_dir: Path, prompt_id: str) -> str:
    path = Path(prompts_dir) / f"{prompt_id}.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise NarrativeUnavailable(f"prompt {prompt_id!r} is not on disk at {path}") from exc


def prompt_version(text: str) -> str:
    """The `version:` field of a prompt's front matter, for the lineage record."""
    for line in text.splitlines():
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip()
    return "unversioned"


class RecordedNarrativeProvider:
    """Replays drafts captured from a live model run. The offline default."""

    def __init__(self, drafts: dict[str, dict[str, Any]], *, prompts_dir: Path) -> None:
        self._drafts = drafts
        self._prompts_dir = Path(prompts_dir)

    @classmethod
    def from_root(cls, root: Path | str) -> RecordedNarrativeProvider:
        root = Path(root)
        path = root / "recordings" / NARRATIVES_FILE
        drafts: dict[str, dict[str, Any]] = {}
        if path.is_file():
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            for entry in payload.get("drafts", []):
                drafts[cls._key(entry["tenant"], entry["datapoint_id"])] = entry
        return cls(drafts, prompts_dir=root / "prompts")

    @staticmethod
    def _key(tenant: str, datapoint_id: str) -> str:
        return f"{tenant}|{datapoint_id}"

    def __call__(self, contract: DatapointContract, context: ResolutionContext) -> NarrativeDraft:
        entry = self._drafts.get(self._key(context.tenant, contract.id))
        if entry is None:
            raise NarrativeUnavailable(
                f"no recorded draft for {contract.id} under tenant {context.tenant!r}; "
                "a narrative is captured from a model run and reviewed, never written here"
            )
        prompt = read_prompt(self._prompts_dir, contract.resolver.prompt_id)
        digest = prompt_digest(prompt)
        if entry.get("prompt_digest") != digest:
            raise StaleNarrativeDraft(
                f"{contract.id}: the recorded draft was captured against prompt "
                f"{str(entry.get('prompt_digest'))[:12]}, and "
                f"{contract.resolver.prompt_id} now digests to {digest[:12]}. Re-capture it "
                "rather than replaying prose the current prompt would not have written."
            )
        return NarrativeDraft(
            text=entry["text"],
            citations=tuple(entry.get("citations", ())),
            prompt_ref=f"{contract.resolver.prompt_id}@{prompt_version(prompt)}",
        )


def build(
    root: Path | str,
    *,
    session: Session | None = None,
    backend: str | None = None,
) -> RecordedNarrativeProvider | Any:
    """The provider this process should use, decided by `ATTESTOR_BACKEND`.

    `session` is required for the live provider and unused by the recorded one: a Bedrock
    call retrieves the tenant's evidence, and there is no tenant outside a session.
    """
    root = Path(root)
    chosen = (backend or os.environ.get("ATTESTOR_BACKEND", "recorded")).lower()
    if chosen != "athena":
        return RecordedNarrativeProvider.from_root(root)

    if session is None:
        raise NarrativeUnavailable(
            "a live narrative provider needs a session; the tenant whose evidence is "
            "retrieved is never ambient"
        )

    # Imported here rather than at module scope: `bedrock` reaches for boto3 on first use,
    # and an offline run must never import it.
    from attestor.agent.bedrock import (  # noqa: PLC0415
        BedrockNarrativeProvider,
        BedrockRetrieval,
        ModelConfig,
    )
    from attestor.retrieval import kb  # noqa: PLC0415

    region = os.environ.get("AWS_REGION", "eu-central-1")
    config = ModelConfig(
        model_id=_required("ATTESTOR_REASONING_MODEL"),
        guardrail_id=_required("ATTESTOR_GUARDRAIL_ID"),
        guardrail_version=_required("ATTESTOR_GUARDRAIL_VER"),
        region=region,
    )
    retrieval = BedrockRetrieval(
        region=region,
        evidence_kb_id=_required("ATTESTOR_EVIDENCE_KB"),
        regulatory_kb_id=_required("ATTESTOR_REGULATORY_KB"),
    )

    def retrieve(contract: DatapointContract, _context: ResolutionContext):
        """Evidence for this datapoint, filtered by the session and nothing else.

        The corpus the contract declares is honoured here, which is what finally makes
        `grounding.corpus` mean something rather than being a field nobody reads.
        """
        wanted = contract.resolver.grounding.corpus
        passages = []
        if wanted in {"evidence", "both"}:
            passages += kb.retrieve(
                retrieval, query=contract.title, session=session, config=kb.EVIDENCE
            )
        if wanted in {"regulatory", "both"}:
            passages += kb.retrieve(
                retrieval,
                query=f"{contract.reference} {contract.title}",
                session=session,
                config=kb.REGULATORY,
                extra_filter={"standard": contract.standard.value},
            )
        return passages

    return BedrockNarrativeProvider(
        config=config,
        session=session,
        prompts_dir=root / "prompts",
        retrieve=retrieve,
    )


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise NarrativeUnavailable(
            f"{name} is unset. Falling back to a recorded draft against a live estate would "
            "publish captured prose as if a model had just written it."
        )
    return value
