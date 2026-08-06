#!/usr/bin/env python3
"""The build context has everything the image build reads, and nothing git-ignored.

A `COPY` of a missing directory fails the build — after the base layers have been pulled and
the dependency install has run, which is several minutes into a deploy. Checking it costs
milliseconds and the failure it prevents costs a re-run of everything before it.

It also catches the subtler case: a directory that exists locally but is git-ignored, so the
build works on the machine that wrote it and fails in CI.

Both of those read the Dockerfile and ask whether the context can satisfy it. That is one
direction, and the *other* direction is where this check was blind: a file the build needs
that no `COPY` mentions at all. `pyproject.toml` names a readme and a licence, hatchling
opens both while generating metadata, and the Dockerfile copied only the first — so
`pip install .` died on `OSError: License file does not exist: LICENSE` while every path the
Dockerfile did mention was present and correct. A check that only validates what is written
cannot see what is missing.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COPY = re.compile(r"^COPY\s+(?:--from=\S+\s+)?(.+)$", re.MULTILINE)
INSTALL = re.compile(r"^RUN\s+.*\bpip install\b.*\s\.\s*$", re.MULTILINE)


def ignored(path: Path) -> bool:
    result = subprocess.run(  # noqa: S603
        ["git", "check-ignore", "-q", str(path)], cwd=ROOT, check=False
    )
    return result.returncode == 0


def packaging_inputs() -> dict[str, str]:
    """The files `pyproject.toml` points the build backend at, by the field that names them.

    Read from the manifest rather than listed here, so adding `license-files` or moving the
    readme cannot leave this check asserting yesterday's answer.
    """
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    named: dict[str, str] = {}
    readme = project.get("readme")
    if isinstance(readme, str):
        named["readme"] = readme
    elif isinstance(readme, dict) and "file" in readme:
        named["readme"] = readme["file"]
    licence = project.get("license")
    if isinstance(licence, dict) and "file" in licence:
        named["license"] = licence["file"]
    for index, pattern in enumerate(project.get("license-files", [])):
        if not any(character in pattern for character in "*?["):
            named[f"license-files[{index}]"] = pattern
    return named


def main() -> int:
    dockerfile = ROOT / "Dockerfile"
    if not dockerfile.is_file():
        print("no Dockerfile", file=sys.stderr)
        return 1

    text = dockerfile.read_text(encoding="utf-8")
    problems: list[str] = []
    copied: set[str] = set()

    for match in COPY.finditer(text):
        parts = match.group(1).split()
        sources = parts[:-1]
        for source in sources:
            if source.startswith("/"):
                continue  # from a build stage, not the context
            copied.add(source)
            path = ROOT / source
            if not path.exists():
                problems.append(f"COPY {source}: not in the build context")
            elif ignored(path):
                problems.append(
                    f"COPY {source}: exists but is git-ignored — the build works here and "
                    "fails in CI"
                )

    # The other direction: what the build reads without the Dockerfile ever naming it.
    install = INSTALL.search(text)
    if install is None:
        problems.append("no `RUN pip install .` stage — this check no longer knows what it guards")
    else:
        before = text[: install.start()]
        for field, filename in packaging_inputs().items():
            if filename not in copied:
                problems.append(
                    f"pyproject `{field}` names {filename}, which no COPY brings into the "
                    "context — the build backend opens it and fails"
                )
            elif filename not in before:
                problems.append(
                    f"{filename} is copied, but after `pip install .` — the build backend "
                    f"reads it for `{field}` before that line runs"
                )

    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
    print(f"  {len(problems)} problem(s) in the build context")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
