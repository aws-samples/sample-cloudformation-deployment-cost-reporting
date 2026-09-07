"""Tests for report delivery.

The interesting behaviour is what happens to a report that will not fit. SNS caps
a message at 256KB, and a large nested application exceeds that. Dropping the
report would violate the rule the whole project is built on — never silently drop
a resource — so it is trimmed instead, and the trim order is what these tests
pin: per-resource detail goes first, the numbers that summarise it never go.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from runtime.publisher import (
    MAX_PAYLOAD_BYTES,
    SNS_MESSAGE_LIMIT,
    CompositePublisher,
    EventBridgePublisher,
    NullPublisher,
    SnsPublisher,
    trim_for_transport,
)


def make_report(**overrides: Any) -> dict[str, Any]:
    report = {
        "reportId": "r-1",
        "stackId": "arn:aws:cloudformation:us-east-1:111122223333:stack/api/abc",
        "stackName": "api",
        "account": "111122223333",
        "region": "us-east-1",
        "action": "UPDATE",
        "direction": "INCREASE",
        "reportPhase": "CONFIRMED",
        "reconciles": True,
        "totals": {"netMonthly": 120.5, "currentStackMonthly": 500.0},
        "coverage": {"pricedPercent": 100, "resourcesPriced": 4},
        "added": [{"logicalId": "Db", "monthlyCost": 120.5}],
        "removed": [],
        "changed": [],
        "retained": [],
        "unpriced": {"usageBased": [], "unsupported": []},
    }
    report.update(overrides)
    return report


def bulky(count: int) -> list[dict[str, Any]]:
    """Resources with enough padding to blow the payload limit."""
    return [
        {
            "logicalId": f"Resource{i}",
            "resourceType": "AWS::EC2::Instance",
            "description": "x" * 400,
            "monthlyCost": 10.0,
        }
        for i in range(count)
    ]


class FakeSns:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def publish(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(kwargs)
        return {"MessageId": "m-1"}


class FakeEvents:
    def __init__(self, failed: int = 0, error_code: str = "") -> None:
        self.calls: list[dict[str, Any]] = []
        self._failed = failed
        self._error_code = error_code

    def put_events(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self._failed:
            return {
                "FailedEntryCount": self._failed,
                "Entries": [
                    {"ErrorCode": self._error_code, "ErrorMessage": "rejected"}
                ],
            }
        return {"FailedEntryCount": 0, "Entries": [{"EventId": "e-1"}]}


class Exploding:
    def publish(self, report: dict[str, Any]) -> None:
        raise RuntimeError("destination down")


# -- trimming -------------------------------------------------------------


def test_a_small_report_is_untouched() -> None:
    report = make_report()

    trimmed, dropped = trim_for_transport(report)

    assert dropped == []
    assert trimmed is report
    assert "truncated" not in trimmed


def test_an_oversized_report_is_trimmed_not_rejected() -> None:
    report = make_report(inventory=bulky(2000))

    trimmed, dropped = trim_for_transport(report)

    assert dropped
    assert trimmed["truncated"] is True
    assert len(json.dumps(trimmed).encode()) <= MAX_PAYLOAD_BYTES


def test_totals_and_coverage_always_survive() -> None:
    """An oversized report must still answer "what did this cost", even if it can
    no longer answer "which resource"."""
    report = make_report(inventory=bulky(2000), added=bulky(2000))

    trimmed, _ = trim_for_transport(report)

    assert trimmed["totals"]["netMonthly"] == 120.5
    assert trimmed["coverage"]["pricedPercent"] == 100
    assert trimmed["stackName"] == "api"
    assert trimmed["reportId"] == "r-1"


def test_inventory_is_dropped_before_added() -> None:
    """Inventory is the least useful section — it is a full listing, not a delta.
    Added resources are the point of the report.

    Sized so that dropping inventory alone brings the report under the limit; if
    the order were reversed, `added` would go while inventory stayed.
    """
    report = make_report(inventory=bulky(1000), added=bulky(50))

    trimmed, dropped = trim_for_transport(report)

    assert len(dropped) == 1
    assert "inventory" in dropped[0]
    assert len(trimmed["added"]) == 50


def test_everything_trimmable_goes_before_the_totals_do() -> None:
    """When no single section is enough, the trim keeps going rather than giving
    up and dropping the report.

    Both sections here are individually oversized, so both must go — and the
    numbers still come through.
    """
    report = make_report(inventory=bulky(1000), added=bulky(1000))

    trimmed, dropped = trim_for_transport(report)

    assert len(dropped) > 1
    assert "added" not in trimmed
    assert trimmed["totals"]["netMonthly"] == 120.5


def test_the_note_names_what_was_removed() -> None:
    """A silently shorter report is indistinguishable from a deployment that
    changed less than it did."""
    report = make_report(inventory=bulky(2000))

    trimmed, _ = trim_for_transport(report)
    notes = " ".join(trimmed["notes"])

    assert "size limit" in notes
    assert "inventory" in notes
    assert "Totals and coverage are unaffected" in notes


def test_existing_notes_are_preserved() -> None:
    report = make_report(inventory=bulky(2000), notes=["Assumed Linux for 3 instances"])

    trimmed, _ = trim_for_transport(report)

    assert "Assumed Linux for 3 instances" in trimmed["notes"]
    assert len(trimmed["notes"]) == 2


def test_the_original_report_is_not_mutated() -> None:
    """The same report object goes to several publishers. Trimming in place would
    mean whichever ran first decided what the others saw."""
    report = make_report(inventory=bulky(2000))

    trim_for_transport(report)

    assert len(report["inventory"]) == 2000
    assert "truncated" not in report


def test_trimming_stops_as_soon_as_it_fits() -> None:
    """Dropping every section when one was enough throws away detail for nothing."""
    report = make_report(inventory=bulky(2000), added=bulky(5), removed=bulky(5))

    trimmed, dropped = trim_for_transport(report)

    assert len(dropped) == 1
    assert "added" in trimmed
    assert "removed" in trimmed


# -- SNS ------------------------------------------------------------------


def test_sns_publishes_message_subject_and_attributes() -> None:
    client = FakeSns()

    SnsPublisher(client, "arn:aws:sns:us-east-1:111122223333:reports").publish(
        make_report()
    )

    call = client.calls[0]
    assert call["TopicArn"] == "arn:aws:sns:us-east-1:111122223333:reports"
    assert call["Subject"]
    assert call["MessageAttributes"]
    assert call["MessageStructure"] == "json"

    envelope = json.loads(call["Message"])
    # Programmatic subscribers (Lambda/Slack, SQS) get the JSON via "default".
    assert json.loads(envelope["default"])["reportId"] == "r-1"
    # The email subscriber gets a human-readable body, not raw JSON.
    assert "Net change:" in envelope["email"]
    assert "api" in envelope["email"]


def test_the_sns_subject_fits_the_100_character_limit() -> None:
    """SNS rejects the whole publish if the subject is too long, so a long stack
    name would mean no report at all."""
    client = FakeSns()
    report = make_report(stackName="a" * 300)

    SnsPublisher(client, "arn:topic").publish(report)

    assert len(client.calls[0]["Subject"]) <= 100


def test_sns_sends_a_trimmed_body_when_oversized() -> None:
    client = FakeSns()

    SnsPublisher(client, "arn:topic").publish(make_report(inventory=bulky(2000)))

    message = client.calls[0]["Message"]
    # The whole SNS envelope (escaped JSON + email body + overhead) must fit.
    assert len(message.encode()) <= SNS_MESSAGE_LIMIT
    envelope = json.loads(message)
    assert json.loads(envelope["default"])["truncated"] is True


# -- EventBridge ----------------------------------------------------------


def test_eventbridge_forwards_to_the_named_bus() -> None:
    client = FakeEvents()

    EventBridgePublisher(client, "arn:aws:events:us-east-1:999:event-bus/central").publish(
        make_report()
    )

    entry = client.calls[0]["Entries"][0]
    assert entry["EventBusName"] == "arn:aws:events:us-east-1:999:event-bus/central"
    assert entry["Source"] == "cfn.cost.plugin"
    assert json.loads(entry["Detail"])["reportId"] == "r-1"


def test_a_rejected_entry_raises_rather_than_passing_silently() -> None:
    """put_events reports per-entry failures in the response body instead of
    raising. Without an explicit check the report is dropped and the invocation
    reports success."""
    client = FakeEvents(failed=1, error_code="AccessDeniedException")

    with pytest.raises(RuntimeError, match="AccessDeniedException"):
        EventBridgePublisher(client, "arn:bus").publish(make_report())


# -- composite ------------------------------------------------------------


def test_every_destination_receives_the_report() -> None:
    first, second = NullPublisher(), NullPublisher()

    CompositePublisher(first, second).publish(make_report())

    assert len(first.published) == 1
    assert len(second.published) == 1


def test_one_failing_destination_does_not_block_the_others() -> None:
    """A Slack outage must not cost the email that would have said the same thing."""
    healthy = NullPublisher()

    with pytest.raises(RuntimeError):
        CompositePublisher(Exploding(), healthy).publish(make_report())

    assert len(healthy.published) == 1


def test_the_failure_is_re_raised_so_the_message_is_retried() -> None:
    """Swallowing it would mark the SQS message done with the report only
    half-delivered."""
    with pytest.raises(RuntimeError, match="destination down"):
        CompositePublisher(NullPublisher(), Exploding()).publish(make_report())


def test_a_composite_with_no_destinations_is_a_no_op() -> None:
    composite = CompositePublisher()

    composite.publish(make_report())

    assert len(composite) == 0


def test_null_publisher_records_for_inspection() -> None:
    publisher = NullPublisher()

    publisher.publish(make_report())

    assert publisher.published[0]["reportId"] == "r-1"
