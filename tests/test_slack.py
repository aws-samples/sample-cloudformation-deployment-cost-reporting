"""Tests for Slack rendering and the two-phase merge.

The merge is the part that can go quietly wrong. An estimate posts, the confirmed
report arrives, and the same message must be *edited* rather than a second one
posted — otherwise the channel shows two figures for one deployment and no
indication which is real. The fallback matters just as much: if the edit fails, a
new post is better than losing the confirmed numbers entirely.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from runtime.slack import (
    InMemoryMessageStore,
    SlackApiError,
    SlackForwarder,
    fallback_text,
    render_blocks,
)

STACK_ID = "arn:aws:cloudformation:us-east-1:111122223333:stack/payments-api-prod/abc-123"


def make_report(**overrides: Any) -> dict[str, Any]:
    report: dict[str, Any] = {
        "reportId": "r-1",
        "stackId": STACK_ID,
        "stackName": "payments-api-prod",
        "account": "111122223333",
        "region": "us-east-1",
        "action": "UPDATE",
        "direction": "INCREASE",
        "reportPhase": "CONFIRMED",
        "analyzedAt": "2026-08-10T17:06:18.123456Z",
        "totals": {
            "netMonthly": 120.5,
            "netAnnual": 1446.0,
            "addedMonthly": 140.0,
            "removedMonthly": 19.5,
            "changedMonthly": 0.0,
            "previousStackMonthly": 379.5,
            "currentStackMonthly": 500.0,
        },
        "coverage": {
            "pricedPercent": 75.0,
            "resourcesPriced": 6,
            "resourcesUsageBased": 1,
            "resourcesUnsupported": 1,
            "resourcesUnresolved": 0,
        },
        "pricingBasis": {
            "rateType": "On-Demand",
            "hoursPerMonth": 730,
            "discountPercent": 0,
        },
        "added": [
            {
                "logicalId": "Database",
                "resourceType": "AWS::RDS::DBInstance",
                "description": "db.r6g.large PostgreSQL Single-AZ",
                "monthlyCost": 140.0,
            }
        ],
        "removed": [],
        "changed": [],
        "retained": [],
        "unpriced": {"usageBased": [], "unsupported": []},
    }
    report.update(overrides)
    return report


def text_of(blocks: list[dict[str, Any]]) -> str:
    """All rendered text, flattened, for substring assertions."""
    parts: list[str] = []
    for block in blocks:
        if "text" in block and isinstance(block["text"], dict):
            parts.append(block["text"].get("text", ""))
        for field in block.get("fields") or []:
            parts.append(field.get("text", ""))
        for element in block.get("elements") or []:
            if isinstance(element, dict):
                parts.append(element.get("text", "") if isinstance(element.get("text"), str) else "")
    return "\n".join(parts)


class FakeClient:
    def __init__(self, update_fails: bool = False, post_fails: bool = False) -> None:
        self.posted: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self._update_fails = update_fails
        self._post_fails = post_fails
        self.next_ts = "1725000000.000100"

    def post_message(self, channel: str, blocks: list[dict[str, Any]], text: str) -> str:
        if self._post_fails:
            raise SlackApiError("channel_not_found")
        self.posted.append({"channel": channel, "blocks": blocks, "text": text})
        return self.next_ts

    def update_message(
        self, channel: str, timestamp: str, blocks: list[dict[str, Any]], text: str
    ) -> None:
        if self._update_fails:
            raise SlackApiError("message_not_found")
        self.updated.append({"channel": channel, "ts": timestamp, "blocks": blocks})


# -- rendering ------------------------------------------------------------


def test_the_header_leads_with_the_net_change() -> None:
    blocks = render_blocks(make_report())

    header = blocks[0]
    assert header["type"] == "header"
    assert "+$120.50" in header["text"]["text"]
    assert "payments-api-prod" in header["text"]["text"]


def test_an_increase_and_a_saving_are_visually_distinct() -> None:
    increase = render_blocks(make_report())[0]["text"]["text"]
    saving = render_blocks(
        make_report(direction="DECREASE", totals={"netMonthly": -340.0})
    )[0]["text"]["text"]

    assert "🔴" in increase
    assert "🟢" in saving


def test_a_saving_is_signed_with_a_minus() -> None:
    """A teardown that saves money is the most compelling output the plugin has;
    its headline, removed subtotal, and resource row must all read as savings."""
    totals = {**make_report()["totals"], "netMonthly": -340.0, "removedMonthly": 340.0}
    report = make_report(
        direction="DECREASE",
        totals=totals,
        added=[],
        removed=[{"logicalId": "OldCluster", "monthlyCost": 340.0}],
    )
    blocks = render_blocks(report)
    body = text_of(blocks)

    assert "−$340.00" in blocks[0]["text"]["text"]
    assert "Removed  −$340.00/mo" in body
    assert "OldCluster" in body
    assert "*−$340.00*" in body


def test_the_phase_is_shown() -> None:
    estimate = text_of(render_blocks(make_report(reportPhase="ESTIMATE")))
    confirmed = text_of(render_blocks(make_report(reportPhase="CONFIRMED")))

    assert "Estimate" in estimate
    assert "Confirmed" in confirmed


def test_added_resources_are_listed_with_their_cost() -> None:
    body = text_of(render_blocks(make_report()))

    assert "Database" in body
    assert "db.r6g.large PostgreSQL Single-AZ" in body
    assert "$140.00" in body


def test_empty_sections_are_omitted() -> None:
    """A "Removed" heading with nothing under it reads as a rendering bug."""
    body = text_of(render_blocks(make_report()))

    assert "Removed" not in body
    assert "Resized" not in body


def test_a_resize_shows_both_sides() -> None:
    """The before and after are the whole point — a delta alone does not say what
    changed."""
    report = make_report(
        changed=[
            {
                "logicalId": "Api",
                "before": {"description": "t3.large"},
                "after": {"description": "t3.xlarge"},
                "deltaMonthly": 60.74,
            }
        ]
    )

    body = text_of(render_blocks(report))

    assert "t3.large" in body
    assert "t3.xlarge" in body
    assert "+$60.74" in body


def test_a_replacement_is_flagged() -> None:
    report = make_report(
        changed=[
            {
                "logicalId": "Api",
                "before": {"description": "t3.large"},
                "after": {"description": "t3.xlarge"},
                "deltaMonthly": 60.74,
                "replacement": True,
            }
        ]
    )

    assert "replaced" in text_of(render_blocks(report))


def test_retained_resources_say_they_are_still_billing() -> None:
    """The most expensive misreading available: a resource removed from the
    template but kept by DeletionPolicy is not a saving."""
    report = make_report(
        retained=[
            {
                "logicalId": "Bucket",
                "resourceType": "AWS::S3::Bucket",
                "deletionPolicy": "Retain",
            }
        ]
    )

    body = text_of(render_blocks(report))

    assert "still billing" in body
    assert "Bucket" in body


def test_coverage_appears_on_every_message() -> None:
    """A report that hides its gaps stops being trustworthy the first time
    someone notices one."""
    body = text_of(render_blocks(make_report()))

    assert "Priced 6 of 8" in body


def test_free_resources_are_excluded_from_the_coverage_denominator() -> None:
    """Counting an IAM role as unpriced would make coverage look bad for a
    resource that costs nothing."""
    report = make_report(
        coverage={
            "resourcesPriced": 6,
            "resourcesUsageBased": 1,
            "resourcesUnsupported": 1,
            "resourcesUnresolved": 0,
            "resourcesFree": 12,
        }
    )

    assert "Priced 6 of 8" in text_of(render_blocks(report))


def test_the_pricing_basis_is_stated() -> None:
    """730 hours, On-Demand, and the discount factor are assumptions, and an
    unstated assumption is indistinguishable from an error."""
    body = text_of(render_blocks(make_report()))

    assert "On-Demand" in body
    assert "730h/mo" in body
    assert "no discount applied" in body


def test_an_applied_discount_is_named() -> None:
    report = make_report(
        pricingBasis={"rateType": "On-Demand", "hoursPerMonth": 730, "discountPercent": 15}
    )

    assert "15% discount" in text_of(render_blocks(report))


def test_usage_based_resources_are_named_not_just_counted() -> None:
    report = make_report(
        unpriced={
            "usageBased": [{"logicalId": "Queue"}, {"logicalId": "Topic"}],
            "unsupported": [],
        }
    )

    body = text_of(render_blocks(report))

    assert "2 resource(s) depend on usage" in body
    assert "Queue" in body


def test_a_baseline_shows_inventory_instead_of_a_delta() -> None:
    """First sighting of a stack has nothing to diff against, so a delta would be
    a fabrication."""
    report = make_report(
        isBaseline=True,
        inventory=[
            {"logicalId": "Api", "description": "t3.large", "monthlyCost": 60.74}
        ],
    )

    blocks = render_blocks(report)
    body = text_of(blocks)

    assert "now tracking" in blocks[0]["text"]["text"]
    assert "Current inventory" in body
    assert "NET" not in body


def test_notes_are_carried_through() -> None:
    report = make_report(notes=["Assumed Linux for 2 instances"])

    assert "Assumed Linux for 2 instances" in text_of(render_blocks(report))


def test_a_console_link_is_added_when_present() -> None:
    report = make_report(consoleUrl="https://console.aws.amazon.com/cloudformation")

    actions = [b for b in render_blocks(report) if b["type"] == "actions"]

    assert actions[0]["elements"][0]["url"].startswith("https://")


def test_the_block_count_stays_under_slacks_limit() -> None:
    """Slack rejects a message with more than 50 blocks outright."""
    report = make_report(
        added=[{"logicalId": f"R{i}", "monthlyCost": 1.0} for i in range(200)],
        removed=[{"logicalId": f"D{i}", "monthlyCost": 1.0} for i in range(200)],
        retained=[{"logicalId": f"K{i}"} for i in range(200)],
    )

    assert len(render_blocks(report)) <= 50


def test_a_long_stack_name_does_not_break_the_header() -> None:
    """Slack's plain_text header caps at 150 characters."""
    blocks = render_blocks(make_report(stackName="s" * 400))

    assert len(blocks[0]["text"]["text"]) <= 150


