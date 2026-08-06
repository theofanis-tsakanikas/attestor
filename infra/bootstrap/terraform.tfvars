# What this account is, recorded where the next apply will read it.
#
# Bootstrap is the one layer applied by hand, so its inputs are the one place a value can be
# retyped differently each time and nobody finds out. Everything here is a durable fact about
# the deployment target rather than a preference, which is why it is committed instead of
# living in somebody's shell history.
#
# `budget_alert_email` is deliberately absent. It is the address an alarm rings at, it belongs
# to a person rather than to the account, and this repository is a portfolio piece that may
# not stay private. Pass it on the command line:
#
#   terraform -chdir=infra/bootstrap apply -var="budget_alert_email=you@example.com"

github_repository = "theofanis-tsakanikas/attestor"

# The ids behind those names. GitHub's subject claim carries both, because a name can be
# released and re-registered by somebody else and an id cannot:
#
#   gh api users/theofanis-tsakanikas --jq .id
#   gh api repos/theofanis-tsakanikas/attestor --jq .id
github_owner_id      = "218610429"
github_repository_id = "1324675810"

# `dbx-github-deploy` federated into this account first, on 2026-07-04, and an account holds
# one provider per issuer. Adopt it. See the reasoning in variables.tf.
create_github_oidc_provider = false
