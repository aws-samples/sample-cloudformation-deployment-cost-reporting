"""Report delivery.

The analyzer publishes once, to SNS (S21). Email, Slack, and anything added later
are subscribers, so a new destination is configuration rather than a code change.

An optional EventBridge publisher forwards to a central bus in a tooling account,
which is the path to organisation-wide rollout with one Slack integration and one
dashboard (S22).

Both transports cap message size. A report from a large nested application can
exceed it, so oversized reports are **trimmed rather than dropped**: identity,
totals, and coverage always survive, and a note records what was removed.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Protocol

from analyzer import message_attributes, render_email_body, subject_line

from .logs import get_logger

logger = get_logger(__name__)

#: SNS and EventBridge both cap payloads at 256KB. The margin covers the
#: attributes and envelope that sit alongside the body.
MAX_PAYLOAD_BYTES = 240_000

#: SNS hard limit for a single publish.
SNS_MESSAGE_LIMIT = 262_144

#: With MessageStructure="json" the JSON report travels as a *string* inside the
#: envelope, so its quotes are escaped and it inflates. The report is trimmed to
#: this smaller budget, and the email body is capped, so the combined envelope
#: (escaped JSON + email text + overhead) always stays under SNS_MESSAGE_LIMIT.
SNS_REPORT_BUDGET = 120_000
EMAIL_BODY_LIMIT = 30_000

#: Dropped in this order until the payload fits. Least useful first: the
#: per-resource detail goes before the numbers that summarise it.
_TRIMMABLE = (
    "inventory",
    "unpriced",
    "retained",
    "changed",
    "removed",
    "added",
)


def trim_for_transport(
    report: dict[str, Any], max_bytes: int = MAX_PAYLOAD_BYTES
) -> tuple[dict[str, Any], list[str]]:
    """Shrink a report until it fits, reporting what was removed.

    Returns the report and the list of dropped sections. Totals, coverage, and
    identity are never dropped, so an oversized report still says what the
    deployment cost — just not resource by resource.
    """
    body = json.dumps(report, default=str)
    if len(body.encode("utf-8")) <= max_bytes:
        return report, []

    trimmed = deepcopy(report)
    dropped: list[str] = []

    for section in _TRIMMABLE:
        if section not in trimmed:
            continue

        count = len(trimmed[section]) if isinstance(trimmed[section], list) else 1
        del trimmed[section]
        dropped.append(f"{section} ({count} entries)")

        body = json.dumps(trimmed, default=str)
        if len(body.encode("utf-8")) <= max_bytes:
            break

    notes = list(trimmed.get("notes") or [])
    notes.append(
        "Report exceeded the message size limit. Omitted: " + "; ".join(dropped) + ". "
        "Totals and coverage are unaffected."
    )
    trimmed["notes"] = notes
    trimmed["truncated"] = True

    return trimmed, dropped


class Publisher(Protocol):
    """Somewhere a finished report can be sent."""

    def publish(self, report: dict[str, Any]) -> None:
        ...


class NullPublisher:
    """Records instead of sending. For dry runs and tests."""

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    def publish(self, report: dict[str, Any]) -> None:
        self.published.append(report)


class SnsPublisher:
    """Publishes to an SNS topic with filterable attributes."""

    def __init__(self, client: Any, topic_arn: str) -> None:
        self._client = client
        self._topic_arn = topic_arn

    def publish(self, report: dict[str, Any]) -> None:
        payload, dropped = trim_for_transport(report, max_bytes=SNS_REPORT_BUDGET)
        if dropped:
            logger.warning(
                "Report trimmed to fit the SNS payload limit",
                extra={"droppedSections": dropped},
            )

        email_body = render_email_body(payload)
        if len(email_body) > EMAIL_BODY_LIMIT:
            email_body = email_body[:EMAIL_BODY_LIMIT].rstrip() + "\n… (truncated)"

        # MessageStructure="json" carries a different body per protocol from one
        # publish: a human-readable email, and the full JSON under "default" for
        # every programmatic subscriber (Lambda/Slack, SQS, and any added later).
        message = json.dumps(
            {"default": json.dumps(payload, default=str), "email": email_body}
        )

        self._client.publish(
            TopicArn=self._topic_arn,
            Subject=subject_line(payload),
            Message=message,
            MessageStructure="json",
            MessageAttributes=message_attributes(payload),
        )
        logger.info(
            "Report published to SNS",
            extra={
                "topicArn": self._topic_arn,
                "netMonthly": (payload.get("totals") or {}).get("netMonthly"),
            },
        )


class EventBridgePublisher:
    """Forwards to a central event bus, typically in a tooling account."""

    def __init__(
        self,
        client: Any,
        bus_arn: str,
        source: str = "cfn.cost.plugin",
        detail_type: str = "CloudFormation Cost Report",
    ) -> None:
        self._client = client
        self._bus_arn = bus_arn
        self._source = source
        self._detail_type = detail_type

    def publish(self, report: dict[str, Any]) -> None:
        payload, dropped = trim_for_transport(report)
        if dropped:
            logger.warning(
                "Report trimmed to fit the EventBridge payload limit",
                extra={"droppedSections": dropped},
            )

        response = self._client.put_events(
            Entries=[
                {
                    "EventBusName": self._bus_arn,
                    "Source": self._source,
                    "DetailType": self._detail_type,
                    "Detail": json.dumps(payload, default=str),
                }
            ]
        )

        # put_events reports per-entry failures in the response rather than
        # raising, so a silent drop is entirely possible without this check.
        if response.get("FailedEntryCount"):
            entry = (response.get("Entries") or [{}])[0]
            logger.error(
                "Central bus rejected the report",
                extra={
                    "errorCode": entry.get("ErrorCode"),
                    "errorMessage": entry.get("ErrorMessage"),
                },
            )
            raise RuntimeError(
                f"EventBridge rejected the report: {entry.get('ErrorCode')}"
            )

        logger.info("Report forwarded to central bus", extra={"busArn": self._bus_arn})


class CompositePublisher:
    """Publishes to several destinations.

    One failing destination does not prevent the others, but the first error is
    re-raised so the message is retried rather than silently half-delivered.
    """

    def __init__(self, *publishers: Publisher) -> None:
        self._publishers = [p for p in publishers if p is not None]

    def publish(self, report: dict[str, Any]) -> None:
        first_error: Exception | None = None

        for publisher in self._publishers:
            try:
                publisher.publish(report)
            except Exception as exc:
                logger.exception(
                    "Destination failed",
                    extra={"destination": type(publisher).__name__},
                )
                first_error = first_error or exc

        if first_error is not None:
            raise first_error

    def __len__(self) -> int:
        return len(self._publishers)
