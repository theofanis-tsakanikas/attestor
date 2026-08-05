from __future__ import annotations

from pathlib import Path

import pytest

from attestor.contracts import loader
from attestor.contracts.loader import ContractSet

REPO_ROOT = Path(__file__).resolve().parents[1]


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
