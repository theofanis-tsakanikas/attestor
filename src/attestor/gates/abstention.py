"""Claim 5 — the cheapest control in the repository and the most important.

A disciplined *"not disclosed, because X"* is not a nicety. Under the CSRD it is what the
standard asks for when a figure cannot be supported, and it is the behaviour that separates a
reporting system from a plausible-text generator. Anyone can produce a number; refusing to,
exactly as often as the evidence requires and not once more, is the hard part.

So the eval measures three things, and the third is the one people forget:

1. **Every gap produces an abstention** — with the reason code the scenario expects, not
   merely *some* refusal.
2. **No gap produces a figure.** Zero fabrications, no threshold.
3. **Nothing else abstains.** A system that refuses everything scores perfectly on the first
   two and is useless. Each scenario names the datapoints that must survive its gap intact,
   and unlisted datapoints are checked too.

Scenarios manipulate the corpus rather than the code: withhold a document class, truncate one
to fewer documents than the contract demands, shift another out of the reporting period. What
runs afterwards is the production resolver, unmodified.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from attestor.contracts import overrides
from attestor.contracts.loader import ContractSet
from attestor.contracts.model import Standard
from attestor.datapoints.backends import RecordedBackend
from attestor.datapoints.evidence import EvidenceDocument, EvidenceIndex
from attestor.datapoints.resolver import (
    Abstained,
    NarrativeDraft,
    ResolutionContext,
    Resolved,
    Resolver,
)
from attestor.policy.tenants import TenantRegistry

PERIOD_START = dt.date(2026, 1, 1)
PERIOD_END = dt.date(2027, 1, 1)
DEFAULT_AS_OF = dt.date(2026, 7, 1)


@dataclass(frozen=True, slots=True)
class Expectation:
    reason: str
    outcome: str


@dataclass(frozen=True, slots=True)
class Scenario:
    id: str
    name: str
    tenant: str
    expect: dict[str, Expectation]
    must_still_resolve: tuple[str, ...] = ()
    withhold_classes: tuple[str, ...] = ()
    keep_at_most: dict[str, int] = field(default_factory=dict)
    shift_classes: dict[str, int] = field(default_factory=dict)
    as_of: dt.date = DEFAULT_AS_OF


@dataclass(slots=True)
class ScenarioResult:
    scenario: Scenario
    problems: list[str] = field(default_factory=list)
    abstentions: int = 0
    fabrications: int = 0

    @property
    def passed(self) -> bool:
        return not self.problems


@dataclass(slots=True)
class AbstentionScore:
    results: list[ScenarioResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.results)

    @property
    def fabrications(self) -> int:
        return sum(result.fabrications for result in self.results)

    @property
    def expected_abstentions(self) -> int:
        return sum(len(result.scenario.expect) for result in self.results)

    @property
    def observed_abstentions(self) -> int:
        return sum(result.abstentions for result in self.results)

    def summary(self) -> str:
        return (
            f"abstention: {self.observed_abstentions}/{self.expected_abstentions} expected "
            f"refusals, {self.fabrications} fabrication(s), "
            f"{'PASS' if self.passed else 'FAIL'}"
        )

    def report(self) -> str:
        lines = [self.summary()]
        for result in self.results:
            if result.problems:
                lines.append(f"  {result.scenario.id} ({result.scenario.name}):")
                lines.extend(f"      {problem}" for problem in result.problems)
        return "\n".join(lines)


def load_scenarios(path: Path | str) -> tuple[Scenario, ...]:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    scenarios: list[Scenario] = []
    for entry in payload.get("scenarios", []):
        as_of = entry.get("as_of")
        scenarios.append(
            Scenario(
                id=entry["id"],
                name=entry["name"],
                tenant=entry["tenant"],
                expect={
                    datapoint: Expectation(value["reason"], value["outcome"])
                    for datapoint, value in entry.get("expect", {}).items()
                },
                must_still_resolve=tuple(entry.get("must_still_resolve", ())),
                withhold_classes=tuple(entry.get("withhold_classes", ())),
                keep_at_most=dict(entry.get("keep_at_most", {})),
                shift_classes=dict(entry.get("shift_classes", {})),
                as_of=as_of if isinstance(as_of, dt.date) else DEFAULT_AS_OF,
            )
        )
    return tuple(scenarios)


def _corpus(root: Path, scenario: Scenario) -> EvidenceIndex:
    """Apply the scenario's damage to a real corpus."""
    documents = list(EvidenceIndex.for_tenant(root, scenario.tenant))

    if scenario.withhold_classes:
        withheld = set(scenario.withhold_classes)
        documents = [d for d in documents if d.document_class not in withheld]

    if scenario.keep_at_most:
        kept: Counter[str] = Counter()
        truncated: list[EvidenceDocument] = []
        for document in documents:
            ceiling = scenario.keep_at_most.get(document.document_class)
            if ceiling is None:
                truncated.append(document)
                continue
            if kept[document.document_class] < ceiling:
                kept[document.document_class] += 1
                truncated.append(document)
        documents = truncated

    if scenario.shift_classes:
        shifted: list[EvidenceDocument] = []
        for document in documents:
            days = scenario.shift_classes.get(document.document_class)
            if days is None:
                shifted.append(document)
                continue
            delta = dt.timedelta(days=days)
            shifted.append(
                document.model_copy(
                    update={
                        "covers_from": document.covers_from + delta,
                        "covers_to": document.covers_to + delta,
                    }
                )
            )
        documents = shifted

    return EvidenceIndex(documents, tenant=scenario.tenant)


