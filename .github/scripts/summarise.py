#!/usr/bin/env python3
"""Turn the run records into the table that goes in the workflow summary.

The summary is the first thing anybody reads after a deploy, so it says what happened rather
than that something happened. A blocked tenant is bold: the interesting outcome is the
refusal, not the two successes beside it.
"""

from __future__ import annotations

import json
import pathlib
import sys

RUNS = pathlib.Path("out/runs")


def main() -> int:
    records = sorted(RUNS.glob("*.json"))
    if not records:
        print("No run records were produced.")
        return 0

    print("| tenant | standard | verdict | disclosed | limitations | blockers |")
    print("|---|---|---|---:|---:|---:|")
    for path in records:
        record = json.loads(path.read_text(encoding="utf-8"))
        verdict = "issued" if record["issued"] else "**BLOCKED**"
        print(
            f"| {record['tenant']} | {record['standard']} | {verdict} | "
            f"{len(record['published'])} | {len(record['limitations'])} | "
            f"{len(record['blockers'])} |"
        )

    blocked = [json.loads(p.read_text()) for p in records]
    for record in blocked:
        if record["blockers"]:
            print()
            print(f"**{record['tenant']} did not issue.** What stopped it:")
            print()
            for entry in record["blockers"]:
                print(f"- `{entry['datapoint_id']}` — `{entry['reason_code']}`: {entry['detail']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
