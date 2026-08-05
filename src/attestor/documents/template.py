"""Templates as YAML, not as binary .docx files.

A committed `.docx` is a zip of XML that no reviewer reads. Changing a sentence inside one
produces a diff of a few thousand unreadable lines, and "the template said so" becomes an
unfalsifiable claim. Since template text is one of only two sources a numeral may legally
come from, that is not acceptable here.

So templates are YAML: reviewable, diffable, and the renderer constructs the document. The
cost is that the visual design lives in code rather than in Word, which for a regulated
statement is the right trade — an auditor cares what it says, not what font it says it in.

Placeholder grammar, and nothing else is recognised::

    {{dp:ESRS_E1-6_gross_scope_1}}      a resolved figure, formatted per its contract
    {{narrative:ESRS_E1-1_transition_plan}}   a model-authored block
    {{meta:tenant_name}}                deterministic context
    {{table:annex}}                     the auditor annex
    {{table:limitations}}               the material-limitations register

An unrecognised `{{...}}` is a load error rather than literal text on the page. A typo in a
placeholder must not silently print `{{dp:ESRS_E1-6_gros_scope_1}}` into a filing.
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

PLACEHOLDER = re.compile(r"\{\{([a-z]+):([A-Za-z0-9_.\-]+)\}\}")
ANY_BRACES = re.compile(r"\{\{.*?\}\}")


class PlaceholderKind(StrEnum):
    DATAPOINT = "dp"
    NARRATIVE = "narrative"
    META = "meta"
    TABLE = "table"


KNOWN_META = frozenset(
    {"tenant_name", "tenant_id", "period", "period_start", "period_end", "report_date", "standard"}
)
KNOWN_TABLES = frozenset({"annex", "limitations", "datapoints"})


class TemplateError(ValueError):
    """The template is not renderable. Always fatal — never rendered "as best we can"."""


class Placeholder(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: PlaceholderKind
    reference: str
    raw: str


def parse_placeholders(text: str) -> tuple[Placeholder, ...]:
    """Extract placeholders, rejecting any brace expression that is not one."""
    found = tuple(
        Placeholder(kind=PlaceholderKind(kind), reference=reference, raw=match.group(0))
        for match in PLACEHOLDER.finditer(text)
        for kind, reference in [match.groups()]
        if kind in {k.value for k in PlaceholderKind}
    )
    recognised = {p.raw for p in found}
    for candidate in ANY_BRACES.findall(text):
        if candidate not in recognised:
            raise TemplateError(
                f"{candidate!r} is not a placeholder. Recognised forms are "
                "{{dp:ID}}, {{narrative:ID}}, {{meta:KEY}}, {{table:NAME}}"
            )
    return found


class Block(BaseModel):
    """One element of a document. `text` may carry placeholders; `kind` decides its shape."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["heading", "paragraph", "table", "bullets", "page_break", "note"]
    text: str = ""
    level: int = Field(default=1, ge=1, le=4)
    items: tuple[str, ...] = ()
    #: For `kind: table` — which generated table to place.
    table: str | None = None

    @model_validator(mode="after")
    def _shape_matches_kind(self) -> Self:
        if self.kind == "table":
            if not self.table:
                raise TemplateError("a table block must name a table")
            if self.table not in KNOWN_TABLES:
                raise TemplateError(
                    f"unknown table {self.table!r}; known: {', '.join(sorted(KNOWN_TABLES))}"
                )
        elif self.kind == "bullets":
            if not self.items:
                raise TemplateError("a bullets block must have items")
        elif self.kind != "page_break" and not self.text:
            raise TemplateError(f"a {self.kind} block must have text")
        for source in (self.text, *self.items):
            for placeholder in parse_placeholders(source):
                if placeholder.kind is PlaceholderKind.META and placeholder.reference not in (
                    KNOWN_META
                ):
                    raise TemplateError(
                        f"unknown meta key {placeholder.reference!r}; "
                        f"known: {', '.join(sorted(KNOWN_META))}"
                    )
        return self

    @property
    def placeholders(self) -> tuple[Placeholder, ...]:
        found: list[Placeholder] = []
        for source in (self.text, *self.items):
            found.extend(parse_placeholders(source))
        return tuple(found)


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str
    #: The clause this section discharges. Printed in the auditor annex.
    reference: str = ""
    blocks: tuple[Block, ...] = ()


class Template(BaseModel):
    """A document, declared."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9_]+$")
    artefact: Literal["docx", "xlsx", "pptx"]
    title: str
    standard: str
    subtitle: str = ""
    sections: tuple[Section, ...] = ()

    @classmethod
    def load(cls, path: Path | str) -> Template:
        path = Path(path)
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.model_validate(payload)

    @classmethod
    def load_all(cls, directory: Path | str) -> tuple[Template, ...]:
        directory = Path(directory)
        return tuple(cls.load(path) for path in sorted(directory.rglob("*.yaml")))

    @property
    def placeholders(self) -> tuple[Placeholder, ...]:
        return tuple(p for section in self.sections for b in section.blocks for p in b.placeholders)

    def datapoints(self) -> frozenset[str]:
        return frozenset(
            p.reference
            for p in self.placeholders
            if p.kind in {PlaceholderKind.DATAPOINT, PlaceholderKind.NARRATIVE}
        )
