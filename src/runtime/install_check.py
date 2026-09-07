"""Install-time CloudTrail check for the pre-deploy estimate path (Path B).

Path B is driven by CloudTrail's record of ``CreateChangeSet``, which EventBridge
only receives if a trail in this region is capturing management events. When no
such trail exists, nothing anywhere reports it: the rule deploys cleanly, matches
nothing, and stays silent forever. Someone eventually notices that estimates
never arrive and has no way to tell whether the feature is broken or simply
inactive.

This custom resource answers that question at install time.

**It never fails the deployment.** A missing trail should cost you the estimate
path, not your install. Every outcome — including an error inside this function —
returns SUCCESS with a ``Status`` describing what was found, which surfaces as a
stack output.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from .logs import configure_logging, get_logger

logger = get_logger(__name__)

_TIMEOUT_SECONDS = 15

#: Reported when a trail is capturing the management writes Path B needs.
ACTIVE = "ACTIVE"
#: A trail exists but is read-only, so ``CreateChangeSet`` is not captured.
READ_ONLY_ONLY = "INACTIVE_TRAIL_IS_READ_ONLY"
#: No trail covers this region.
NO_TRAIL = "INACTIVE_NO_TRAIL"
#: A trail exists but its selectors could not be read.
UNKNOWN = "UNKNOWN"


def handler(event: dict[str, Any], context: Any = None) -> None:
    """CloudFormation custom resource entry point.

    Always responds. An unhandled exception here would leave the stack waiting on
    a response for an hour before timing out, so every path is caught.
    """
    configure_logging()
    status = UNKNOWN
    reason = ""

    try:
        request_type = event.get("RequestType")

        if request_type == "Delete":
            # Nothing was created, so nothing needs undoing.
            _respond(event, context, status="SUCCESS", data={"Status": "DELETED"})
            return

        status, reason = check_trail()

        if status == ACTIVE:
            logger.info("Pre-deploy estimates are active", extra={"detail": reason})
        else:
            # Warning rather than error: the install succeeded and Path A is
            # unaffected. This is the line an operator greps for when estimates
            # never appear.
            logger.warning(
                "Pre-deploy estimates will not fire",
                extra={"status": status, "detail": reason},
            )

    except Exception as exc:
        # Being unable to check is not a reason to block an install.
        logger.exception("CloudTrail check failed; reporting UNKNOWN")
        status, reason = UNKNOWN, f"Check failed: {exc}"

    _respond(event, context, status="SUCCESS", data={"Status": status, "Detail": reason})


def check_trail(client: Any = None, region: str | None = None) -> tuple[str, str]:
    """Return ``(status, human-readable detail)`` for this region's trails.

    Args:
        client: A boto3 CloudTrail client. Injected by tests.
        region: Region to evaluate. Defaults to the Lambda's own region.
    """
    import os

    if client is None:
        import boto3

        client = boto3.client("cloudtrail")

    region = region or os.environ.get("AWS_REGION", "")

    # Shadow trails are this region's view of a multi-region or organisation
    # trail defined elsewhere. They are exactly what makes Path B work without a
    # local trail, so they must be included.
    trails = client.describe_trails(includeShadowTrails=True).get("trailList") or []

    if not trails:
        return NO_TRAIL, "No CloudTrail trail is visible from this region."

    relevant = [t for t in trails if _covers_region(t, region)]
    if not relevant:
        return (
            NO_TRAIL,
            f"{len(trails)} trail(s) exist but none cover {region or 'this region'}.",
        )

    read_only_seen = False
    unreadable = 0

    for trail in relevant:
        name = trail.get("TrailARN") or trail.get("Name") or ""
        try:
            selectors = client.get_event_selectors(TrailName=name)
        except Exception as exc:
            # Common with organisation trails, where the member account cannot
            # read the management account's selectors. Counted, not fatal.
            logger.info(
                "Could not read event selectors",
                extra={"trail": name, "error": str(exc)},
            )
            unreadable += 1
            continue

        verdict = _evaluate_selectors(selectors)
        if verdict is True:
            return ACTIVE, f"Trail {name} captures management write events."
        if verdict is False:
            read_only_seen = True

    if unreadable:
        return (
            UNKNOWN,
            f"{unreadable} trail(s) cover this region but their event selectors "
            "could not be read, which is normal for an organisation trail. "
            "Estimates may still work.",
        )

    if read_only_seen:
        return (
            READ_ONLY_ONLY,
            "A trail covers this region but only records read-only events, so "
            "CreateChangeSet is not captured.",
        )

    return (
        NO_TRAIL,
        "Trails cover this region but none include management events.",
    )


def _covers_region(trail: dict[str, Any], region: str) -> bool:
    """True when this trail delivers events for ``region``."""
    if trail.get("IsMultiRegionTrail"):
        return True
    if not region:
        # Cannot tell which region we are in, so no trail can be ruled out.
        return True
    return str(trail.get("HomeRegion") or "") == region


def _evaluate_selectors(selectors: dict[str, Any]) -> bool | None:
    """Whether these selectors capture management *writes*.

    Returns:
        True if management writes are captured, False if management events are
        captured but only read-only ones, and None if management events are not
        covered at all.
    """
    # Classic event selectors.
    for selector in selectors.get("EventSelectors") or []:
        if not selector.get("IncludeManagementEvents"):
            continue
        # ReadWriteType defaults to All when absent.
        read_write = str(selector.get("ReadWriteType") or "All")
        return read_write in ("All", "WriteOnly")

    # Advanced event selectors, which express the same thing as field filters.
    result: bool | None = None
    for selector in selectors.get("AdvancedEventSelectors") or []:
        fields = selector.get("FieldSelectors") or []
        if not _is_management_category(fields):
            continue
        if _excludes_writes(fields):
            result = False
            continue
        return True

    return result


def _is_management_category(fields: list[dict[str, Any]]) -> bool:
    for field in fields:
        if field.get("Field") != "eventCategory":
            continue
        if "Management" in (field.get("Equals") or []):
            return True
    return False


def _excludes_writes(fields: list[dict[str, Any]]) -> bool:
    """True when a selector is pinned to read-only events.

    ``readOnly: ["true"]`` captures reads only, so ``CreateChangeSet`` never
    appears. ``readOnly: ["false"]`` or an absent field both include writes.
    """
    for field in fields:
        if field.get("Field") != "readOnly":
            continue
        equals = [str(v).lower() for v in (field.get("Equals") or [])]
        if equals == ["true"]:
            return True
    return False


def _respond(
    event: dict[str, Any],
    context: Any,
    status: str,
    data: dict[str, Any] | None = None,
    reason: str | None = None,
) -> None:
    """PUT the result to CloudFormation's pre-signed callback URL.

    A failure to respond leaves the stack waiting an hour, so a send error is
    logged rather than raised — raising would guarantee the timeout it is trying
    to avoid.
    """
    url = event.get("ResponseURL")
    if not url:
        logger.error("Custom resource event has no ResponseURL; cannot respond")
        return

    physical_id = event.get("PhysicalResourceId") or (
        getattr(context, "log_stream_name", None) or "cfn-cost-plugin-install-check"
    )

    body = json.dumps(
        {
            "Status": status,
            "Reason": reason or "See CloudWatch Logs for detail",
            "PhysicalResourceId": physical_id,
            "StackId": event.get("StackId"),
            "RequestId": event.get("RequestId"),
            "LogicalResourceId": event.get("LogicalResourceId"),
            "NoEcho": False,
            "Data": data or {},
        }
    ).encode("utf-8")

    # The URL is generated by CloudFormation and always HTTPS, but the scheme is
    # asserted rather than assumed so this cannot become an open request if the
    # event is ever fed from somewhere else.
    if not str(url).startswith("https://"):
        logger.error("ResponseURL is not HTTPS; refusing to send")
        return

    request = urllib.request.Request(  # noqa: S310 - scheme checked above
        url,
        data=body,
        headers={"Content-Type": "", "Content-Length": str(len(body))},
        method="PUT",
    )

    last_error: Exception | None = None
    for attempt in range(3):
        try:
            urllib.request.urlopen(  # noqa: S310 - scheme checked above
                request, timeout=_TIMEOUT_SECONDS
            ).close()
            return
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            logger.warning(
                "Custom resource response failed; retrying",
                extra={"error": str(exc), "attempt": attempt + 1},
            )
            if attempt < 2:
                time.sleep(2**attempt)
    raise RuntimeError("Could not send custom resource response") from last_error
