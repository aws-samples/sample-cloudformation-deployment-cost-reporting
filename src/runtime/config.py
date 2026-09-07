"""Runtime configuration from environment variables.

Every value maps to a parameter on the SAM template, so what an operator typed at
install time is what the Lambda reads. Parsing is tolerant: a malformed number
falls back to its default and logs, because an unreadable environment variable
should degrade one behaviour rather than crash the function on cold start.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .logs import get_logger

logger = get_logger(__name__)

_TRUTHY = frozenset({"1", "true", "yes", "on", "enabled"})


def _decimal(env: Mapping[str, str], name: str, default: str) -> Decimal:
    raw = env.get(name, "").strip()
    if not raw:
        return Decimal(default)
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        logger.warning(
            "Ignoring malformed numeric setting",
            extra={"setting": name, "value": raw, "usingDefault": default},
        )
        return Decimal(default)


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "Ignoring malformed integer setting",
            extra={"setting": name, "value": raw, "usingDefault": default},
        )
        return default


def _bool(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def _optional(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name, "").strip()
    return value or None


@dataclass(frozen=True)
class RuntimeConfig:
    """Everything the handlers need to know about their deployment."""

    # Storage
    price_table: str = ""
    state_table: str = ""
    idempotency_table: str = ""
    slack_message_table: str = ""

    # Deployment identity
    # Full ARN of the CloudFormation stack that installed this plugin. Events
    # for this one exact stack are suppressed so an install or update does not
    # generate a report about the reporting infrastructure itself.
    plugin_stack_id: str | None = None

    # Destinations
    topic_arn: str | None = None
    central_bus_arn: str | None = None

    # Pricing behaviour
    discount_percent: Decimal = Decimal(0)
    enable_platform_lookup: bool = False

    # Reporting behaviour
    notify_threshold: Decimal = Decimal(0)
    rollup_nested_stacks: bool = True
    max_nested_depth: int = 5
    snapshot_retention_days: int = 90
    enable_metrics: bool = True

    # Slack
    slack_secret_arn: str | None = None
    slack_channel_id: str | None = None

    # Email (SES)
    notification_email: str | None = None
    ses_sender_email: str | None = None

    # Price sync
    sync_regions: tuple[str, ...] = ()

    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> RuntimeConfig:
        env = env if env is not None else os.environ

        raw_regions = env.get("SYNC_REGIONS", "").strip()
        regions = tuple(
            part.strip() for part in raw_regions.split(",") if part.strip()
        ) or ((env.get("AWS_REGION", "").strip(),) if env.get("AWS_REGION") else ())

        return cls(
            price_table=env.get("PRICE_TABLE", "").strip(),
            state_table=env.get("STATE_TABLE", "").strip(),
            idempotency_table=env.get("IDEMPOTENCY_TABLE", "").strip(),
            slack_message_table=env.get("SLACK_MESSAGE_TABLE", "").strip(),
            plugin_stack_id=_optional(env, "PLUGIN_STACK_ID"),
            topic_arn=_optional(env, "TOPIC_ARN"),
            central_bus_arn=_optional(env, "CENTRAL_BUS_ARN"),
            discount_percent=_decimal(env, "DISCOUNT_PERCENT", "0"),
            enable_platform_lookup=_bool(env, "ENABLE_PLATFORM_LOOKUP", False),
            notify_threshold=_decimal(env, "NOTIFY_THRESHOLD", "0"),
            rollup_nested_stacks=env.get("NESTED_STACK_HANDLING", "ROLLUP").upper()
            != "SEPARATE",
            max_nested_depth=_int(env, "MAX_NESTED_DEPTH", 5),
            snapshot_retention_days=_int(env, "SNAPSHOT_RETENTION_DAYS", 90),
            enable_metrics=_bool(env, "ENABLE_METRICS", True),
            slack_secret_arn=_optional(env, "SLACK_SECRET_ARN"),
            slack_channel_id=_optional(env, "SLACK_CHANNEL_ID"),
            notification_email=_optional(env, "NOTIFICATION_EMAIL"),
            ses_sender_email=_optional(env, "SES_SENDER_EMAIL"),
            sync_regions=regions,
            log_level=env.get("LOG_LEVEL", "INFO").strip() or "INFO",
        )

    @property
    def slack_enabled(self) -> bool:
        return bool(self.slack_secret_arn and self.slack_channel_id)

    @property
    def ses_enabled(self) -> bool:
        return bool(self.ses_sender_email and self.notification_email)

    @property
    def central_bus_enabled(self) -> bool:
        return bool(self.central_bus_arn)
