"""Email delivery through Amazon SES.

Cost reports arrive through SNS as canonical JSON and are rendered into HTML plus
a plain-text alternative. Operational CloudWatch alarms use a separate topic but
share the same Lambda; their native fields are preserved in an alarm-specific
message rather than being rendered as a zero-cost report.

Retryable SES failures are allowed to escape the handler so Lambda's asynchronous
retry policy and delivery DLQ can recover them. Permanent identity/message
configuration failures are logged once and acknowledged.
"""

from __future__ import annotations

import html
from collections.abc import Mapping
from typing import Any

_CHARSET = "UTF-8"
_RETRYABLE_CODES = frozenset(
    {
        "InternalFailure",
        "RequestTimeout",
        "ServiceUnavailable",
        "Throttling",
        "ThrottlingException",
        "TooManyRequestsException",
    }
)
_PERMANENT_CODES = frozenset(
    {
        "AccountSendingPausedException",
        "ConfigurationSetDoesNotExistException",
        "MailFromDomainNotVerifiedException",
        "MessageRejected",
    }
)
_RETRYABLE_EXCEPTION_NAMES = frozenset(
    {
        "ConnectTimeoutError",
        "ConnectionClosedError",
        "EndpointConnectionError",
        "ReadTimeoutError",
    }
)


class SesDeliveryError(Exception):
    """Base class for SES delivery failures."""


class SesRetryableError(SesDeliveryError):
    """Temporary SES/network failure that should be retried."""


class SesPermanentError(SesDeliveryError):
    """Message or identity configuration that retrying cannot repair."""


def _error_code(exc: Exception) -> tuple[str, int]:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return type(exc).__name__, 0
    error = response.get("Error") or {}
    metadata = response.get("ResponseMetadata") or {}
    return str(error.get("Code") or type(exc).__name__), int(
        metadata.get("HTTPStatusCode") or 0
    )


def _delivery_error(exc: Exception) -> SesDeliveryError:
    code, status = _error_code(exc)
    message = f"{code}: {exc}"
    if code in _RETRYABLE_CODES or code in _RETRYABLE_EXCEPTION_NAMES or status >= 500:
        return SesRetryableError(message)
    if code in _PERMANENT_CODES or (400 <= status < 500 and status != 429):
        return SesPermanentError(message)
    # Unknown failures are retried: losing one email silently is worse than a
    # possible duplicate, and report IDs remain stable across analyzer retries.
    return SesRetryableError(message)


def is_alarm_message(payload: Mapping[str, Any]) -> bool:
    return bool(payload.get("AlarmName") and payload.get("NewStateValue"))


def alarm_subject(payload: Mapping[str, Any]) -> str:
    state = str(payload.get("NewStateValue") or "ALARM")
    name = str(payload.get("AlarmName") or "CloudWatch alarm")
    return f"[{state}] {name}"[:100]


def alarm_text(payload: Mapping[str, Any]) -> str:
    lines = [
        f"CloudWatch alarm: {payload.get('AlarmName') or 'unknown'}",
        f"State: {payload.get('OldStateValue') or '?'} -> {payload.get('NewStateValue') or '?'}",
    ]
    if payload.get("Region"):
        lines.append(f"Region: {payload['Region']}")
    if payload.get("StateChangeTime"):
        lines.append(f"Changed: {payload['StateChangeTime']}")
    if payload.get("NewStateReason"):
        lines.extend(["", str(payload["NewStateReason"])])
    return "\n".join(lines)


def _escape(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def alarm_html(payload: Mapping[str, Any]) -> str:
    name = _escape(payload.get("AlarmName") or "CloudWatch alarm")
    old = _escape(payload.get("OldStateValue") or "?")
    new = _escape(payload.get("NewStateValue") or "?")
    reason = _escape(payload.get("NewStateReason") or "")
    region = _escape(payload.get("Region") or "")
    changed = _escape(payload.get("StateChangeTime") or "")
    context = " &middot; ".join(part for part in (region, changed) if part)
    return (
        "<!DOCTYPE html><html><body style='margin:0;background:#f4f5f7;"
        "font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Arial,sans-serif;'>"
        "<table role='presentation' width='100%' cellpadding='0' cellspacing='0' "
        "style='padding:24px 12px;'><tr><td align='center'>"
        "<table role='presentation' width='640' cellpadding='0' cellspacing='0' "
        "style='max-width:640px;width:100%;background:#fff;border:1px solid #d5dbdb;'>"
        "<tr><td style='background:#d13212;color:#fff;padding:20px 24px;'>"
        f"<div style='font-size:12px;font-weight:700;'>CLOUDWATCH ALARM</div>"
        f"<div style='font-size:22px;font-weight:700;margin-top:4px;'>{name}</div></td></tr>"
        "<tr><td style='padding:20px 24px;color:#16191f;'>"
        f"<div style='font-size:18px;font-weight:700;'>{old} &rarr; {new}</div>"
        f"<div style='color:#5f6b7a;font-size:13px;margin-top:5px;'>{context}</div>"
        f"<div style='font-size:14px;line-height:1.5;margin-top:16px;'>{reason}</div>"
        "</td></tr></table></td></tr></table></body></html>"
    )


class SesMailer:
    """Minimal SES client that preserves retryability classification."""

    def __init__(self, client: Any, sender: str) -> None:
        self._client = client
        self._sender = sender

    def send(self, *, to: str, subject: str, html: str, text: str) -> str:
        """Send one email and return the SES message ID."""
        try:
            response = self._client.send_email(
                Source=self._sender,
                Destination={"ToAddresses": [to]},
                Message={
                    "Subject": {"Data": subject, "Charset": _CHARSET},
                    "Body": {
                        "Html": {"Data": html, "Charset": _CHARSET},
                        "Text": {"Data": text, "Charset": _CHARSET},
                    },
                },
            )
        except Exception as exc:
            raise _delivery_error(exc) from exc
        return str(response.get("MessageId") or "")
