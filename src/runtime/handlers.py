"""Lambda entry points.

Four functions:

* :func:`analyzer_handler` — SQS batch of CloudFormation and CloudTrail events.
* :func:`price_sync_handler` — scheduled refresh of the price cache.
* :func:`slack_handler` — SNS subscriber that renders to Slack.
* :func:`email_handler` — SNS subscriber that renders an HTML email via SES.

Clients and wiring are built once per container and reused, so a warm invocation
pays no setup cost and the AMI and price caches survive between events.

The analyzer returns **partial batch failures**, so one bad message does not
force nine good ones to be reprocessed. That requires
``FunctionResponseTypes: [ReportBatchItemFailures]`` on the event source mapping,
which the SAM template sets.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from analyzer import (
    SCHEMA_VERSION,
    Analyzer,
    AnalyzerConfig,
    Boto3CloudFormationReader,
    ChangeSetNotReady,
    NotActionable,
    parse_change_set_event,
    parse_stack_event,
    render_email_body,
    render_email_html,
    subject_line,
)
from pricing import (
    Boto3ProductSource,
    DynamoPriceCatalog,
    DynamoPriceWriter,
    Ec2PlatformResolver,
    PriceSync,
)
from state import DynamoStateStore

from .config import RuntimeConfig
from .email import (
    SesMailer,
    SesPermanentError,
    SesRetryableError,
    alarm_html,
    alarm_subject,
    alarm_text,
    is_alarm_message,
)
from .idempotency import ClaimResult, DynamoIdempotencyStore, IdempotencyStore
from .install_check import _respond as respond_custom_resource
from .logs import configure_logging, get_logger, log_context
from .metrics import CloudWatchMetrics, MetricsPublisher, NullMetrics
from .publisher import (
    CompositePublisher,
    EventBridgePublisher,
    NullPublisher,
    Publisher,
    SnsPublisher,
)
from .slack import (
    DynamoMessageStore,
    SlackApiError,
    SlackClient,
    SlackForwarder,
    SlackRetryableError,
)

logger = get_logger(__name__)

_CONFIG: RuntimeConfig | None = None
_ANALYZER: Analyzer | None = None
_PUBLISHER: Publisher | None = None
_METRICS: MetricsPublisher | None = None
_IDEMPOTENCY: IdempotencyStore | None = None
_SLACK: SlackForwarder | None = None
_MAILER: SesMailer | None = None


class RetryLater(Exception):
    """Expected transient work that should return to SQS without failure metrics."""


def _boto3() -> Any:
    import boto3

    return boto3


def config() -> RuntimeConfig:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = RuntimeConfig.from_env()
        configure_logging(_CONFIG.log_level)
    return _CONFIG


def _table(name: str) -> Any:
    return _boto3().resource("dynamodb").Table(name)


def analyzer() -> Analyzer:
    """Build the analyzer once per container."""
    global _ANALYZER
    if _ANALYZER is not None:
        return _ANALYZER

    settings = config()
    boto3 = _boto3()

    platforms = (
        Ec2PlatformResolver(boto3.client("ec2"))
        if settings.enable_platform_lookup
        else None
    )

    _ANALYZER = Analyzer(
        reader=Boto3CloudFormationReader(boto3.client("cloudformation")),
        catalog=DynamoPriceCatalog(_table(settings.price_table)),
        store=DynamoStateStore(
            _table(settings.state_table),
            retention_days=settings.snapshot_retention_days,
        ),
        config=AnalyzerConfig(
            discount_percent=settings.discount_percent,
            rollup_nested_stacks=settings.rollup_nested_stacks,
            notify_threshold=settings.notify_threshold,
            max_nested_depth=settings.max_nested_depth,
        ),
        platforms=platforms,
    )
    return _ANALYZER


def publisher() -> Publisher:
    global _PUBLISHER
    if _PUBLISHER is not None:
        return _PUBLISHER

    settings = config()
    boto3 = _boto3()
    destinations: list[Publisher] = []

    if settings.topic_arn:
        destinations.append(SnsPublisher(boto3.client("sns"), settings.topic_arn))
    if settings.central_bus_enabled:
        destinations.append(
            EventBridgePublisher(boto3.client("events"), settings.central_bus_arn or "")
        )

    if not destinations:
        logger.warning("No publish destination configured; reports will be discarded")
        _PUBLISHER = NullPublisher()
    else:
        _PUBLISHER = CompositePublisher(*destinations)

    return _PUBLISHER


def metrics() -> MetricsPublisher:
    global _METRICS
    if _METRICS is None:
        settings = config()
        _METRICS = (
            CloudWatchMetrics(_boto3().client("cloudwatch"))
            if settings.enable_metrics
            else NullMetrics()
        )
    return _METRICS


def idempotency() -> IdempotencyStore:
    global _IDEMPOTENCY
    if _IDEMPOTENCY is None:
        _IDEMPOTENCY = DynamoIdempotencyStore(_table(config().idempotency_table))
    return _IDEMPOTENCY


# -- analyzer -------------------------------------------------------------


def analyzer_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Process a batch of SQS messages carrying EventBridge events."""
    settings = config()
    failures: list[dict[str, str]] = []

    records = event.get("Records") or []
    logger.info("Batch received", extra={"messageCount": len(records)})

    for record in records:
        message_id = record.get("messageId", "unknown")
        try:
            _handle_record(record)
        except RetryLater as reason:
            logger.info(
                "Message will be retried",
                extra={"messageId": message_id, "reason": str(reason)},
            )
            failures.append({"itemIdentifier": message_id})
        except Exception:
            logger.exception("Message failed", extra={"messageId": message_id})
            account, region = _identity(record, settings)
            metrics().emit_failure(account=account, region=region)
            failures.append({"itemIdentifier": message_id})

    # Only the failed messages return to the queue.
    return {"batchItemFailures": failures}


