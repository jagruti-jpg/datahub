"""Durable record of every verdict the classifier has reached.

The interactive flow can hold a proposal in memory for the minute a reviewer spends
reading it. The automated flow cannot: its verdicts come out of an ingestion event with
nobody watching, and the decisions taken on them have to outlive the process. Since that
flow writes without asking, the ledger is also the only thing that can undo it — `applied`
rows are what `revert` reads to tell a machine-written tag from a human's.
"""
from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

from pydantic import BaseModel

from db import get_db_connection
from pii_models import Verdict

logger = logging.getLogger("pii_store")

TABLE = "pii_verdicts"

# Statuses a fresh classification is allowed to overwrite. `applied` and `reverted` are
# excluded: re-recording over them would lose the record of a tag that exists, or of a
# deliberate undo. `failed` stays open so the next run retries it.
OPEN_STATUSES = frozenset({"would_apply", "skipped", "failed"})

LEGAL_TRANSITIONS: Dict[str, frozenset] = {
    "applied": frozenset({"reverted"}),
}


class TransitionError(RuntimeError):
    pass


class StoredVerdict(BaseModel):
    id: str
    dataset_urn: str
    dataset_name: str
    field_path: str
    label: str
    confidence: float
    source: str
    reason: str = ""
    tier: str
    status: str
    schema_fingerprint: str
    created_by: str = ""
    created_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    resolved_by: Optional[str] = None


class VerdictRecord(BaseModel):
    """A verdict together with the policy decision taken on it, ready to persist."""

    verdict: Verdict
    tier: str
    status: str


class RecordResult(BaseModel):
    inserted: int = 0
    updated: int = 0
    left_resolved: int = 0


@contextmanager
def _cursor(*, commit: bool = False) -> Iterator:
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        yield cursor
        if commit:
            conn.commit()
    finally:
        cursor.close()
        conn.close()


_INSERT = f"""
    INSERT INTO {TABLE}
        (id, dataset_urn, dataset_name, field_path, label, confidence,
         source, reason, tier, status, schema_fingerprint, created_by)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

# The status guard is repeated here rather than trusted from the SELECT that chose this
# row, so a resolve landing between the two cannot be overwritten.
_UPDATE = f"""
    UPDATE {TABLE}
       SET confidence = %s, source = %s, reason = %s, tier = %s, status = %s,
           schema_fingerprint = %s, created_by = %s, created_at = CURRENT_TIMESTAMP
     WHERE id = %s
       AND status IN ({", ".join(["%s"] * len(OPEN_STATUSES))})
"""


def record_many(
    records: Sequence[VerdictRecord],
    *,
    dataset_urn: str,
    dataset_name: str,
    schema_fingerprint: str,
    created_by: str,
) -> RecordResult:
    """Persist one classification pass, updating prior open rows instead of duplicating.

    Deduplication matters even though already-tagged columns are skipped upstream: that
    check keys off the tag, so it covers `applied` verdicts but not the ones that never
    got written. A dataset re-ingested repeatedly would otherwise accumulate a row per run.
    """
    if not records:
        return RecordResult()

    open_statuses = sorted(OPEN_STATUSES)

    with _cursor(commit=True) as cursor:
        # Served by idx_field (dataset_urn, field_path, label). Reading the dataset's
        # rows in one go beats a lookup per verdict and keeps the whole pass in one
        # transaction with the writes below.
        cursor.execute(
            f"SELECT id, field_path, label, status FROM {TABLE} WHERE dataset_urn = %s",
            (dataset_urn,),
        )
        existing = {(r["field_path"], r["label"]): r for r in cursor.fetchall()}

        inserts: List[tuple] = []
        updates: List[tuple] = []
        left_resolved = 0

        for record in records:
            v = record.verdict
            prior = existing.get((v.field, v.label))
            if prior is None:
                inserts.append(
                    (
                        str(uuid.uuid4()),
                        dataset_urn,
                        dataset_name,
                        v.field,
                        v.label,
                        v.confidence,
                        v.source.value,
                        v.reason,
                        record.tier,
                        record.status,
                        schema_fingerprint,
                        created_by,
                    )
                )
            elif prior["status"] in OPEN_STATUSES:
                updates.append(
                    (
                        v.confidence,
                        v.source.value,
                        v.reason,
                        record.tier,
                        record.status,
                        schema_fingerprint,
                        created_by,
                        prior["id"],
                        *open_statuses,
                    )
                )
            else:
                left_resolved += 1

        if inserts:
            cursor.executemany(_INSERT, inserts)
        if updates:
            cursor.executemany(_UPDATE, updates)

    if left_resolved:
        logger.info(
            "Left %d already-resolved verdict(s) on %s untouched",
            left_resolved,
            dataset_name,
        )
    return RecordResult(
        inserted=len(inserts), updated=len(updates), left_resolved=left_resolved
    )


def mark_failed(dataset_urn: str, pairs: Sequence[Tuple[str, str]]) -> int:
    """Downgrade `applied` rows whose tag never reached DataHub.

    Verdicts are recorded as applied before the write, so that a crash between the two
    leaves a row to investigate rather than a tag nobody knows about. This is the other
    half of that bargain: once the write returns, anything it did not land is corrected.
    """
    if not pairs:
        return 0

    with _cursor(commit=True) as cursor:
        cursor.executemany(
            f"UPDATE {TABLE} SET status = 'failed' "
            f"WHERE dataset_urn = %s AND field_path = %s AND label = %s "
            f"AND status = 'applied'",
            [(dataset_urn, field, label) for field, label in pairs],
        )
        return cursor.rowcount


def applied(dataset_urn: str) -> List[StoredVerdict]:
    """Verdicts whose tags are live in DataHub — exactly what a revert has to undo.

    The ledger is the source of truth here rather than the tags themselves: reading the
    aspect would find human-applied tags too, and a revert must not touch those.
    """
    with _cursor() as cursor:
        cursor.execute(
            f"SELECT * FROM {TABLE} WHERE dataset_urn = %s AND status = 'applied' "
            f"ORDER BY field_path",
            (dataset_urn,),
        )
        return [StoredVerdict(**row) for row in cursor.fetchall()]


def resolve(verdict_id: str, *, status: str, resolved_by: str) -> StoredVerdict:
    """Move a verdict to a terminal state, refusing transitions that make no sense.

    Guarding here rather than at the caller keeps the rule in one place: a double-click
    in the review panel and a retrying action both arrive as the same illegal move.
    """
    with _cursor(commit=True) as cursor:
        cursor.execute(f"SELECT * FROM {TABLE} WHERE id = %s FOR UPDATE", (verdict_id,))
        row = cursor.fetchone()
        if row is None:
            raise TransitionError(f"No verdict with id {verdict_id}")

        allowed = LEGAL_TRANSITIONS.get(row["status"], frozenset())
        if status not in allowed:
            raise TransitionError(
                f"Verdict {verdict_id} is {row['status']}; it cannot become {status}"
            )

        cursor.execute(
            f"UPDATE {TABLE} SET status = %s, resolved_at = CURRENT_TIMESTAMP, "
            f"resolved_by = %s WHERE id = %s",
            (status, resolved_by, verdict_id),
        )
        cursor.execute(f"SELECT * FROM {TABLE} WHERE id = %s", (verdict_id,))
        return StoredVerdict(**cursor.fetchone())
