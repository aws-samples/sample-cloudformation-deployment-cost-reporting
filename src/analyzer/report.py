"""Canonical report assembly.

One object that every delivery channel renders from — SNS, Slack, CloudWatch, and
the state store. Building it once means the email, the Slack message, and the
dashboard cannot disagree about what a deployment cost.
"""

from __future__ import annotations

import html
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from urllib.parse import quote

from pricing import PricingBasis
from state import StackDelta


class ReportPhase(str, Enum):
    """When the report was produced relative to the deployment.

    An ``ESTIMATE`` is priced from the template before resources exist, so it
    arrives within seconds of a change set being created — before money is
    committed. A ``CONFIRMED`` report is priced against what actually got created.
    """

    ESTIMATE = "ESTIMATE"
    CONFIRMED = "CONFIRMED"


SCHEMA_VERSION = "1.0"


def console_url(stack_id: str, region: str) -> str:
    """Deep link to the stack in the CloudFormation console (S26)."""
    return (
        f"https://{region}.console.aws.amazon.com/cloudformation/home"
        f"?region={region}#/stacks/stackinfo?stackId={quote(stack_id, safe='')}"
    )


def build_report(
    delta: StackDelta,
    *,
    phase: ReportPhase = ReportPhase.CONFIRMED,
    status: str | None = None,
    client_request_token: str | None = None,
    tags: Mapping[str, str] | None = None,
    pricing_basis: PricingBasis | None = None,
    root_stack_id: str | None = None,
    is_nested_stack: bool = False,
    notes: list[str] | None = None,
    report_id: str | None = None,
    analyzed_at: str | None = None,
) -> dict[str, Any]:
    """Assemble the canonical result.

    Everything cost-related comes from the delta; everything contextual comes from
    the event and the stack description.
    """
    payload: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "reportId": report_id or uuid.uuid4().hex,
        "reportPhase": phase.value,
        "clientRequestToken": client_request_token,
        "status": status,
        "analyzedAt": analyzed_at or datetime.now(UTC).isoformat(),
        "rootStackId": root_stack_id,
        "isNestedStack": is_nested_stack,
        "consoleUrl": console_url(delta.stack_id, delta.region),
        "tags": dict(tags or {}),
    }

    # The delta supplies action, identity, totals, coverage, unpriced buckets,
    # and either the added/removed/changed lists or a baseline inventory.
    payload.update(delta.to_dict())

    if pricing_basis is not None:
        payload["pricingBasis"] = pricing_basis.to_dict()
        payload["currency"] = pricing_basis.currency

    if notes:
        payload["notes"] = list(notes)

    return payload


def subject_line(report: Mapping[str, Any]) -> str:
    """SNS subject, cost first.

    SNS truncates at 100 characters, so the figure leads — it is the part that
    must survive (S27).
    """
    totals = report.get("totals") or {}
    net = totals.get("netMonthly", 0)
    action = str(report.get("action") or "").lower()
    stack = report.get("stackName") or "unknown"
    account = report.get("account") or ""
    region = report.get("region") or ""

    if report.get("isBaseline"):
        current = totals.get("currentStackMonthly", 0)
        head = f"[${current:,.2f}/mo baseline]"
    else:
        head = f"[{'+' if net >= 0 else '-'}${abs(net):,.2f}/mo]"

    subject = f"{head} {stack} {action} ({account}/{region})"
    return subject[:100]


