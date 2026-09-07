"""EventBridge event parsing.

CloudFormation sends stack events to the default bus with no setup required. Only
terminal statuses are acted on: mid-deployment the resource list is a partial,
shifting picture, so reading it would produce numbers that change on retry (S6).

The event is treated purely as a signal that something finished. Nothing is
inferred from event ordering, because delivery is guaranteed but ordering is not
(S10) — authoritative state is always re-read from the CloudFormation APIs.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from state import DeltaAction

#: Terminal statuses and the action each represents.
#:
#: ``ROLLBACK_COMPLETE`` follows a failed creation, where resources were rolled
#: back; ``UPDATE_ROLLBACK_COMPLETE`` follows a failed update, where the previous
#: state was restored. Both should net to roughly zero.
TERMINAL_STATUSES: Mapping[str, DeltaAction] = {
    "CREATE_COMPLETE": DeltaAction.CREATE,
    "UPDATE_COMPLETE": DeltaAction.UPDATE,
    "DELETE_COMPLETE": DeltaAction.DELETE,
    "UPDATE_ROLLBACK_COMPLETE": DeltaAction.ROLLBACK,
    "ROLLBACK_COMPLETE": DeltaAction.ROLLBACK,
}

STACK_STATUS_CHANGE = "CloudFormation Stack Status Change"


@dataclass(frozen=True)
class StackEvent:
    """An actionable CloudFormation stack event."""

    stack_id: str
    stack_name: str
    status: str
    action: DeltaAction
    account: str
    region: str
    client_request_token: str | None = None
    event_id: str | None = None
    event_time: str | None = None

    @property
    def dedupe_key(self) -> str:
        """Identifies one stack operation.

        Every event from a single deployment carries the same client request
        token, so this collapses dozens of events into one report (S9). Without a
        token the event ID is used, which dedupes nothing but at least never
        collides.
        """
        if self.client_request_token:
            return f"{self.stack_id}#{self.client_request_token}"
        return f"{self.stack_id}#{self.event_id or self.event_time or 'unknown'}"

    @property
    def is_delete(self) -> bool:
        return self.action is DeltaAction.DELETE


def stack_name_from_id(stack_id: str) -> str:
    """Extract the stack name from a stack ARN.

    ``arn:aws:cloudformation:us-east-1:111122223333:stack/my-stack/abc-123``
    """
    if ":" not in stack_id:
        return stack_id
    resource = stack_id.split(":")[-1]
    segments = resource.split("/")
    return segments[1] if len(segments) > 1 else resource


class NotActionable(Exception):
    """The event is well-formed but should not produce a report."""


def parse_stack_event(event: Mapping[str, Any]) -> StackEvent:
    """Parse an EventBridge event into a :class:`StackEvent`.

    Raises:
        NotActionable: The event is not a terminal CloudFormation stack status
            change, or is missing the fields needed to act on it. Raised rather
            than returned so a caller cannot accidentally treat a filtered event
            as a successful parse.
    """
    if not isinstance(event, Mapping):
        raise NotActionable("Event is not a mapping")

    detail_type = event.get("detail-type")
    if detail_type != STACK_STATUS_CHANGE:
        raise NotActionable(f"Unsupported detail-type: {detail_type!r}")

    if event.get("source") != "aws.cloudformation":
        raise NotActionable(f"Unexpected source: {event.get('source')!r}")

    detail = event.get("detail")
    if not isinstance(detail, Mapping):
        raise NotActionable("Event has no detail block")

    stack_id = detail.get("stack-id")
    if not isinstance(stack_id, str) or not stack_id:
        raise NotActionable("Event has no stack-id")

    status_details = detail.get("status-details")
    status = (
        status_details.get("status") if isinstance(status_details, Mapping) else None
    )
    if not isinstance(status, str):
        raise NotActionable("Event has no status")

    action = TERMINAL_STATUSES.get(status)
    if action is None:
        # In-progress, failed, and cleanup states are filtered at the rule, but
        # the guard is repeated here so a misconfigured rule cannot cause a
        # mid-deployment read.
        raise NotActionable(f"Not a terminal status: {status}")

    return StackEvent(
        stack_id=stack_id,
        stack_name=stack_name_from_id(stack_id),
        status=status,
        action=action,
        account=str(event.get("account") or ""),
        region=str(event.get("region") or ""),
        client_request_token=detail.get("client-request-token"),
        event_id=event.get("id"),
        event_time=event.get("time"),
    )
