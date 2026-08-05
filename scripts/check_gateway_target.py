#!/usr/bin/env python3
"""Is the gateway-target provisioner still necessary?

`infra/agent` attaches its tool handlers to each gateway with a `null_resource` and a shell
script, because no Terraform provider has a gateway-target resource. That is a compromise,
and compromises rot: the day a provider ships one, the script becomes a worse way of doing
something Terraform now does properly — and nobody notices, because it still works.

So the compromise is checked. This asserts two things:

1. **The resource still does not exist.** If the installed provider gained one, this fails
   and the message says to delete the script. A red build is a better reminder than a TODO.
2. **The provisioner is still wired.** If somebody removes the `null_resource` without adding
   a real resource, the gateway goes back to serving an empty toolset — silently, which is
   the failure this whole file exists to prevent.

It reads the provider schemas already downloaded by `terraform init`, so it needs no network
and no credentials.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAYER = ROOT / "infra" / "agent"

#: What a real resource would be called, in either provider.
CANDIDATES = (
    "awscc_bedrockagentcore_gateway_target",
    "aws_bedrockagentcore_gateway_target",
)


def provider_resources() -> set[str]:
    """Every resource type the layer's installed providers offer.

    Asked of a throwaway module rather than of `infra/agent` itself. `terraform providers
    schema` insists on an initialised backend, and this layer's backend is S3 — so asking it
    directly would make this check need credentials, which is the one thing no check in this
    repository is allowed to need.

    The throwaway module declares no backend and installs from the plugin directory the layer
    has already populated, so it runs offline and pins to exactly the versions being deployed.
    """
    plugins = LAYER / ".terraform" / "providers"
    if not plugins.is_dir():
        subprocess.run(  # noqa: S603 — fixed arguments, no shell, no user input
            ["terraform", f"-chdir={LAYER}", "init", "-backend=false", "-input=false"],
            check=True,
            capture_output=True,
            text=True,
        )

    with tempfile.TemporaryDirectory() as raw:
        probe = Path(raw)
        (probe / "main.tf").write_text(
            "terraform {\n  required_providers {\n"
            '    aws   = { source = "hashicorp/aws" }\n'
            '    awscc = { source = "hashicorp/awscc" }\n'
            "  }\n}\n",
            encoding="utf-8",
        )
        subprocess.run(  # noqa: S603
            ["terraform", f"-chdir={probe}", "init", "-input=false", f"-plugin-dir={plugins}"],
            check=True,
            capture_output=True,
            text=True,
        )
        completed = subprocess.run(  # noqa: S603
            ["terraform", f"-chdir={probe}", "providers", "schema", "-json"],
            check=True,
            capture_output=True,
            text=True,
        )
    schema = json.loads(completed.stdout)
    return {
        name
        for body in schema.get("provider_schemas", {}).values()
        for name in body.get("resource_schemas", {})
    }


def main() -> int:
    main_tf = (LAYER / "main.tf").read_text(encoding="utf-8")
    problems: list[str] = []

    if 'resource "null_resource" "gateway_target"' not in main_tf:
        problems.append(
            "the gateway-target provisioner is gone. If it was replaced by a real resource, "
            "update CANDIDATES here; if it was simply deleted, every gateway is now serving "
            "an empty toolset and the deploy will look entirely successful."
        )

    if not (LAYER / "gateway-target.sh").is_file():
        problems.append("infra/agent/gateway-target.sh is missing; nothing attaches the tools")

    if not (LAYER / "tools.openapi.json").is_file():
        problems.append("infra/agent/tools.openapi.json is missing; run `attestor gateway spec`")

    available = sorted(set(CANDIDATES) & provider_resources())
    if available:
        problems.append(
            f"{', '.join(available)} now exists in an installed provider. Replace the "
            "null_resource + gateway-target.sh with it and delete both — a provisioner that "
            "shells out is only defensible while there is no resource that does the job."
        )

    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
    if problems:
        return 1
    print("gateway target: still provider-gapped, still wired")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
