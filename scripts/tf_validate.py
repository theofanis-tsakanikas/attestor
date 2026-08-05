#!/usr/bin/env python3
"""`terraform validate` on every layer, offline.

`-backend=false` so no state is touched and no credentials are needed; the provider registry
is the only thing reached. This is what catches an attribute that does not exist, and it is
the difference between "this should apply" and "this applies".
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAYERS = ["bootstrap", "foundation", "data", "knowledge", "agent"]


def main() -> int:
    failures: list[str] = []
    for layer in LAYERS:
        directory = ROOT / "infra" / layer
        init = subprocess.run(  # noqa: S603
            ["terraform", f"-chdir={directory}", "init", "-backend=false", "-input=false"],
            capture_output=True,
            text=True,
            check=False,
        )
        if init.returncode != 0:
            failures.append(f"{layer}: init failed\n{init.stderr[-1500:]}")
            continue
        result = subprocess.run(  # noqa: S603
            ["terraform", f"-chdir={directory}", "validate"],
            capture_output=True,
            text=True,
            check=False,
        )
        status = "ok" if result.returncode == 0 else "FAIL"
        print(f"  {layer:<11} {status}")
        if result.returncode != 0:
            failures.append(f"{layer}:\n{(result.stdout + result.stderr)[-2500:]}")

    for failure in failures:
        print(failure, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
