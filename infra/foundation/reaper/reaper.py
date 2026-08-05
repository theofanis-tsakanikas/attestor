"""Report resources whose expiry has passed. It does not delete them.

A lambda with permission to destroy the estate is a larger standing risk than the estate
outliving its window by an afternoon — and the destroy workflow already exists, is already
gated, and already verifies that nothing survived. What was missing was not the ability to
tear down; it was somebody noticing that nobody had.

So this reads tags and publishes. Its IAM policy has no delete verb in it, deliberately.

Runs on the Lambda Python runtime with boto3 provided; no dependencies are vendored.
"""

from __future__ import annotations

import datetime as dt
import json
import os

import boto3

TAG_KEY = os.environ.get("TAG_KEY", "attestor:expires-at")
TOPIC_ARN = os.environ["TOPIC_ARN"]


def _resources() -> list[dict]:
    client = boto3.client("resourcegroupstaggingapi")
    paginator = client.get_paginator("get_resources")
    found: list[dict] = []
    for page in paginator.paginate(TagFilters=[{"Key": TAG_KEY}]):
        found.extend(page.get("ResourceTagMappingList", []))
    return found


def _expiry(resource: dict) -> dt.date | None:
    for tag in resource.get("Tags", []):
        if tag.get("Key") == TAG_KEY:
            try:
                return dt.date.fromisoformat(tag.get("Value", ""))
            except ValueError:
                # An unparseable expiry is worse than a missing one: it looks like a control.
                return dt.date.min
    return None


def handler(event=None, context=None) -> dict:
    today = dt.date.today()
    overdue: list[str] = []
    soon: list[str] = []

    for resource in _resources():
        expires = _expiry(resource)
        if expires is None:
            continue
        arn = resource["ResourceARN"]
        if expires < today:
            overdue.append(f"{arn} (expired {expires})")
        elif (expires - today).days <= 1:
            soon.append(f"{arn} (expires {expires})")

    if overdue or soon:
        lines = []
        if overdue:
            lines += [
                f"{len(overdue)} resource(s) are past their expiry. Run the Destroy workflow.",
                *(f"  {item}" for item in overdue[:40]),
            ]
        if soon:
            lines += [
                "",
                f"{len(soon)} resource(s) expire within a day:",
                *(f"  {item}" for item in soon[:40]),
            ]
        boto3.client("sns").publish(
            TopicArn=TOPIC_ARN,
            Subject=f"attestor: {len(overdue)} overdue, {len(soon)} expiring",
            Message="\n".join(lines),
        )

    summary = {"checked": len(_resources()), "overdue": len(overdue), "expiring": len(soon)}
    print(json.dumps({"event": "reaper.swept", **summary}))
    return summary