def test_a_sparse_report_still_renders() -> None:
    """Rendering must not be the thing that loses a report."""
    assert render_blocks({"stackName": "api"})


# -- fallback text --------------------------------------------------------


def test_fallback_text_carries_the_number() -> None:
    """This is what shows in a push notification, where blocks do not render."""
    text = fallback_text(make_report())

    assert "payments-api-prod" in text
    assert "+$120.50" in text


def test_baseline_fallback_text_does_not_claim_a_change() -> None:
    text = fallback_text(make_report(isBaseline=True))

    assert "Now tracking" in text
    assert "$500.00" in text


# -- two-phase merge ------------------------------------------------------


def test_an_estimate_is_posted_and_remembered() -> None:
    client, store = FakeClient(), InMemoryMessageStore()

    SlackForwarder(client, "C123", store).send(make_report(reportPhase="ESTIMATE"))

    assert len(client.posted) == 1
    assert store.get(STACK_ID) == client.next_ts


def test_a_confirmed_report_edits_the_estimate_in_place() -> None:
    """Two messages for one deployment leaves the reader guessing which figure
    is real."""
    client, store = FakeClient(), InMemoryMessageStore()
    forwarder = SlackForwarder(client, "C123", store)

    forwarder.send(make_report(reportPhase="ESTIMATE"))
    forwarder.send(make_report(reportPhase="CONFIRMED"))

    assert len(client.posted) == 1
    assert len(client.updated) == 1
    assert client.updated[0]["ts"] == client.next_ts


