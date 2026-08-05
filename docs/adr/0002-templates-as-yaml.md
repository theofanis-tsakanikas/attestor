# ADR-0002 — Templates are YAML, not .docx

**Status:** accepted · **Date:** 2026-08-05

## Context

The obvious way to template a Word document is to template a Word document: put the
placeholders in a `.docx`, commit it, let the business own the layout.

That is the right answer in most systems and the wrong one here, for a reason specific to
claim 3. A numeral may legally appear on a page for exactly two reasons: a resolver produced
it, or it was in reviewed template text. The second half of that sentence is only worth
anything if the template text is genuinely reviewed — and a `.docx` is a zip of XML that
produces a several-thousand-line diff when somebody changes a sentence. Nobody reads it. So
"a human reviewed this number" becomes unfalsifiable, and one of the two sources a figure may
come from is no longer trustworthy.

## Decision

Templates are YAML: sections, blocks, and typed placeholders. The renderer constructs the
DOCX, XLSX and PPTX from them.

An unrecognised `{{...}}` is a load error rather than literal text. A typo must not print
`{{dp:ESRS_E1-6_gros_scope_1}}` into a filing.

## Consequences

- A template change is a readable diff. That is the whole point.
- Visual design lives in code. For a regulated statement that is an acceptable trade — an
  auditor cares what it says, not what font it says it in — and it would not be acceptable
  for a marketing deck.
- The business cannot edit the template in Word. If that becomes a requirement, the honest
  answer is a reviewed import step that converts a `.docx` into this YAML and shows the diff,
  not a change to what the gate trusts.
