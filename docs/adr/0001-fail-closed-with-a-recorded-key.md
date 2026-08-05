# ADR-0001 — Fail closed, with a recorded key

**Status:** accepted · **Date:** 2026-08-05

## Context

The system refuses to publish a figure it cannot support. `reason_codes.py` splits the
reasons a datapoint may go undisclosed into *lawful omissions* (the standard permits them)
and *internal failures* (our pipeline broke). Internal failures block the report.

The question is whether that block is absolute.

An absolute block is the obviously safe answer and the wrong one. It is 22:40 the night
before a filing deadline, one resolver is failing on a schema change nobody noticed, and the
report will not build. What actually happens next is not that the deadline moves. What
happens is that somebody exports the draft, edits the number in Word, and files it — and now
the disclosure has no lineage, no gate, no record, and nobody will ever know which figure was
touched.

**A control with no override does not prevent the override. It moves it outside the system.**
That is worse than a controlled one, because the controlled one leaves evidence.

## Decision

Fail closed by default, with a break-glass path governed by seven rules. These are the
project's standing doctrine, not a one-off for this module.

1. **The safe state is no output.** Every gate's default is refusal.
2. **Every closed door has a key, and the key is a named human.** The system can never open
   a door for itself. No model, no agent, no service principal may request, approve or
   classify an override.
3. **An override changes what ships, never what is true.** It does not relabel a defect as
   compliant. `E_UPSTREAM_QUARANTINE` remains `E_UPSTREAM_QUARANTINE` in the record after
   it is overridden; what changes is whether the build stops.
4. **An override is visible in the artefact, not only in a log.** It prints on the face of
   the statement, in the material-limitations section, where the auditor reads. Our
   CloudWatch is not where an auditor looks.
5. **Overrides expire.** On expiry the finding returns and CI goes red again. Nobody gets
   to grant themselves a permanent exemption and forget about it.
6. **Severity decides who turns the key** — how many approvers, in which roles, for how long.
7. **One door has no key at all.**

## The door with no key

`E_RESOLVER_ERROR` is not overridable, by any approver, for any duration.

The reasoning is narrow and worth stating precisely. Every other internal failure is a
*known* deficiency: we know evidence is missing, we know rows were quarantined, we know two
computations disagree. A human can look at a known deficiency, judge its materiality, and
sign for it.

A resolver that crashed is an *unknown* deficiency. We do not know what the figure would
have been, so nobody can judge whether the gap is material — including the person being asked
to sign. An approval given without the information the approval is about is theatre. The fix
is to fix the resolver.

Having exactly one unopenable door is also what keeps the other six rules honest. A
break-glass mechanism that opens everything is a rubber stamp with extra ceremony.

## What an override may do

| Effect | Meaning | Available for |
|---|---|---|
| `publish_with_qualification` | The figure is published, with the defect disclosed beside it | `E_OUT_OF_TOLERANCE` only — it is the one internal failure where a figure actually exists |
| `omit_with_material_limitation` | The report is issued, this datapoint is not, and the statement says so as a material limitation | The known-deficiency failures |
| *(nothing)* | — | `E_RESOLVER_ERROR` |

No override may produce a lawful omission. That path stays closed: it is the exact
laundering the reason-code split exists to prevent, and a human signature does not make
"our code crashed" into "not material".

## Consequences

- An expired override turns CI red. That is the point, not a bug.
- The material-limitations section of every report is generated from the override register,
  so a reader sees the same list the build saw.
- A report issued under an override is *still issued*. Availability was never the enemy;
  undisclosed defects were.
- The register is a first-class artefact under `overrides/`, reviewed in pull requests like
  everything else. An override is a commit with a name on it.

## Related

- `src/attestor/contracts/reason_codes.py` — the lawful / internal split
- `src/attestor/contracts/overrides.py` — this decision, implemented
- The signed, expiring exception ledger in `multicloud-governance-platform` is the same
  pattern applied to access grants; this is its sibling.
