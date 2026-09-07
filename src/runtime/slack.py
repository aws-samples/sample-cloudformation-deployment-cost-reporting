"""Slack delivery (S8) and the two-phase message merge (S7, S11).

An estimate posts as soon as a change set is created. When the confirmed report
arrives, the *same message* is edited via ``chat.update`` rather than a second one
posted — so the message sharpens instead of two competing for attention. That
requires remembering the message timestamp against the stack, which is what
:class:`MessageStore` is for.

HTTP goes through :mod:`urllib.request` rather than ``requests``, which is not in
the Lambda runtime. One less thing to package, one less thing to keep patched.

The bot token is read from Secrets Manager per invocation, cached for the life of
the container, and never logged.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from .logs import get_logger

logger = get_logger(__name__)

SLACK_API = "https://slack.com/api"
_TIMEOUT_SECONDS = 10

#: Direction to header emoji. Savings get equal visual weight to increases — a
#: teardown that saves $340/month is the most compelling output the plugin has
#: and should not read as a footnote (S29).
_DIRECTION_EMOJI = {"INCREASE": "🔴", "DECREASE": "🟢", "NEUTRAL": "⚪"}

_PHASE_BADGE = {"ESTIMATE": "⏳ Estimate", "CONFIRMED": "✅ Confirmed"}


def _money(value: Any, signed: bool = False, sign: int = 1) -> str:
    amount = float(value or 0) * sign
    if signed:
        prefix = "+" if amount >= 0 else "−"
        return f"{prefix}${abs(amount):,.2f}"
    return f"${amount:,.2f}"


def _mrkdwn(value: Any) -> str:
    text = str(value if value is not None else "")
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _resource_lines(
    resources: list[dict[str, Any]], signed: bool, sign: int = 1
) -> list[str]:
    lines = []
    for resource in resources:
        cost = resource.get("monthlyCost") or 0
        label = _mrkdwn(resource.get("description") or resource.get("resourceType") or "?")
        suffix = ""
        if resource.get("excluded"):
            suffix = f"  _{_mrkdwn(', '.join(resource['excluded']))} excluded_"
        lines.append(
            f"• `{resource.get('logicalId')}`  {label}  "
            f"*{_money(cost, signed=signed, sign=sign)}*{suffix}"
        )
    return lines


def render_blocks(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Render a report as Slack Block Kit.

    Empty sections are omitted rather than shown as headings with nothing under
    them.
    """
    totals = report.get("totals") or {}
    coverage = report.get("coverage") or {}
    basis = report.get("pricingBasis") or {}
    direction = report.get("direction") or "NEUTRAL"
    is_baseline = bool(report.get("isBaseline"))

    emoji = _DIRECTION_EMOJI.get(direction, "⚪")
    stack = report.get("stackName") or "unknown"

    if is_baseline:
        headline = f"📋 {stack} · now tracking · {_money(totals.get('currentStackMonthly'))}/month"
    else:
        headline = f"{emoji} {stack} · {_money(totals.get('netMonthly'), signed=True)}/month"

    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": headline[:150]}},
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": _PHASE_BADGE.get(
                        report.get("reportPhase", ""), report.get("reportPhase", "")
                    ),
                }
            ],
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Account*\n{report.get('account')}"},
                {"type": "mrkdwn", "text": f"*Region*\n{report.get('region')}"},
                {
                    "type": "mrkdwn",
                    "text": f"*Action*\n{str(report.get('action') or '').title()}",
                },
                {"type": "mrkdwn", "text": f"*When*\n{report.get('analyzedAt', '')[:19]}"},
            ],
        },
    ]

    if not report.get("reconciles", True):
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        ":warning: *Figures failed their arithmetic check.* "
                        "Treat the totals as unreliable."
                    ),
                },
            }
        )

    if is_baseline:
        inventory = report.get("inventory") or []
        if inventory:
            lines = _resource_lines(inventory, signed=False)
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "*Current inventory*\n" + "\n".join(lines[:15]),
                    },
                }
            )
    else:
        for title, key, sign in (
            ("➕ Added", "added", 1),
            ("➖ Removed", "removed", -1),
        ):
            resources = report.get(key) or []
            if not resources:
                continue
            subtotal = totals.get(f"{key}Monthly")
            lines = _resource_lines(resources, signed=True, sign=sign)
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*{title}  {_money(subtotal, signed=True, sign=sign)}/mo*\n"
                        + "\n".join(lines[:15]),
                    },
                }
            )

        changed = report.get("changed") or []
        if changed:
            lines = []
            for change in changed:
                before = change.get("before") or {}
                after = change.get("after") or {}
                marker = " ⚠️ replaced" if change.get("replacement") else ""
                lines.append(
                    f"• `{change.get('logicalId')}`  "
                    f"{before.get('description')} → {after.get('description')}  "
                    f"*{_money(change.get('deltaMonthly'), signed=True)}*{marker}"
                )
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*🔄 Resized  "
                        f"{_money(totals.get('changedMonthly'), signed=True)}/mo*\n"
                        + "\n".join(lines[:15]),
                    },
                }
            )

        retained = report.get("retained") or []
        if retained:
            lines = []
            for resource in retained:
                policy = resource.get("deletionPolicy") or resource.get(
                    "updateReplacePolicy"
                )
                lines.append(
                    f"• `{resource.get('logicalId')}`  "
                    f"{_mrkdwn(resource.get('resourceType'))}  "
                    f"*{_money(resource.get('monthlyCost'))}/mo*  "
                    f"_{_mrkdwn(policy)}_"
                )
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            "*📌 Retained — still billing  "
                            f"{_money(totals.get('retainedMonthly'))}/mo*\n"
                            + "\n".join(lines[:10])
                        ),
                    },
                }
            )

    blocks.append({"type": "divider"})

    if not is_baseline:
        summary = (
            f"*NET  {_money(totals.get('netMonthly'), signed=True)}/month*  ·  "
            f"{_money(totals.get('netAnnual'), signed=True)}/year\n"
            f"Stack total: {_money(totals.get('previousStackMonthly'))} → "
            f"*{_money(totals.get('currentStackMonthly'))}*/month"
        )
    else:
        summary = f"*Stack total {_money(totals.get('currentStackMonthly'))}/month*"

    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": summary}})

    # Coverage on every message. Stated gaps stay trusted; hidden gaps do not (S17).
    unpriced = report.get("unpriced") or {}
    context_lines = []

    usage_based = unpriced.get("usageBased") or []
    if usage_based:
        names = ", ".join(_mrkdwn(r.get("logicalId")) for r in usage_based[:5])
        context_lines.append(
            f"⚠️ {len(usage_based)} resource(s) depend on usage and were not priced: {names}"
        )

    unsupported = unpriced.get("unsupported") or []
    if unsupported:
        names = ", ".join(_mrkdwn(r.get("logicalId")) for r in unsupported[:5])
        context_lines.append(
            f"⚠️ {len(unsupported)} resource(s) have no pricing mapping: {names}"
        )

    unresolved = unpriced.get("unresolved") or []
    if unresolved:
        names = ", ".join(_mrkdwn(r.get("logicalId")) for r in unresolved[:5])
        context_lines.append(
            f"⚠️ {len(unresolved)} resource(s) could not be priced: {names}"
        )

    # Free resources are excluded from the denominator, matching how the plugin
    # computes coverage everywhere else.
    chargeable = sum(
        coverage.get(key, 0)
        for key in (
            "resourcesPriced",
            "resourcesUsageBased",
            "resourcesUnsupported",
            "resourcesUnresolved",
        )
    )
    discount = basis.get("discountPercent")
    discount_text = (
        f" · {discount}% discount" if discount else " · no discount applied"
    )
    context_lines.append(
        f"Priced {coverage.get('resourcesPriced', 0)} of {chargeable}"
        f" · {basis.get('rateType', 'On-Demand')}"
        f" · {basis.get('hoursPerMonth', 730)}h/mo{discount_text}"
    )

    for note in report.get("notes") or []:
        context_lines.append(f"• {_mrkdwn(note)}")

    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "\n".join(context_lines)[:3000]}],
        }
    )

    if report.get("consoleUrl"):
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "View stack"},
                        "url": report["consoleUrl"],
                    }
                ],
            }
        )

    # Slack rejects more than 50 blocks.
    return blocks[:50]


