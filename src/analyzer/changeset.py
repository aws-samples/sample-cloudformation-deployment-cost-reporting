"""Pre-deploy estimates from change sets (Path B, S7 and S8).

CloudFormation emits no native change set event, so this path is driven by
CloudTrail's record of the ``CreateChangeSet`` API call.

The estimate is produced the same way a confirmed report is — resolve the
template, price it, diff against the stored snapshot — with one critical
difference: **the snapshot is never persisted.** A change set is a proposal. Storing
it would make the subsequent confirmed report diff against something that was
never deployed, and if the change set were abandoned the stored state would be
permanently wrong.

Timing needs care. ``CreateChangeSet`` returns before the change set exists, so
the CloudTrail event routinely arrives while it is still ``CREATE_PENDING``.
Rather than sleeping inside the Lambda, that case is reported as retryable and
SQS does the waiting.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .events import NotActionable

CLOUDTRAIL_DETAIL_TYPE = "AWS API Call via CloudTrail"
CHANGE_SET_EVENT_NAMES = frozenset({"CreateChangeSet"})


@dataclass(frozen=True)
class ChangeSetEvent:
    """A CloudTrail record of a change set being created."""

    change_set_id: str
    stack_id: str
    account: str
    region: str
    event_id: str | None = None
    event_time: str | None = None

    @property
    def dedupe_key(self) -> str:
        """One estimate per change set.

        Keyed on the change set rather than the stack, so creating two change
        sets for one stack produces two estimates — which is correct, they are
        different proposals.
        """
        return f"estimate#{self.change_set_id}"


def parse_change_set_event(event: Mapping[str, Any]) -> ChangeSetEvent:
    """Parse a CloudTrail EventBridge event.

    Raises:
        NotActionable: The event is not a usable ``CreateChangeSet`` record.
            Raised rather than returned so a filtered event cannot be mistaken
            for a successful parse.
    """
    if not isinstance(event, Mapping):
        raise NotActionable("Event is not a mapping")

    if event.get("detail-type") != CLOUDTRAIL_DETAIL_TYPE:
        raise NotActionable(f"Unsupported detail-type: {event.get('detail-type')!r}")

    detail = event.get("detail")
    if not isinstance(detail, Mapping):
        raise NotActionable("Event has no detail block")

    if detail.get("eventSource") != "cloudformation.amazonaws.com":
        raise NotActionable(f"Unexpected eventSource: {detail.get('eventSource')!r}")

    event_name = detail.get("eventName")
    if event_name not in CHANGE_SET_EVENT_NAMES:
        raise NotActionable(f"Not a change set creation: {event_name!r}")

    # A failed API call has an errorCode and produced nothing to price.
    if detail.get("errorCode"):
        raise NotActionable(f"API call failed: {detail.get('errorCode')}")

    response = detail.get("responseElements")
    if not isinstance(response, Mapping):
        raise NotActionable("Event has no responseElements")

    change_set_id = response.get("id")
    stack_id = response.get("stackId")

    if not isinstance(change_set_id, str) or not change_set_id:
        raise NotActionable("Event has no change set ID")
    if not isinstance(stack_id, str) or not stack_id:
        raise NotActionable("Event has no stack ID")

    account = str(
        (detail.get("userIdentity") or {}).get("accountId")
        or event.get("account")
        or ""
    )

    return ChangeSetEvent(
        change_set_id=change_set_id,
        stack_id=stack_id,
        account=account,
        region=str(detail.get("awsRegion") or event.get("region") or ""),
        event_id=event.get("id"),
        event_time=event.get("time"),
    )


class ChangeSetNotReady(Exception):
    """The change set is still being computed.

    Signals that the message should be returned to the queue rather than treated
    as a failure. SQS's visibility timeout provides the wait, which keeps the
    Lambda from billing for sleep.
    """
