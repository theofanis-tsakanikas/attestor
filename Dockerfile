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
RUN pip install --no-cache-dir --target /install .

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
