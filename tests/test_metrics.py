"""Tests for CloudWatch metrics.

Two behaviours carry weight. A baseline must not emit deltas, because publishing
a zero for a stack that did not change drags down the average and makes a real
zero indistinguishable from "nothing happened". And a metrics failure must never
propagate: the report is already delivered by then, so raising would retry the
whole analysis and duplicate the notification for the sake of a datapoint.
"""

from __future__ import annotations

from typing import Any

from runtime.metrics import (
    NAMESPACE,
    CloudWatchMetrics,
    NullMetrics,
    build_metric_data,
)


def make_report(**overrides: Any) -> dict[str, Any]:
    report: dict[str, Any] = {
        "stackName": "api",
        "account": "111122223333",
        "region": "us-east-1",
        "reconciles": True,
        "totals": {
            "netMonthly": 120.5,
            "addedMonthly": 140.0,
            "removedMonthly": -19.5,
            "changedMonthly": 0.0,
            "retainedMonthly": 8.0,
            "currentStackMonthly": 500.0,
        },
        "coverage": {
            "pricedPercent": 75.0,
            "resourcesPriced": 6,
            "resourcesUnsupported": 2,
        },
    }
    report.update(overrides)
    return report


def names(data: list[dict[str, Any]]) -> set[str]:
    return {entry["MetricName"] for entry in data}


def value_of(data: list[dict[str, Any]], name: str) -> float:
    return next(entry["Value"] for entry in data if entry["MetricName"] == name)


