"""Tests for the Lambda entry points.

The behaviour worth protecting here is failure handling, because every mistake in
it is silent. A message that fails must come back for a retry, but only that
message — returning the whole batch would send nine good reports to the
dead-letter queue alongside one bad one. And a failed analysis must release its
idempotency claim, or the retry is skipped and the report is lost with nothing to
say so.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from analyzer.changeset import ChangeSetNotReady
from runtime import handlers
from runtime.idempotency import InMemoryIdempotencyStore
from runtime.metrics import NullMetrics
from runtime.publisher import NullPublisher

STACK_ID = "arn:aws:cloudformation:us-east-1:111122223333:stack/payments-api-prod/abc-123"
CHANGE_SET_ID = "arn:aws:cloudformation:us-east-1:111122223333:changeSet/deploy-42/cs-abc"


# -- fixtures -------------------------------------------------------------


class Outcome:
    def __init__(
        self,
        reported: bool = True,
        skipped: str | None = None,
        report: dict[str, Any] | None = None,
    ) -> None:
        self.reported = reported
        self.skipped = skipped
        self.report = report if report is not None else {
            "reportId": "r-1",
            "stackId": STACK_ID,
            "stackName": "payments-api-prod",
            "reconciles": True,
            "totals": {"netMonthly": 120.5},
            "coverage": {},
        }


class FakeAnalyzer:
    def __init__(
        self,
        outcome: Outcome | None = None,
        raises: Exception | None = None,
        change_set_raises: Exception | None = None,
    ) -> None:
        self.outcome = outcome or Outcome()
        self.raises = raises
        self.change_set_raises = change_set_raises
        self.analyzed: list[Any] = []
        self.estimated: list[Any] = []
        self.committed: list[Any] = []

    def analyze(self, event: Any, *, defer_state_commit: bool = False) -> Outcome:
        self.analyzed.append(event)
        if self.raises is not None:
            raise self.raises
        return self.outcome

    def analyze_change_set(self, event: Any) -> Outcome:
        self.estimated.append(event)
        if self.change_set_raises is not None:
            raise self.change_set_raises
        return self.outcome

    def commit(self, outcome: Outcome) -> None:
        self.committed.append(outcome)


@pytest.fixture(autouse=True)
def _clean_container() -> Any:
    """Handlers cache their wiring at container scope. Without a reset, one
    test's fakes leak into the next."""
    handlers.reset_caches()
    yield
    handlers.reset_caches()


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace every external dependency with an inspectable fake."""
    analyzer = FakeAnalyzer()
    publisher = NullPublisher()
    metrics = NullMetrics()
    store = InMemoryIdempotencyStore()

    monkeypatch.setattr(handlers, "analyzer", lambda: analyzer)
    monkeypatch.setattr(handlers, "publisher", lambda: publisher)
    monkeypatch.setattr(handlers, "metrics", lambda: metrics)
    monkeypatch.setattr(handlers, "idempotency", lambda: store)
    monkeypatch.setattr(handlers, "config", lambda: handlers.RuntimeConfig())

    return {
        "analyzer": analyzer,
        "publisher": publisher,
        "metrics": metrics,
        "store": store,
    }


def stack_event(status: str = "UPDATE_COMPLETE", token: str = "token-1") -> dict[str, Any]:
    return {
        "detail-type": "CloudFormation Stack Status Change",
        "source": "aws.cloudformation",
        "account": "111122223333",
        "region": "us-east-1",
        "time": "2026-08-10T17:06:18Z",
        "detail": {
            "stack-id": STACK_ID,
            "status-details": {"status": status},
            "client-request-token": token,
        },
    }


def change_set_event() -> dict[str, Any]:
    return {
        "detail-type": "AWS API Call via CloudTrail",
        "source": "aws.cloudformation",
        "account": "111122223333",
        "region": "us-east-1",
        "detail": {
            "eventSource": "cloudformation.amazonaws.com",
            "eventName": "CreateChangeSet",
            "awsRegion": "us-east-1",
            "userIdentity": {"accountId": "111122223333"},
            "responseElements": {"id": CHANGE_SET_ID, "stackId": STACK_ID},
        },
    }


def sqs(*payloads: dict[str, Any]) -> dict[str, Any]:
    return {
        "Records": [
            {
                "messageId": f"m-{i}",
                "awsRegion": "us-east-1",
                "body": json.dumps(payload),
            }
            for i, payload in enumerate(payloads)
        ]
    }


# -- happy path -----------------------------------------------------------


def test_a_terminal_stack_event_is_analyzed_and_published(wired: dict[str, Any]) -> None:
    result = handlers.analyzer_handler(sqs(stack_event()))

    assert result == {"batchItemFailures": []}
    assert len(wired["analyzer"].analyzed) == 1
    assert len(wired["publisher"].published) == 1


def test_metrics_are_emitted_alongside_the_report(wired: dict[str, Any]) -> None:
    handlers.analyzer_handler(sqs(stack_event()))

    assert len(wired["metrics"].emitted) == 1


def test_a_change_set_event_takes_the_estimate_path(wired: dict[str, Any]) -> None:
    handlers.analyzer_handler(sqs(change_set_event()))

    assert len(wired["analyzer"].estimated) == 1
    assert wired["analyzer"].analyzed == []


def test_a_batch_of_several_messages_is_processed(wired: dict[str, Any]) -> None:
    event = sqs(
        stack_event(token="token-1"),
        stack_event(token="token-2"),
        stack_event(token="token-3"),
    )

    result = handlers.analyzer_handler(event)

    assert result == {"batchItemFailures": []}
    assert len(wired["publisher"].published) == 3


def test_an_empty_batch_is_handled(wired: dict[str, Any]) -> None:
    assert handlers.analyzer_handler({}) == {"batchItemFailures": []}


# -- filtering ------------------------------------------------------------


def test_a_non_terminal_status_is_discarded(wired: dict[str, Any]) -> None:
    """The rule already filters these, but a hand-edited rule must not be able to
    cause a mid-deployment read."""
    result = handlers.analyzer_handler(sqs(stack_event(status="UPDATE_IN_PROGRESS")))

    assert result == {"batchItemFailures": []}
    assert wired["analyzer"].analyzed == []


def test_a_malformed_body_is_dropped_rather_than_retried(wired: dict[str, Any]) -> None:
    """Retrying unparseable JSON just burns three attempts before the
    dead-letter queue."""
    event = {"Records": [{"messageId": "m-0", "body": "not json {"}]}

    result = handlers.analyzer_handler(event)

    assert result == {"batchItemFailures": []}


def test_a_message_with_no_body_is_dropped(wired: dict[str, Any]) -> None:
    result = handlers.analyzer_handler({"Records": [{"messageId": "m-0"}]})

    assert result == {"batchItemFailures": []}


def test_an_unrecognised_change_set_call_is_discarded(wired: dict[str, Any]) -> None:
    payload = change_set_event()
    payload["detail"]["eventName"] = "ExecuteChangeSet"

    result = handlers.analyzer_handler(sqs(payload))

    assert result == {"batchItemFailures": []}
    assert wired["analyzer"].estimated == []


# -- idempotency ----------------------------------------------------------


def test_a_duplicate_event_is_analyzed_once(wired: dict[str, Any]) -> None:
    """One deployment emits dozens of events carrying the same token."""
    handlers.analyzer_handler(sqs(stack_event(token="token-1")))
    handlers.analyzer_handler(sqs(stack_event(token="token-1")))

    assert len(wired["analyzer"].analyzed) == 1
    assert len(wired["publisher"].published) == 1


def test_distinct_deployments_are_both_analyzed(wired: dict[str, Any]) -> None:
    handlers.analyzer_handler(sqs(stack_event(token="token-1")))
    handlers.analyzer_handler(sqs(stack_event(token="token-2")))

    assert len(wired["analyzer"].analyzed) == 2


def test_a_failed_analysis_releases_its_claim(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The load-bearing assertion. Holding the claim through a transient failure
    turns a retryable error into a permanently missing report."""
    failing = FakeAnalyzer(raises=RuntimeError("pricing unavailable"))
    monkeypatch.setattr(handlers, "analyzer", lambda: failing)

    handlers.analyzer_handler(sqs(stack_event(token="token-1")))

    assert wired["store"].claimed == set()


