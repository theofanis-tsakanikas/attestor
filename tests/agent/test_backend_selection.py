"""Which backend a run uses, and why the default is the safe one in both directions.

The defect this guards against was live and quiet: `attestor run` hardcoded the recorded
backend, so the deploy workflow's "run against the live estate" step replayed the fixtures and
printed PASS without touching Athena. A deploy that appears to succeed while proving nothing
is worse than one that fails, because nobody goes looking.
"""

from __future__ import annotations

import pytest
import typer

from attestor.cli import main as cli
from attestor.datapoints.backends import AthenaBackend, RecordedBackend


def test_the_default_is_recorded(repo_root, monkeypatch) -> None:
    """Every gate replays recordings; reaching for an account by default would need creds."""
    monkeypatch.delenv("ATTESTOR_BACKEND", raising=False)
    assert isinstance(cli._backend(repo_root), RecordedBackend)


def test_athena_is_selected_explicitly(repo_root, monkeypatch) -> None:
    monkeypatch.setenv("ATTESTOR_BACKEND", "athena")
    monkeypatch.setenv("ATTESTOR_WORKGROUP", "attestor")
    monkeypatch.setenv("ATTESTOR_DATABASE", "attestor_gold")
    monkeypatch.setenv("ATTESTOR_ATHENA_OUTPUT", "s3://bucket/results/")
    assert isinstance(cli._backend(repo_root), AthenaBackend)


def test_athena_without_its_environment_fails_rather_than_falling_back(
    repo_root, monkeypatch
) -> None:
    """Falling back here would produce a green run that queried nothing — the original bug."""
    monkeypatch.setenv("ATTESTOR_BACKEND", "athena")
    for name in ("ATTESTOR_WORKGROUP", "ATTESTOR_DATABASE", "ATTESTOR_ATHENA_OUTPUT"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(typer.Exit):
        cli._backend(repo_root)
