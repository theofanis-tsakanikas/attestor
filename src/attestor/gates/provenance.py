"""Claim 3, checked against the artefact rather than against the code that made it.

The gate opens a finished DOCX, XLSX or PPTX, pulls out every scrap of text, and asks two
questions of it.

**Where did each numeral come from?** A digit may legally appear for exactly two reasons:
a resolver produced it, or it was in template text that a human reviewed in a pull request.
The manifest knows the multiset of numerals those two sources permit. Anything the document
contains beyond that multiset is a numeral nobody can account for, and the build fails.

**Did a narrative smuggle one in?** Narrative runs are the only text a language model wrote,
so they are held to a stricter rule: zero digits, no threshold, no allowlist. The manifest
refuses such a run at construction time; this gate looks for it again in the file, because a
manifest is a claim about a document and a claim nobody re-checks is a comment.

The honest limit: the numeral check is over a multiset, not over positions. A narrative digit
that happens to match a permitted one, in a document with spare count, would survive the
first question. It does not survive the second — which is why both exist.
"""

from __future__ import annotations

import html
import re
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from attestor.documents.manifest import RenderManifest, RunKind, numerals

_XML_TAG = re.compile(r"<[^>]+>")

#: A worksheet `<v>` element. In an xlsx a `<v>` holds either a *shared-string index* or a
#: raw number whose displayed form comes from the cell format — in neither case is it the
#: text a reader sees. Scanning it reports the index `2` as an unaccounted numeral in any
#: annex with three strings, so it is removed before the scan. What a reader actually sees
#: lives in `sharedStrings.xml`, which is scanned in full.
#:
#: Removing it would be a blind spot if a real numeric cell existed, so `check` refuses one:
#: see `numeric-cell`.
_SHEET_VALUE = re.compile(r"<v>.*?</v>", re.DOTALL)

#: A cell that carries a value without declaring a string type — i.e. a genuine number.
_NUMERIC_CELL = re.compile(r"<c(?![^>]*\bt=\"(?:s|str|inlineStr)\")[^>]*>\s*<v>", re.DOTALL)

#: The parts of an Office file a reader actually sees.
#:
#: Scoping this precisely matters. Office documents ship with slide masters, layouts, themes
#: and property blocks full of boilerplate — a slide master carries the literal text of its
#: date placeholder, so an untargeted scan finds numerals like "27" and "13" that no author
#: ever wrote and no reader will ever see. Including them would force an allowlist, and an
#: allowlist is where a gate goes to die.
#:
#: So the rule is content, not chrome: body, headers, footers, notes, footnotes, slides,
#: speaker notes, worksheet cells and shared strings. A part outside this list cannot put a
#: visible numeral on a page.
_CONTENT_PARTS = (
    re.compile(r"^word/(document|footnotes|endnotes|comments)\.xml$"),
    re.compile(r"^word/(header|footer)\d*\.xml$"),
    re.compile(r"^ppt/slides/slide\d+\.xml$"),
    re.compile(r"^ppt/notesSlides/notesSlide\d+\.xml$"),
    re.compile(r"^xl/sharedStrings\.xml$"),
    re.compile(r"^xl/worksheets/sheet\d+\.xml$"),
)


def is_content_part(name: str) -> bool:
    return any(pattern.match(name) for pattern in _CONTENT_PARTS)


@dataclass(frozen=True, slots=True)
class Finding:
    kind: str
    detail: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.detail}"


@dataclass(slots=True)
class GateResult:
    artefact: Path
    findings: list[Finding] = field(default_factory=list)
    numerals_checked: int = 0
    runs_checked: int = 0

    @property
    def passed(self) -> bool:
        return not self.findings

    def summary(self) -> str:
        verdict = "PASS" if self.passed else "FAIL"
        return (
            f"{verdict} {self.artefact.name} — {self.numerals_checked} numeral(s) over "
            f"{self.runs_checked} run(s), {len(self.findings)} finding(s)"
        )


