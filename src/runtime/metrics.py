"""CloudWatch metrics (S24).

The cheapest high-value output. Notifications need someone to read them; alarms
do not. A team can set ``NetMonthlyCostChange > 500`` on their own stack without
anyone touching the plugin.

``PricingCoveragePercent`` trending down is the early warning that the resource
mapping is falling behind what people actually deploy, and
``ReconciliationFailures`` should be flat at zero — a non-zero value means the
diff has lost or double-counted a resource.
"""

from __future__ import annotations

from typing import Any, Protocol

from .logs import get_logger

logger = get_logger(__name__)

NAMESPACE = "CFNCostPlugin"

#: CloudWatch rejects more than 1000 metrics per PutMetricData call. Nowhere near
#: that here, but the batching keeps it true if metrics are added later.
_MAX_BATCH = 20


class MetricsPublisher(Protocol):
    def emit(self, report: dict[str, Any]) -> None:
        ...

    def emit_failure(self, account: str, region: str) -> None:
        ...


def failure_metric_data(account: str, region: str) -> list[dict[str, Any]]:
    """Detailed and aggregate series; the dimensionless alarm selects the latter."""
    return [
        {
            "MetricName": "AnalysisFailures",
            "Value": 1.0,
            "Unit": "Count",
            "Dimensions": [
                {"Name": "Account", "Value": account or "unknown"},
                {"Name": "Region", "Value": region or "unknown"},
            ],
        },
        {"MetricName": "AnalysisFailures", "Value": 1.0, "Unit": "Count"},
    ]


class NullMetrics:
    """Records instead of sending. For tests and when metrics are disabled."""

    def __init__(self) -> None:
        self.emitted: list[list[dict[str, Any]]] = []

    def emit(self, report: dict[str, Any]) -> None:
        self.emitted.append(build_metric_data(report))

    def emit_failure(self, account: str, region: str) -> None:
        self.emitted.append(failure_metric_data(account, region))


def build_metric_data(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn a report into PutMetricData entries.

    A baseline emits the absolute stack cost and coverage but no deltas, because
    nothing changed — publishing a zero would flatten a real average.
    """
    totals = report.get("totals") or {}
    coverage = report.get("coverage") or {}

    dimensions = [
        {"Name": "StackName", "Value": str(report.get("stackName") or "unknown")},
        {"Name": "Account", "Value": str(report.get("account") or "unknown")},
        {"Name": "Region", "Value": str(report.get("region") or "unknown")},
    ]

    def metric(name: str, value: Any, unit: str = "None") -> dict[str, Any]:
        return {
            "MetricName": name,
            "Value": float(value or 0),
            "Unit": unit,
            "Dimensions": dimensions,
        }

    data = [
        metric("StackMonthlyCost", totals.get("currentStackMonthly")),
        metric(
            "PricingCoveragePercent", coverage.get("pricedPercent"), unit="Percent"
        ),
        metric(
            "UnsupportedResourceCount",
            coverage.get("resourcesUnsupported"),
            unit="Count",
        ),
    ]

    if not report.get("isBaseline"):
        data.extend(
            [
                metric("NetMonthlyCostChange", totals.get("netMonthly")),
                metric("AddedMonthlyCost", totals.get("addedMonthly")),
                metric("RemovedMonthlyCost", totals.get("removedMonthly")),
                metric("ChangedMonthlyCost", totals.get("changedMonthly")),
                metric("RetainedMonthlyCost", totals.get("retainedMonthly")),
            ]
        )

    # Should always be zero. Emitted unconditionally so an alarm on it has a
    # continuous stream to evaluate rather than only firing when data appears.
    reconciliation_value = 0 if report.get("reconciles", True) else 1
    data.append(metric("ReconciliationFailures", reconciliation_value, unit="Count"))
    # Built-in alarm is dimensionless; preserve detailed per-stack data above and
    # emit one aggregate series specifically for that alarm.
    data.append(
        {
            "MetricName": "ReconciliationFailures",
            "Value": float(reconciliation_value),
            "Unit": "Count",
        }
    )

    return data


class CloudWatchMetrics:
    """Publishes to CloudWatch."""

    def __init__(self, client: Any, namespace: str = NAMESPACE) -> None:
        self._client = client
        self._namespace = namespace

    def emit(self, report: dict[str, Any]) -> None:
        data = build_metric_data(report)
        self._put(data)

    def emit_failure(self, account: str, region: str) -> None:
        self._put(failure_metric_data(account, region))

    def _put(self, data: list[dict[str, Any]]) -> None:
        for start in range(0, len(data), _MAX_BATCH):
            batch = data[start : start + _MAX_BATCH]
            try:
                self._client.put_metric_data(
                    Namespace=self._namespace, MetricData=batch
                )
            except Exception as exc:
                logger.warning(
                    "Could not publish metrics",
                    extra={"error": str(exc), "metricCount": len(batch)},
                )
                return