def test_the_stored_timestamp_is_cleared_after_the_merge() -> None:
    """The cycle is complete. Leaving it would make the next deployment's
    estimate edit a message from the previous one."""
    client, store = FakeClient(), InMemoryMessageStore()
    forwarder = SlackForwarder(client, "C123", store)

    forwarder.send(make_report(reportPhase="ESTIMATE"))
    forwarder.send(make_report(reportPhase="CONFIRMED"))

    assert store.get(STACK_ID) is None


def test_a_confirmed_report_with_no_estimate_posts_normally() -> None:
    """Path B is best-effort. Path A must work on its own."""
    client, store = FakeClient(), InMemoryMessageStore()

    SlackForwarder(client, "C123", store).send(make_report(reportPhase="CONFIRMED"))

    assert len(client.posted) == 1
    assert client.updated == []


def test_a_failed_edit_falls_back_to_a_new_post() -> None:
    """The estimate may have been deleted, or be outside Slack's edit window.
    Losing the confirmed report over that would be the worse outcome."""
    client = FakeClient(update_fails=True)
    store = InMemoryMessageStore()
    store.put(STACK_ID, "1725000000.000001")

    SlackForwarder(client, "C123", store).send(make_report(reportPhase="CONFIRMED"))

    assert len(client.posted) == 1


