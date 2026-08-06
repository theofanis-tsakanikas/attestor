#!/usr/bin/env python3
"""Every subject the deploy role trusts names one repository and one environment.

This exists because the borrowed check went blind. CKV_AWS_358 is the industry check for
"GitHub OIDC trust policies only allow safe claims", and it splits the subject on `:` and
requires segment 1 to look like `owner/repo`. GitHub now issues an *immutable* subject —
`repo:owner@218610429/attestor@1324675810:environment:deploy`, with the numeric ids inlined
so that releasing a repository name cannot hand its trust to whoever registers it next. The
`@` fails checkov's regex, and worse, checkov inspects only the **first** element of the
values array: on a list it cannot parse it returns a verdict about array ordering rather than
about array contents. A green from it would mean "the one element I could read was fine".

So this reads all of them, and asks the questions that actually matter:

  - every subject is fully qualified — `repo:<something>:environment:<name>`
  - the repository segment is this repository, under either the mutable or the immutable form
  - the environment is one this repository actually gates on
  - no wildcard appears anywhere, in any segment

The last one is the point. `repo:owner/*` and `repo:owner/repo:*` are both syntactically fine
and both hand the role to workflows nobody reviewed — the first to every repository the owner
will ever create, the second to every branch, tag and pull request in this one.

Runs against the Terraform source, offline, with no AWS account: the values are literals and
locals in `infra/bootstrap`, and reading them from a plan would make this a check you can only
run once you have credentials to the thing it is protecting.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "infra" / "bootstrap"

#: The environments `deploy.yml` and `destroy.yml` declare. A subject naming anything else is
#: a trust nothing in this repository can use, which means somebody added it by hand.
ENVIRONMENTS = {"deploy", "destroy"}

SUBJECT = re.compile(r"^repo:(?P<repository>[^:]+):environment:(?P<environment>[A-Za-z0-9_.-]+)$")
TFVAR = re.compile(r"^\s*(?P<name>[a-z_]+)\s*=\s*\"(?P<value>[^\"]*)\"", re.MULTILINE)


def tfvars() -> dict[str, str]:
    path = BOOTSTRAP / "terraform.tfvars"
    if not path.is_file():
        return {}
    return {m["name"]: m["value"] for m in TFVAR.finditer(path.read_text(encoding="utf-8"))}


def subjects(text: str) -> list[str]:
    """The subject templates, read out of the `github_subjects` local.

    Deliberately literal. Evaluating HCL here would mean reimplementing Terraform badly; the
    local is a flat list of interpolated strings and the shape this check cares about — a
    wildcard, a missing environment segment — survives interpolation unchanged.

    This is why that local spells each subject out instead of generating them: a `for` over a
    list of environments moves the `repo:` prefix somewhere else, and then the check is
    reading an expression rather than a subject.
    """
    start = text.find("github_subjects")
    if start == -1:
        return []
    end = text.find("]", start)
    if end == -1:
        return []
    return re.findall(r'"(repo:[^"]+)"', text[start:end])


def resolve(template: str, values: dict[str, str]) -> str:
    def substitute(match: re.Match[str]) -> str:
        name = match["name"]
        return values.get(name, f"<{name}>")

    resolved = re.sub(r"\$\{var\.(?P<name>[a-z_]+)\}", substitute, template)
    # `local.github_owner` / `local.github_repo` are the two halves of github_repository.
    repository = values.get("github_repository", "<github_repository>")
    owner, _, repo = repository.partition("/")
    resolved = resolved.replace("${local.github_owner}", owner)
    resolved = resolved.replace("${local.github_repo}", repo)
    return re.sub(r"\$\{environment\}", "deploy", resolved)


def main() -> int:
    main_tf = BOOTSTRAP / "main.tf"
    if not main_tf.is_file():
        print("no infra/bootstrap/main.tf", file=sys.stderr)
        return 1

    text = main_tf.read_text(encoding="utf-8")
    values = tfvars()
    templates = subjects(text)

    problems: list[str] = []
    if not templates:
        problems.append(
            "no subjects found in the `github_subjects` local — this check has lost its target"
        )

    repository = values.get("github_repository", "")
    owner, _, repo = repository.partition("/")
    accepted = set()
    if repository:
        accepted.add(repository)
    if owner and repo and "github_owner_id" in values and "github_repository_id" in values:
        accepted.add(f"{owner}@{values['github_owner_id']}/{repo}@{values['github_repository_id']}")

    seen_environments = set()
    for template in templates:
        subject = resolve(template, values)
        if "*" in subject or "?" in subject:
            problems.append(
                f"{subject}: contains a wildcard. StringLike makes it match workflows nobody "
                "reviewed — every branch, or every repository the owner will ever create"
            )
            continue
        match = SUBJECT.match(subject)
        if match is None:
            problems.append(
                f"{subject}: not of the form repo:<repository>:environment:<name>, so it is "
                "not scoped to an environment at all"
            )
            continue
        seen_environments.add(match["environment"])
        if accepted and match["repository"] not in accepted:
            problems.append(
                f"{subject}: names {match['repository']}, which is neither "
                f"{repository} nor its immutable form"
            )
        if match["environment"] not in ENVIRONMENTS:
            problems.append(
                f"{subject}: environment {match['environment']!r} is not one this repository "
                f"gates on ({', '.join(sorted(ENVIRONMENTS))})"
            )

    missing = ENVIRONMENTS - seen_environments
    if templates and missing:
        problems.append(
            f"no subject for environment(s) {', '.join(sorted(missing))} — that workflow "
            "cannot mint credentials at all"
        )

    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
    print(f"  {len(templates)} subject(s) checked, {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
