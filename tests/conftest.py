from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from attestor.contracts import loader
from attestor.contracts.loader import ContractSet
from attestor.policy.tenants import TenantRegistry

REPO_ROOT = Path(__file__).resolve().parents[1]


def _claims_for(tenant_id: str, group: str, *, subject: str = "user-1") -> dict:
    """A well-formed token for a tenant, built from that tenant's own registry entry.

    Tests construct claims through this rather than by hand so that adding a claim the
    session verifies — `iss` and `aud` are the current pair — updates every test at once
    instead of leaving half of them asserting against a token shape nothing accepts.
    """
    tenant = TenantRegistry.load(REPO_ROOT)[tenant_id]
    return {
        "sub": subject,
        "iss": tenant.identity.issuer,
        "aud": tenant.identity.audience,
        tenant.identity.groups_claim: [group],
    }


@pytest.fixture(scope="session")
def claims_for() -> Callable[..., dict]:
    """`_claims_for`, reached the way pytest intends.

    Handed over as a fixture rather than imported. `from tests.conftest import ...` needs the
    repository root on `sys.path`, which an editable install happens to arrange on a developer's
    machine and a clean checkout does not — so the suite passed locally and failed collection in
    CI, which is the worst place for the difference to show up.
    """
    return _claims_for


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def contract_set() -> ContractSet:
    """The real contract set. If this fixture raises, the repository is inconsistent."""
    return loader.load(REPO_ROOT)


@pytest.fixture
def contract_yaml() -> dict:
    """A minimal valid quantitative contract, for tests that mutate one field at a time."""
    return {
        "id": "ESRS_TEST_figure",
        "standard": "ESRS",
        "standard_version": "2023-12",
        "reference": "ESRS TEST §1",
        "title": "A test figure",
        "kind": "quantitative",
        "unit": "MWh",
        "precision": 1,
        "resolver": {"kind": "sql", "query": "esrs/e1_5_electricity_consumption.sql"},
        "evidence": {"required": True, "classes": ["utility_invoice"]},
    }
