"""Turning paper into rows — and the boundary that keeps paper out of a figure.

Most of a tenant's evidence is not structured. A scanned fuel invoice is a genuine document
supporting a genuine disclosure, and until now it produced nothing at all: the datapoint it
backs abstained for lack of evidence the undertaking demonstrably had. This module reads
those documents.

**What an extracted value is.** Untrusted input to the ingestion pipeline. It lands in the
lakehouse as data, under the same data contracts and the same quarantine as every other row,
and the resolver cannot tell it from a row a source system wrote. That indistinguishability
is the design, not an accident of it: the moment the resolver has to know where a row came
from, the boundary has moved into the resolver and claim 3 is a matter of care rather than
of structure.

**What an extracted value is not.** A figure. An OCR engine that reads `1` as `7` is not a
rare pathology — it is the ordinary failure of the technology, and no amount of confidence
scoring turns it into a control. So an extracted value may support a *published* figure only
where the contract declares a `tolerance.cross_check` that reconciles it against an
independent path, and only from the side of that reconciliation that is not itself paper.
Reconciling OCR against OCR proves nothing. Where a contract has no cross-check, extracted
rows are admissible as evidence coverage and nothing more — see `admissibility.py`.

**Two implementations, one protocol**, the same split as `backends.py` and for the same
reason. `RecordedExtractor` replays captures and is the default everywhere; the live
extractor requires an explicit opt-in and is unreachable by accident. A capture is keyed on
the digest of the bytes it was taken from, so a document that changed under a recording
raises rather than replaying an answer about different paper.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import yaml

from attestor.datapoints.evidence import EvidenceDocument
from attestor.observability.cost import CostMeter, Meter

EXTRACTIONS_FILE = "extractions.yaml"

#: Terminal states of a Bedrock Data Automation invocation.
_SUCCEEDED = "Success"
_FAILED = frozenset({"ServiceError", "ClientError"})


class ExtractionError(RuntimeError):
    """A document could not be turned into fields. Never a partial answer."""


class ExtractionUnavailable(ExtractionError):
    """No extraction exists for this document, and none is invented for it."""


class StaleExtraction(ExtractionError):
    """A capture was taken from different bytes than the document now carries.

    Deliberately fatal, for the same reason `StaleRecording` is. A replay that answers with
    the fields of a previous version of a document is a pipeline that reports on paper
    nobody is holding.
    """


@dataclass(frozen=True, slots=True)
class ExtractedField:
    """One value read off a page, with where it was read and how sure the reader was.

    `value` is a string even when it is plainly a number. Parsing is a separate, typed step
    that can fail loudly; a `float` here would mean the parse already happened somewhere
    with nowhere to report that it went wrong.
    """

    name: str
    value: str
    confidence: float
    page: int

    def as_decimal(self) -> Decimal:
        """The value as an exact decimal, or an error naming the field.

        No cleaning, no coercion, no stripping of currency symbols. A value that does not
        parse is a value the extractor read wrong, and repairing it here would hide exactly
        the class of defect this module exists to surface.
        """
        try:
            return Decimal(self.value)
        except InvalidOperation as exc:
            raise ExtractionError(
                f"field {self.name!r} on page {self.page} read as {self.value!r}, "
                "which is not a number; it is not repaired here"
            ) from exc


@dataclass(frozen=True, slots=True)
class Extraction:
    """Everything one document yielded."""

    document_id: str
    content_sha256: str
    #: Pages processed. The unit Bedrock Data Automation bills in, and therefore the unit the
    #: cost meter charges in.
    pages: int
    fields: tuple[ExtractedField, ...] = ()
    #: The document's text, for the injection scan that runs before anything is indexed.
    text: str = ""

    def field(self, name: str) -> ExtractedField | None:
        return next((f for f in self.fields if f.name == name), None)

    def require(self, name: str) -> ExtractedField:
        found = self.field(name)
        if found is None:
            raise ExtractionError(
                f"{self.document_id}: no field {name!r} was extracted; "
                f"the document yielded {', '.join(f.name for f in self.fields) or 'nothing'}"
            )
        return found


@runtime_checkable
class Extractor(Protocol):
    def extract(self, document: EvidenceDocument) -> Extraction: ...


# ── Replay ───────────────────────────────────────────────────────────────────


class RecordedExtractor:
    """Replays captures. The default everywhere, including CI."""

    def __init__(self, captures: dict[str, dict[str, Any]]) -> None:
        self._captures = captures

    @classmethod
    def from_root(cls, root: Path | str) -> RecordedExtractor:
        path = Path(root) / "recordings" / EXTRACTIONS_FILE
        captures: dict[str, dict[str, Any]] = {}
        if path.is_file():
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            for entry in payload.get("extractions", []):
                captures[entry["content_sha256"]] = entry
        return cls(captures)

    def extract(self, document: EvidenceDocument) -> Extraction:
        entry = self._captures.get(document.content_sha256)
        if entry is None:
            # Distinguish "never captured" from "captured against different bytes". The
            # second is the dangerous one: the document changed, the capture did not, and a
            # silent replay would describe paper that no longer exists.
            for captured in self._captures.values():
                if captured.get("document_id") == document.document_id:
                    raise StaleExtraction(
                        f"{document.document_id} was captured from bytes "
                        f"{captured['content_sha256'][:12]} and now digests to "
                        f"{document.content_sha256[:12]}. Re-capture it rather than "
                        "replaying an extraction of a different document."
                    )
            raise ExtractionUnavailable(
                f"no captured extraction for {document.document_id} "
                f"({document.content_sha256[:12]}); paper is read once and reviewed, "
                "never invented at replay time"
            )
        return _from_entry(entry)


def _from_entry(entry: dict[str, Any]) -> Extraction:
    return Extraction(
        document_id=entry["document_id"],
        content_sha256=entry["content_sha256"],
        pages=int(entry["pages"]),
        fields=tuple(
            ExtractedField(
                name=str(field["name"]),
                value=str(field["value"]),
                confidence=float(field.get("confidence", 0.0)),
                page=int(field.get("page", 1)),
            )
            for field in entry.get("fields", [])
        ),
        text=str(entry.get("text", "")),
    )


# ── The live extractor ───────────────────────────────────────────────────────


@dataclass
class BedrockDataAutomationExtractor:
    """Bedrock Data Automation: a document in, fields and a page count out.

    The polling loop takes its clock and its sleep as arguments, the same way
    `AthenaBackend` does, so the timeout path is a test rather than a test that waits.
    """

    bucket: str
    project_arn: str
    region: str = "eu-central-1"
    client: Any = None
    s3: Any = None
    timeout_seconds: float = 600.0
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep

    def _bda(self) -> Any:
        if self.client is None:
            import boto3  # noqa: PLC0415 — optional dependency, never imported offline

            self.client = boto3.client("bedrock-data-automation-runtime", region_name=self.region)
        return self.client

    def _s3(self) -> Any:
        if self.s3 is None:
            import boto3  # noqa: PLC0415

            self.s3 = boto3.client("s3", region_name=self.region)
        return self.s3

    def extract(self, document: EvidenceDocument) -> Extraction:
        started = self._bda().invoke_data_automation_async(
            inputConfiguration={"s3Uri": document.source_uri},
            outputConfiguration={"s3Uri": self._output_uri(document)},
            dataAutomationConfiguration={"dataAutomationProjectArn": self.project_arn},
        )
        arn = started["invocationArn"]
        status = self._await(arn)
        return self._read(status, document)

    def _output_uri(self, document: EvidenceDocument) -> str:
        """Content-addressed, like the evidence object itself.

        The extraction of a document is a claim about specific bytes. Writing it to a path
        keyed only on the document id would let a re-extraction overwrite the output a
        lineage record was written against — the same defect `object_key` exists to prevent,
        one layer along.
        """
        return (
            f"s3://{self.bucket}/_extracted/{document.tenant}/"
            f"{document.document_id}/{document.content_sha256[:16]}"
        )

    def _await(self, arn: str) -> dict[str, Any]:
        """Poll until the job settles, or give up loudly.

        A job that has not finished in this long is a job whose document is not the size we
        think it is. Waiting indefinitely turns one bad scan into an ingestion that never
        completes and never says why.
        """
        deadline = self.clock() + self.timeout_seconds
        delay = 1.0
        while True:
            status = self._bda().get_data_automation_status(invocationArn=arn)
            state = status.get("status")
            if state == _SUCCEEDED:
                return status
            if state in _FAILED:
                raise ExtractionError(
                    f"data automation {arn} finished {state}: "
                    f"{status.get('errorMessage', 'no reason given')}"
                )
            if self.clock() > deadline:
                raise ExtractionError(
                    f"data automation {arn} exceeded {self.timeout_seconds}s and was "
                    "abandoned; extraction is not left running into the next stage"
                )
            self.sleep(delay)
            delay = min(delay * 2, 30.0)

    def _read(self, status: dict[str, Any], document: EvidenceDocument) -> Extraction:
        uri = status.get("outputConfiguration", {}).get("s3Uri")
        if not uri:
            raise ExtractionError(f"{document.document_id}: the job reported no output location")
        bucket, _, key = uri.removeprefix("s3://").partition("/")
        body = self._s3().get_object(Bucket=bucket, Key=key)["Body"].read()
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ExtractionError(f"{document.document_id}: extraction output is not JSON") from exc
        return parse_output(payload, document)


def parse_output(payload: dict[str, Any], document: EvidenceDocument) -> Extraction:
    """Read Bedrock Data Automation's output into fields.

    Deliberately narrow. It expects the shape the configured project emits and raises on
    anything else, because the alternative — a parser that copes — is repair logic, and
    repair logic under an extraction step is how a misread page becomes a plausible number.
    If the project's output shape changes, this should fail, loudly, on the first document.
    """
    try:
        pages = int(payload["metadata"]["number_of_pages"])
        text = str(payload["document"]["representation"]["text"])
        inference = payload["inference_result"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ExtractionError(
            f"{document.document_id}: extraction output does not have the expected shape "
            f"({type(exc).__name__}: {exc}). It is not interpreted further."
        ) from exc

    if not isinstance(inference, dict):
        raise ExtractionError(f"{document.document_id}: inference_result is not a mapping")

    fields = tuple(
        ExtractedField(
            name=str(name),
            value=str(body.get("value", "")),
            confidence=float(body.get("confidence", 0.0)),
            page=int(body.get("page", 1)),
        )
        for name, body in sorted(inference.items())
        if isinstance(body, dict)
    )
    return Extraction(
        document_id=document.document_id,
        content_sha256=document.content_sha256,
        pages=pages,
        fields=fields,
        text=text,
    )


# ── Metering ─────────────────────────────────────────────────────────────────


@dataclass
class MeteredExtractor:
    """Charges every extraction to the tenant whose document it was.

    A decorator rather than a branch inside each extractor: metering is not the extractor's
    concern, and a charge recorded in one implementation and forgotten in the other is how a
    per-tenant cost figure becomes wrong in exactly the direction nobody checks.
    """

    inner: Extractor
    meter: CostMeter
    session_id: str = "ingest"

    def extract(self, document: EvidenceDocument) -> Extraction:
        extraction = self.inner.extract(document)
        if extraction.pages > 0:
            self.meter.record(
                Meter.DOCUMENT_PARSE,
                extraction.pages,
                tenant=document.tenant,
                session_id=self.session_id,
                operation="ingest",
            )
        return extraction


# ── The switch ───────────────────────────────────────────────────────────────


def build(
    root: Path | str = ".",
    *,
    meter: CostMeter | None = None,
    extractor: str | None = None,
    **live: Any,
) -> Extractor:
    """The extractor this process should use.

    Recorded unless `ATTESTOR_EXTRACTOR=bda` says otherwise — the same shape as the query
    backend and the narrative provider, and for the same reason. An offline run must not be
    able to reach a paid service by forgetting a flag, so the live path is opt-in and asserts
    what it needs rather than defaulting.
    """
    chosen = (extractor or os.environ.get("ATTESTOR_EXTRACTOR", "recorded")).lower()
    if chosen != "bda":
        base: Extractor = RecordedExtractor.from_root(root)
    else:
        missing = [name for name in ("bucket", "project_arn") if not live.get(name)]
        if missing:
            raise ExtractionError(
                f"ATTESTOR_EXTRACTOR=bda but {', '.join(missing)} are unset. Falling back to "
                "a capture against live paper would report on a document nobody read."
            )
        base = BedrockDataAutomationExtractor(**live)
    return MeteredExtractor(base, meter) if meter is not None else base