def fallback_text(report: dict[str, Any]) -> str:
    """Notification text, used where blocks cannot render."""
    totals = report.get("totals") or {}
    if report.get("isBaseline"):
        return (
            f"Now tracking {report.get('stackName')}: "
            f"{_money(totals.get('currentStackMonthly'))}/month"
        )
    return (
        f"{report.get('stackName')} {str(report.get('action') or '').lower()}: "
        f"{_money(totals.get('netMonthly'), signed=True)}/month"
    )


# -- message store, for the two-phase merge ------------------------------


class MessageStore(Protocol):
    """Remembers which Slack message belongs to which stack."""

    def get(self, stack_id: str) -> str | None:
        ...

    def put(self, stack_id: str, timestamp: str) -> None:
        ...

    def delete(self, stack_id: str) -> None:
        ...


@dataclass
class InMemoryMessageStore:
    messages: dict[str, str] = field(default_factory=dict)

    def get(self, stack_id: str) -> str | None:
        return self.messages.get(stack_id)

    def put(self, stack_id: str, timestamp: str) -> None:
        self.messages[stack_id] = timestamp

    def delete(self, stack_id: str) -> None:
        self.messages.pop(stack_id, None)


class DynamoMessageStore:
    """DynamoDB-backed, with a TTL so abandoned change sets expire."""

    def __init__(self, table: Any, ttl_hours: int = 24) -> None:
        self._table = table
        self._ttl_hours = ttl_hours

    def get(self, stack_id: str) -> str | None:
        try:
            response = self._table.get_item(Key={"pk": stack_id})
        except Exception:
            return None
        item = response.get("Item") or {}
        value = item.get("messageTs")
        return str(value) if value else None

    def put(self, stack_id: str, timestamp: str) -> None:

        expires_at = int(
            (datetime.now(UTC) + timedelta(hours=self._ttl_hours)).timestamp()
        )
        try:
            self._table.put_item(
                Item={"pk": stack_id, "messageTs": timestamp, "expiresAt": expires_at}
            )
        except Exception as exc:
            logger.warning(
                "Could not remember the Slack message timestamp",
                extra={"error": str(exc)},
            )

    def delete(self, stack_id: str) -> None:
        # A failure here leaves a stale timestamp, which the TTL removes.
        with suppress(Exception):
            self._table.delete_item(Key={"pk": stack_id})


