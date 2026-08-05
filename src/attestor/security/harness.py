"""Scoring the injection corpus.

Two numbers, and they are not equally important.

The block rate over poisoned documents is the one people quote. The **false-positive rate
over benign documents is the one that decides whether the control still exists in six
months**, because a scanner that flags genuine supplier letters gets switched off, and a
switched-off scanner defends nothing.

So the gate demands both: every poisoned document flagged, for the reason it was designed to
trip, and *zero* benign documents flagged. The "for the right reason" part matters — a
document caught by an unrelated rule is a coincidence, and a coincidence will not survive
the attacker's next rewrite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from attestor.security import injection
from attestor.security.injection import EnvelopeError, ScanResult


@dataclass(frozen=True, slots=True)
class Case:
    id: str
    text: str
    poisoned: bool
    expect_rules: tuple[str, ...] = ()
    envelope_forgery: bool = False
    note: str = ""


@dataclass(slots=True)
class CaseOutcome:
    case: Case
    result: ScanResult | None
    envelope_refused: bool
    problems: list[str] = field(default_factory=list)

    @property
    def detected(self) -> bool:
        return self.envelope_refused or bool(self.result and self.result.flagged)

    @property
    def passed(self) -> bool:
        return not self.problems


@dataclass(slots=True)
class Score:
    outcomes: list[CaseOutcome] = field(default_factory=list)

    @property
    def poisoned(self) -> list[CaseOutcome]:
        return [o for o in self.outcomes if o.case.poisoned]

    @property
    def benign(self) -> list[CaseOutcome]:
        return [o for o in self.outcomes if not o.case.poisoned]

    @property
    def blocked(self) -> int:
        return sum(1 for o in self.poisoned if o.detected)

    @property
    def false_positives(self) -> list[CaseOutcome]:
        return [o for o in self.benign if o.detected]

    @property
    def wrong_reason(self) -> list[CaseOutcome]:
        return [o for o in self.poisoned if o.detected and o.problems]

    @property
    def passed(self) -> bool:
        return all(o.passed for o in self.outcomes)

    def summary(self) -> str:
        total = len(self.poisoned)
        return (
            f"injection: {self.blocked}/{total} poisoned flagged, "
            f"{len(self.false_positives)}/{len(self.benign)} benign wrongly flagged, "
            f"{'PASS' if self.passed else 'FAIL'}"
        )

    def report(self) -> str:
        lines = [self.summary()]
        for outcome in self.outcomes:
            if outcome.problems:
                lines.append(f"  {outcome.case.id}: {'; '.join(outcome.problems)}")
                if outcome.result:
                    lines.extend(f"      {signal}" for signal in outcome.result.signals)
        return "\n".join(lines)


def load_corpus(path: Path | str) -> tuple[Case, ...]:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    cases: list[Case] = []
    for entry in payload.get("poisoned", []):
        cases.append(
            Case(
                id=entry["id"],
                text=entry["text"],
                poisoned=True,
                expect_rules=tuple(entry.get("expect_rules", ())),
                envelope_forgery=bool(entry.get("envelope_forgery", False)),
                note=entry.get("attack", ""),
            )
        )
    for entry in payload.get("benign", []):
        cases.append(
            Case(id=entry["id"], text=entry["text"], poisoned=False, note=entry.get("note", ""))
        )
    return tuple(cases)


def score(cases: tuple[Case, ...]) -> Score:
    result = Score()
    for case in cases:
        result.outcomes.append(_evaluate(case))
    return result


def _evaluate(case: Case) -> CaseOutcome:
    envelope_refused = False
    try:
        injection.envelope(case.text, document_id=case.id, document_class="evidence")
    except EnvelopeError:
        envelope_refused = True

    scan = injection.scan(case.text, document_id=case.id)
    outcome = CaseOutcome(case=case, result=scan, envelope_refused=envelope_refused)

    if case.poisoned:
        if not outcome.detected:
            outcome.problems.append("not flagged")
        elif case.envelope_forgery and not envelope_refused:
            outcome.problems.append("envelope forgery was delivered rather than refused")
        else:
            fired = {signal.rule for signal in scan.signals}
            missing = sorted(set(case.expect_rules) - fired)
            if missing:
                # Caught, but by something other than the rule this case exercises. The
                # rule it was written for has regressed, and the coincidence that saved it
                # will not survive the attacker's next rewrite.
                outcome.problems.append(
                    f"flagged, but rule(s) {', '.join(missing)} did not fire "
                    f"(fired: {', '.join(sorted(fired)) or 'none'})"
                )
    elif outcome.detected:
        rules = ", ".join(sorted({s.rule for s in scan.signals})) or "envelope"
        outcome.problems.append(f"false positive on benign document ({rules})")

    return outcome


def run(corpus_path: Path | str) -> Score:
    return score(load_corpus(corpus_path))