def extract_text(path: Path) -> str:
    """Pull the visible text out of an Office file without a rendering library.

    Office formats are zipped XML. Stripping tags from the *content* parts gets every string
    a reader can see, including the ones a library-level API would hide — headers, footers,
    speaker notes, shared strings. That breadth is the point: a number smuggled into a footer
    is still a number in a filing.
    """
    if not zipfile.is_zipfile(path):
        return path.read_text(encoding="utf-8", errors="replace")

    chunks: list[str] = []
    with zipfile.ZipFile(path) as archive:
        for name in sorted(archive.namelist()):
            if not is_content_part(name):
                continue
            raw = archive.read(name).decode("utf-8", errors="replace")
            if name.startswith("xl/worksheets/"):
                raw = _SHEET_VALUE.sub(" ", raw)
            # Strip tags first, then resolve entities. Order matters: `&#8212;` is an em dash
            # written as a decimal character reference, and scanning before unescaping would
            # report "8212" as an unaccounted numeral in every document containing a dash.
            chunks.append(html.unescape(_XML_TAG.sub(" ", raw)))
    return " ".join(chunks)


def collapse(text: str) -> str:
    """Collapse runs of whitespace.

    Word splits a paragraph across as many XML runs as it likes, and stripping tags leaves a
    space wherever one was. So the text of a sentence in the file is the same sentence with
    arbitrary internal whitespace, and a literal containment check fails on a document that
    is entirely correct. Both sides are collapsed before comparison.
    """
    return " ".join(text.split())


def check(artefact: Path, manifest: RenderManifest) -> GateResult:
    """Run the gate over one artefact."""
    result = GateResult(artefact=artefact)
    text = extract_text(artefact)
    flattened = collapse(text)

    permitted = manifest.permitted_numerals()
    found = Counter(numerals(text))
    result.numerals_checked = sum(found.values())
    result.runs_checked = len(manifest.runs)

    unaccounted = found - permitted
    for token, count in sorted(unaccounted.items()):
        result.findings.append(
            Finding(
                "unaccounted-numeral",
                f"{token!r} appears {count} time(s) more than the manifest accounts for; "
                "a numeral must come from a resolved datapoint or from reviewed template text",
            )
        )

    result.findings.extend(_numeric_cells(artefact))

    for run in manifest.of_kind(RunKind.NARRATIVE):
        offending = run.numerals
        if offending:
            result.findings.append(
                Finding(
                    "numeral-in-narrative",
                    f"{run.datapoint_id}: model-authored text contains {offending}",
                )
            )
        if run.text.strip() and collapse(run.text) not in flattened:
            result.findings.append(
                Finding(
                    "narrative-not-in-artefact",
                    f"{run.datapoint_id}: the manifest claims narrative text the file does "
                    "not contain — the manifest does not describe this document",
                )
            )

    for run in manifest.figures:
        if run.lineage_id is None:
            result.findings.append(
                Finding("figure-without-lineage", f"{run.datapoint_id}: no lineage id recorded")
            )
        if run.text.strip() and collapse(run.text) not in flattened:
            result.findings.append(
                Finding(
                    "figure-not-in-artefact",
                    f"{run.datapoint_id}: {run.text!r} is in the manifest but not in the file",
                )
            )

    return result


def _numeric_cells(artefact: Path) -> list[Finding]:
    """Refuse a spreadsheet cell that holds a number rather than the disclosed string.

    The annex writes every value as text on purpose: a float cell invites a reader to
    recompute a disclosure in Excel, and the figure that was signed for is the one the
    contract rounded. This check makes that a rule rather than a habit — and it closes the
    blind spot opened by skipping `<v>` when extracting text.
    """
    if artefact.suffix != ".xlsx" or not zipfile.is_zipfile(artefact):
        return []
    findings: list[Finding] = []
    with zipfile.ZipFile(artefact) as archive:
        for name in sorted(archive.namelist()):
            if not name.startswith("xl/worksheets/"):
                continue
            body = archive.read(name).decode("utf-8", errors="replace")
            count = len(_NUMERIC_CELL.findall(body))
            if count:
                findings.append(
                    Finding(
                        "numeric-cell",
                        f"{name} holds {count} numeric cell(s); annex values are written as "
                        "text so a reader cannot recompute a disclosure past its contract",
                    )
                )
    return findings


def check_all(pairs: list[tuple[Path, RenderManifest]]) -> list[GateResult]:
    return [check(artefact, manifest) for artefact, manifest in pairs]


def report(results: list[GateResult]) -> str:
    lines = [result.summary() for result in results]
    for result in results:
        lines.extend(f"    {finding}" for finding in result.findings)
    failed = sum(1 for result in results if not result.passed)
    lines.append(
        f"\nprovenance gate: {len(results) - failed}/{len(results)} artefact(s) clean"
        if results
        else "provenance gate: nothing to check"
    )
    return "\n".join(lines)
