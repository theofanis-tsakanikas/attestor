#!/usr/bin/env python3
"""A workflow may not ask for a longer session than the role it assumes allows.

`configure-aws-credentials` passes `role-duration-seconds` to `AssumeRoleWithWebIdentity`,
and STS refuses outright when it exceeds the role's `max_session_duration`:

    Could not assume role with OIDC: The requested DurationSeconds exceeds the
    MaxSessionDuration set for this role.

That is not a slow run or a truncated session. It is a job that fails on its third step,
before it has touched a resource — and it fails on every run until somebody notices that two
numbers in two files disagree.

Both numbers are in this repository: the request in `.github/workflows/*.yml`, the ceiling in
`infra/bootstrap/main.tf`. Neither file is wrong on its own, which is exactly the shape of
defect that survives review, and it is the same shape as the reusable-workflow permissions
this repository already checks. Destroy 31555029391 found it the expensive way.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
BOOTSTRAP = ROOT / "infra" / "bootstrap" / "main.tf"

#: STS's own default when a role does not say otherwise.
DEFAULT_MAX_SESSION = 3600


def ceiling() -> int:
    """The role's `max_session_duration`, read from the layer that creates it."""
    match = re.search(r"max_session_duration\s*=\s*(\d+)", BOOTSTRAP.read_text(encoding="utf-8"))
    return int(match.group(1)) if match else DEFAULT_MAX_SESSION


def _requests(workflow: dict) -> list[tuple[str, int]]:
    """Every `role-duration-seconds` a workflow asks for, with the job that asks."""
    found: list[tuple[str, int]] = []
    for name, job in (workflow.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        for step in job.get("steps") or []:
            if not isinstance(step, dict):
                continue
            asked = (step.get("with") or {}).get("role-duration-seconds")
            if asked is not None:
                found.append((name, int(asked)))
    return found


def problems() -> list[str]:
    allowed = ceiling()
    found: list[str] = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for job, asked in _requests(workflow):
            if asked > allowed:
                found.append(
                    f"{path.name}: job '{job}' asks for {asked}s, but the role's "
                    f"max_session_duration is {allowed}s. STS refuses the assume; the job "
                    f"fails before touching a resource."
                )
    return found


def main() -> int:
    found = problems()
    for problem in found:
        print(f"  {problem}", file=sys.stderr)
    print(f"  role ceiling {ceiling()}s, {len(found)} problem(s)")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