def _identity(record: dict[str, Any], settings: RuntimeConfig) -> tuple[str, str]:
    """Best-effort account and region for a failed message.

    Read from the EventBridge envelope, which carries both on Path A and Path B.
    The message may have failed precisely because it was unparseable, so this
    never raises: the dimensions fall back to the Lambda's own region and
    ``unknown``, which keeps the failure metric alarmable either way.
    """
    account = ""
    region = ""
    try:
        body = record.get("body")
        payload = json.loads(body) if isinstance(body, str) else (body or {})
        if isinstance(payload, dict):
            account = str(payload.get("account") or "")
            region = str(payload.get("region") or "")
    except (json.JSONDecodeError, TypeError):
        pass

    if not region:
        region = record.get("awsRegion") or (
            settings.sync_regions[0] if settings.sync_regions else ""
        )

    return account or "unknown", region or "unknown"


def _handle_record(record: dict[str, Any]) -> None:
    body = record.get("body")
    if not body:
        logger.warning("Message has no body; discarding")
        return

    try:
        payload = json.loads(body) if isinstance(body, str) else body
    except json.JSONDecodeError:
        # Malformed beyond recovery. Retrying will not help, so it is dropped
        # rather than cycled to the dead-letter queue.
        logger.warning("Message body is not JSON; discarding")
        return

    detail_type = payload.get("detail-type")

    if detail_type == "AWS API Call via CloudTrail":
        _handle_change_set(payload)
    else:
        _handle_stack_event(payload)


def _is_plugin_stack(stack_id: str) -> bool:
    """Whether an event belongs to this plugin's own deployment stack.

    The full stack ARN is injected by CloudFormation. Exact equality is
    deliberate: similarly named stacks, nested stacks, and a later stack
    incarnation with the same name must remain eligible for reports.
    """
    plugin_stack_id = config().plugin_stack_id
    return bool(plugin_stack_id) and stack_id == plugin_stack_id


def _handle_stack_event(payload: dict[str, Any]) -> None:
    try:
        stack_event = parse_stack_event(payload)
    except NotActionable as reason:
        logger.debug("Event not actionable", extra={"reason": str(reason)})
        return

    if _is_plugin_stack(stack_event.stack_id):
        logger.debug(
            "Plugin stack event suppressed",
            extra={"stackId": stack_event.stack_id},
        )
        return

    with log_context(
        stackId=stack_event.stack_id,
        stackName=stack_event.stack_name,
        status=stack_event.status,
        dedupeKey=stack_event.dedupe_key,
    ):
        store = idempotency()
        claim = store.claim(stack_event.dedupe_key)
        if claim is ClaimResult.COMPLETE:
            return
        if claim is ClaimResult.BUSY:
            raise RetryLater("Another invocation still owns this stack event")

        try:
            outcome = analyzer().analyze(stack_event, defer_state_commit=True)
            _deliver(outcome, stack_event.account, stack_event.region)
            store.complete(stack_event.dedupe_key)
        except Exception:
            # Released so an SQS retry can take the claim. Holding it would turn a
            # transient failure into a permanently missing report.
            store.release(stack_event.dedupe_key)
            raise


