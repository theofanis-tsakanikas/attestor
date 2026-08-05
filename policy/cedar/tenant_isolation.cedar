// Tenant isolation.
//
// The first policy is the one that matters, and it is a forbid. In Cedar a forbid always
// wins, so no combination of permits anywhere else in this directory can grant a principal
// access to a resource belonging to another tenant. That property is what makes the
// isolation suite in evals/isolation/ a test of the *system* rather than of one code path:
// there is no path that gets to be the exception.
//
// Note it is written with `unless` rather than `when`. `forbid ... unless { equal }` denies
// by default and requires the equality to be demonstrable; a missing attribute is an error,
// not a pass. Written the other way round — `forbid ... when { not equal }` — a request
// whose tenant attribute was absent would slip through the condition and fall to whatever
// permit matched next.

@id("forbid-cross-tenant")
forbid (
    principal,
    action,
    resource
) unless {
    resource.tenant == principal.tenant
};

// A session is scoped to one tenant when it is created. If the request's tenant and the
// session's tenant ever disagree, something has rewritten one of them mid-flight.
@id("forbid-session-tenant-mismatch")
forbid (
    principal,
    action,
    resource
) unless {
    context.session_tenant == principal.tenant
};

// Retrieval must carry a tenant filter. A retrieval with no filter is not a narrow query
// that happens to return the right rows; it is an unscoped read waiting for a bug.
@id("forbid-unfiltered-retrieval")
forbid (
    principal,
    action in [Action::"search_evidence", Action::"search_regulation"],
    resource
) unless {
    context.filter_tenant == principal.tenant
};
