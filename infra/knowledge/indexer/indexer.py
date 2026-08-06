"""Create the vector indexes a Bedrock knowledge base expects to already exist.

Bedrock does not create them. `CreateKnowledgeBase` opens the collection, looks for the index
named in `storage_configuration`, and fails with `The knowledge base storage configuration
provided is invalid` — a message that names neither the index nor the fact that it is missing.
The console hides this by creating the index for you behind "quick create"; Terraform has no
such step, so this is it.

It runs inside the VPC because the collection is reachable only through its VPC endpoint, and
that is a control worth keeping: a data access policy decides *who*, and the network policy
decides *from where*. Creating the index from a CI runner would have meant opening the
collection to the internet for the duration of one PUT that happens once per estate.

No dependencies beyond what the Lambda runtime already ships. botocore signs, urllib sends.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

#: Two minutes of retries, ten seconds apart. A fresh collection takes about a minute to
#: accept writes; anything still refusing after that is refusing for a reason.
ATTEMPTS = 12
BACKOFF_SECONDS = 10


def mapping(vector_field: str, text_field: str, metadata_field: str, dimension: int) -> dict:
    """The shape Bedrock requires.

    A knn_vector for the embedding, a text field for the chunk, and a metadata field it
    stores JSON in and never searches — hence `index: false`, which keeps the analyzer off a
    blob that is not prose.

    `dimension` arrives in the event rather than in the environment: it has to move with the
    embedding model, and a mismatch is reported much later, as a failed ingestion job.
    """
    return {
        "settings": {"index": {"knn": True, "knn.algo_param.ef_search": 512}},
        "mappings": {
            "properties": {
                vector_field: {
                    "type": "knn_vector",
                    "dimension": dimension,
                    "method": {
                        "name": "hnsw",
                        "engine": "faiss",
                        "space_type": "l2",
                        "parameters": {"ef_construction": 512, "m": 16},
                    },
                },
                text_field: {"type": "text"},
                metadata_field: {"type": "text", "index": False},
            }
        },
    }


def _signed_put(endpoint: str, index: str, body: dict, region: str) -> tuple[int, str]:
    url = f"{endpoint.rstrip('/')}/{index}"
    payload = json.dumps(body).encode("utf-8")

    request = AWSRequest(
        method="PUT", url=url, data=payload, headers={"Content-Type": "application/json"}
    )
    credentials = boto3.Session().get_credentials().get_frozen_credentials()
    # `aoss`, not `es`. Signing against the wrong service name is a 403 that reads exactly
    # like a missing data access policy entry.
    SigV4Auth(credentials, "aoss", region).add_auth(request)

    sent = urllib.request.Request(  # noqa: S310 - the URL is the collection endpoint
        url, data=payload, headers=dict(request.headers), method="PUT"
    )
    try:
        with urllib.request.urlopen(sent, timeout=30) as response:  # noqa: S310
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8")


def handler(event, _context):
    endpoint = event["endpoint"]
    region = os.environ["AWS_REGION"]
    created, existed = [], []

    for index in event["indexes"]:
        name = index["name"]
        body = mapping(
            index["vector_field"],
            index["text_field"],
            index["metadata_field"],
            int(index["dimension"]),
        )

        # The collection reports ACTIVE before its data plane accepts writes, so the first
        # PUT of a fresh estate can 403 on a policy that is already correct. Retrying is the
        # documented behaviour, and it is bounded: a real authorization failure still fails,
        # it just takes a minute to say so.
        for attempt in range(ATTEMPTS):
            status, text = _signed_put(endpoint, name, body, region)
            if status in (200, 201):
                LOG.info("created index %s", name)
                created.append(name)
                break
            if "resource_already_exists_exception" in text:
                LOG.info("index %s already exists", name)
                existed.append(name)
                break
            if status in (403, 404, 409) and attempt < ATTEMPTS - 1:
                LOG.info("index %s not ready (%s), retrying", name, status)
                time.sleep(BACKOFF_SECONDS)
                continue
            raise RuntimeError(f"creating index {name}: HTTP {status} {text}")
        else:
            raise RuntimeError(f"creating index {name}: still unavailable after retries")

    # Newly created indexes are not immediately visible to CreateKnowledgeBase. Without this
    # the knowledge base fails with the same "storage configuration provided is invalid" the
    # missing index produced, which would send the next person looking in the wrong place.
    if created:
        time.sleep(45)

    return {"created": created, "existed": existed}