def _handle_change_set(payload: dict[str, Any]) -> None:
    try:
        change_set_event = parse_change_set_event(payload)
    except NotActionable as reason:
        logger.debug("Not a change set event", extra={"reason": str(reason)})
        return

    if _is_plugin_stack(change_set_event.stack_id):
        logger.debug(
            "Plugin stack change set suppressed",
            extra={"stackId": change_set_event.stack_id},
        )
        return

    with log_context(
        stackId=change_set_event.stack_id,
        changeSetId=change_set_event.change_set_id,
        dedupeKey=change_set_event.dedupe_key,
        phase="ESTIMATE",
    ):
        store = idempotency()
        claim = store.claim(change_set_event.dedupe_key)
        if claim is ClaimResult.COMPLETE:
            return
        if claim is ClaimResult.BUSY:
            raise RetryLater("Another invocation still owns this change set")

        try:
            outcome = analyzer().analyze_change_set(change_set_event)
            _deliver(outcome, change_set_event.account, change_set_event.region)
            store.complete(change_set_event.dedupe_key)
        except ChangeSetNotReady as reason:
            # Expected readiness polling: release the lease and return the record
            # to SQS without counting an analysis failure.
            store.release(change_set_event.dedupe_key)
            raise RetryLater(str(reason)) from reason
        except Exception:
            store.release(change_set_event.dedupe_key)
            raise


def _deliver(outcome: Any, account: str, region: str) -> None:
    analysis = analyzer()
    if outcome.skipped:
        # Threshold suppression still advances the confirmed baseline. Other
        # skipped outcomes carry no pending mutation, so commit is a no-op.
        analysis.commit(outcome)
        logger.info("No report published", extra={"reason": outcome.skipped})
        return

    if not outcome.reported:
        analysis.commit(outcome)
        return

    report = outcome.report
    with log_context(reportId=report.get("reportId")):
        if not report.get("reconciles", True):
            logger.error(
                "Report failed its reconciliation check",
                extra={"totals": report.get("totals")},
            )

        # SNS is the durable hand-off. Confirmed state is committed only after it
        # accepts the exact report, so a publish failure can be retried against the
        # original baseline instead of recomputing a zero delta.
        publisher().publish(report)
        analysis.commit(outcome)
        metrics().emit(report)


# -- price sync -----------------------------------------------------------


