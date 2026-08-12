"""Every declared dependency is imported by something.

An unused dependency is not free. It is downloaded on every CI run, vendored into the Lambda
payload, layered into the container, and watched by whatever scans this repository for
vulnerabilities — all to support code nobody wrote. `jsonschema` sat in the runtime
dependency list here for exactly that long, and two OpenTelemetry packages sat in the cloud
extra while `observability/` imported neither.

The check is deliberately crude — a text search for the import name across the source tree.
A crude check that runs beats a precise one that has to be maintained, and the failure mode
of the crude version (a false positive on a package genuinely used through a plugin) is a
one-line entry in `INDIRECT` with a reason next to it.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: Distribution name → the module name you actually import.
IMPORT_NAME = {
    "pyyaml": "yaml",
    "python-docx": "docx",
    "python-pptx": "pptx",
    "pytest-cov": "pytest_cov",
    "pillow": "PIL",
}

#: Declared but never imported *by us*, with the reason. Anything not here must be reachable
#: from a real import statement.
INDIRECT = {
    "ruff": "a linter, invoked as a command by CI and the Makefile",
    "checkov": "an IaC scanner, invoked as a command",
    "pytest-cov": "a pytest plugin, activated by --cov rather than imported",
}


def _declared() -> dict[str, set[str]]:
    payload = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = payload["project"]
    groups = {"runtime": set(project["dependencies"])}
    for name, entries in project.get("optional-dependencies", {}).items():
        groups[name] = set(entries)
    return {
        group: {re.split(r"[<>=!\[]", entry)[0].strip().lower() for entry in entries}
        for group, entries in groups.items()
    }


def _sources() -> str:
    parts = []
    for directory in ("src", "tests", "scripts", "pipelines"):
        for path in (ROOT / directory).rglob("*.py"):
            parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


@pytest.fixture(scope="module")
def sources() -> str:
    return _sources()


@pytest.mark.parametrize("group", ["runtime", "cloud", "dev"])
def test_every_declared_dependency_is_used(group: str, sources: str) -> None:
    unused = []
    for distribution in sorted(_declared()[group]):
        if distribution in INDIRECT:
            continue
        module = IMPORT_NAME.get(distribution, distribution.replace("-", "_"))
        if not re.search(rf"^\s*(?:from|import)\s+{re.escape(module)}\b", sources, re.M):
            unused.append(distribution)
    assert not unused, (
        f"declared in [{group}] and imported by nothing: {', '.join(unused)}. "
        "Remove it, or add it to INDIRECT with the reason it is still needed."
    )


def test_boto3_is_never_a_runtime_dependency() -> None:
    """The whole suite, every eval and every gate run with no cloud client installed.

    If boto3 became a hard dependency, "offline is the default" would be a claim about
    habits rather than about what the package can do.
    """
    assert "boto3" not in _declared()["runtime"]


def test_boto3_is_never_imported_at_module_scope() -> None:
    """Lazily, inside the function that needs it — so an offline run cannot reach for it."""
    offenders = []
    for path in (ROOT / "src").rglob("*.py"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.match(r"^import boto3|^from boto3", line):
                offenders.append(f"{path.relative_to(ROOT)}:{number}")
    assert not offenders, offenders


def test_no_test_imports_the_suite_as_a_package() -> None:
    """`from tests.conftest import ...` needs the repository root on `sys.path`.

    An editable install happens to arrange that on a developer's machine; a clean checkout
    does not. So the suite passed locally, failed collection in CI, and the difference was
    invisible from either side — which is exactly the failure this file exists to catch.
    Share helpers through a fixture; `conftest.py` is loaded by pytest, not imported.
    """
    offenders = []
    for path in (ROOT / "tests").rglob("*.py"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.match(r"^\s*(?:from tests[. ]|import tests\b)", line):
                offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
    assert not offenders, offenders