def message_attributes(report: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """SNS message attributes, so subscribers filter without code (S23)."""
    totals = report.get("totals") or {}
    tags = report.get("tags") or {}

    attributes = {
        "account": report.get("account") or "",
        "region": report.get("region") or "",
        "action": report.get("action") or "",
        "direction": report.get("direction") or "",
        "stackName": report.get("stackName") or "",
        "reportPhase": report.get("reportPhase") or "",
        "isBaseline": "true" if report.get("isBaseline") else "false",
        "environment": tags.get("Environment", ""),
    }

    payload: dict[str, dict[str, str]] = {
        key: {"DataType": "String", "StringValue": value}
        for key, value in attributes.items()
        if value
    }
    payload["netMonthly"] = {
        "DataType": "Number",
        "StringValue": str(totals.get("netMonthly", 0)),
    }
    return payload


def _email_money(value: Any, signed: bool = False, sign: int = 1) -> str:
    """Format a number as USD for human-readable report bodies."""
    amount = float(value or 0) * sign
    if signed:
        prefix = "+" if amount >= 0 else "-"
        return f"{prefix}${abs(amount):,.2f}"
    return f"${amount:,.2f}"


def _email_resource_line(
    resource: Mapping[str, Any], *, signed: bool, sign: int = 1
) -> str:
    label = resource.get("description") or resource.get("resourceType") or "?"
    cost = _email_money(resource.get("monthlyCost"), signed=signed, sign=sign)
    excluded = resource.get("excluded") or []
    suffix = f"   ({', '.join(excluded)} excluded)" if excluded else ""
    return f"  {cost}/mo  {resource.get('logicalId')} — {label}{suffix}"


def _email_resource_lines(
    items: list[dict[str, Any]], *, signed: bool, sign: int = 1
) -> list[str]:
    """Render a resource list: the ones that cost money first (largest first),
    with the ``$0.00`` resources collapsed to a single count.

    A report's added/removed lists include free scaffolding (VPC, subnets, route
    tables, …) and any usage-based resources, all at ``$0.00``. Itemising them
    buries the handful that actually cost something, so they are summarised.
    """
    priced = [r for r in items if float(r.get("monthlyCost") or 0) != 0]
    zero = len(items) - len(priced)
    priced.sort(key=lambda r: abs(float(r.get("monthlyCost") or 0)), reverse=True)
    out = [_email_resource_line(r, signed=signed, sign=sign) for r in priced[:50]]
    if zero:
        out.append(f"  … and {zero} more at $0.00/mo")
    return out


def render_email_body(report: Mapping[str, Any]) -> str:
    """Human-readable plain-text rendering of a report, for the SNS email subscriber.

    SNS delivers email as plain text, so this is what an operator actually reads.
    The full JSON stays available to programmatic subscribers (Lambda/Slack, SQS)
    through the SNS message's ``default`` field; this is the ``email`` field. The
    sections mirror the Slack renderer, and every list is capped so an oversized
    report cannot produce an unbounded email.
    """
    totals = report.get("totals") or {}
    coverage = report.get("coverage") or {}
    basis = report.get("pricingBasis") or {}
    stack = report.get("stackName") or "unknown"
    action = str(report.get("action") or "").upper()
    is_baseline = bool(report.get("isBaseline"))

    lines: list[str] = []

    # -- header --
    if is_baseline:
        lines.append(f"{stack} — now tracking")
    else:
        lines.append(f"{stack} — {action}".rstrip(" —"))
        lines.append(
            f"Net change: {_email_money(totals.get('netMonthly'), signed=True)}/month"
            f"  ({_email_money(totals.get('netAnnual'), signed=True)}/year)"
        )

    context = f"Account {report.get('account') or '?'} · {report.get('region') or '?'}"
    when = str(report.get("analyzedAt") or "")[:19].replace("T", " ")
    if when:
        context += f" · {when} UTC"
    lines.append(context)

    phase = report.get("reportPhase")
    if phase:
        lines.append(f"Phase: {phase}")

    # A report that fails its own arithmetic is still delivered — withholding it
    # would hide the problem — but the reader has to know the totals below cannot
    # be trusted, which is otherwise only in the logs and the alarm.
    if not report.get("reconciles", True):
        lines.append("")
        lines.append(
            "WARNING: these figures failed their arithmetic check. The parts below do"
            " not sum to the net change, which means a resource was lost or"
            " double-counted. Treat the totals as unreliable."
        )

    # -- body --
    if is_baseline:
        inventory = report.get("inventory") or []
        if inventory:
            lines.append("")
            lines.append("Current inventory")
            lines.extend(_email_resource_lines(inventory, signed=False))
    else:
        for title, key, sign in (
            ("Added", "added", 1),
            ("Removed", "removed", -1),
        ):
            items = report.get(key) or []
            if not items:
                continue
            subtotal = totals.get(f"{key}Monthly")
            lines.append("")
            lines.append(
                f"{title} ({_email_money(subtotal, signed=True, sign=sign)}/mo)"
            )
            lines.extend(_email_resource_lines(items, signed=True, sign=sign))

        changed = report.get("changed") or []
        if changed:
            lines.append("")
            lines.append(
                f"Resized ({_email_money(totals.get('changedMonthly'), signed=True)}/mo)"
            )
            ordered_changes = sorted(
                changed,
                key=lambda c: abs(float(c.get("deltaMonthly") or 0)),
                reverse=True,
            )[:50]
            for change in ordered_changes:
                before = (change.get("before") or {}).get("description")
                after = (change.get("after") or {}).get("description")
                marker = "  [replaced]" if change.get("replacement") else ""
                lines.append(
                    f"  {_email_money(change.get('deltaMonthly'), signed=True)}/mo  "
                    f"{change.get('logicalId')} — {before} -> {after}{marker}"
                )

        retained = report.get("retained") or []
        if retained:
            lines.append("")
            lines.append(
                "Retained — still billing "
                f"({_email_money(totals.get('retainedMonthly'))}/mo)"
            )
            for resource in retained[:50]:
                policy = resource.get("deletionPolicy") or resource.get(
                    "updateReplacePolicy"
                )
                lines.append(
                    f"  {_email_money(resource.get('monthlyCost'))}/mo  "
                    f"{resource.get('logicalId')} — {resource.get('resourceType')}"
                    f" ({policy})"
                )

    # -- summary --
    lines.append("")
    if is_baseline:
        lines.append(f"Stack total: {_email_money(totals.get('currentStackMonthly'))}/month")
    else:
        lines.append(
            f"Stack total: {_email_money(totals.get('previousStackMonthly'))}"
            f" -> {_email_money(totals.get('currentStackMonthly'))}/month"
        )

    # -- coverage, matching how the plugin computes it everywhere else --
    chargeable = sum(
        int(coverage.get(key, 0) or 0)
        for key in (
            "resourcesPriced",
            "resourcesUsageBased",
            "resourcesUnsupported",
            "resourcesUnresolved",
        )
    )
    discount = basis.get("discountPercent")
    discount_text = f"{float(discount):g}% discount" if discount else "no discount"
    free_count = int(coverage.get("resourcesFree", 0) or 0)
    lines.append(
        f"Coverage: priced {coverage.get('resourcesPriced', 0)} of {chargeable}"
        f" · {free_count} free"
        f" · {basis.get('rateType', 'On-Demand')}"
        f" · {basis.get('hoursPerMonth', 730)}h/mo · {discount_text}"
    )

    unpriced = report.get("unpriced") or {}
    usage_based = unpriced.get("usageBased") or []
    if usage_based:
        names = ", ".join(str(r.get("logicalId")) for r in usage_based[:5])
        lines.append(f"Not priced (usage-based): {len(usage_based)} — {names}")
    unsupported = unpriced.get("unsupported") or []
    if unsupported:
        names = ", ".join(str(r.get("logicalId")) for r in unsupported[:5])
        lines.append(f"Not priced (no mapping): {len(unsupported)} — {names}")
    unresolved = unpriced.get("unresolved") or []
    if unresolved:
        names = ", ".join(str(r.get("logicalId")) for r in unresolved[:5])
        lines.append(f"Not priced (unresolved properties): {len(unresolved)} — {names}")

    version = basis.get("priceListVersion")
    if version:
        lines.append(
            f"Price list version {version}. Marginal On-Demand cost;"
            " may be lower with Savings Plans or Reserved Instances."
        )

    for note in report.get("notes") or []:
        lines.append(f"Note: {note}")

    if report.get("consoleUrl"):
        lines.append("")
        lines.append(f"View stack: {report['consoleUrl']}")

    return "\n".join(lines)


# -- HTML email (S8: rich delivery via SES) -------------------------------

#: Header colour by direction. Savings are green, increases red, everything
#: else the AWS console blue.
_DIRECTION_COLOR = {"INCREASE": "#d13212", "DECREASE": "#037f0c", "NEUTRAL": "#0972d3"}

_MUTED = "#5f6b7a"
_FAINT = "#8a8f98"
_BORDER = "#eaeded"


def _html_esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _html_resource_rows(
    items: list[dict[str, Any]], *, signed: bool, sign: int = 1
) -> str:
    """Table rows for a resource list: priced first (largest first), the $0.00
    ones collapsed into a single muted row."""
    priced = [r for r in items if float(r.get("monthlyCost") or 0) != 0]
    zero = len(items) - len(priced)
    priced.sort(key=lambda r: abs(float(r.get("monthlyCost") or 0)), reverse=True)

    rows = []
    for r in priced[:50]:
        label = _html_esc(r.get("description") or r.get("resourceType") or "?")
        excluded = r.get("excluded") or []
        excl = (
            f"<div style='color:{_FAINT};font-size:12px;margin-top:1px;'>"
            f"excludes {_html_esc(', '.join(excluded))}</div>"
            if excluded
            else ""
        )
        cost = _html_esc(
            _email_money(r.get("monthlyCost"), signed=signed, sign=sign)
        )
        rows.append(
            f"<tr>"
            f"<td style='padding:9px 0;border-bottom:1px solid {_BORDER};'>"
            f"<span style='font-weight:600;'>{_html_esc(r.get('logicalId'))}</span>"
            f"<div style='color:{_MUTED};font-size:13px;margin-top:1px;'>{label}</div>{excl}</td>"
            f"<td align='right' style='padding:9px 0;border-bottom:1px solid {_BORDER};"
            f"white-space:nowrap;font-weight:700;'>{cost}/mo</td>"
            f"</tr>"
        )
    if zero:
        rows.append(
            f"<tr><td colspan='2' style='padding:9px 0;color:{_FAINT};font-size:13px;'>"
            f"+ {zero} more at $0.00/mo (free / usage-based)</td></tr>"
        )
    return "".join(rows)


def _html_change_rows(changed: list[dict[str, Any]]) -> str:
    rows = []
    ordered = sorted(
        changed, key=lambda c: abs(float(c.get("deltaMonthly") or 0)), reverse=True
    )[:50]
    for change in ordered:
        before = _html_esc((change.get("before") or {}).get("description"))
        after = _html_esc((change.get("after") or {}).get("description"))
        marker = (
            f" <span style='color:{_DIRECTION_COLOR['INCREASE']};'>(replaced)</span>"
            if change.get("replacement")
            else ""
        )
        delta = _html_esc(_email_money(change.get("deltaMonthly"), signed=True))
        rows.append(
            f"<tr>"
            f"<td style='padding:9px 0;border-bottom:1px solid {_BORDER};'>"
            f"<span style='font-weight:600;'>{_html_esc(change.get('logicalId'))}</span>"
            f"<div style='color:{_MUTED};font-size:13px;margin-top:1px;'>"
            f"{before} &rarr; {after}{marker}</div></td>"
            f"<td align='right' style='padding:9px 0;border-bottom:1px solid {_BORDER};"
            f"white-space:nowrap;font-weight:700;'>{delta}/mo</td>"
            f"</tr>"
        )
    return "".join(rows)


def _html_unpriced(report: Mapping[str, Any]) -> str:
    """The coverage gaps, named.

    A count alone ("priced 6 of 10") says something is missing without saying
    what, which is the one thing that makes a coverage line untrustworthy. Each
    bucket is listed with the resources in it, capped so a large stack cannot
    produce an unbounded email (S17).
    """
    unpriced = report.get("unpriced") or {}
    rows = []
    for key, label in (
        ("usageBased", "depend on usage"),
        ("unsupported", "have no pricing mapping"),
        ("unresolved", "could not be priced"),
    ):
        items = unpriced.get(key) or []
        if not items:
            continue
        names = ", ".join(str(r.get("logicalId")) for r in items[:5])
        if len(items) > 5:
            names += f", and {len(items) - 5} more"
        rows.append(
            f"<div style='margin-top:5px;'><span style='font-weight:700;'>"
            f"{len(items)}</span> {_html_esc(label)}: {_html_esc(names)}</div>"
        )
    if not rows:
        return ""
    return (
        f"<tr><td style='padding:12px 24px;background:#fffaf0;"
        f"border-top:1px solid #ffd9a0;color:#7a4b00;font-size:13px;'>"
        f"<span style='font-weight:700;'>Not priced</span>{''.join(rows)}</td></tr>"
    )


def _html_reconcile_banner(report: Mapping[str, Any]) -> str:
    """Shown when the report failed its own arithmetic check.

    Such a report is still delivered — withholding it would hide the problem —
    but the reader has to know the totals below cannot be trusted, which is
    otherwise visible only in the logs and the ReconciliationFailures alarm.
    """
    if report.get("reconciles", True):
        return ""
    return (
        "<tr><td style='padding:12px 24px;background:#fdf3f1;"
        "border-bottom:1px solid #f5c6ba;color:#8b1a08;font-size:13px;'>"
        "<span style='font-weight:700;'>These figures failed their arithmetic check.</span> "
        "The parts below do not sum to the net change, which means a resource was "
        "lost or double-counted. Treat the totals as unreliable.</td></tr>"
    )


def _html_section(title: str, right: str, rows_html: str) -> str:
    if not rows_html:
        return ""
    # text-transform:none, because the heading is uppercased and would otherwise
    # render the subtotal's unit as "/MO".
    right_html = (
        f"<span style='float:right;font-weight:700;color:#16191f;text-transform:none;'>"
        f"{_html_esc(right)}</span>"
        if right
        else ""
    )
    return (
        f"<tr><td style='padding:18px 24px 0;'>"
        f"<div style='font-size:12px;font-weight:700;color:#16191f;text-transform:uppercase;"
        f"letter-spacing:.5px;border-bottom:2px solid {_BORDER};padding-bottom:6px;'>"
        f"{_html_esc(title)}{right_html}</div>"
        f"<table role='presentation' width='100%' cellpadding='0' cellspacing='0' "
        f"style='font-size:14px;color:#16191f;'>{rows_html}</table>"
        f"</td></tr>"
    )


def render_email_html(report: Mapping[str, Any]) -> str:
    """Render a report as an HTML email body, for delivery through SES.

    Layout is table-based with inline styles, the only combination email clients
    render reliably. The plain-text :func:`render_email_body` remains the text
    alternative sent alongside it.
    """
    totals = report.get("totals") or {}
    coverage = report.get("coverage") or {}
    basis = report.get("pricingBasis") or {}
    is_baseline = bool(report.get("isBaseline"))
    direction = str(report.get("direction") or "NEUTRAL")
    color = "#0972d3" if is_baseline else _DIRECTION_COLOR.get(direction, "#0972d3")

    stack = _html_esc(report.get("stackName") or "unknown")
    action = _html_esc(str(report.get("action") or "").title())
    phase = _html_esc(report.get("reportPhase") or "")
    account = _html_esc(report.get("account") or "")
    region = _html_esc(report.get("region") or "")
    when = _html_esc(str(report.get("analyzedAt") or "")[:19].replace("T", " "))

    if is_baseline:
        headline = _html_esc(f"{_email_money(totals.get('currentStackMonthly'))}/mo")
        sub = "now tracking"
    else:
        headline = _html_esc(f"{_email_money(totals.get('netMonthly'), signed=True)}/mo")
        sub = _html_esc(f"{_email_money(totals.get('netAnnual'), signed=True)}/yr")

    # Sections.
    sections = []
    if is_baseline:
        sections.append(
            _html_section(
                "Current inventory",
                "",
                _html_resource_rows(report.get("inventory") or [], signed=False),
            )
        )
    else:
        for title, key, sign in (
            ("Added", "added", 1),
            ("Removed", "removed", -1),
        ):
            items = report.get(key) or []
            if items:
                subtotal = _email_money(
                    totals.get(f"{key}Monthly"), signed=True, sign=sign
                )
                sections.append(
                    _html_section(
                        title,
                        f"{subtotal}/mo",
                        _html_resource_rows(items, signed=True, sign=sign),
                    )
                )
        changed = report.get("changed") or []
        if changed:
            subtotal = _email_money(totals.get("changedMonthly"), signed=True)
            sections.append(_html_section("Resized", f"{subtotal}/mo", _html_change_rows(changed)))
        retained = report.get("retained") or []
        if retained:
            rows_parts = []
            for resource in retained[:20]:
                policy = resource.get("deletionPolicy") or resource.get(
                    "updateReplacePolicy"
                )
                rows_parts.append(
                    f"<tr><td style='padding:7px 0;border-bottom:1px solid {_BORDER};'>"
                    f"<span style='font-weight:600;'>{_html_esc(resource.get('logicalId'))}</span> "
                    f"<span style='color:{_MUTED};font-size:13px;'>"
                    f"{_html_esc(resource.get('resourceType'))} &middot; {_html_esc(policy)}"
                    f"</span></td><td align='right' style='padding:7px 0;white-space:nowrap;'>"
                    f"{_html_esc(_email_money(resource.get('monthlyCost')))}/mo</td></tr>"
                )
            retained_total = _email_money(totals.get("retainedMonthly"))
            sections.append(
                _html_section(
                    "Retained — still billing",
                    f"{retained_total}/mo",
                    "".join(rows_parts),
                )
            )

    # Footer figures.
    chargeable = sum(
        int(coverage.get(k, 0) or 0)
        for k in (
            "resourcesPriced",
            "resourcesUsageBased",
            "resourcesUnsupported",
            "resourcesUnresolved",
        )
    )
    free_count = int(coverage.get("resourcesFree", 0) or 0)
    discount = basis.get("discountPercent")
    discount_text = f"{float(discount):g}% discount" if discount else "no discount"
    if is_baseline:
        stack_total = _html_esc(f"{_email_money(totals.get('currentStackMonthly'))}/mo")
    else:
        stack_total = _html_esc(
            f"{_email_money(totals.get('previousStackMonthly'))} "
            f"\u2192 {_email_money(totals.get('currentStackMonthly'))}/mo"
        )
    version = _html_esc(basis.get("priceListVersion") or "")
    version_text = f" &middot; list {version}" if version else ""

    notes_html = "".join(
        f"<div style='color:{_FAINT};font-size:12px;margin-top:3px;'>{_html_esc(note)}</div>"
        for note in report.get("notes") or []
    )

    console = report.get("consoleUrl")
    button = (
        f"<tr><td style='padding:4px 24px 24px;'>"
        f"<a href='{_html_esc(console)}' style='display:inline-block;background:#0972d3;"
        f"color:#ffffff;text-decoration:none;padding:10px 20px;border-radius:4px;"
        f"font-weight:600;font-size:14px;'>View stack &rarr;</a></td></tr>"
        if console
        else ""
    )

    phase_label = f" &middot; {phase}" if phase else ""

    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'></head>"
        "<body style='margin:0;padding:0;background:#f4f5f7;"
        "font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'>"
        "<table role='presentation' width='100%' cellpadding='0' cellspacing='0' "
        "style='background:#f4f5f7;padding:24px 12px;'><tr><td align='center'>"
        "<table role='presentation' width='640' cellpadding='0' cellspacing='0' "
        "style='max-width:640px;width:100%;background:#ffffff;border-radius:10px;"
        "overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.12);'>"
        f"<tr><td style='background:{color};padding:22px 24px;color:#ffffff;'>"
        f"<div style='font-size:12px;letter-spacing:.5px;text-transform:uppercase;opacity:.85;'>"
        f"CloudFormation Cost Delta{phase_label}</div>"
        f"<div style='font-size:20px;font-weight:700;margin-top:2px;'>{stack}</div>"
        f"<div style='font-size:30px;font-weight:800;margin-top:8px;line-height:1;'>{headline} "
        f"<span style='font-size:14px;font-weight:500;opacity:.85;'>{sub}</span></div></td></tr>"
        f"<tr><td style='padding:12px 24px;color:{_MUTED};font-size:13px;"
        f"border-bottom:1px solid {_BORDER};'>{action} &middot; {account} &middot; {region} "
        f"&middot; {when} UTC</td></tr>"
        f"{_html_reconcile_banner(report)}"
        f"{''.join(sections)}"
        f"{_html_unpriced(report)}"
        f"<tr><td style='padding:16px 24px;background:#fafbfc;border-top:1px solid {_BORDER};"
        f"color:#43505f;font-size:13px;'>"
        f"<div><span style='font-weight:700;'>Stack total</span>&nbsp;&nbsp;{stack_total}</div>"
        f"<div style='margin-top:5px;'><span style='font-weight:700;'>Coverage</span>&nbsp;&nbsp;"
        f"{coverage.get('resourcesPriced', 0)} of {chargeable} chargeable priced "
        f"&middot; {free_count} free</div>"
        f"<div style='margin-top:5px;'><span style='font-weight:700;'>Pricing</span>&nbsp;&nbsp;"
        f"{_html_esc(basis.get('rateType', 'On-Demand'))} &middot; "
        f"{_html_esc(basis.get('hoursPerMonth', 730))}h/mo &middot; "
        f"{discount_text}{version_text}</div>"
        f"<div style='margin-top:6px;color:{_FAINT};font-size:12px;'>Marginal On-Demand cost; "
        f"may be lower with Savings Plans or Reserved Instances.</div>{notes_html}</td></tr>"
        f"{button}"
        "</table>"
        "<div style='color:#8a8f98;font-size:11px;margin-top:12px;'>"
        "Automated report &middot; please do not reply</div>"
        "</td></tr></table></body></html>"
    )
