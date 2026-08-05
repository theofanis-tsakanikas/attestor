// What each role may do.
//
// Roles are groups the principal belongs to, resolved through the entity hierarchy rather
// than by matching strings against an attribute. That distinction matters: group membership
// is assigned at session creation from the identity provider's claims, so a principal cannot
// argue its way into a role during a conversation.

@id("permit-reporter-read")
permit (
    principal in Group::"role:reporter",
    action in [
        Action::"resolve_datapoint",
        Action::"search_evidence",
        Action::"search_regulation",
        Action::"read_lineage"
    ],
    resource
);

// Rendering produces an artefact that leaves the system, so it is a preparer's act, not a
// reader's. The report itself still cannot be issued while anything blocks — that is decided
// by the resolver, not here. Authorization says who may ask; the gates say what may ship.
@id("permit-preparer-render")
permit (
    principal in Group::"role:preparer",
    action in [
        Action::"resolve_datapoint",
        Action::"search_evidence",
        Action::"search_regulation",
        Action::"read_lineage",
        Action::"draft_narrative",
        Action::"render_report",
        // Reading an existing acceptance and drafting a request for a new one are both
        // preparer work — the preparer is the person who discovers the gap. Neither is
        // approval: `forbid-approval-through-the-agent` below has no exception for a role.
        Action::"read_override",
        Action::"request_override"
    ],
    resource
);

// An auditor reads everything within the engagement and writes nothing. There is no action
// in this file that would let one, which is the point of enumerating rather than excluding.
@id("permit-auditor-read")
permit (
    principal in Group::"role:auditor",
    action in [
        Action::"resolve_datapoint",
        Action::"search_evidence",
        Action::"search_regulation",
        Action::"read_lineage",
        Action::"read_override"
    ],
    resource
);

// Nobody gets `approve_override` from a policy. Approving an override is a signature in a
// committed register with a named human and an expiry date, reviewed in a pull request —
// see contracts/overrides.py. Granting it here would create a second, weaker path to the
// same decision, and the weaker path is the one that gets used.
@id("forbid-approval-through-the-agent")
forbid (
    principal,
    action == Action::"approve_override",
    resource
);
