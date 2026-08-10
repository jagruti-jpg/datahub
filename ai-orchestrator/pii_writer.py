"""Writes column tags in a single request.

The tags live in the `editableSchemaMetadata` aspect, which the OpenAPI v3 endpoint
replaces wholesale. So this reads it, merges, and writes once. That is both faster than
per-column calls and the only way to be correct: DataHub's per-column write paths each
read-modify-write the same aspect, so a batch of them races and the last write wins,
which is how a run that reported seven tagged columns once left tags on one.

Merging is additive. A steward's own tags, descriptions, and glossary terms on these
fields are preserved, because the aspect carries them too and a replace would drop them.
"""
from __future__ import annotations

import logging
import os
import urllib.parse

import httpx
from pydantic import BaseModel

from pii_taxonomy import PROVENANCE_TAG, is_label_tag, tag_urn

logger = logging.getLogger("pii_writer")

GMS_URL = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080").rstrip("/")
GMS_TOKEN = os.environ.get("DATAHUB_GMS_TOKEN", "")
TIMEOUT = float(os.environ.get("PII_WRITE_TIMEOUT", "20"))

_ASPECT = "editableschemametadata"


class WriteError(RuntimeError):
    pass


class WriteResult(BaseModel):
    written: list[str] = []
    unchanged: list[str] = []


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if GMS_TOKEN:
        headers["Authorization"] = f"Bearer {GMS_TOKEN}"
    return headers


def _aspect_url(dataset_urn: str) -> str:
    encoded = urllib.parse.quote(dataset_urn, safe="")
    return f"{GMS_URL}/openapi/v3/entity/dataset/{encoded}/{_ASPECT}"


def merge_field_tags(
    aspect: dict, tags_by_field: dict[str, list[str]]
) -> tuple[dict, WriteResult]:
    """Add tags to the aspect without disturbing anything already there.

    Pure, so the merge can be tested without a GMS.
    """
    entries = list(aspect.get("editableSchemaFieldInfo") or [])
    by_path = {entry.get("fieldPath"): entry for entry in entries}
    result = WriteResult()

    for field_path, tag_names in tags_by_field.items():
        entry = by_path.get(field_path)
        if entry is None:
            entry = {"fieldPath": field_path}
            entries.append(entry)
            by_path[field_path] = entry

        global_tags = entry.setdefault("globalTags", {})
        existing = list(global_tags.get("tags") or [])
        present = {item.get("tag") for item in existing}

        added = False
        for name in tag_names:
            urn = tag_urn(name)
            if urn in present:
                continue
            existing.append({"tag": urn})
            present.add(urn)
            added = True

        global_tags["tags"] = existing
        (result.written if added else result.unchanged).append(field_path)

    return {"editableSchemaFieldInfo": entries}, result


async def apply_field_tags(
    dataset_urn: str, tags_by_field: dict[str, list[str]]
) -> WriteResult:
    """Read the aspect, merge every column's tags, write once."""
    if not tags_by_field:
        return WriteResult()

    url = _aspect_url(dataset_urn)
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.get(url, headers=_headers())
        if response.status_code == 404:
            aspect: dict = {}
        elif response.status_code >= 400:
            raise WriteError(
                f"Reading {_ASPECT} for {dataset_urn} returned "
                f"{response.status_code}: {response.text[:200]}"
            )
        else:
            aspect = (response.json() or {}).get("value") or {}

        merged, result = merge_field_tags(aspect, tags_by_field)
        if not result.written:
            logger.info("No tag changes needed for %s", dataset_urn)
            return result

        written = await client.post(
            url,
            headers=_headers(),
            params={"createIfNotExists": "false"},
            json={"value": merged},
        )
        if written.status_code >= 400:
            raise WriteError(
                f"Writing {_ASPECT} for {dataset_urn} returned "
                f"{written.status_code}: {written.text[:200]}"
            )

    logger.info(
        "Tagged %d column(s) on %s in one write (%d already current)",
        len(result.written),
        dataset_urn,
        len(result.unchanged),
    )
    return result


def strip_field_tags(
    aspect: dict, tags_by_field: dict[str, list[str]]
) -> tuple[dict, WriteResult]:
    """Remove exactly the named tags, leaving everything else on the field alone.

    Pure, like `merge_field_tags`, so the subtraction can be tested without a GMS. This is
    the inverse of an additive merge and has to be equally conservative: a steward's own
    tags, descriptions and glossary terms live in the same aspect, and a revert that
    replaced the aspect wholesale would take them with it.
    """
    entries = list(aspect.get("editableSchemaFieldInfo") or [])
    result = WriteResult()
    kept: list[dict] = []

    for entry in entries:
        targets = tags_by_field.get(entry.get("fieldPath"))
        if targets is None:
            kept.append(entry)
            continue

        global_tags = entry.get("globalTags") or {}
        existing = list(global_tags.get("tags") or [])

        doomed = {tag_urn(name) for name in targets if name != PROVENANCE_TAG}
        remaining = [item for item in existing if item.get("tag") not in doomed]

        # Provenance goes only when the last AI-applied label on this column goes with it.
        # Removing it while another label remains would leave a machine-written tag that
        # nothing downstream can recognise as machine-written.
        if PROVENANCE_TAG in targets and not any(
            is_label_tag(item.get("tag", "")) for item in remaining
        ):
            provenance = tag_urn(PROVENANCE_TAG)
            remaining = [item for item in remaining if item.get("tag") != provenance]

        if len(remaining) == len(existing):
            result.unchanged.append(entry["fieldPath"])
            kept.append(entry)
            continue

        result.written.append(entry["fieldPath"])
        updated = {**entry, "globalTags": {**global_tags, "tags": remaining}}

        # Drop an entry the revert has emptied, so undoing a write leaves the aspect as it
        # was rather than littered with fieldPaths carrying nothing.
        if not remaining and set(updated) == {"fieldPath", "globalTags"}:
            continue
        kept.append(updated)

    return {"editableSchemaFieldInfo": kept}, result


async def remove_field_tags(
    dataset_urn: str, tags_by_field: dict[str, list[str]]
) -> WriteResult:
    """Read the aspect, subtract every column's tags, write once."""
    if not tags_by_field:
        return WriteResult()

    url = _aspect_url(dataset_urn)
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.get(url, headers=_headers())
        # Nothing to undo if the aspect was never written. Not an error: a revert of an
        # already-reverted dataset should be a no-op, not a failure.
        if response.status_code == 404:
            logger.info("No %s on %s; nothing to remove", _ASPECT, dataset_urn)
            return WriteResult()
        if response.status_code >= 400:
            raise WriteError(
                f"Reading {_ASPECT} for {dataset_urn} returned "
                f"{response.status_code}: {response.text[:200]}"
            )

        aspect = (response.json() or {}).get("value") or {}
        stripped, result = strip_field_tags(aspect, tags_by_field)
        if not result.written:
            logger.info("No tags to remove from %s", dataset_urn)
            return result

        written = await client.post(
            url,
            headers=_headers(),
            params={"createIfNotExists": "false"},
            json={"value": stripped},
        )
        if written.status_code >= 400:
            raise WriteError(
                f"Writing {_ASPECT} for {dataset_urn} returned "
                f"{written.status_code}: {written.text[:200]}"
            )

    logger.info(
        "Removed tags from %d column(s) on %s in one write",
        len(result.written),
        dataset_urn,
    )
    return result


def tags_for(label: str) -> list[str]:
    """The label plus the provenance tag, so every machine write stays identifiable."""
    return [label, PROVENANCE_TAG]
