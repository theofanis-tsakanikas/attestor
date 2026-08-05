#!/usr/bin/env bash
# `dbt build`, not `dbt run` then `dbt test`.
#
# `build` interleaves them, so a model whose data contract fails does not get a downstream
# model built on top of it. Running them separately means the failure is discovered after
# everything that depends on the bad data already exists.
set -euo pipefail

export ATTESTOR_LAKE_S3="s3://$(terraform -chdir=infra/foundation output -raw lake_bucket)"
export ATTESTOR_ATHENA_OUTPUT="${ATTESTOR_LAKE_S3}/athena-results/"
export ATTESTOR_WORKGROUP="${ATTESTOR_WORKGROUP:-attestor}"

pip install --quiet "dbt-athena-community>=1.9" "dbt-core>=1.9"

cd pipelines/dbt
export DBT_PROFILES_DIR="$PWD"
dbt deps --quiet || true
dbt build --fail-fast
