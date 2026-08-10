# Copyright 2021 Acryl Data, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
from typing import Optional

import requests
from pydantic import Field

from datahub.configuration import ConfigModel
from datahub.metadata.schema_classes import ChangeTypeClass, MetadataChangeLogClass
from datahub_actions.action.action import Action
from datahub_actions.event.event_envelope import EventEnvelope
from datahub_actions.event.event_registry import METADATA_CHANGE_LOG_EVENT_V1_TYPE
from datahub_actions.pipeline.pipeline_context import PipelineContext

logger = logging.getLogger(__name__)

TRIGGER_ENTITY_TYPE = "dataset"

# Triggering on schemaMetadata while the orchestrator writes editableSchemaMetadata is what
# keeps this from feeding itself. Same-aspect trigger and write is an infinite loop.
TRIGGER_ASPECT = "schemaMetadata"

# RESTATE is deliberately absent: it means the aspect was re-emitted unchanged, so there is
# no new column to classify. DELETE and PATCH are likewise not reclassification triggers.
TRIGGER_CHANGE_TYPES = frozenset(
    {
        ChangeTypeClass.UPSERT,
        ChangeTypeClass.CREATE,
        ChangeTypeClass.CREATE_ENTITY,
    }
)

AUTOTAG_PATH = "/api/pii/autotag"


class PiiAutoTagConfig(ConfigModel):
    orchestrator_url: str = Field(
        "http://host.docker.internal:8000",
        description=(
            "Base URL of the AI orchestrator. Defaults to the host gateway rather than "
            "localhost: the actions container and the orchestrator run in separate "
            "compose projects, so localhost resolves to the wrong container."
        ),
    )
    auth_token: Optional[str] = Field(
        None,
        description="Bearer token for the orchestrator's PII endpoints.",
    )
    dry_run: bool = Field(
        True,
        description=(
            "Classify and record verdicts without writing any tag. Defaults on so that "
            "deploying this action cannot start modifying metadata by omission — turning "
            "writes on has to be a deliberate edit."
        ),
    )
    rules_only: bool = Field(
        False,
        description=(
            "Skip the model and classify with deterministic rules only. Useful for a "
            "catalog-wide backfill, where per-dataset model spend is the binding constraint."
        ),
    )
    timeout: float = Field(
        30,
        description=(
            "Seconds to wait for the orchestrator. act() is synchronous in the actions "
            "framework, so an unbounded call would stall the whole pipeline behind one "
            "slow dataset."
        ),
    )


class PiiAutoTagAction(Action):
    """Ask the AI orchestrator to classify a dataset whenever its schema changes.

    The action itself holds no classification logic and writes no metadata. It exists to
    turn an ingestion event into one HTTP call, so that the same code path a human drives
    from the chat assistant also runs unattended.
    """

    def __init__(self, config: PiiAutoTagConfig, ctx: PipelineContext):
        self.config = config
        self.ctx = ctx
        self._url = config.orchestrator_url.rstrip("/") + AUTOTAG_PATH
        self._session = requests.Session()
        if config.auth_token:
            self._session.headers["Authorization"] = f"Bearer {config.auth_token}"

    @classmethod
    def create(cls, config_dict: dict, ctx: PipelineContext) -> "PiiAutoTagAction":
        config = PiiAutoTagConfig.model_validate(config_dict or {})
        logger.info(
            "PiiAutoTagAction posting to %s (dry_run=%s, rules_only=%s, timeout=%ss, "
            "authenticated=%s)",
            config.orchestrator_url,
            config.dry_run,
            config.rules_only,
            config.timeout,
            bool(config.auth_token),
        )
        return cls(config, ctx)

    def name(self) -> str:
        return "PiiAutoTag"

    def _dataset_urn(self, event: EventEnvelope) -> Optional[str]:
        """The dataset to classify, or None if this event is not ours.

        This repeats the pipeline's YAML filter on purpose. The filter is deployer-editable,
        and a widened one must not turn into orchestrator calls for entity types it cannot
        classify — the cost of a mistake there is model spend on every event in the stream.
        """
        if event.event_type != METADATA_CHANGE_LOG_EVENT_V1_TYPE:
            return None

        mcl = event.event
        if not isinstance(mcl, MetadataChangeLogClass):
            return None

        if mcl.entityType != TRIGGER_ENTITY_TYPE or mcl.aspectName != TRIGGER_ASPECT:
            return None

        if mcl.changeType not in TRIGGER_CHANGE_TYPES:
            logger.debug(
                "Ignoring %s on %s: %s is not a reclassification trigger",
                TRIGGER_ASPECT,
                mcl.entityUrn,
                mcl.changeType,
            )
            return None

        return mcl.entityUrn

    def act(self, event: EventEnvelope) -> None:
        dataset_urn = self._dataset_urn(event)
        if dataset_urn is None:
            return

        logger.info("Classifying %s", dataset_urn)
        # Transport failures propagate so the pipeline's retry_count applies. Retrying is
        # safe because classification skips columns that already carry a label, so a
        # duplicate delivery cannot double-write or double-charge for the same columns.
        response = self._session.post(
            self._url,
            json={
                "dataset_urn": dataset_urn,
                "dry_run": self.config.dry_run,
                "rules_only": self.config.rules_only,
                "created_by": self.name(),
            },
            timeout=self.config.timeout,
        )

        # A 4xx is a decision the orchestrator made about this dataset — no key configured,
        # entity not readable. Retrying cannot change it, so log and acknowledge instead of
        # blocking the partition behind an event that will never succeed.
        if 400 <= response.status_code < 500:
            logger.error(
                "Orchestrator refused %s with %s: %s",
                dataset_urn,
                response.status_code,
                response.text[:500],
            )
            return
        response.raise_for_status()

        result = response.json()
        counts = result.get("counts", {})
        recorded = result.get("recorded", {})
        logger.info(
            "Classified %s: %s auto, %s below floor; wrote_tags=%s, tagged=%s; "
            "ledger +%s new, %s refreshed",
            result.get("dataset", dataset_urn),
            counts.get("auto", 0),
            counts.get("weak", 0),
            result.get("wrote_tags"),
            len(result.get("written") or []),
            recorded.get("inserted", 0),
            recorded.get("updated", 0),
        )
        if result.get("write_failures"):
            logger.error(
                "Tags did not land on %s for %s",
                result["write_failures"],
                dataset_urn,
            )

    def close(self) -> None:
        self._session.close()
