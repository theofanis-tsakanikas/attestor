#!/usr/bin/env python3
"""A called workflow may not ask for a permission its caller does not hold.

`deploy.yml` re-runs `ci.yml` through `uses:` so the gates run against the exact commit being
deployed. GitHub builds one token for that call, bounded by what the calling job granted, and
it builds it **before any job exists**.

So exceeding the grant is not a job that fails. It is `startup_failure`: no jobs, no logs, no
annotation, and a dispatch that reports failure with nothing to read. Deploy 31454172729 died
that way, because the secret scan had gained `pull-requests: read` — needed on the
`pull_request` path, where gitleaks asks the API which commits a PR contains — and the deploy
grants only `id-token: write` and `contents: read`.

Nothing catches it in review: both files are correct on their own, and the incompatibility
lives in the pair. Nothing catches it in CI either, because `ci.yml` on a pull request is not
the call that breaks. It surfaces on the next deploy, which is the most expensive place to
find anything and the one furthest from the change that caused it.

This reads both files and compares the sets. No credentials, no cloud, and it runs in
`make preflight` beside the other things that must be true before an estate is stood up.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

#: GitHub grants these to every token regardless of the `permissions:` block, so a called
#: workflow asking for them is never the reason a run refuses to start.
ALWAYS_GRANTED = {"metadata"}


def _permissions(block) -> dict[str, str]:
    """Normalise a `permissions:` value. `read-all` / `write-all` are shorthands, not scopes."""
    if block is None:
        return {}
    if isinstance(block, str):
        return {"*": "read" if block == "read-all" else "write"}
    return dict(block)


def _requested(workflow: dict) -> dict[str, str]:
    """Every scope the called workflow needs — top level, and each job that narrows it.

    A job-level block *replaces* the workflow-level one rather than adding to it, so the
    requirement is the union across jobs: the token has to satisfy whichever job asks for most.
    """
    needed = _permissions(workflow.get("permissions"))
    for job in (workflow.get("jobs") or {}).values():
        if isinstance(job, dict) and "permissions" in job:
            for scope, level in _permissions(job["permissions"]).items():
                if level != "none" and scope not in needed:
                    needed[scope] = level
    return needed


def problems() -> list[str]:
    found: list[str] = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        caller = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        caller_default = _permissions(caller.get("permissions"))

        for name, job in (caller.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            uses = job.get("uses", "")
            if not uses.startswith("./.github/workflows/"):
                continue

            called_path = ROOT / uses.removeprefix("./")
            if not called_path.is_file():
                found.append(f"{path.name}: job '{name}' calls {uses}, which does not exist")
                continue

            granted = _permissions(job.get("permissions")) or caller_default
            if "*" in granted:
                continue  # read-all / write-all covers every scope

            called = yaml.safe_load(called_path.read_text(encoding="utf-8")) or {}
            for scope, level in _requested(called).items():
                if scope in ALWAYS_GRANTED or scope == "*":
                    continue
                if scope not in granted:
                    found.append(
                        f"{path.name}: job '{name}' calls {called_path.name}, which needs "
                        f"'{scope}: {level}' — the caller grants only "
                        f"{{{', '.join(sorted(granted)) or 'nothing'}}}. The run will not start."
                    )
                elif granted[scope] == "read" and level == "write":
                    found.append(
                        f"{path.name}: job '{name}' grants '{scope}: read' but "
                        f"{called_path.name} needs write. A called workflow cannot elevate."
                    )
    return found


def main() -> int:
    found = problems()
    for problem in found:
        print(f"  {problem}", file=sys.stderr)
    callers = sum(
        1
        for path in WORKFLOWS.glob("*.yml")
        for job in (
            (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("jobs") or {}
        ).values()
        if isinstance(job, dict) and str(job.get("uses", "")).startswith("./.github/workflows/")
    )
    print(f"  {callers} reusable-workflow call(s), {len(found)} problem(s)")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
