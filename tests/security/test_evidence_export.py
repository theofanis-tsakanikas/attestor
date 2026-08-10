"""The generated evidence has to be a measurement, and the digest has to be a check.

Two failure modes, both of which this repository has already had.

A generated document that is not deterministic makes `--check` flap, and a flapping gate is
turned off within a week. A digest nobody compares is decoration — three of `lumen`'s
documents carried a hash of some earlier draft and every gate stayed green, because nothing
in the repository read the bytes and the field back to back.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest
import yaml

from attestor.evals import export
from attestor.security import harness

ROOT = Path(__file__).resolve().parents[2]


def test_the_export_is_byte_stable() -> None:
    """Two runs on one tree produce identical bytes, or the digest below means nothing."""
    assert export.evaluation_report(ROOT) == export.evaluation_report(ROOT)
    assert export.data_sheet(ROOT) == export.data_sheet(ROOT)


def test_the_committed_export_is_what_the_harnesses_produce() -> None:
    assert export.generate(ROOT, check=True) == []


def test_every_local_document_digests_to_its_manifest_entry() -> None:
    """The check the repository lacked. `reconcile` reporting nothing means they agree."""
    assert export.reconcile(ROOT, check=True) == []


def test_the_report_states_the_measured_block_rate() -> None:
    """Not any ratio — the one the harness returns, to four places.

    Asserting only that the document contains a table would pass on a report that had been
    edited by hand, which is the mutation `gate-proof` plants.
    """
    score = harness.run(ROOT / "evals" / "injection" / "corpus.yaml")
    expected = f"{score.blocked / len(score.poisoned):.4f}"
    assert "| Block rate on manipulated passages | " in export.evaluation_report(ROOT)
    assert expected in export.evaluation_report(ROOT)


def _repo_with_writable_evidence(tmp_path: Path) -> Path:
    """A root whose inputs are the real ones and whose `evidence/` can be edited.

    Symlinks rather than a copy: the builders run the actual harnesses, which read the corpus,
    the contracts and the recordings. A fixture-shaped stand-in for those would test the
    fixture. Only `evidence/` is copied, because that is the tree the mutation edits.
    """
    root = tmp_path / "repo"
    root.mkdir()
    for entry in ROOT.iterdir():
        if entry.name in {".git", ".venv", "out", "evidence"}:
            continue
        (root / entry.name).symlink_to(entry)
    shutil.copytree(ROOT / "evidence", root / "evidence")
    return root


def test_a_hand_edit_is_detected(tmp_path: Path) -> None:
    """The mutation, in miniature: change a figure, and `--check` must object."""
    root = _repo_with_writable_evidence(tmp_path)
    assert export.generate(root, check=True) == []  # green before, or this proves nothing

    document = root / "evidence/lumen/documents/EVALREPORT-ATT-2026.md"
    document.write_text(document.read_text("utf-8").replace("1.0000", "0.9900"), "utf-8")

    assert "evidence/lumen/documents/EVALREPORT-ATT-2026.md" in export.generate(root, check=True)


def test_a_stale_digest_is_detected(tmp_path: Path) -> None:
    """Edit a hand-written document, leave its digest, and the manifest must disagree."""
    root = _repo_with_writable_evidence(tmp_path)
    assert export.reconcile(root, check=True) == []

    document = root / "evidence/lumen/documents/MODELCARD-ATT-2026.md"
    document.write_text(document.read_text("utf-8") + "\nappended\n", "utf-8")

    assert export.reconcile(root, check=True) == ["MODELCARD-ATT-2026.content_sha256"]


def test_documents_outside_the_repository_are_reported_not_skipped() -> None:
    """Silence about what cannot be checked reads as a clean bill of health."""
    unchecked = export.unverifiable(ROOT)
    assert set(unchecked) == {"INCIDENTS-ATT-2026", "RISKREG-ATT-2026"}


@pytest.mark.parametrize("document_id", [export.EVALUATION_REPORT, export.DATA_SHEET])
def test_the_generated_documents_are_declared_locally(document_id: str) -> None:
    """A generated document still pointing at S3 would be checked against nothing."""
    manifest = yaml.safe_load((ROOT / export.MANIFEST).read_text(encoding="utf-8"))
    entry = next(d for d in manifest["documents"] if d["document_id"] == document_id)

    assert not entry["source_uri"].startswith("s3://")
    path = ROOT / entry["source_uri"]
    assert entry["content_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
