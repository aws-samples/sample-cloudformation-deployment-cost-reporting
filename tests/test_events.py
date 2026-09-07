"""EventBridge event parsing."""

import pytest

from analyzer import NotActionable, StackEvent, parse_stack_event, stack_name_from_id
from state import DeltaAction

STACK_ID = (
    "arn:aws:cloudformation:us-east-1:111122223333:stack/payments-api-prod/abc-123"
)


def event(status="UPDATE_COMPLETE", **overrides):
    payload = {
        "version": "0",
        "id": "6a7e8feb-b491-4cf7-a9f1-bf3703467718",
        "detail-type": "CloudFormation Stack Status Change",
        "source": "aws.cloudformation",
        "account": "111122223333",
        "time": "2026-08-10T17:06:18Z",
        "region": "us-east-1",
        "resources": [STACK_ID],
        "detail": {
            "stack-id": STACK_ID,
            "status-details": {"status": status, "status-reason": ""},
            "client-request-token": "Console-UpdateStack-7f59c3cf",
        },
    }
    payload.update(overrides)
    return payload


def test_a_terminal_event_parses():
    parsed = parse_stack_event(event())

    assert isinstance(parsed, StackEvent)
    assert parsed.stack_id == STACK_ID
    assert parsed.stack_name == "payments-api-prod"
    assert parsed.status == "UPDATE_COMPLETE"
    assert parsed.action is DeltaAction.UPDATE
    assert parsed.account == "111122223333"
    assert parsed.region == "us-east-1"
    assert parsed.client_request_token == "Console-UpdateStack-7f59c3cf"


@pytest.mark.parametrize(
    "status,action",
    [
        ("CREATE_COMPLETE", DeltaAction.CREATE),
        ("UPDATE_COMPLETE", DeltaAction.UPDATE),
        ("DELETE_COMPLETE", DeltaAction.DELETE),
        ("UPDATE_ROLLBACK_COMPLETE", DeltaAction.ROLLBACK),
        ("ROLLBACK_COMPLETE", DeltaAction.ROLLBACK),
    ],
)
def test_every_terminal_status_maps_to_an_action(status, action):
    assert parse_stack_event(event(status)).action is action


@pytest.mark.parametrize(
    "status",
    [
        "CREATE_IN_PROGRESS",
        "UPDATE_IN_PROGRESS",
        "DELETE_IN_PROGRESS",
        "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS",
        "CREATE_FAILED",
        "REVIEW_IN_PROGRESS",
    ],
)
def test_non_terminal_statuses_are_rejected(status):
    """Reading mid-deployment gives a partial, shifting picture (S6)."""
    with pytest.raises(NotActionable, match="terminal"):
        parse_stack_event(event(status))


def test_other_detail_types_are_rejected():
    with pytest.raises(NotActionable, match="detail-type"):
        parse_stack_event(event(**{"detail-type": "CloudFormation Resource Status Change"}))


def test_other_sources_are_rejected():
    with pytest.raises(NotActionable, match="source"):
        parse_stack_event(event(source="aws.ec2"))


def test_missing_detail_is_rejected():
    payload = event()
    del payload["detail"]
    with pytest.raises(NotActionable, match="detail"):
        parse_stack_event(payload)


def test_missing_stack_id_is_rejected():
    payload = event()
    del payload["detail"]["stack-id"]
    with pytest.raises(NotActionable, match="stack-id"):
        parse_stack_event(payload)


def test_missing_status_is_rejected():
    payload = event()
    payload["detail"]["status-details"] = {}
    with pytest.raises(NotActionable, match="status"):
        parse_stack_event(payload)


def test_non_mapping_event_is_rejected():
    with pytest.raises(NotActionable):
        parse_stack_event(["not", "a", "mapping"])


def test_dedupe_key_uses_the_client_request_token():
    """Every event from one deployment shares the token (S9)."""
    first = parse_stack_event(event())
    second = parse_stack_event(event(id="a-different-event-id"))

    assert first.dedupe_key == second.dedupe_key


def test_different_operations_get_different_dedupe_keys():
    payload = event()
    payload["detail"]["client-request-token"] = "Console-UpdateStack-other"

    assert parse_stack_event(event()).dedupe_key != parse_stack_event(payload).dedupe_key


def test_dedupe_key_falls_back_to_the_event_id_when_no_token():
    payload = event()
    del payload["detail"]["client-request-token"]
    parsed = parse_stack_event(payload)

    assert parsed.client_request_token is None
    assert parsed.event_id in parsed.dedupe_key


def test_delete_is_flagged():
    assert parse_stack_event(event("DELETE_COMPLETE")).is_delete
    assert not parse_stack_event(event("UPDATE_COMPLETE")).is_delete


@pytest.mark.parametrize(
    "stack_id,expected",
    [
        (STACK_ID, "payments-api-prod"),
        ("arn:aws:cloudformation:eu-west-1:1:stack/my-stack/def", "my-stack"),
        ("plain-name", "plain-name"),
    ],
)
def test_stack_name_extraction(stack_id, expected):
    assert stack_name_from_id(stack_id) == expected