def price_sync_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Refresh and atomically activate one complete price-cache generation."""
    is_custom_resource = bool(event.get("RequestType"))
    if is_custom_resource and event.get("RequestType") == "Delete":
        respond_custom_resource(
            event,
            context,
            status="SUCCESS",
            data={"Status": "DELETED"},
        )
        return {"status": "DELETED"}

    try:
        summary = _run_price_sync()
    except Exception as exc:
        if is_custom_resource:
            respond_custom_resource(
                event,
                context,
                status="FAILED",
                data={"Status": "FAILED", "Reason": str(exc)},
                reason=str(exc),
            )
        raise

    if is_custom_resource:
        healthy = bool(summary.get("healthy"))
        respond_custom_resource(
            event,
            context,
            status="SUCCESS" if healthy else "FAILED",
            data={
                "Status": "HEALTHY" if healthy else "UNHEALTHY",
                "Generation": str(summary.get("activeGeneration") or ""),
                "PriceCount": str(summary.get("pricesWritten") or 0),
            },
            reason=None if healthy else "Price sync did not produce a complete generation",
        )
    return summary


def _run_price_sync() -> dict[str, Any]:
    settings = config()
    boto3 = _boto3()

    # The Price List API is reachable from a small number of regions, so the
    # client region is independent of the region whose prices are wanted.
    source = Boto3ProductSource(boto3.client("pricing", region_name="us-east-1"))
    table = _table(settings.price_table)

    regions = list(settings.sync_regions) or ["us-east-1"]
    generation = uuid4().hex
    summary: dict[str, Any] = {"regions": {}, "healthy": True}
    versions: set[str] = set()
    total_written = 0

    for region in regions:
        with log_context(syncRegion=region):
            writer = DynamoPriceWriter(table, generation=generation)
            with writer:
                report = PriceSync().run(source, region, writer)

            summary["regions"][region] = report.to_dict()
            total_written += writer.written
            if report.version:
                versions.add(report.version)

            if report.healthy:
                logger.info("Price sync region complete", extra=report.to_dict())
            else:
                summary["healthy"] = False
                logger.error(
                    "Price sync generation incomplete; keeping previous generation active",
                    extra=report.to_dict(),
                )

    summary["pricesWritten"] = total_written
    if summary["healthy"] and total_written:
        metadata = DynamoPriceWriter(table, generation=generation)
        metadata.written = total_written
        metadata.write_metadata(",".join(sorted(versions)) or "unknown")
        summary["activeGeneration"] = generation
        logger.info(
            "Price sync generation activated",
            extra={"generation": generation, "priceCount": total_written},
        )
    else:
        summary["healthy"] = False
        summary["activeGeneration"] = None

    return summary


# -- slack ----------------------------------------------------------------


def slack_forwarder() -> SlackForwarder | None:
    global _SLACK
    if _SLACK is not None:
        return _SLACK

    settings = config()
    if not settings.slack_enabled:
        return None

    boto3 = _boto3()
    secret = boto3.client("secretsmanager").get_secret_value(
        SecretId=settings.slack_secret_arn
    )
    raw = secret.get("SecretString") or ""

    # Accept either a bare token or a JSON document with a token field, since
    # both are common ways to store one.
    token = raw.strip()
    if token.startswith("{"):
        try:
            parsed = json.loads(token)
        except json.JSONDecodeError:
            logger.error("Slack secret is not valid JSON; disabling Slack")
            return None
        token = parsed.get("token") or parsed.get("botToken") or ""

    if not token:
        logger.error("Slack secret contains no token")
        return None

    messages = (
        DynamoMessageStore(_table(settings.slack_message_table))
        if settings.slack_message_table
        else None
    )

    _SLACK = SlackForwarder(
        SlackClient(token), settings.slack_channel_id or "", messages
    )
    return _SLACK


def slack_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """SNS subscriber that renders reports into Slack."""
    config()
    forwarder = slack_forwarder()
    if forwarder is None:
        logger.info("Slack is not configured; nothing to do")
        return {"delivered": 0}

    delivered = 0
    for record in event.get("Records") or []:
        message = (record.get("Sns") or {}).get("Message")
        if not message:
            continue
        try:
            report = json.loads(message)
        except json.JSONDecodeError:
            logger.warning("SNS message is not JSON; discarding")
            continue
        if not isinstance(report, dict) or report.get("schemaVersion") != SCHEMA_VERSION:
            logger.warning("SNS message is not a canonical cost report; discarding")
            continue

        with log_context(
            stackId=report.get("stackId"),
            reportId=report.get("reportId"),
            phase=report.get("reportPhase"),
        ):
            try:
                forwarder.send(report)
            except SlackRetryableError as exc:
                logger.warning(
                    "Temporary Slack delivery failure; Lambda will retry",
                    extra={"error": str(exc)},
                )
                raise
            except SlackApiError as exc:
                # Permanent configuration/message errors cannot be repaired by
                # retrying the same payload; record and acknowledge them.
                logger.error(
                    "Slack delivery failed; report not posted to Slack",
                    extra={"error": str(exc)},
                )
                continue
            delivered += 1

    return {"delivered": delivered}


# -- email ----------------------------------------------------------------


def mailer() -> SesMailer | None:
    global _MAILER
    if _MAILER is not None:
        return _MAILER

    settings = config()
    if not settings.ses_enabled:
        return None

    _MAILER = SesMailer(_boto3().client("ses"), settings.ses_sender_email or "")
    return _MAILER


def email_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """SNS subscriber for canonical cost reports and native alarm messages."""
    settings = config()
    sender = mailer()
    if sender is None:
        logger.info("SES email is not configured; nothing to do")
        return {"delivered": 0}

    recipient = settings.notification_email or ""
    delivered = 0
    for record in event.get("Records") or []:
        message = (record.get("Sns") or {}).get("Message")
        if not message:
            continue
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            logger.warning("SNS message is not JSON; discarding")
            continue
        if not isinstance(payload, dict):
            logger.warning("SNS message is not a JSON object; discarding")
            continue

        if is_alarm_message(payload):
            subject = alarm_subject(payload)
            html_body = alarm_html(payload)
            text_body = alarm_text(payload)
        elif payload.get("schemaVersion") == SCHEMA_VERSION and payload.get("reportId"):
            subject = subject_line(payload)
            html_body = render_email_html(payload)
            text_body = render_email_body(payload)
        else:
            logger.warning("SNS JSON is neither a cost report nor an alarm; discarding")
            continue

        with log_context(
            stackId=payload.get("stackId"),
            reportId=payload.get("reportId"),
            phase=payload.get("reportPhase"),
        ):
            try:
                sender.send(
                    to=recipient,
                    subject=subject,
                    html=html_body,
                    text=text_body,
                )
            except SesPermanentError as exc:
                logger.error(
                    "Permanent SES delivery failure; message not emailed",
                    extra={"error": str(exc)},
                )
                continue
            except SesRetryableError as exc:
                logger.warning(
                    "Temporary SES delivery failure; Lambda will retry",
                    extra={"error": str(exc)},
                )
                raise
            delivered += 1

    return {"delivered": delivered}


def reset_caches() -> None:
    """Clear the container-level singletons. For tests."""
    global _CONFIG, _ANALYZER, _PUBLISHER, _METRICS, _IDEMPOTENCY, _SLACK, _MAILER
    _CONFIG = _ANALYZER = _PUBLISHER = _METRICS = _IDEMPOTENCY = _SLACK = _MAILER = None
