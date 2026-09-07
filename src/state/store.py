"""State store.

Holds one snapshot per stack, keyed by stack ID. The stack ID is used rather than
the stack name because it carries a unique suffix — a stack that is deleted and
recreated with the same name gets a different ID, so it correctly starts from a
baseline instead of diffing against its predecessor.

A missing row means there is genuinely no history. DynamoDB errors and corrupt
snapshots raise :class:`StateStoreError`; treating those as "no history" would
silently convert a retryable failure into a misleading baseline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from .models import SnapshotTooLarge, StackSnapshot

# Standard library logging, not runtime.logs. `state` sits below `runtime` in the
# dependency order and importing upward would make the two circular. Nothing is
# lost: configure_logging() installs the JSON formatter on the *root* logger, so
# records from here are formatted identically and carry the same invocation
# context.
logger = logging.getLogger(__name__)


class StateStoreError(RuntimeError):
    """Snapshot state could not be read or committed safely."""


def _is_conditional_failure(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code")
        if code == "ConditionalCheckFailedException":
            return True
    return type(exc).__name__ == "ConditionalCheckFailedException"


class StateStore(Protocol):
    """Where snapshots live between deployments."""

    def load(self, stack_id: str) -> StackSnapshot | None:
        """Return the last snapshot, or None if the stack has no history."""
        ...

    def save(self, snapshot: StackSnapshot) -> None:
        ...

    def delete(self, stack_id: str, event_time: str | None = None) -> None:
        ...


@dataclass
class InMemoryStateStore:
    """Non-persistent store, for tests and local runs."""

    snapshots: dict[str, StackSnapshot] = field(default_factory=dict)

    def load(self, stack_id: str) -> StackSnapshot | None:
        return self.snapshots.get(stack_id)

    def save(self, snapshot: StackSnapshot) -> None:
        self.snapshots[snapshot.stack_id] = snapshot

    def delete(self, stack_id: str, event_time: str | None = None) -> None:
        self.snapshots.pop(stack_id, None)

    def __len__(self) -> int:
        return len(self.snapshots)

    def __bool__(self) -> bool:
        # An empty store is still a usable store. Without this, __len__ makes it
        # falsy and the `store or fallback` idiom silently replaces it.
        return True


class DynamoStateStore:
    """DynamoDB-backed store.

    Args:
        table: A boto3 DynamoDB ``Table`` resource, or anything with the same
            ``get_item`` / ``put_item`` / ``delete_item`` interface.
        retention_days: TTL applied to snapshots of deleted stacks so the table
            does not grow without bound. Set to 0 to hard-delete instead.
    """

    def __init__(self, table: Any, retention_days: int = 90) -> None:
        self._table = table
        self._retention_days = retention_days
        self.load_failures = 0
        self.oversized_snapshots = 0

    def load(self, stack_id: str) -> StackSnapshot | None:
        try:
            response = self._table.get_item(
                Key={"pk": stack_id}, ConsistentRead=True
            )
        except Exception as exc:
            self.load_failures += 1
            raise StateStoreError(f"Could not read snapshot for {stack_id}") from exc

        item = response.get("Item")
        if not item:
            return None

        try:
            return StackSnapshot.from_item(item)
        except Exception as exc:
            self.load_failures += 1
            raise StateStoreError(f"Snapshot for {stack_id} is unreadable") from exc

    def save(self, snapshot: StackSnapshot) -> None:
        """Persist a snapshot, or discard history if it cannot be persisted.

        A snapshot too large to store leaves the previously stored one in place,
        and that stored snapshot is now *stale* — it describes the deployment
        before last. Diffing the next deployment against it would report a delta
        spanning two deployments as though it were one, which is a wrong number
        rather than a missing one.

        So the old snapshot is deleted instead. The next report finds no history
        and publishes a baseline, which is honest about not knowing. A baseline is
        recoverable; a plausible-looking wrong delta is not, because nothing about
        it invites a second look.
        """
        try:
            item = snapshot.to_item()
        except SnapshotTooLarge as reason:
            self.oversized_snapshots += 1
            logger.error(
                "Snapshot too large to store; discarding history so the next "
                "report is a baseline rather than a stale delta",
                extra={
                    "stackId": snapshot.stack_id,
                    "stackName": snapshot.stack_name,
                    "resourceCount": len(snapshot.resources),
                    "reason": str(reason),
                },
            )
            # Hard delete, not the retention path: this snapshot is not being
            # retired for a deleted stack, it is being invalidated. Failure must
            # propagate; silently keeping stale history would corrupt the next
            # deployment delta.
            try:
                self._table.delete_item(Key={"pk": snapshot.stack_id})
            except Exception as exc:
                raise StateStoreError(
                    f"Could not invalidate oversized snapshot for {snapshot.stack_id}"
                ) from exc
            return

        if snapshot.event_time:
            try:
                self._table.put_item(
                    Item=item,
                    ConditionExpression=(
                        "attribute_not_exists(pk) OR attribute_not_exists(#eventTime) "
                        "OR #eventTime <= :eventTime"
                    ),
                    ExpressionAttributeNames={"#eventTime": "eventTime"},
                    ExpressionAttributeValues={":eventTime": snapshot.event_time},
                )
            except Exception as exc:
                if _is_conditional_failure(exc):
                    logger.warning(
                        "Newer snapshot already committed; stale state write ignored",
                        extra={"stackId": snapshot.stack_id, "eventTime": snapshot.event_time},
                    )
                    return
                # A timeout can occur after DynamoDB committed the write. Verify
                # strongly before asking SQS to replay an already-published report.
                try:
                    response = self._table.get_item(
                        Key={"pk": snapshot.stack_id}, ConsistentRead=True
                    )
                    stored = response.get("Item") or {}
                    if (
                        stored.get("eventTime") == snapshot.event_time
                        and stored.get("clientRequestToken")
                        == snapshot.client_request_token
                    ):
                        return
                except Exception as verify_exc:
                    logger.warning(
                        "Could not verify ambiguous snapshot write",
                        extra={"stackId": snapshot.stack_id, "error": str(verify_exc)},
                    )
                raise StateStoreError(
                    f"Could not commit snapshot for {snapshot.stack_id}"
                ) from exc
            return

        self._table.put_item(Item=item)

    def delete(self, stack_id: str, event_time: str | None = None) -> None:
        """Retire a snapshot without allowing an older deletion to win."""
        if self._retention_days <= 0:
            request: dict[str, Any] = {"Key": {"pk": stack_id}}
            if event_time:
                request.update(
                    ConditionExpression=(
                        "attribute_not_exists(#eventTime) OR #eventTime <= :eventTime"
                    ),
                    ExpressionAttributeNames={"#eventTime": "eventTime"},
                    ExpressionAttributeValues={":eventTime": event_time},
                )
            try:
                self._table.delete_item(**request)
            except Exception as exc:
                if _is_conditional_failure(exc):
                    return
                raise StateStoreError(f"Could not delete snapshot for {stack_id}") from exc
            return

        expires_at = datetime.now(UTC) + timedelta(days=self._retention_days)
        values: dict[str, Any] = {
            ":expires": int(expires_at.timestamp()),
            ":retired": True,
        }
        names: dict[str, str] = {}
        update = "SET expiresAt = :expires, retired = :retired"
        request = {
            "Key": {"pk": stack_id},
            "UpdateExpression": update,
            "ExpressionAttributeValues": values,
        }
        if event_time:
            values[":eventTime"] = event_time
            names["#eventTime"] = "eventTime"
            request["UpdateExpression"] = f"{update}, #eventTime = :eventTime"
            request["ExpressionAttributeNames"] = names
            request["ConditionExpression"] = (
                "attribute_not_exists(#eventTime) OR #eventTime <= :eventTime"
            )

        try:
            self._table.update_item(**request)
        except Exception as exc:
            if _is_conditional_failure(exc):
                logger.warning(
                    "Newer snapshot already committed; stale retirement ignored",
                    extra={"stackId": stack_id, "eventTime": event_time},
                )
                return
            raise StateStoreError(f"Could not retire snapshot for {stack_id}") from exc
