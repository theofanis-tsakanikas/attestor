// Everything a token that reached this gateway may do, and nothing more.
//
// AgentCore's policy engine is default-deny: with no permit, a gateway serves nothing. So
// this exists to say that reaching the gateway at all is the grant, and to be explicit that
// the tenant boundary is *not* decided here.
//
// It is decided one layer out, structurally. There is one Cognito pool per tenant and one
// gateway per tenant, and the gateway's JWT authorizer validates against exactly that pool's
// issuer — so a token minted for aegis cannot authenticate at the helios gateway at all. That
// is a stronger control than a Cedar condition, because it fails before any policy runs.
//
// Re-deriving it here from `principal.getTag(...)` would mean asserting a claim-to-tag mapping
// this repository has not verified against a live token, in the one file where being nearly
// right is indistinguishable from being wrong.

permit (
    principal is AgentCore::OAuthUser,
    action,
    resource == AgentCore::Gateway::"${gateway_arn}"
);