def test_a_confirmed_report_is_not_remembered() -> None:
    """Only an estimate is worth remembering; a confirmed report is the end of
    the cycle."""
    client, store = FakeClient(), InMemoryMessageStore()

    SlackForwarder(client, "C123", store).send(make_report(reportPhase="CONFIRMED"))

    assert store.get(STACK_ID) is None


def test_the_forwarder_works_without_a_store() -> None:
    """A store is optional. Without one, every report simply posts fresh."""
    client = FakeClient()
    forwarder = SlackForwarder(client, "C123", None)

    forwarder.send(make_report(reportPhase="ESTIMATE"))
    forwarder.send(make_report(reportPhase="CONFIRMED"))

    assert len(client.posted) == 2
    assert client.updated == []


def test_two_stacks_do_not_share_a_message() -> None:
    client, store = FakeClient(), InMemoryMessageStore()
    forwarder = SlackForwarder(client, "C123", store)

    forwarder.send(make_report(reportPhase="ESTIMATE"))
    client.next_ts = "1725000000.000200"
    forwarder.send(make_report(reportPhase="ESTIMATE", stackId="other-stack"))

    assert store.get(STACK_ID) == "1725000000.000100"
    assert store.get("other-stack") == "1725000000.000200"


def test_a_report_with_no_stack_id_is_still_delivered() -> None:
    """It cannot participate in the merge, but the numbers still reach the
    channel."""
    client, store = FakeClient(), InMemoryMessageStore()
    report = make_report(reportPhase="ESTIMATE")
    del report["stackId"]

    SlackForwarder(client, "C123", store).send(report)

    assert len(client.posted) == 1
    assert store.messages == {}


# -- message store --------------------------------------------------------


def test_the_in_memory_store_round_trips() -> None:
    store = InMemoryMessageStore()

    store.put("stack-1", "1725000000.000100")

    assert store.get("stack-1") == "1725000000.000100"

    store.delete("stack-1")

    assert store.get("stack-1") is None


def test_deleting_an_absent_key_is_harmless() -> None:
    InMemoryMessageStore().delete("never-stored")


# -- dynamo message store -------------------------------------------------


class FakeTable:
    def __init__(self, fail: bool = False) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.fail = fail

    def get_item(self, Key: dict[str, Any]) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("DynamoDB unavailable")
        item = self.items.get(Key["pk"])
        return {"Item": item} if item else {}

    def put_item(self, Item: dict[str, Any]) -> None:
        if self.fail:
            raise RuntimeError("DynamoDB unavailable")
        self.items[Item["pk"]] = Item

    def delete_item(self, Key: dict[str, Any]) -> None:
        if self.fail:
            raise RuntimeError("DynamoDB unavailable")
        self.items.pop(Key["pk"], None)


def test_the_dynamo_store_round_trips() -> None:
    from runtime.slack import DynamoMessageStore

    table = FakeTable()
    store = DynamoMessageStore(table)

    store.put(STACK_ID, "1725000000.000100")

    assert store.get(STACK_ID) == "1725000000.000100"


def test_the_dynamo_store_sets_a_ttl() -> None:
    """An abandoned change set leaves a timestamp that will never be edited. The
    TTL is what stops the table growing forever."""
    from runtime.slack import DynamoMessageStore

    table = FakeTable()
    DynamoMessageStore(table).put(STACK_ID, "1725000000.000100")

    assert isinstance(table.items[STACK_ID]["expiresAt"], int)


