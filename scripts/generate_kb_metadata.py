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

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence"
REGULATORY = ROOT / "corpus" / "regulatory"

#: Which standard a regulatory note belongs to, from the datapoint id it is named for. The
#: corpus is shared across tenants, so this is the only thing keeping an ESRS engagement from
#: retrieving an AI Act article and citing it as though it applied.
STANDARDS = {"ESRS": "ESRS", "AIACT": "EU_AI_ACT"}


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

    # A sidecar with no document beside it would filter nothing and confuse the next reader.
    for stray in sorted({*EVIDENCE.rglob("*.metadata.json"), *REGULATORY.glob("*.metadata.json")}):
        if stray not in wanted:
            problems.append(f"{stray.relative_to(ROOT)} describes a document that is not here")

    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
    verb = "checked" if arguments.check else "wrote"
    print(f"  {verb} {len(wanted)} sidecar(s), {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
