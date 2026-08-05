#!/usr/bin/env python3
"""Every path the Dockerfile copies must exist, and be committed.

A `COPY` of a missing directory fails the build — after the base layers have been pulled and
the dependency install has run, which is several minutes into a deploy. Checking it costs
milliseconds and the failure it prevents costs a re-run of everything before it.

It also catches the subtler case: a directory that exists locally but is git-ignored, so the
build works on the machine that wrote it and fails in CI.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COPY = re.compile(r"^COPY\s+(?:--from=\S+\s+)?(.+)$", re.MULTILINE)


def ignored(path: Path) -> bool:
    result = subprocess.run(  # noqa: S603
        ["git", "check-ignore", "-q", str(path)], cwd=ROOT, check=False
    )
    return result.returncode == 0


def main() -> int:
    dockerfile = ROOT / "Dockerfile"
    if not dockerfile.is_file():
        print("no Dockerfile", file=sys.stderr)
        return 1

    problems: list[str] = []
    for match in COPY.finditer(dockerfile.read_text(encoding="utf-8")):
        parts = match.group(1).split()
        sources = parts[:-1]
        for source in sources:
            if source.startswith("/"):
                continue  # from a build stage, not the context
            path = ROOT / source
            if not path.exists():
                problems.append(f"COPY {source}: not in the build context")
            elif ignored(path):
                problems.append(
                    f"COPY {source}: exists but is git-ignored — the build works here and "
                    "fails in CI"
                )

    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
    print(f"  {len(problems)} problem(s) in the build context")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