def test_an_absent_timestamp_reads_as_none() -> None:
    from runtime.slack import DynamoMessageStore

    assert DynamoMessageStore(FakeTable()).get("never-stored") is None


def test_a_read_failure_reads_as_none_rather_than_raising() -> None:
    """Not knowing the earlier message means posting a new one, which is a
    degraded outcome. Raising would mean no report at all."""
    from runtime.slack import DynamoMessageStore

    assert DynamoMessageStore(FakeTable(fail=True)).get(STACK_ID) is None


def test_a_write_failure_does_not_raise() -> None:
    """The message is already posted. Failing here would retry the whole analysis
    and post it a second time."""
    from runtime.slack import DynamoMessageStore

    DynamoMessageStore(FakeTable(fail=True)).put(STACK_ID, "1725000000.000100")


def test_a_delete_failure_does_not_raise() -> None:
    """A stale timestamp is removed by the TTL anyway."""
    from runtime.slack import DynamoMessageStore

    DynamoMessageStore(FakeTable(fail=True)).delete(STACK_ID)


# -- HTTP client ----------------------------------------------------------


class FakeResponse:
    def __init__(self, body: str) -> None:
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def test_a_slack_application_error_is_raised_despite_http_200(
    monkeypatch: Any,
) -> None:
    """Slack returns 200 with {"ok": false} for channel_not_found and
    invalid_auth. Treating 200 as success swallows both."""
    import urllib.request

    from runtime.slack import SlackClient

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *a, **k: FakeResponse('{"ok": false, "error": "channel_not_found"}'),
    )

    with pytest.raises(SlackApiError, match="channel_not_found"):
        SlackClient("test-bot-token").post_message("C123", [], "text")


def test_a_successful_post_returns_the_timestamp(monkeypatch: Any) -> None:
    import urllib.request

    from runtime.slack import SlackClient

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *a, **k: FakeResponse('{"ok": true, "ts": "1725000000.000100"}'),
    )

    ts = SlackClient("test-bot-token").post_message("C123", [], "text")

    assert ts == "1725000000.000100"


def test_a_non_json_body_is_reported_as_an_error(monkeypatch: Any) -> None:
    """A proxy or captive portal returning HTML must not surface as a confusing
    JSONDecodeError from inside the forwarder."""
    import urllib.request

    from runtime.slack import SlackClient

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *a, **k: FakeResponse("<html>nope</html>")
    )

    with pytest.raises(SlackApiError, match="non-JSON"):
        SlackClient("test-bot-token").post_message("C123", [], "text")


def test_a_timeout_is_reported_as_an_error(monkeypatch: Any) -> None:
    import urllib.request

    from runtime.slack import SlackClient

    def timeout(*args: object, **kwargs: object) -> None:
        raise TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", timeout)

    with pytest.raises(SlackApiError, match="could not be reached"):
        SlackClient("test-bot-token").post_message("C123", [], "text")


def test_the_token_is_sent_as_a_bearer_header_not_in_the_body(
    monkeypatch: Any,
) -> None:
    """A token in the body would end up in any request log."""
    import urllib.request

    from runtime.slack import SlackClient

    captured: dict[str, Any] = {}

    def capture(request: Any, **kwargs: object) -> FakeResponse:
        captured["headers"] = request.headers
        captured["body"] = request.data.decode()
        return FakeResponse('{"ok": true, "ts": "1"}')

    monkeypatch.setattr(urllib.request, "urlopen", capture)

    SlackClient("test-secret-token").post_message("C123", [], "text")

    assert captured["headers"]["Authorization"] == "Bearer test-secret-token"
    assert "test-secret-token" not in captured["body"]


def test_update_targets_the_stored_timestamp(monkeypatch: Any) -> None:
    import urllib.request

    from runtime.slack import SlackClient

    captured: dict[str, Any] = {}

    def capture(request: Any, **kwargs: object) -> FakeResponse:
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode())
        return FakeResponse('{"ok": true}')

    monkeypatch.setattr(urllib.request, "urlopen", capture)

    SlackClient("test-bot-token").update_message("C123", "1725000000.000100", [], "text")

    assert captured["url"].endswith("/chat.update")
    assert captured["body"]["ts"] == "1725000000.000100"
