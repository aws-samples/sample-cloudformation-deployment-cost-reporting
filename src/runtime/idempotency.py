"""Deduplication.

Two independent sources of duplicates:

* **CloudFormation.** One deployment emits dozens of stack events, all carrying
  the same ``client-request-token`` (S9).
* **SQS.** Delivery is at-least-once, so the same message can arrive twice even
  after a successful invocation.

Both are handled by claiming a key before doing the work. The claim is a
conditional write, so two concurrent Lambdas cannot both win.

On failure the claim is **released**, so an SQS retry can proceed. Claiming
without releasing would turn a transient throttle into a permanently missing
report — and a missing report is worse than a duplicate one, because nothing
signals its absence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any, Protocol

from .logs import get_logger

logger = get_logger(__name__)


class ClaimResult(str, Enum):
    """Outcome of trying to own one event."""

    ACQUIRED = "ACQUIRED"
    COMPLETE = "COMPLETE"
    BUSY = "BUSY"


class IdempotencyStore(Protocol):
    """Coordinates processing and completed-event deduplication."""

    def claim(self, key: str) -> ClaimResult:
        """Acquire work, or report that it is busy/already complete."""
        ...

    def complete(self, key: str) -> None:
        """Mark successfully delivered work as complete."""
        ...

    def release(self, key: str) -> None:
        """Release in-progress work so a retry can acquire it."""
        ...


@dataclass
class InMemoryIdempotencyStore:
    """Non-persistent processing/completion state for tests and local runs."""

    claimed: set[str] = field(default_factory=set)
    completed: set[str] = field(default_factory=set)

    def claim(self, key: str) -> ClaimResult:
        if key in self.completed:
            return ClaimResult.COMPLETE
        if key in self.claimed:
            return ClaimResult.BUSY
        self.claimed.add(key)
        return ClaimResult.ACQUIRED

    def complete(self, key: str) -> None:
        self.claimed.discard(key)
        self.completed.add(key)

    def release(self, key: str) -> None:
        self.claimed.discard(key)

    def __bool__(self) -> bool:
        # An empty store is a usable store; without this, `store or fallback`
        # would silently swap it out once __len__-like truthiness is assumed.
        return True


class DynamoIdempotencyStore:
    """DynamoDB-backed claim, using a conditional put.

    Args:
        table: A boto3 DynamoDB ``Table``, or anything with the same
            ``put_item`` / ``delete_item`` interface.
        ttl_hours: How long a claim is remembered. Only needs to outlive SQS's
            redrive window and CloudFormation's event burst, so a day is ample.
    """

    def __init__(
        self,
        table: Any,
        lease_seconds: int = 330,
        completion_ttl_hours: int = 24 * 15,
    ) -> None:
        self._table = table
        self._lease_seconds = lease_seconds
        self._completion_ttl_hours = completion_ttl_hours

    def claim(self, key: str) -> ClaimResult:
        now = int(datetime.now(UTC).timestamp())
        lease_until = now + self._lease_seconds
        expires_at = int(
            (datetime.now(UTC) + timedelta(hours=self._completion_ttl_hours)).timestamp()
        )
        try:
            self._table.put_item(
                Item={
                    "pk": key,
                    "status": "PROCESSING",
                    "claimedAt": datetime.now(UTC).isoformat(),
                    "leaseUntil": lease_until,
                    "expiresAt": expires_at,
                },
                ConditionExpression=(
                    "attribute_not_exists(pk) OR "
                    "(#status = :processing AND leaseUntil < :now)"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":processing": "PROCESSING",
                    ":now": now,
                },
            )
            return ClaimResult.ACQUIRED
        except Exception as exc:
            if _is_conditional_failure(exc):
                return self._existing_claim(key)

            # Any other error means the store is unavailable. Proceeding risks a
            # duplicate report; refusing risks losing it entirely. A duplicate is
            # the recoverable outcome, so the work goes ahead.
            logger.warning(
                "Idempotency store unavailable, proceeding without a claim",
                extra={"dedupeKey": key, "error": str(exc)},
            )
            return ClaimResult.ACQUIRED

    def _existing_claim(self, key: str) -> ClaimResult:
        try:
            response = self._table.get_item(Key={"pk": key}, ConsistentRead=True)
        except Exception as exc:
            logger.warning(
                "Could not inspect existing claim; retrying later",
                extra={"dedupeKey": key, "error": str(exc)},
            )
            return ClaimResult.BUSY

        item = response.get("Item") or {}
        status = item.get("status")
        # Rows written by older releases had no status and represented completed
        # work, so preserve their deduplication semantics during upgrades.
        if status in (None, "COMPLETED"):
            logger.info("Work already completed, skipping", extra={"dedupeKey": key})
            return ClaimResult.COMPLETE
        logger.info("Work is already in progress; retrying later", extra={"dedupeKey": key})
        return ClaimResult.BUSY

    def complete(self, key: str) -> None:
        expires_at = int(
            (datetime.now(UTC) + timedelta(hours=self._completion_ttl_hours)).timestamp()
        )
        try:
            self._table.update_item(
                Key={"pk": key},
                UpdateExpression=(
                    "SET #status = :completed, completedAt = :completedAt, "
                    "expiresAt = :expires REMOVE leaseUntil"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":completed": "COMPLETED",
                    ":completedAt": datetime.now(UTC).isoformat(),
                    ":expires": expires_at,
                },
            )
        except Exception as exc:
            # Report publication and state commit have already succeeded. Failing
            # the SQS item here would replay/recompute accepted work; retain the
            # processing row and let its lease/TTL handle a later duplicate.
            logger.error(
                "Could not mark delivered work complete; not replaying accepted report",
                extra={"dedupeKey": key, "error": str(exc)},
            )

    def release(self, key: str) -> None:
        try:
            self._table.delete_item(Key={"pk": key})
        except Exception as exc:
            logger.warning(
                "Could not release claim; a retry of this event will be skipped",
                extra={"dedupeKey": key, "error": str(exc)},
            )


def _is_conditional_failure(exc: Exception) -> bool:
    """True when DynamoDB rejected the put because the key already existed.

    Matched on the error code rather than the exception class so the store does
    not need botocore imported, which keeps it testable with a plain fake.
    """
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code")
        if code == "ConditionalCheckFailedException":
            return True
    return type(exc).__name__ == "ConditionalCheckFailedException"
