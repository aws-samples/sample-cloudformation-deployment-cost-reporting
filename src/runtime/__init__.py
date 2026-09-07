"""Lambda runtime.

Steps 6 to 11 of the build order: everything needed to run the plugin in AWS, as
distinct from the pure logic in ``resolver``, ``pricing``, ``state``, and
``analyzer``.

* :mod:`~runtime.logs` — JSON logging with per-invocation context
* :mod:`~runtime.idempotency` — claim-before-work deduplication
* :mod:`~runtime.publisher` — SNS and central-bus delivery, with size trimming
* :mod:`~runtime.metrics` — CloudWatch metrics
* :mod:`~runtime.slack` — Block Kit rendering and the two-phase message merge
* :mod:`~runtime.config` — environment to typed settings
* :mod:`~runtime.handlers` — the analyzer, price sync, and Slack entry points
* :mod:`~runtime.install_check` — the fourth entry point, a CloudFormation custom
  resource that reports at install time whether Path B will fire

Importing this package pulls in ``handlers``, and therefore ``resolver``, which
imports PyYAML. That matters for one function only: if PyYAML were missing from the
deployment package, the install check would fail at import rather than responding,
and a custom resource that never responds leaves the stack waiting an hour before
timing out. PyYAML missing would break the analyzer too, so this is a symptom of an
unusable build rather than an independent fault — but it is a slow way to find out.
``src/requirements.txt`` is what prevents it.
"""

from .config import RuntimeConfig
from .handlers import (
    analyzer_handler,
    price_sync_handler,
    reset_caches,
    slack_handler,
)
from .idempotency import (
    DynamoIdempotencyStore,
    IdempotencyStore,
    InMemoryIdempotencyStore,
)
from .install_check import check_trail
from .logs import configure_logging, current_context, get_logger, log_context
from .metrics import NAMESPACE, CloudWatchMetrics, NullMetrics, build_metric_data
from .publisher import (
    MAX_PAYLOAD_BYTES,
    CompositePublisher,
    EventBridgePublisher,
    NullPublisher,
    Publisher,
    SnsPublisher,
    trim_for_transport,
)
from .slack import (
    DynamoMessageStore,
    InMemoryMessageStore,
    MessageStore,
    SlackApiError,
    SlackClient,
    SlackForwarder,
    fallback_text,
    render_blocks,
)

__all__ = [
    "MAX_PAYLOAD_BYTES",
    "NAMESPACE",
    "CloudWatchMetrics",
    "CompositePublisher",
    "DynamoIdempotencyStore",
    "DynamoMessageStore",
    "EventBridgePublisher",
    "IdempotencyStore",
    "InMemoryIdempotencyStore",
    "InMemoryMessageStore",
    "MessageStore",
    "NullMetrics",
    "NullPublisher",
    "Publisher",
    "RuntimeConfig",
    "SlackApiError",
    "SlackClient",
    "SlackForwarder",
    "SnsPublisher",
    "analyzer_handler",
    "build_metric_data",
    "check_trail",
    "configure_logging",
    "current_context",
    "fallback_text",
    "get_logger",
    "log_context",
    "price_sync_handler",
    "render_blocks",
    "reset_caches",
    "slack_handler",
    "trim_for_transport",
]