def test_the_retry_after_a_failure_is_processed(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proves the release actually enables recovery rather than just clearing a
    set."""
    failing = FakeAnalyzer(raises=RuntimeError("transient"))
    monkeypatch.setattr(handlers, "analyzer", lambda: failing)
    handlers.analyzer_handler(sqs(stack_event(token="token-1")))

    healthy = FakeAnalyzer()
    monkeypatch.setattr(handlers, "analyzer", lambda: healthy)
    handlers.analyzer_handler(sqs(stack_event(token="token-1")))

    assert len(healthy.analyzed) == 1


def test_a_not_ready_change_set_releases_its_claim(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A change set still being computed is not a failure. The claim must go back
    so the redelivery can produce the estimate."""
    pending = FakeAnalyzer(change_set_raises=ChangeSetNotReady("CREATE_PENDING"))
    monkeypatch.setattr(handlers, "analyzer", lambda: pending)

    result = handlers.analyzer_handler(sqs(change_set_event()))

    assert wired["store"].claimed == set()
    assert result["batchItemFailures"] == [{"itemIdentifier": "m-0"}]


# -- partial batch failures -----------------------------------------------


def test_only_the_failing_message_is_returned(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without this, one bad message drags nine good reports to the DLQ."""
    calls = {"n": 0}
    outcome = Outcome()

    class Selective:
        def analyze(self, event: Any, *, defer_state_commit: bool = False) -> Outcome:
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("second one fails")
            return outcome

        def analyze_change_set(self, event: Any) -> Outcome:
            return outcome

        def commit(self, outcome: Outcome) -> None:
            return None

    monkeypatch.setattr(handlers, "analyzer", lambda: Selective())

    result = handlers.analyzer_handler(
        sqs(
            stack_event(token="token-1"),
            stack_event(token="token-2"),
            stack_event(token="token-3"),
        )
    )

    assert result["batchItemFailures"] == [{"itemIdentifier": "m-1"}]
    assert len(wired["publisher"].published) == 2


def test_a_failure_emits_a_metric(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AnalysisFailures is what the alarm watches, so it has to fire even when the
    event itself was the problem."""
    monkeypatch.setattr(
        handlers, "analyzer", lambda: FakeAnalyzer(raises=RuntimeError("boom"))
    )

    handlers.analyzer_handler(sqs(stack_event()))

    assert len(wired["metrics"].emitted) == 1
    assert wired["metrics"].emitted[0][0]["MetricName"] == "AnalysisFailures"


def test_the_failure_metric_carries_the_real_account_and_region(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read from the EventBridge envelope. Reporting the region where the account
    should be would make the metric unusable for finding the affected account."""
    monkeypatch.setattr(
        handlers, "analyzer", lambda: FakeAnalyzer(raises=RuntimeError("boom"))
    )

    handlers.analyzer_handler(sqs(stack_event()))

    dimensions = {
        d["Name"]: d["Value"] for d in wired["metrics"].emitted[0][0]["Dimensions"]
    }
    assert dimensions == {"Account": "111122223333", "Region": "us-east-1"}


def test_an_unparseable_message_still_yields_usable_dimensions(
    wired: dict[str, Any],
) -> None:
    """`_identity` runs on the same body that just failed to parse, so it must not
    raise."""
    account, region = handlers._identity(
        {"body": "not json {", "awsRegion": "eu-west-1"}, handlers.RuntimeConfig()
    )

    assert account == "unknown"
    assert region == "eu-west-1"


def test_identity_falls_back_to_unknown_with_nothing_to_go_on() -> None:
    account, region = handlers._identity({}, handlers.RuntimeConfig())

    assert (account, region) == ("unknown", "unknown")


# -- delivery decisions ---------------------------------------------------


def test_a_skipped_outcome_publishes_nothing(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Below the notify threshold, for instance."""
    monkeypatch.setattr(
        handlers,
        "analyzer",
        lambda: FakeAnalyzer(outcome=Outcome(reported=False, skipped="below threshold")),
    )

    handlers.analyzer_handler(sqs(stack_event()))

    assert wired["publisher"].published == []


def test_an_unreported_outcome_publishes_nothing(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        handlers, "analyzer", lambda: FakeAnalyzer(outcome=Outcome(reported=False))
    )

    handlers.analyzer_handler(sqs(stack_event()))

    assert wired["publisher"].published == []


def test_a_report_that_fails_reconciliation_is_still_published(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Withholding it would hide the problem. It is published, logged at error,
    and the metric drives the alarm."""
    report = {
        "reportId": "r-1",
        "stackId": STACK_ID,
        "reconciles": False,
        "totals": {"netMonthly": 1.0},
        "coverage": {},
    }
    monkeypatch.setattr(
        handlers, "analyzer", lambda: FakeAnalyzer(outcome=Outcome(report=report))
    )

    handlers.analyzer_handler(sqs(stack_event()))

    assert len(wired["publisher"].published) == 1


def test_a_publish_failure_returns_the_message_for_retry(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An analysis that succeeded but was never delivered is still a missing
    report."""

    class Exploding:
        def publish(self, report: dict[str, Any]) -> None:
            raise RuntimeError("SNS unavailable")

    monkeypatch.setattr(handlers, "publisher", lambda: Exploding())

    result = handlers.analyzer_handler(sqs(stack_event()))

    assert result["batchItemFailures"] == [{"itemIdentifier": "m-0"}]
    assert wired["store"].claimed == set()


# -- slack handler --------------------------------------------------------


def test_the_slack_handler_does_nothing_when_slack_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(handlers, "config", lambda: handlers.RuntimeConfig())
    monkeypatch.setattr(handlers, "slack_forwarder", lambda: None)

    assert handlers.slack_handler({"Records": []}) == {"delivered": 0}


def test_the_slack_handler_forwards_each_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[dict[str, Any]] = []

    class Forwarder:
        def send(self, report: dict[str, Any]) -> None:
            sent.append(report)

    monkeypatch.setattr(handlers, "config", lambda: handlers.RuntimeConfig())
    monkeypatch.setattr(handlers, "slack_forwarder", lambda: Forwarder())

    event = {
        "Records": [
            {"Sns": {"Message": json.dumps({"schemaVersion": handlers.SCHEMA_VERSION, "stackId": STACK_ID, "reportId": "r-1"})}},
            {"Sns": {"Message": json.dumps({"schemaVersion": handlers.SCHEMA_VERSION, "stackId": STACK_ID, "reportId": "r-2"})}},
        ]
    }

    assert handlers.slack_handler(event) == {"delivered": 2}
    assert len(sent) == 2


def test_the_slack_handler_skips_a_non_json_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subscription confirmation or a hand-published test message must not stop
    the real reports in the same batch."""
    sent: list[dict[str, Any]] = []

    class Forwarder:
        def send(self, report: dict[str, Any]) -> None:
            sent.append(report)

    monkeypatch.setattr(handlers, "config", lambda: handlers.RuntimeConfig())
    monkeypatch.setattr(handlers, "slack_forwarder", lambda: Forwarder())

    event = {
        "Records": [
            {"Sns": {"Message": "plain text"}},
            {"Sns": {"Message": json.dumps({"schemaVersion": handlers.SCHEMA_VERSION, "reportId": "r-1"})}},
        ]
    }

    assert handlers.slack_handler(event) == {"delivered": 1}


def test_the_slack_handler_skips_a_record_with_no_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A record with no Sns.Message must be skipped, not passed on as an empty
    report that renders as a $0 change."""
    sent: list[dict[str, Any]] = []

    class Forwarder:
        def send(self, report: dict[str, Any]) -> None:
            sent.append(report)

    monkeypatch.setattr(handlers, "config", lambda: handlers.RuntimeConfig())
    monkeypatch.setattr(handlers, "slack_forwarder", lambda: Forwarder())

    result = handlers.slack_handler({"Records": [{}, {"Sns": {}}]})

    assert result == {"delivered": 0}
    assert sent == []


def test_the_slack_handler_continues_past_a_slack_api_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Slack-side failure (bot not in channel, invalid token, rejected blocks)
    must not crash the invocation or abort the rest of the batch — the
    authoritative copy already went to SNS/email."""
    from runtime.slack import SlackApiError

    sent: list[dict[str, Any]] = []

    class Forwarder:
        def send(self, report: dict[str, Any]) -> None:
            if report.get("stackName") == "bad":
                raise SlackApiError("not_in_channel")
            sent.append(report)

    monkeypatch.setattr(handlers, "config", lambda: handlers.RuntimeConfig())
    monkeypatch.setattr(handlers, "slack_forwarder", lambda: Forwarder())

    event = {
        "Records": [
            {"Sns": {"Message": json.dumps({"schemaVersion": handlers.SCHEMA_VERSION, "reportId": "bad", "stackName": "bad"})}},
            {"Sns": {"Message": json.dumps({"schemaVersion": handlers.SCHEMA_VERSION, "reportId": "good", "stackName": "good"})}},
        ]
    }

    assert handlers.slack_handler(event) == {"delivered": 1}
    assert len(sent) == 1


def test_slack_forwarder_returns_none_on_a_malformed_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A secret that looks like JSON but is malformed must disable Slack, not
    crash the function on cold start."""
    handlers.reset_caches()
    cfg = handlers.RuntimeConfig(
        slack_secret_arn="arn:aws:secretsmanager:us-east-1:111122223333:secret:x",
        slack_channel_id="C123",
    )
    monkeypatch.setattr(handlers, "config", lambda: cfg)

    class FakeSecrets:
        def get_secret_value(self, SecretId: str) -> dict[str, Any]:
            return {"SecretString": "{not valid json"}

    class FakeBoto:
        def client(self, name: str) -> Any:
            return FakeSecrets()

    monkeypatch.setattr(handlers, "_boto3", lambda: FakeBoto())

    assert handlers.slack_forwarder() is None
    handlers.reset_caches()


# -- email handler (SES) --------------------------------------------------


def _ses_config() -> Any:
    return handlers.RuntimeConfig(
        notification_email="ops@example.com",
        ses_sender_email="reports@example.com",
    )


class FakeMailer:
    """Records what would have been sent, in place of the SES client."""

    def __init__(self, fail_for: str | None = None) -> None:
        self.sent: list[dict[str, str]] = []
        self._fail_for = fail_for

    def send(self, *, to: str, subject: str, html: str, text: str) -> str:
        from runtime.email import SesPermanentError

        if self._fail_for and self._fail_for in html:
            raise SesPermanentError("Email address is not verified")
        self.sent.append({"to": to, "subject": subject, "html": html, "text": text})
        return "msg-1"


def test_the_email_handler_does_nothing_when_ses_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(handlers, "config", lambda: handlers.RuntimeConfig())
    monkeypatch.setattr(handlers, "mailer", lambda: None)

    assert handlers.email_handler({"Records": []}) == {"delivered": 0}


def test_the_email_handler_sends_html_and_a_text_alternative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both parts go in one message: HTML for clients that render it, plain text
    for those that will not."""
    sender = FakeMailer()
    monkeypatch.setattr(handlers, "config", _ses_config)
    monkeypatch.setattr(handlers, "mailer", lambda: sender)

    report = {
        "schemaVersion": handlers.SCHEMA_VERSION,
        "stackId": STACK_ID,
        "stackName": "payments-api-prod",
        "reportId": "r-1",
        "action": "UPDATE",
        "direction": "INCREASE",
        "totals": {"netMonthly": 120.5, "netAnnual": 1446.0},
    }

    result = handlers.email_handler(
        {"Records": [{"Sns": {"Message": json.dumps(report)}}]}
    )

    assert result == {"delivered": 1}
    assert len(sender.sent) == 1
    message = sender.sent[0]
    assert message["to"] == "ops@example.com"
    assert message["html"].startswith("<!DOCTYPE html>")
    assert "payments-api-prod" in message["html"]
    assert "payments-api-prod" in message["text"]
    assert not message["text"].startswith("<")
    assert message["subject"]


def test_the_email_handler_sends_cost_reports_and_native_alarms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = FakeMailer()
    monkeypatch.setattr(handlers, "config", _ses_config)
    monkeypatch.setattr(handlers, "mailer", lambda: sender)

    event = {
        "Records": [
            {
                "Sns": {
                    "Message": json.dumps(
                        {
                            "schemaVersion": handlers.SCHEMA_VERSION,
                            "stackName": "a",
                            "reportId": "r-1",
                        }
                    )
                }
            },
            {
                "Sns": {
                    "Message": json.dumps(
                        {
                            "AlarmName": "AnalysisFailureAlarm",
                            "OldStateValue": "OK",
                            "NewStateValue": "ALARM",
                            "NewStateReason": "threshold crossed",
                        }
                    )
                }
            },
        ]
    }

    assert handlers.email_handler(event) == {"delivered": 2}
    assert len(sender.sent) == 2
    assert sender.sent[1]["subject"] == "[ALARM] AnalysisFailureAlarm"
    assert "threshold crossed" in sender.sent[1]["text"]


def test_the_email_handler_skips_a_non_json_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subscription confirmation must not stop the real reports in the batch."""
    sender = FakeMailer()
    monkeypatch.setattr(handlers, "config", _ses_config)
    monkeypatch.setattr(handlers, "mailer", lambda: sender)

    event = {
        "Records": [
            {"Sns": {"Message": "plain text"}},
            {"Sns": {"Message": json.dumps({"schemaVersion": handlers.SCHEMA_VERSION, "stackName": "a", "reportId": "r-a"})}},
        ]
    }

    assert handlers.email_handler(event) == {"delivered": 1}


def test_the_email_handler_skips_a_record_with_no_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = FakeMailer()
    monkeypatch.setattr(handlers, "config", _ses_config)
    monkeypatch.setattr(handlers, "mailer", lambda: sender)

    assert handlers.email_handler({"Records": [{}, {"Sns": {}}]}) == {"delivered": 0}
    assert sender.sent == []


def test_the_email_handler_continues_past_a_send_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A permanent identity rejection is recorded without aborting later records."""
    sender = FakeMailer(fail_for="bad-stack")
    monkeypatch.setattr(handlers, "config", _ses_config)
    monkeypatch.setattr(handlers, "mailer", lambda: sender)

    event = {
        "Records": [
            {"Sns": {"Message": json.dumps({"schemaVersion": handlers.SCHEMA_VERSION, "stackName": "bad-stack", "reportId": "bad"})}},
            {"Sns": {"Message": json.dumps({"schemaVersion": handlers.SCHEMA_VERSION, "stackName": "good-stack", "reportId": "good"})}},
        ]
    }

    assert handlers.email_handler(event) == {"delivered": 1}
    assert len(sender.sent) == 1


def test_the_mailer_is_none_until_ses_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handlers.reset_caches()
    monkeypatch.setattr(handlers, "config", lambda: handlers.RuntimeConfig())

    assert handlers.mailer() is None
    handlers.reset_caches()


def test_the_mailer_is_built_once_per_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handlers.reset_caches()
    builds = {"n": 0}

    class FakeBoto3:
        def client(self, name: str, **kwargs: Any) -> Any:
            builds["n"] += 1
            return object()

    monkeypatch.setattr(handlers, "config", _ses_config)
    monkeypatch.setattr(handlers, "_boto3", lambda: FakeBoto3())

    first = handlers.mailer()
    second = handlers.mailer()

    assert first is second
    assert builds["n"] == 1
    handlers.reset_caches()


# -- SES mailer -----------------------------------------------------------


def test_the_mailer_sends_both_bodies_in_one_ses_call() -> None:
    from runtime.email import SesMailer

    calls: list[dict[str, Any]] = []

    class FakeSes:
        def send_email(self, **kwargs: Any) -> dict[str, str]:
            calls.append(kwargs)
            return {"MessageId": "0100-abc"}

    mailer = SesMailer(FakeSes(), "reports@example.com")
    message_id = mailer.send(
        to="ops@example.com", subject="Subject", html="<p>hi</p>", text="hi"
    )

    assert message_id == "0100-abc"
    assert len(calls) == 1
    sent = calls[0]
    assert sent["Source"] == "reports@example.com"
    assert sent["Destination"] == {"ToAddresses": ["ops@example.com"]}
    assert sent["Message"]["Subject"]["Data"] == "Subject"
    assert sent["Message"]["Body"]["Html"]["Data"] == "<p>hi</p>"
    assert sent["Message"]["Body"]["Text"]["Data"] == "hi"


def test_the_mailer_wraps_an_ses_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """The caller catches SesDeliveryError, so a botocore exception must not
    escape and crash the invocation."""
    from runtime.email import SesDeliveryError, SesMailer

    class ExplodingSes:
        def send_email(self, **kwargs: Any) -> dict[str, str]:
            raise RuntimeError("Email address is not verified")

    mailer = SesMailer(ExplodingSes(), "reports@example.com")

    with pytest.raises(SesDeliveryError):
        mailer.send(to="ops@example.com", subject="s", html="<p>h</p>", text="h")


# -- container reuse ------------------------------------------------------


def test_wiring_is_built_once_per_container(monkeypatch: pytest.MonkeyPatch) -> None:
    """A warm invocation must not rebuild clients or discard the AMI and price
    caches."""
    builds = {"n": 0}

    class FakeBoto3:
        def client(self, name: str, **kwargs: Any) -> Any:
            builds["n"] += 1
            return object()

        def resource(self, name: str) -> Any:
            return type("R", (), {"Table": lambda self, name: object()})()

    monkeypatch.setattr(handlers, "_boto3", lambda: FakeBoto3())
    monkeypatch.setattr(
        handlers,
        "config",
        lambda: handlers.RuntimeConfig(topic_arn="arn:topic"),
    )

    first = handlers.publisher()
    second = handlers.publisher()

    assert first is second
    assert builds["n"] == 1


def test_no_destination_configured_discards_rather_than_crashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Misconfiguration should cost the report, not the function."""
    monkeypatch.setattr(handlers, "config", lambda: handlers.RuntimeConfig())

    publisher = handlers.publisher()
    publisher.publish({"schemaVersion": handlers.SCHEMA_VERSION, "reportId": "r-1"})

    assert isinstance(publisher, NullPublisher)


def test_metrics_can_be_turned_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        handlers, "config", lambda: handlers.RuntimeConfig(enable_metrics=False)
    )

    assert isinstance(handlers.metrics(), NullMetrics)


def test_reset_caches_clears_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handlers, "config", lambda: handlers.RuntimeConfig())
    handlers.publisher()

    handlers.reset_caches()

    assert handlers._PUBLISHER is None
    assert handlers._ANALYZER is None
    assert handlers._CONFIG is None


# -- plugin self-exclusion -----------------------------------------------


def test_the_plugin_stack_event_is_suppressed_before_any_side_effect(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Installing or updating the plugin must not produce a cost report about
    the reporting infrastructure itself."""
    monkeypatch.setattr(
        handlers,
        "config",
        lambda: handlers.RuntimeConfig(plugin_stack_id=STACK_ID),
    )

    result = handlers.analyzer_handler(sqs(stack_event()))

    assert result == {"batchItemFailures": []}
    assert wired["analyzer"].analyzed == []
    assert wired["publisher"].published == []
    assert wired["store"].claimed == set()


def test_the_plugin_stack_change_set_is_suppressed_before_any_side_effect(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same exclusion applies to the optional pre-deploy estimate path."""
    monkeypatch.setattr(
        handlers,
        "config",
        lambda: handlers.RuntimeConfig(plugin_stack_id=STACK_ID),
    )

    result = handlers.analyzer_handler(sqs(change_set_event()))

    assert result == {"batchItemFailures": []}
    assert wired["analyzer"].estimated == []
    assert wired["publisher"].published == []
    assert wired["store"].claimed == set()


def test_a_similarly_named_stack_with_a_different_id_is_not_suppressed(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exclusion is exact ARN equality, never stack-name or prefix matching."""
    monkeypatch.setattr(
        handlers,
        "config",
        lambda: handlers.RuntimeConfig(plugin_stack_id=STACK_ID),
    )
    payload = stack_event()
    payload["detail"]["stack-id"] = STACK_ID.rsplit("/", 1)[0] + "/different-id"

    result = handlers.analyzer_handler(sqs(payload))

    assert result == {"batchItemFailures": []}
    assert len(wired["analyzer"].analyzed) == 1
    assert len(wired["publisher"].published) == 1


def test_a_change_set_for_a_different_stack_id_is_not_suppressed(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        handlers,
        "config",
        lambda: handlers.RuntimeConfig(plugin_stack_id=STACK_ID),
    )
    payload = change_set_event()
    payload["detail"]["responseElements"]["stackId"] = (
        STACK_ID.rsplit("/", 1)[0] + "/different-id"
    )

    result = handlers.analyzer_handler(sqs(payload))

    assert result == {"batchItemFailures": []}
    assert len(wired["analyzer"].estimated) == 1
