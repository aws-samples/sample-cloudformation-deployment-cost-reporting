"""Tests for change set event parsing (Path B).

Path B is driven by CloudTrail, which carries a much looser contract than a native
stack event: the record may describe a *failed* API call, may be missing the
response entirely, and arrives while the change set is often still being computed.
Each of those has to be rejected as not-actionable rather than half-parsed into an
estimate for a change set that does not exist.
"""

from __future__ import annotations

from typing import Any

import pytest

from analyzer.changeset import (
    CHANGE_SET_EVENT_NAMES,
    CLOUDTRAIL_DETAIL_TYPE,
    ChangeSetEvent,
    ChangeSetNotReady,
    parse_change_set_event,
)
from analyzer.events import NotActionable

CHANGE_SET_ID = (
    "arn:aws:cloudformation:us-east-1:111122223333:changeSet/deploy-42/cs-abc"
)
STACK_ID = "arn:aws:cloudformation:us-east-1:111122223333:stack/payments-api-prod/abc-123"


def make_event(**detail_overrides: Any) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "eventSource": "cloudformation.amazonaws.com",
        "eventName": "CreateChangeSet",
        "awsRegion": "us-east-1",
        "userIdentity": {"accountId": "111122223333"},
        "responseElements": {"id": CHANGE_SET_ID, "stackId": STACK_ID},
    }
    detail.update(detail_overrides)
    return {
        "id": "6a7e8feb-b491-4cf7-a9f1-bf3703467718",
        "detail-type": CLOUDTRAIL_DETAIL_TYPE,
        "source": "aws.cloudformation",
        "account": "111122223333",
        "region": "us-east-1",
        "time": "2026-08-10T17:06:18Z",
        "detail": detail,
    }


# -- happy path -----------------------------------------------------------


def test_a_create_change_set_event_is_parsed() -> None:
    event = parse_change_set_event(make_event())

    assert event.change_set_id == CHANGE_SET_ID
    assert event.stack_id == STACK_ID
    assert event.account == "111122223333"
    assert event.region == "us-east-1"
    assert event.event_id == "6a7e8feb-b491-4cf7-a9f1-bf3703467718"
    assert event.event_time == "2026-08-10T17:06:18Z"


def test_the_account_falls_back_to_the_envelope() -> None:
    """userIdentity is not guaranteed to carry an accountId for every principal
    type, but the EventBridge envelope always does."""
    event = parse_change_set_event(make_event(userIdentity={}))

    assert event.account == "111122223333"


def test_the_region_falls_back_to_the_envelope() -> None:
    detail_event = make_event()
    del detail_event["detail"]["awsRegion"]

    assert parse_change_set_event(detail_event).region == "us-east-1"


# -- dedupe ---------------------------------------------------------------


def test_the_dedupe_key_is_the_change_set_not_the_stack() -> None:
    """Two change sets on one stack are two different proposals, and each deserves
    its own estimate."""
    first = parse_change_set_event(make_event())
    second = parse_change_set_event(
        make_event(responseElements={"id": CHANGE_SET_ID + "-2", "stackId": STACK_ID})
    )

    assert first.dedupe_key != second.dedupe_key
    assert first.dedupe_key == f"estimate#{CHANGE_SET_ID}"


def test_the_estimate_key_cannot_collide_with_a_stack_event_key() -> None:
    """Both paths share one idempotency table. A collision would mean the
    confirmed report is skipped because the estimate already claimed the key."""
    assert parse_change_set_event(make_event()).dedupe_key.startswith("estimate#")


def test_the_same_event_twice_produces_the_same_key() -> None:
    assert (
        parse_change_set_event(make_event()).dedupe_key
        == parse_change_set_event(make_event()).dedupe_key
    )


# -- rejection ------------------------------------------------------------


def test_a_non_cloudtrail_event_is_rejected() -> None:
    event = make_event()
    event["detail-type"] = "CloudFormation Stack Status Change"

    with pytest.raises(NotActionable, match="detail-type"):
        parse_change_set_event(event)


def test_another_services_api_call_is_rejected() -> None:
    """The rule filters on eventSource, but a hand-edited rule should not be able
    to feed an S3 event into the change set parser."""
    with pytest.raises(NotActionable, match="eventSource"):
        parse_change_set_event(make_event(eventSource="s3.amazonaws.com"))


def test_execute_change_set_is_rejected() -> None:
    """Deliberate: the confirmed report comes from Path A's terminal status, so
    handling ExecuteChangeSet here would double-report."""
    with pytest.raises(NotActionable, match="change set creation"):
        parse_change_set_event(make_event(eventName="ExecuteChangeSet"))

    assert "ExecuteChangeSet" not in CHANGE_SET_EVENT_NAMES


def test_an_unrelated_cloudformation_call_is_rejected() -> None:
    with pytest.raises(NotActionable):
        parse_change_set_event(make_event(eventName="DescribeStacks"))


def test_a_failed_api_call_is_rejected() -> None:
    """An AccessDenied on CreateChangeSet produced no change set, so there is
    nothing to price."""
    with pytest.raises(NotActionable, match="AccessDenied"):
        parse_change_set_event(make_event(errorCode="AccessDenied"))


def test_an_event_with_no_response_is_rejected() -> None:
    """CloudTrail omits responseElements for some calls. Proceeding would mean
    describing a change set whose ID we invented."""
    with pytest.raises(NotActionable, match="responseElements"):
        parse_change_set_event(make_event(responseElements=None))


def test_a_missing_change_set_id_is_rejected() -> None:
    with pytest.raises(NotActionable, match="change set ID"):
        parse_change_set_event(make_event(responseElements={"stackId": STACK_ID}))


def test_a_missing_stack_id_is_rejected() -> None:
    with pytest.raises(NotActionable, match="stack ID"):
        parse_change_set_event(make_event(responseElements={"id": CHANGE_SET_ID}))


def test_a_blank_id_is_rejected_like_a_missing_one() -> None:
    with pytest.raises(NotActionable):
        parse_change_set_event(
            make_event(responseElements={"id": "", "stackId": STACK_ID})
        )


def test_an_event_with_no_detail_is_rejected() -> None:
    event = make_event()
    event["detail"] = None

    with pytest.raises(NotActionable, match="detail"):
        parse_change_set_event(event)


def test_a_non_mapping_is_rejected() -> None:
    with pytest.raises(NotActionable, match="mapping"):
        parse_change_set_event("not an event")  # type: ignore[arg-type]


def test_rejection_raises_rather_than_returning_none() -> None:
    """A None return would be easy to forget to check, and an unchecked None
    becomes an estimate against a change set that was never created."""
    with pytest.raises(NotActionable):
        parse_change_set_event({})


# -- retry signal ---------------------------------------------------------


def test_not_ready_is_an_exception_so_it_cannot_be_ignored() -> None:
    """CreateChangeSet returns before the change set exists. Treating that as an
    empty result would report a $0 estimate for a real change."""
    assert issubclass(ChangeSetNotReady, Exception)

    with pytest.raises(ChangeSetNotReady):
        raise ChangeSetNotReady("CREATE_PENDING")


def test_the_event_is_immutable() -> None:
    """The event is the dedupe key's source. Mutating it mid-flight would change
    which claim gets released on failure."""
    import dataclasses

    event = ChangeSetEvent(
        change_set_id=CHANGE_SET_ID,
        stack_id=STACK_ID,
        account="111122223333",
        region="us-east-1",
    )

    with pytest.raises(dataclasses.FrozenInstanceError):
        event.stack_id = "other"  # type: ignore[misc]
