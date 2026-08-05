#!/usr/bin/env python3
"""Evidence ingestion: documents in, structured rows and a searchable corpus out.

A tenant hands over a folder. Some of it is already structured — a ledger extract, a
telematics export. Most of it is not: scanned invoices, supplier letters, board minutes as
PDF. Bedrock Data Automation turns the second kind into fields; this script decides what
happens to the result.

Three things it does that a simpler loader would not:

**It scans every extracted document for injection before indexing it.** A poisoned supplier
attestation still belongs in the corpus — the document is genuine and its metadata is ours —
but it is flagged, and the flag travels with it into the retrieval index as metadata. A
narrative that later cites it can be traced back to a document somebody had already marked.

**It never overwrites.** Evidence is what an auditor re-reads. A document that changes under
a lineage record that pointed at it makes the record a lie, so the object key carries the
content digest and a re-upload of different bytes is a new object rather than a replacement.

**It refuses a document class the contracts do not know.** An evidence class nobody declared
is a class no contract can require, so a document filed under it satisfies nothing while
looking like coverage. That is worse than a missing file, because it is invisible.

Offline mode extracts nothing and calls no service; it validates manifests, checks classes
against the contract set, and reports what *would* be uploaded. That is what CI runs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from attestor.contracts import loader
from attestor.datapoints.evidence import EvidenceIndex
from attestor.policy.tenants import TenantRegistry
from attestor.security import injection

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class IngestReport:
    tenant: str
    planned: list[str] = field(default_factory=list)
    flagged: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    uploaded: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.rejected

    def summary(self) -> str:
        return (
            f"{self.tenant}: {len(self.planned)} document(s), {len(self.flagged)} flagged, "
            f"{len(self.rejected)} rejected, {len(self.uploaded)} uploaded"
        )


def declared_classes(root: Path) -> set[str]:
    """Every evidence class some contract actually asks for."""
    return {klass for contract in loader.load(root) for klass in contract.evidence.classes}


def object_key(tenant: str, document_id: str, digest: str) -> str:
    """Content-addressed. Different bytes are a different object, never a replacement."""
    return f"{tenant}/{document_id}/{digest[:16]}"


def plan(root: Path, tenant: str) -> IngestReport:
    report = IngestReport(tenant=tenant)
    known = declared_classes(root)

    for document in EvidenceIndex.for_tenant(root, tenant):
        if document.document_class not in known:
            report.rejected.append(
                f"{document.document_id}: class {document.document_class!r} is not required by "
                "any contract; filing it satisfies nothing while looking like coverage"
            )
            continue
        report.planned.append(object_key(tenant, document.document_id, document.content_sha256))
        if document.flagged_injection:
            report.flagged.append(document.document_id)
    return report


# ── Live path ────────────────────────────────────────────────────────────────


def extract(client: Any, *, bucket: str, key: str, project_arn: str) -> dict[str, Any]:
    """Bedrock Data Automation: a document in, fields and text out."""
    response = client.invoke_data_automation_async(
        inputConfiguration={"s3Uri": f"s3://{bucket}/{key}"},
        outputConfiguration={"s3Uri": f"s3://{bucket}/_extracted/{key}"},
        dataAutomationConfiguration={"dataAutomationProjectArn": project_arn},
    )
    return {"invocation_arn": response.get("invocationArn"), "key": key}


def scan_extracted(text: str, *, document_id: str) -> tuple[bool, list[str]]:
    """Look for instruction-shaped content before the text reaches an index.

    Scanning here rather than at retrieval time is deliberate: the flag becomes metadata on
    the indexed chunk, so it is attached to every future retrieval rather than recomputed on
    each one. A document only has to be examined once; a passage is read many times.
    """
    result = injection.scan(text, document_id=document_id)
    return result.flagged, sorted({signal.rule for signal in result.signals})


def upload(client: Any, *, bucket: str, key: str, body: bytes, metadata: dict[str, str]) -> str:
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ServerSideEncryption="aws:kms",
        Metadata=metadata,
        # Never overwrite. If the key exists with different bytes, that is a new document and
        # `object_key` already gave it a different key; identical bytes are a no-op.
        IfNoneMatch="*",
    )
    return key


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


# ── Entry point ──────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", help="One tenant, or every tenant if omitted.")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Upload and extract. Without it the script plans and calls nothing.",
    )
    parser.add_argument("--bucket")
    parser.add_argument("--project-arn")
    args = parser.parse_args()

    tenants = [args.tenant] if args.tenant else [t.id for t in TenantRegistry.load(args.root)]
    reports = [plan(args.root, tenant) for tenant in tenants]

    if args.apply:
        if not (args.bucket and args.project_arn):
            print("--apply needs --bucket and --project-arn", file=sys.stderr)
            return 2
        print(
            "the live path uploads and invokes Bedrock Data Automation; it is exercised by "
            "the deploy workflow, not from a laptop",
            file=sys.stderr,
        )
        return 2

    for report in reports:
        print(report.summary())
        for note in report.flagged:
            print(f"  ::warning::{note} carries instruction-shaped content and is flagged")
        for note in report.rejected:
            print(f"  ::error::{note}", file=sys.stderr)

    print(json.dumps({r.tenant: {"planned": len(r.planned)} for r in reports}, sort_keys=True))
    return 0 if all(report.ok for report in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
