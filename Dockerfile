# The AgentCore Runtime container. ARM64 because Runtime requires it.
#
# Deliberately boring: a slim base, no build tools in the final image, a non-root user, and
# the library installed as a package rather than copied as loose files. The interesting
# engineering in this project is not in here, and a container that tries to be interesting is
# a container with a CVE feed.

FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.12-slim AS build

WORKDIR /build
# LICENSE is here because `pyproject.toml` names it in `license = { file = ... }`, and
# hatchling reads that file while building metadata. Leaving it out failed the build with
# `OSError: License file does not exist` — several minutes in, on the runner, long after the
# point where a laptop could have told us.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
# `.[cloud]`, not `.`. The cloud clients are an optional extra on purpose — the whole suite,
# every eval and every gate run without them, which is what makes "offline is the default" true
# rather than aspirational. This image is the one place that is *only* ever online: it exists to
# talk to Athena, Bedrock and AgentCore Memory.
#
# Installed as `.` it started, answered `/ping`, served a tool call, and returned
# `E_RESOLVER_ERROR: ModuleNotFoundError: No module named 'boto3'`. Which is the resolver doing
# exactly the right thing with a broken dependency — abstaining rather than guessing — and a
# container that cannot reach anything is not much of an agent.
RUN pip install --no-cache-dir --target /install ".[cloud]"

FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.12-slim

# The declarative material the resolver reads at runtime: contracts, queries, prompts,
# templates, tenants, the override register, the Cedar policies and the evidence manifests.
# They are data, they are reviewed in pull requests, and they ship with the code that reads
# them so a container can never be running one version of a contract and another of its query.
WORKDIR /app
COPY --from=build /install /usr/local/lib/python3.12/site-packages
COPY contracts ./contracts
COPY queries ./queries
COPY prompts ./prompts
COPY templates ./templates
COPY tenants ./tenants
COPY overrides ./overrides
COPY policy ./policy
COPY evidence ./evidence

ENV ATTESTOR_ROOT=/app \
    PYTHONUNBUFFERED=1 \
    PORT=8080

RUN useradd --uid 10001 --create-home attestor && chown -R attestor:attestor /app
USER 10001

EXPOSE 8080
CMD ["python", "-m", "attestor.agent.server"]
