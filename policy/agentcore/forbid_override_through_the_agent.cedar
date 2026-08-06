// The one door with no key, held shut at the edge.
//
// Doctrine rule 2: the system may never open a door for itself — no model, no agent, no
// service principal may request, approve or classify an override. An override is granted in a
// committed register, by a named human, with an expiry date, reviewed in a pull request.
//
// `request_override` exists as a tool so the agent can *report* that a datapoint is blocked
// and say which override would unblock it. Nothing about that requires it to be callable
// through the gateway, and a second path to the same decision is the weaker one, which is the
// one that gets used.
//
// This is the whole reason the policy engine is in the estate. It needs no token claims and no
// tool arguments — only the name of an action — so it is a rule that can be stated here
// without asserting anything unverified. `forbid` overrides `permit` in Cedar, so this wins
// against the grant next to it regardless of ordering.

forbid (
    principal is AgentCore::OAuthUser,
    action == AgentCore::Action::"${target}___request_override",
    resource == AgentCore::Gateway::"${gateway_arn}"
);
