#!/usr/bin/env python3
"""Write the `.metadata.json` sidecars Bedrock filters retrieval on.

Retrieval is filtered **at the index**: `kb.search` refuses to run without a `tenant` filter
for evidence or a `standard` filter for the regulatory corpus, and Bedrock evaluates that
filter against metadata attributes it reads from a sidecar file next to each object. Without
sidecars there are no attributes, so every filtered query matches nothing — and the ingestion
job says so quietly, in a statistic nobody reads: `numberOfMetadataDocumentsScanned: 0`.

That is what "no deliverable evidence" meant. The documents were indexed, the filter was
correct, and the two had nothing in common to match on.

Generated rather than committed by hand, from the manifest for evidence and from the file name
for the regulatory corpus. A sidecar that disagrees with the manifest is a document retrievable
under the wrong tenant, which is the one mistake this system exists to not make.

`--check` verifies without writing, for CI.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from attestor.policy.tenants import Session, TenantRegistry

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence"
REGULATORY = ROOT / "corpus" / "regulatory"

#: Which standard a regulatory note belongs to, from the datapoint id it is named for. The
#: corpus is shared across tenants, so this is the only thing keeping an ESRS engagement from
#: retrieving an AI Act article and citing it as though it applied.
STANDARDS = {"ESRS": "ESRS", "AIACT": "EU_AI_ACT"}

#: Files that live in the corpus directory for a reader, not for the index. The deploy's
#: `s3 sync` uploads only `ESRS_*` and `AIACT_*`; anything else here must be named, so that
#: "no sidecar" is always a decision somebody made rather than a name that missed a prefix.
SKIPPED = ("README",)


def sidecar(path: Path) -> Path:
    return path.with_name(path.name + ".metadata.json")


def payload(attributes: dict[str, str]) -> str:
    return json.dumps({"metadataAttributes": attributes}, indent=2, sort_keys=True) + "\n"


def evidence_sidecars() -> dict[Path, str]:
    wanted: dict[Path, str] = {}
    for manifest in sorted(EVIDENCE.glob("*/*.yaml")):
        tenant = manifest.parent.name
        declared = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        for document in declared.get("documents", []):
            source = str(document.get("source_uri", ""))
            if not source.startswith("evidence/"):
                # Described but not present. The manifest is metadata about a corpus that may
                # live elsewhere; only what is on disk can carry a sidecar.
                continue
            wanted[sidecar(ROOT / source)] = payload(
                {
                    "tenant": tenant,
                    # The period the session is scoped to. `kb.metadata_filter` builds
                    # `{tenant, period}` and Bedrock ANDs the clauses, so a sidecar without
                    # this matched nothing: evidence retrieval returned zero passages for
                    # every narrative datapoint, silently, while the regulatory corpus went on
                    # answering. A narrative grounded entirely in the standard's own text
                    # reads perfectly well and cites no evidence at all.
                    "period": manifest.stem,
                    "document_id": str(document["document_id"]),
                    "document_class": str(document["document_class"]),
                }
            )
    return wanted


def regulatory_sidecars() -> dict[Path, str]:
    wanted: dict[Path, str] = {}
    for note in sorted(REGULATORY.glob("*.md")):
        prefix = note.stem.split("_", 1)[0]
        standard = STANDARDS.get(prefix)
        if standard is None:
            continue
        wanted[sidecar(note)] = payload({"standard": standard, "datapoint_id": note.stem})
    return wanted


def filter_keys_a_session_builds() -> set[str]:
    """The keys `kb.metadata_filter` will actually send, asked of the code that builds them.

    Read rather than listed, because this is the exact gap that hid the last defect: the
    filter carried `{tenant, period}`, the sidecars carried `tenant` alone, and Bedrock ANDed
    a clause nothing satisfied.
    """
    keys: set[str] = set()
    for manifest in sorted(EVIDENCE.glob("*/*.yaml")):
        tenant = TenantRegistry.load(ROOT)[manifest.parent.name]
        claims = {
            "sub": "checker",
            "iss": tenant.identity.issuer,
            "aud": tenant.identity.audience,
            tenant.identity.groups_claim: [next(iter(tenant.identity.role_map))],
        }
        session = Session.from_claims(
            claims=claims, tenant=tenant, session_id="kbcheck", period=manifest.stem
        )
        keys |= set(session.retrieval_filter())
    return keys


def _undescribed_and_stray(wanted: dict[Path, str]) -> list[str]:
    """Both directions of the sidecar/document correspondence.

    A sidecar with no document beside it filters nothing and confuses the next reader. A
    document with no sidecar is the one that got into the account: Bedrock indexes it carrying
    no attribute, so it is matched by no filtered query, governed by no tenant, and sitting in
    an index three tenants share. Live, that was `corpus/regulatory/README.md` and each
    tenant's `2026.yaml` manifest — 17 indexed against 16 described, and for `aegis`, 1
    against 0. Every ingestion job had been saying so, in a statistic nobody read.

    The deploy now uploads only what this script describes. This keeps the two in step, since
    they live in different files and drift quietly.
    """
    problems: list[str] = []
    for stray in sorted({*EVIDENCE.rglob("*.metadata.json"), *REGULATORY.glob("*.metadata.json")}):
        if stray not in wanted:
            problems.append(f"{stray.relative_to(ROOT)} describes a document that is not here")

    for document in sorted(REGULATORY.glob("*.md")):
        if sidecar(document) not in wanted and not document.name.startswith(SKIPPED):
            problems.append(
                f"{document.relative_to(ROOT)} gets no sidecar and is not an excluded name. "
                "Either name it for a datapoint or add it to SKIPPED — an undescribed file "
                "that reaches the index is a document nothing governs"
            )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="verify without writing")
    arguments = parser.parse_args()

    wanted = {**evidence_sidecars(), **regulatory_sidecars()}
    if not wanted:
        print("  no documents on disk to describe", file=sys.stderr)
        return 1

    problems: list[str] = []
    for path, body in sorted(wanted.items()):
        if arguments.check:
            if not path.is_file():
                problems.append(f"{path.relative_to(ROOT)} is missing")
            elif path.read_text(encoding="utf-8") != body:
                problems.append(f"{path.relative_to(ROOT)} disagrees with its manifest")
        else:
            path.write_text(body, encoding="utf-8")

    # Every key the session's filter sends must be an attribute some document carries.
    required = filter_keys_a_session_builds()
    for path in sorted(p for p in wanted if "/documents/" in p.as_posix()):
        attributes = set(json.loads(wanted[path])["metadataAttributes"])
        for missing in sorted(required - attributes):
            problems.append(
                f"{path.relative_to(ROOT)} has no `{missing}` attribute, and the retrieval "
                "filter sends one. Bedrock ANDs its clauses, so this document is unreachable"
            )

    problems += _undescribed_and_stray(wanted)

    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
    verb = "checked" if arguments.check else "wrote"
    print(f"  {verb} {len(wanted)} sidecar(s), {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
