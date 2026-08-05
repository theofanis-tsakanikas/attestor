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
from attestor.datapoints import extraction
from attestor.datapoints.admissibility import require_admissible
from attestor.datapoints.evidence import EvidenceDocument, EvidenceIndex
from attestor.datapoints.extraction import (
    Extraction,
    ExtractionError,
    ExtractionUnavailable,
    Extractor,
    RowSet,
    RowSpec,
    to_rows,
)
from attestor.observability.cost import CostMeter
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
    #: Documents read through the extractor, and the pages that cost money to read.
    extracted: int = 0
    pages: int = 0

    @property
    def ok(self) -> bool:
        return not self.rejected

    def summary(self) -> str:
        return (
            f"{self.tenant}: {len(self.planned)} document(s), {len(self.flagged)} flagged, "
            f"{len(self.rejected)} rejected, {len(self.uploaded)} uploaded, "
            f"{self.extracted} extracted over {self.pages} page(s)"
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


# ── Extraction ───────────────────────────────────────────────────────────────


def extract(document: EvidenceDocument, *, extractor: Extractor) -> Extraction:
    """Read one document into fields.

    This used to fire `invoke_data_automation_async` and return the invocation ARN. Nothing
    waited for the job, nothing read its output, and nothing parsed a field — so a tenant
    whose evidence was paper abstained on datapoints it demonstrably had evidence for, and
    the scanned half of the corpus was decorative.

    The extractor is injected rather than constructed, because which one is in use is the
    single most important fact about a run: recorded everywhere by default, live only behind
    an explicit opt-in. See `datapoints/extraction.py`.
    """
    return extractor.extract(document)


def extract_all(
    root: Path,
    tenant: str,
    *,
    extractor: Extractor,
    report: IngestReport | None = None,
) -> dict[str, Extraction]:
    """Every document this tenant has, read.

    Two absences, deliberately not the same thing:

    **No capture exists** — the ordinary case. Most of a corpus is already structured: a
    telematics export and a general-ledger extract are files, not paper, and there is nothing
    for an extractor to read. That is not a rejection, and treating it as one would make the
    ingest fail on a corpus that is entirely healthy.

    **The capture does not match the document** — a rejection. The bytes changed under a
    reading somebody reviewed, and replaying it would describe paper nobody is holding. So
    would a malformed output or a field that does not parse.

    Either way the ingest continues. One unreadable scan must not hold the corpus hostage,
    and the datapoint behind an unread document abstains on its own for lack of evidence,
    which is the honest outcome anyway.
    """
    report = report if report is not None else IngestReport(tenant=tenant)
    extractions: dict[str, Extraction] = {}

    for document in EvidenceIndex.for_tenant(root, tenant):
        try:
            read = extract(document, extractor=extractor)
        except ExtractionUnavailable:
            continue
        except ExtractionError as exc:
            report.rejected.append(f"{document.document_id}: {exc}")
            continue

        # The scan runs on what the extractor read, before any of it reaches an index. A
        # scanned attestation that a supplier filled with instructions is still evidence —
        # its metadata is ours — but the flag has to be attached at the moment the text
        # first exists, not recomputed on every retrieval afterwards.
        flagged, rules = scan_extracted(read.text, document_id=document.document_id)
        already = any(note.startswith(document.document_id) for note in report.flagged)
        if flagged and not already:
            # The manifest may already carry the flag from a previous ingest. Recording it
            # twice would make a count of flagged documents wrong in the direction that
            # looks like a worsening corpus.
            report.flagged.append(f"{document.document_id} ({', '.join(rules)})")
        extractions[document.document_id] = read

    return extractions


# ── Fields to rows ───────────────────────────────────────────────────────────
#
# One spec per document class that becomes data. Everything else is read for its text and
# its evidence coverage and stops there.
#
# Note which datasets these name. `procurement_fuel_spend` backs the *cross-check* for Scope
# 1, not the primary — the primary reads telematics. That direction is the control:
# reconciling a reading of the paper against the same reading proves the reader is
# consistent, not that the figure is right. `admissibility.judge` enforces it, and refuses
# the reverse.
ROW_SPECS: tuple[RowSpec, ...] = (
    RowSpec(
        dataset="gold.procurement_fuel_spend",
        document_class="fuel_invoice",
        columns={
            "invoice_date": "invoice_date",
            "fuel_type": "fuel_type",
            "net_amount_eur": "net_amount_eur",
        },
        numeric=frozenset({"net_amount_eur"}),
    ),
    RowSpec(
        dataset="gold.meter_interval_reading",
        document_class="utility_invoice",
        columns={"interval_end": "period_end", "kwh": "kwh"},
        numeric=frozenset({"kwh"}),
    ),
)


def rows_for(
    root: Path,
    tenant: str,
    extractions: dict[str, Extraction],
    *,
    contracts: Any = None,
) -> dict[str, RowSet]:
    """Build the rows every extraction contributes, refusing an inadmissible dataset first.

    `require_admissible` runs *before* a row is built, not after. Writing extracted values
    into a dataset a published figure reads and trusting the cross-check to catch a misread
    afterwards is the same mistake as publishing and trusting review — the check has to be
    the reason the write is allowed, not a hope about what happens next.
    """
    contracts = contracts if contracts is not None else loader.load(root)
    by_id = {d.document_id: d for d in EvidenceIndex.for_tenant(root, tenant)}
    built: dict[str, RowSet] = {}

    for spec in ROW_SPECS:
        require_admissible(spec.dataset, contracts)
        rows: list[dict[str, Any]] = []
        for document_id, read in sorted(extractions.items()):
            document = by_id.get(document_id)
            if document is None or document.document_class != spec.document_class:
                continue
            rows.extend(to_rows(read, document, spec).rows)
        if rows:
            built[spec.dataset] = RowSet(dataset=spec.dataset, rows=tuple(rows))
    return built


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
    parser.add_argument(
        "--extract",
        action="store_true",
        help="Read every document through the extractor. Recorded unless ATTESTOR_EXTRACTOR=bda.",
    )
    args = parser.parse_args()

    tenants = [args.tenant] if args.tenant else [t.id for t in TenantRegistry.load(args.root)]
    reports = [plan(args.root, tenant) for tenant in tenants]

    if args.extract:
        # Metered per page, per tenant. `Meter.DOCUMENT_PARSE` was defined and charged by
        # nothing, which on a platform that calls per-tenant cost a first-class metric meant
        # the one line item that scales with paper was invisible.
        meter = CostMeter()
        extractor = extraction.build(args.root, meter=meter)
        for report in reports:
            found = extract_all(args.root, report.tenant, extractor=extractor, report=report)
            report.extracted = len(found)
            report.pages = sum(e.pages for e in found.values())
        print(meter.report())

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