class FakeCloudWatch:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self._fail = fail

    def put_metric_data(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        if self._fail:
            raise RuntimeError("CloudWatch unavailable")


# -- shape ----------------------------------------------------------------


def test_a_delta_report_emits_every_cost_metric() -> None:
    data = build_metric_data(make_report())

    assert {
        "NetMonthlyCostChange",
        "AddedMonthlyCost",
        "RemovedMonthlyCost",
        "ChangedMonthlyCost",
        "RetainedMonthlyCost",
        "StackMonthlyCost",
        "PricingCoveragePercent",
        "UnsupportedResourceCount",
        "ReconciliationFailures",
    } <= names(data)


def test_values_come_from_the_report() -> None:
    data = build_metric_data(make_report())

    assert value_of(data, "NetMonthlyCostChange") == 120.5
    assert value_of(data, "StackMonthlyCost") == 500.0
    assert value_of(data, "PricingCoveragePercent") == 75.0
    assert value_of(data, "UnsupportedResourceCount") == 2.0


def test_detailed_metrics_are_dimensioned_and_alarm_series_is_aggregate() -> None:
    """Teams keep per-stack dimensions while the built-in alarm selects the
    additional dimensionless reconciliation series."""
    data = build_metric_data(make_report())
    for entry in (item for item in data if "Dimensions" in item):
        dimensions = {d["Name"]: d["Value"] for d in entry["Dimensions"]}
        assert dimensions == {
            "StackName": "api",
            "Account": "111122223333",
            "Region": "us-east-1",
        }

    aggregate = [
        item
        for item in data
        if item["MetricName"] == "ReconciliationFailures" and "Dimensions" not in item
    ]
    assert len(aggregate) == 1


def test_percent_and_count_units_are_set() -> None:
    """A Percent metric published as None renders as a raw number on a dashboard
    and cannot be compared against a threshold sensibly."""
    data = build_metric_data(make_report())

    units = {entry["MetricName"]: entry["Unit"] for entry in data}
    assert units["PricingCoveragePercent"] == "Percent"
    assert units["UnsupportedResourceCount"] == "Count"
    assert units["ReconciliationFailures"] == "Count"


def test_missing_fields_become_zero_rather_than_raising() -> None:
    """A partially built report should still emit what it has."""
    data = build_metric_data({"stackName": "api"})

    assert value_of(data, "StackMonthlyCost") == 0.0
    assert value_of(data, "NetMonthlyCostChange") == 0.0


def test_absent_identity_is_labelled_unknown_not_empty() -> None:
    """CloudWatch silently discards a dimension with an empty value, which would
    make the metric land on a different series."""
    data = build_metric_data({"totals": {}, "coverage": {}})

    dimensions = {d["Name"]: d["Value"] for d in data[0]["Dimensions"]}
    assert dimensions["StackName"] == "unknown"
    assert dimensions["Account"] == "unknown"


# -- baseline -------------------------------------------------------------


def test_a_baseline_emits_absolute_cost_but_no_deltas() -> None:
    """Nothing changed, so a zero delta would be a fabricated datapoint."""
    data = build_metric_data(make_report(isBaseline=True))

    emitted = names(data)
    assert "StackMonthlyCost" in emitted
    assert "PricingCoveragePercent" in emitted
    assert "NetMonthlyCostChange" not in emitted
    assert "AddedMonthlyCost" not in emitted


def test_a_baseline_still_reports_reconciliation() -> None:
    """The alarm needs a continuous stream to evaluate."""
    data = build_metric_data(make_report(isBaseline=True))

    assert "ReconciliationFailures" in names(data)


# -- reconciliation -------------------------------------------------------


def test_reconciliation_is_zero_when_the_arithmetic_holds() -> None:
    assert value_of(build_metric_data(make_report()), "ReconciliationFailures") == 0.0


def test_reconciliation_is_one_when_the_diff_lost_a_resource() -> None:
    data = build_metric_data(make_report(reconciles=False))

    assert value_of(data, "ReconciliationFailures") == 1.0


def test_reconciliation_defaults_to_passing_when_absent() -> None:
    """An older report shape must not raise a false alarm."""
    report = make_report()
    del report["reconciles"]

    assert value_of(build_metric_data(report), "ReconciliationFailures") == 0.0


# -- publishing -----------------------------------------------------------


def test_metrics_are_published_to_the_plugin_namespace() -> None:
    client = FakeCloudWatch()

    CloudWatchMetrics(client).emit(make_report())

    assert client.calls[0]["Namespace"] == NAMESPACE
    assert client.calls[0]["MetricData"]


def test_a_cloudwatch_failure_is_swallowed() -> None:
    """The report is already delivered. Raising would retry the analysis and send
    the notification twice to recover a datapoint."""
    CloudWatchMetrics(FakeCloudWatch(fail=True)).emit(make_report())


def test_data_is_batched_under_the_api_limit() -> None:
    """PutMetricData rejects more than 1000 metrics in one call."""
    client = FakeCloudWatch()

    CloudWatchMetrics(client)._put([{"MetricName": f"M{i}"} for i in range(45)])

    assert len(client.calls) == 3
    assert all(len(call["MetricData"]) <= 20 for call in client.calls)


def test_batching_stops_after_a_failure_rather_than_hammering() -> None:
    client = FakeCloudWatch(fail=True)

    CloudWatchMetrics(client)._put([{"MetricName": f"M{i}"} for i in range(45)])

    assert len(client.calls) == 1


# -- failures -------------------------------------------------------------


def test_a_failure_metric_carries_account_and_region() -> None:
    client = FakeCloudWatch()

    CloudWatchMetrics(client).emit_failure(account="111122223333", region="us-east-1")

    entry = client.calls[0]["MetricData"][0]
    assert entry["MetricName"] == "AnalysisFailures"
    assert entry["Value"] == 1.0
    dimensions = {d["Name"]: d["Value"] for d in entry["Dimensions"]}
    assert dimensions == {"Account": "111122223333", "Region": "us-east-1"}


def test_a_failure_with_no_identity_still_emits() -> None:
    """The event may have been unparseable, which is exactly when the metric
    matters most."""
    client = FakeCloudWatch()

    CloudWatchMetrics(client).emit_failure(account="", region="")

    dimensions = {
        d["Name"]: d["Value"] for d in client.calls[0]["MetricData"][0]["Dimensions"]
    }
    assert dimensions == {"Account": "unknown", "Region": "unknown"}


# -- null implementation --------------------------------------------------


def test_null_metrics_records_instead_of_sending() -> None:
    metrics = NullMetrics()

    metrics.emit(make_report())
    metrics.emit_failure("111122223333", "us-east-1")

    assert len(metrics.emitted) == 2
    assert "NetMonthlyCostChange" in names(metrics.emitted[0])
    assert names(metrics.emitted[1]) == {"AnalysisFailures"}
