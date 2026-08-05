"""Generalization: two regulatory regimes, one code path.

This is the claim `lumen` exists to test. Everything about the AI Act vertical is different
from the ESRS one — different standard, different clause numbering, different evidence
classes, different units, different templates, an entirely different identity provider — and
none of it is a different *code path*. If a `if standard == "ESRS"` branch ever appears in
`src/`, this file is where it should start failing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from attestor.contracts import derivation
from attestor.contracts.loader import ContractSet
from attestor.contracts.model import Standard
from attestor.documents.template import Template
from attestor.policy.tenants import TenantRegistry


@pytest.fixture(scope="module")
def registry(request) -> TenantRegistry:
    return TenantRegistry.load(Path(request.config.rootpath))


def test_both_standards_are_represented(contract_set: ContractSet) -> None:
    esrs = contract_set.for_standard(Standard.ESRS)
    ai_act = contract_set.for_standard(Standard.EU_AI_ACT)
    assert len(esrs) > 0
    assert len(ai_act) > 0
    assert len(esrs) + len(ai_act) == len(contract_set)


def test_a_subset_is_still_internally_consistent(contract_set: ContractSet) -> None:
    """A derived datapoint's operands must survive the filter with it."""
    for standard in Standard:
        subset = contract_set.for_standard(standard)
        for contract in subset:
            if contract.resolver.kind != "derived":
                continue
            for operand in derivation.referenced_datapoints(contract.resolver.expression):
                assert operand in subset, f"{contract.id} lost operand {operand} when filtered"


def test_every_tenant_has_contracts_for_its_standard(
    contract_set: ContractSet, registry: TenantRegistry
) -> None:
    """A tenant with nothing to report is a registry entry nobody finished."""
    for tenant in registry:
        subset = contract_set.for_standard(Standard(tenant.standard))
        assert len(subset) > 0, tenant.id


def test_every_tenant_has_at_least_one_template(repo_root: Path, registry: TenantRegistry) -> None:
    templates = Template.load_all(repo_root / "templates")
    for tenant in registry:
        matching = [t for t in templates if t.standard == tenant.standard]
        assert matching, f"{tenant.id} reports under {tenant.standard} and has no template"


def test_no_template_mixes_standards(repo_root: Path, contract_set: ContractSet) -> None:
    """A placeholder from another regime would be a citation to a law that does not apply."""
    for template in Template.load_all(repo_root / "templates"):
        for datapoint_id in template.datapoints():
            contract = contract_set[datapoint_id]
            assert contract.standard.value == template.standard, (
                f"{template.id} ({template.standard}) references {datapoint_id} "
                f"from {contract.standard.value}"
            )


def test_the_two_verticals_share_no_datapoint(contract_set: ContractSet) -> None:
    esrs = {c.id for c in contract_set.for_standard(Standard.ESRS)}
    ai_act = {c.id for c in contract_set.for_standard(Standard.EU_AI_ACT)}
    assert esrs.isdisjoint(ai_act)


def test_the_ai_act_vertical_uses_the_same_machinery(contract_set: ContractSet) -> None:
    """Narrative, SQL and derived resolvers, a cross-check, and a lawful omission — the same
    constructs the ESRS vertical uses, in a regime that shares none of its vocabulary."""
    ai_act = contract_set.for_standard(Standard.EU_AI_ACT)
    kinds = {c.resolver.kind for c in ai_act}
    assert {"narrative", "sql", "derived"} <= kinds
    assert any(c.tolerance.cross_check for c in ai_act)
    assert any(c.abstention.allowed_reasons for c in ai_act)


def test_no_module_branches_on_a_standard(repo_root: Path) -> None:
    """The whole point. Generalization means no `if standard ==` anywhere in the engine.

    The CLI and the harnesses may *filter* by standard — that is selecting which contracts a
    tenant owes. What must not exist is behaviour that differs because of which regime is
    being reported under.
    """
    offenders: list[str] = []
    for path in (repo_root / "src" / "attestor").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for marker in ('== "ESRS"', "== Standard.ESRS", '== "EU_AI_ACT"', "== Standard.EU_AI_ACT"):
            if marker in text:
                offenders.append(f"{path.relative_to(repo_root)}: {marker}")
    assert not offenders, offenders