def _narrative(_contract, _context) -> NarrativeDraft:
    """A cooperative provider. The eval is about evidence, not about the model."""
    return NarrativeDraft(
        text="The undertaking maintains a transition plan. [ev:aaaa] [ev:bbbb] [ev:cccc]",
        citations=("ev:aaaa", "ev:bbbb", "ev:cccc"),
        prompt_ref="esrs_e1_1_transition_plan@3",
    )


def evaluate(scenario: Scenario, *, root: Path, contracts: ContractSet) -> ScenarioResult:
    result = ScenarioResult(scenario=scenario)
    registry = TenantRegistry.load(root)
    resolver = Resolver(
        contracts=contracts.for_standard(Standard(registry[scenario.tenant].standard)),
        backend=RecordedBackend.from_directory(root / "recordings"),
        evidence=_corpus(root, scenario),
        override_register=overrides.load_register(root),
        root=root,
        narrative_provider=_narrative,
    )
    resolved = resolver.resolve_all(
        ResolutionContext(
            tenant=scenario.tenant,
            period="2026",
            period_start=PERIOD_START,
            period_end=PERIOD_END,
            as_of=scenario.as_of,
        )
    )

    for datapoint_id, expectation in scenario.expect.items():
        outcome = resolved.get(datapoint_id)
        if outcome is None:
            result.problems.append(f"{datapoint_id}: never resolved")
            continue
        if isinstance(outcome, Resolved):
            # A fabrication: the evidence was removed and a number appeared anyway.
            result.fabrications += 1
            result.problems.append(
                f"{datapoint_id}: produced {outcome.value} despite the gap — fabrication"
            )
            continue
        result.abstentions += 1
        if outcome.reason_code != expectation.reason:
            result.problems.append(
                f"{datapoint_id}: abstained with {outcome.reason_code}, "
                f"expected {expectation.reason}"
            )
        if outcome.outcome.value != expectation.outcome:
            result.problems.append(
                f"{datapoint_id}: outcome {outcome.outcome.value}, expected {expectation.outcome}"
            )

    # The half that stops "refuse everything" from scoring well.
    for datapoint_id in scenario.must_still_resolve:
        outcome = resolved.get(datapoint_id)
        if not isinstance(outcome, Resolved):
            detail = outcome.reason_code if isinstance(outcome, Abstained) else "missing"
            result.problems.append(
                f"{datapoint_id}: had to survive this gap but abstained ({detail})"
            )

    unexpected = [
        abstention.datapoint_id
        for abstention in resolved.abstentions
        if abstention.datapoint_id not in scenario.expect
        and abstention.datapoint_id not in scenario.must_still_resolve
    ]
    if unexpected:
        result.problems.append(
            "abstained on datapoint(s) the scenario did not damage: "
            + ", ".join(sorted(unexpected))
        )

    return result


def run(root: Path | str = ".", *, contracts: ContractSet | None = None) -> AbstentionScore:
    from attestor.contracts.loader import load  # noqa: PLC0415 — avoids an import cycle

    root = Path(root)
    contract_set = contracts or load(root)
    score = AbstentionScore()
    for scenario in load_scenarios(root / "evals" / "abstention" / "scenarios.yaml"):
        score.results.append(evaluate(scenario, root=root, contracts=contract_set))
    return score
