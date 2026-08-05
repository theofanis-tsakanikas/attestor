#!/usr/bin/env python3
"""Does the deployed guardrail agree with what the contracts declare?

`Grounding.threshold` says, in the contract, how strongly a narrative must be anchored to its
evidence. `var.grounding_threshold` says, in Terraform, what the guardrail is configured
with. Two comments in this repository already claimed the pair was cross-checked in CI. It
was not — the field was declared, mirrored, and read by nothing.

That is the specific failure this script exists for. A contract that demands 0.9 while the
deployed guardrail enforces 0.7 is not a stricter contract; it is a contract stating a number
nobody applies, and every narrative it governs is assured against a threshold its own
paperwork disagrees with.

Read from the two sources of truth directly — the contract set and the variable default — so
that raising one without the other turns the build red rather than producing a quiet
divergence between the document and the estate.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from attestor.contracts import loader

ROOT = Path(__file__).resolve().parents[1]
VARIABLES = ROOT / "infra" / "knowledge" / "variables.tf"

#: Float comparison slack. The two sides are written by hand in two files; they are meant to
#: be the same number, not merely close, so this is a float-repr allowance and nothing more.
TOLERANCE = 1e-9

_DEFAULT = re.compile(
    r'variable\s+"(?P<name>grounding_threshold|relevance_threshold)"\s*\{'
    r"[^}]*?default\s*=\s*(?P<value>[0-9.]+)",
    re.DOTALL,
)


def terraform_defaults() -> dict[str, float]:
    text = VARIABLES.read_text(encoding="utf-8")
    found = {m.group("name"): float(m.group("value")) for m in _DEFAULT.finditer(text)}
    missing = {"grounding_threshold", "relevance_threshold"} - set(found)
    if missing:
        raise SystemExit(
            f"{VARIABLES.relative_to(ROOT)}: cannot read {', '.join(sorted(missing))}. "
            "This check reads the variable defaults; if they moved, it is not checking "
            "anything and must be updated rather than deleted."
        )
    return found


def main() -> int:
    deployed = terraform_defaults()["grounding_threshold"]
    contracts = loader.load(ROOT)

    narratives = [c for c in contracts if c.is_model_authored]
    if not narratives:
        print("no narrative contracts to align", file=sys.stderr)
        return 1

    problems = [
        f"{c.id}: contract demands grounding {c.resolver.grounding.threshold}, "
        f"the guardrail is configured with {deployed}"
        for c in narratives
        if abs(c.resolver.grounding.threshold - deployed) > TOLERANCE
    ]

    for problem in sorted(problems):
        print(f"  {problem}", file=sys.stderr)
    if problems:
        print(
            "\nRaise both, or neither. A contract that states a threshold nobody enforces is "
            "a disclosure about a control that does not exist.",
            file=sys.stderr,
        )
        return 1

    print(
        f"guardrail alignment: {len(narratives)} narrative contract(s) at grounding "
        f"{deployed}, matching infra/knowledge"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
