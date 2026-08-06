// Everything a token that reached this gateway may do, and nothing more.
//
// AgentCore's policy engine is default-deny: with no permit, a gateway serves nothing. So
// this exists to say that reaching the gateway at all is the grant, and to be explicit about
// two things it deliberately does not decide.
//
// **It does not decide the tenant.** That is settled one layer out, structurally: one Cognito
// pool per tenant, one gateway per tenant, and a JWT authorizer bound to exactly that pool's
// issuer — so a token minted for aegis cannot authenticate at the helios gateway at all. That
// fails before any policy runs, which is stronger than a Cedar condition. Re-deriving it from
// `principal.getTag(...)` would mean asserting a claim-to-tag mapping this repository has not
// verified against a live token, in the one file where being nearly right is the same as
// being wrong.
//
// **It does not grant future tools.** The actions are listed rather than left open, so a tool
// added to `SPECS` is denied at the gateway until somebody names it here. That is the safer
// direction to fail in, and `scripts/check_agentcore_policies.py` makes it a build failure
// rather than a surprise: the list must match the MCP tool schema exactly, minus
// `request_override` — which is forbidden next door and deliberately not permitted here
// either, so that forbid is a belt to a brace rather than the only thing holding the door.

permit (
    principal is AgentCore::OAuthUser,
    action in [${actions}],
    resource == AgentCore::Gateway::"${gateway_arn}"
);