# -- client ---------------------------------------------------------------


class SlackApiError(Exception):
    """Permanent Slack message/configuration error."""


class SlackRetryableError(SlackApiError):
    """Temporary Slack/network failure that should be retried."""


class SlackClient:
    """Minimal Slack Web API client.

    Slack returns HTTP 200 with ``{"ok": false, "error": "..."}`` for application
    errors, so the body is always inspected. Treating 200 as success would swallow
    ``channel_not_found`` and ``invalid_auth`` silently.
    """

    def __init__(self, token: str, timeout: int = _TIMEOUT_SECONDS) -> None:
        self._token = token
        self._timeout = timeout

    def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{SLACK_API}/{method}"

        # urlopen honours whatever scheme it is given, so `file:` or a custom
        # handler would be opened just as readily as HTTPS. The URL here is built
        # from a module constant and cannot be influenced by a report, but the
        # scheme is checked anyway so that stays true if the base ever becomes
        # configurable.
        if not url.startswith("https://"):
            raise SlackApiError(f"Refusing to call a non-HTTPS URL: {url}")

        request = urllib.request.Request(  # noqa: S310 - scheme checked above
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {self._token}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(  # noqa: S310 - scheme checked above
                request, timeout=self._timeout
            ) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            error_type = (
                SlackRetryableError
                if exc.code == 429 or exc.code >= 500
                else SlackApiError
            )
            raise error_type(f"{method} returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise SlackRetryableError(f"{method} could not be reached: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise SlackRetryableError(f"{method} returned a non-JSON body") from exc

        if not body.get("ok"):
            error = str(body.get("error") or "unknown_error")
            if error in {"fatal_error", "internal_error", "ratelimited", "service_unavailable"}:
                raise SlackRetryableError(f"{method} failed: {error}")
            raise SlackApiError(f"{method} failed: {error}")

        return body

    def post_message(
        self, channel: str, blocks: list[dict[str, Any]], text: str
    ) -> str:
        """Post a message and return its timestamp."""
        body = self._call(
            "chat.postMessage",
            {"channel": channel, "blocks": blocks, "text": text},
        )
        return str(body.get("ts") or "")

    def update_message(
        self, channel: str, timestamp: str, blocks: list[dict[str, Any]], text: str
    ) -> None:
        self._call(
            "chat.update",
            {"channel": channel, "ts": timestamp, "blocks": blocks, "text": text},
        )


class SlackForwarder:
    """Posts reports, editing an earlier estimate in place when one exists."""

    def __init__(
        self,
        client: SlackClient,
        channel: str,
        messages: MessageStore | None = None,
    ) -> None:
        self._client = client
        self._channel = channel
        self._messages = messages

    def send(self, report: dict[str, Any]) -> None:
        blocks = render_blocks(report)
        text = fallback_text(report)
        stack_id = str(report.get("stackId") or "")
        phase = report.get("reportPhase")

        existing = (
            self._messages.get(stack_id)
            if self._messages is not None and stack_id
            else None
        )

        if existing and phase == "CONFIRMED":
            try:
                self._client.update_message(self._channel, existing, blocks, text)
                logger.info(
                    "Estimate updated in place with confirmed figures",
                    extra={"messageTs": existing},
                )
            except SlackRetryableError:
                raise
            except SlackApiError as exc:
                # A permanent edit failure usually means the estimate was
                # deleted or aged out. Posting confirmed figures is preferable
                # to losing them; temporary failures are retried instead.
                logger.warning(
                    "Could not update the earlier message; posting a new one",
                    extra={"error": str(exc)},
                )
                self._post(blocks, text, stack_id, phase)
            else:
                if self._messages is not None and stack_id:
                    # The cycle is complete; the next estimate starts a new thread.
                    self._messages.delete(stack_id)
            return

        self._post(blocks, text, stack_id, phase)

    def _post(
        self,
        blocks: list[dict[str, Any]],
        text: str,
        stack_id: str,
        phase: Any,
    ) -> None:
        timestamp = self._client.post_message(self._channel, blocks, text)
        logger.info("Report posted to Slack", extra={"messageTs": timestamp})

        # Only an estimate is worth remembering — it is the message a later
        # confirmed report will edit.
        if (
            phase == "ESTIMATE"
            and timestamp
            and stack_id
            and self._messages is not None
        ):
            self._messages.put(stack_id, timestamp)
