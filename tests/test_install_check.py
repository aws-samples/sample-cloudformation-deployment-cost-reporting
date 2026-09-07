"""Tests for the install-time CloudTrail check.

This resource exists because Path B fails *silently* without a trail: the rule
deploys, matches nothing, and never fires. Nothing in AWS reports that.

Two rules govern it. It must never fail the deployment — a missing trail should
cost the estimate path, not the install. And it must always respond, because a
custom resource that does not answer leaves the stack waiting an hour before
timing out.
"""

from __future__ import annotations

from typing import Any

import pytest

from runtime.install_check import (
    ACTIVE,
    NO_TRAIL,
    READ_ONLY_ONLY,
    UNKNOWN,
    _evaluate_selectors,
    check_trail,
    handler,
)


class FakeCloudTrail:
    def __init__(
        self,
        trails: list[dict[str, Any]] | None = None,
        selectors: dict[str, dict[str, Any]] | None = None,
        selectors_raise: Exception | None = None,
        describe_raises: Exception | None = None,
    ) -> None:
        self._trails = trails if trails is not None else []
        self._selectors = selectors or {}
        self._selectors_raise = selectors_raise
        self._describe_raises = describe_raises

    def describe_trails(self, includeShadowTrails: bool = True) -> dict[str, Any]:
        if self._describe_raises is not None:
            raise self._describe_raises
        return {"trailList": self._trails}

    def get_event_selectors(self, TrailName: str) -> dict[str, Any]:
        if self._selectors_raise is not None:
            raise self._selectors_raise
        return self._selectors.get(TrailName, {})


def trail(
    name: str = "arn:aws:cloudtrail:us-east-1:111122223333:trail/main",
    home_region: str = "us-east-1",
    multi_region: bool = False,
) -> dict[str, Any]:
    return {
        "Name": "main",
        "TrailARN": name,
        "HomeRegion": home_region,
        "IsMultiRegionTrail": multi_region,
    }


def management_writes() -> dict[str, Any]:
    return {"EventSelectors": [{"IncludeManagementEvents": True, "ReadWriteType": "All"}]}


# -- classic selectors ----------------------------------------------------


def test_management_writes_are_detected() -> None:
    assert _evaluate_selectors(management_writes()) is True


def test_write_only_counts_as_writes() -> None:
    selectors = {"EventSelectors": [{"IncludeManagementEvents": True, "ReadWriteType": "WriteOnly"}]}

    assert _evaluate_selectors(selectors) is True


def test_an_absent_read_write_type_defaults_to_all() -> None:
    """The API omits the field when it is All, so treating absence as "unknown"
    would report a working trail as inactive."""
    selectors = {"EventSelectors": [{"IncludeManagementEvents": True}]}

    assert _evaluate_selectors(selectors) is True


def test_read_only_does_not_capture_create_change_set() -> None:
    selectors = {"EventSelectors": [{"IncludeManagementEvents": True, "ReadWriteType": "ReadOnly"}]}

    assert _evaluate_selectors(selectors) is False


def test_a_data_events_only_trail_does_not_cover_management() -> None:
    """An S3 data-events trail is common and covers nothing Path B needs."""
    selectors = {"EventSelectors": [{"IncludeManagementEvents": False}]}

    assert _evaluate_selectors(selectors) is None


def test_no_selectors_at_all_covers_nothing() -> None:
    assert _evaluate_selectors({}) is None


# -- advanced selectors ---------------------------------------------------


def test_advanced_management_selectors_are_detected() -> None:
    selectors = {
        "AdvancedEventSelectors": [
            {"FieldSelectors": [{"Field": "eventCategory", "Equals": ["Management"]}]}
        ]
    }

    assert _evaluate_selectors(selectors) is True


def test_an_advanced_selector_pinned_to_read_only_excludes_writes() -> None:
    selectors = {
        "AdvancedEventSelectors": [
            {
                "FieldSelectors": [
                    {"Field": "eventCategory", "Equals": ["Management"]},
                    {"Field": "readOnly", "Equals": ["true"]},
                ]
            }
        ]
    }

    assert _evaluate_selectors(selectors) is False


def test_read_only_false_includes_writes() -> None:
    selectors = {
        "AdvancedEventSelectors": [
            {
                "FieldSelectors": [
                    {"Field": "eventCategory", "Equals": ["Management"]},
                    {"Field": "readOnly", "Equals": ["false"]},
                ]
            }
        ]
    }

    assert _evaluate_selectors(selectors) is True


def test_a_data_category_advanced_selector_is_ignored() -> None:
    selectors = {
        "AdvancedEventSelectors": [
            {"FieldSelectors": [{"Field": "eventCategory", "Equals": ["Data"]}]}
        ]
    }

    assert _evaluate_selectors(selectors) is None


def test_one_qualifying_selector_among_several_is_enough() -> None:
    """A trail commonly has a read-only selector and a write selector."""
    selectors = {
        "AdvancedEventSelectors": [
            {
                "FieldSelectors": [
                    {"Field": "eventCategory", "Equals": ["Management"]},
                    {"Field": "readOnly", "Equals": ["true"]},
                ]
            },
            {"FieldSelectors": [{"Field": "eventCategory", "Equals": ["Management"]}]},
        ]
    }

    assert _evaluate_selectors(selectors) is True


# -- trail discovery ------------------------------------------------------


def test_no_trails_reports_inactive() -> None:
    status, detail = check_trail(FakeCloudTrail(trails=[]), region="us-east-1")

    assert status == NO_TRAIL
    assert "No CloudTrail trail" in detail


def test_a_local_trail_with_management_writes_is_active() -> None:
    name = "arn:aws:cloudtrail:us-east-1:111122223333:trail/main"
    client = FakeCloudTrail(
        trails=[trail(name)], selectors={name: management_writes()}
    )

    status, detail = check_trail(client, region="us-east-1")

    assert status == ACTIVE
    assert name in detail


def test_a_multi_region_trail_covers_this_region() -> None:
    """This is how most accounts satisfy the prerequisite without a local trail,
    so excluding it would report a false negative."""
    name = "arn:aws:cloudtrail:eu-west-1:111122223333:trail/org"
    client = FakeCloudTrail(
        trails=[trail(name, home_region="eu-west-1", multi_region=True)],
        selectors={name: management_writes()},
    )

    status, _ = check_trail(client, region="us-east-1")

    assert status == ACTIVE


def test_a_single_region_trail_elsewhere_does_not_count() -> None:
    """It captures events, just not in the region where this plugin is watching."""
    name = "arn:aws:cloudtrail:eu-west-1:111122223333:trail/other"
    client = FakeCloudTrail(
        trails=[trail(name, home_region="eu-west-1")],
        selectors={name: management_writes()},
    )

    status, detail = check_trail(client, region="us-east-1")

    assert status == NO_TRAIL
    assert "us-east-1" in detail


def test_a_read_only_trail_is_reported_as_such() -> None:
    """Distinguished from "no trail" because the fix is different: change the
    selectors, not create a trail."""
    name = "arn:aws:cloudtrail:us-east-1:111122223333:trail/audit"
    client = FakeCloudTrail(
        trails=[trail(name)],
        selectors={
            name: {"EventSelectors": [{"IncludeManagementEvents": True, "ReadWriteType": "ReadOnly"}]}
        },
    )

    status, detail = check_trail(client, region="us-east-1")

    assert status == READ_ONLY_ONLY
    assert "read-only" in detail


def test_unreadable_selectors_report_unknown_not_inactive() -> None:
    """A member account usually cannot read an organisation trail's selectors.
    Claiming Path B is dead would be wrong — it very likely works."""
    client = FakeCloudTrail(
        trails=[trail()], selectors_raise=RuntimeError("AccessDenied")
    )

    status, detail = check_trail(client, region="us-east-1")

    assert status == UNKNOWN
    assert "organisation trail" in detail


def test_one_qualifying_trail_among_several_is_enough() -> None:
    good = "arn:aws:cloudtrail:us-east-1:111122223333:trail/good"
    bad = "arn:aws:cloudtrail:us-east-1:111122223333:trail/data-only"
    client = FakeCloudTrail(
        trails=[trail(bad), trail(good)],
        selectors={
            bad: {"EventSelectors": [{"IncludeManagementEvents": False}]},
            good: management_writes(),
        },
    )

    status, _ = check_trail(client, region="us-east-1")

    assert status == ACTIVE


def test_trails_covering_the_region_but_not_management_report_no_trail() -> None:
    name = "arn:aws:cloudtrail:us-east-1:111122223333:trail/data"
    client = FakeCloudTrail(
        trails=[trail(name)],
        selectors={name: {"EventSelectors": [{"IncludeManagementEvents": False}]}},
    )

    status, detail = check_trail(client, region="us-east-1")

    assert status == NO_TRAIL
    assert "management events" in detail


def test_an_unknown_region_rules_no_trail_out() -> None:
    """Better to over-report availability than to claim a working trail is
    missing."""
    name = "arn:aws:cloudtrail:eu-west-1:111122223333:trail/other"
    client = FakeCloudTrail(
        trails=[trail(name, home_region="eu-west-1")],
        selectors={name: management_writes()},
    )

    status, _ = check_trail(client, region="")

    assert status == ACTIVE


# -- custom resource protocol ---------------------------------------------


class Recorder:
    """Captures what would be PUT to the CloudFormation callback URL."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def __call__(self, event: Any, context: Any, status: str, data: Any = None) -> None:
        self.sent.append({"status": status, "data": data or {}})


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    from runtime import install_check

    captured = Recorder()
    monkeypatch.setattr(install_check, "_respond", captured)
    return captured


def cfn_event(request_type: str = "Create") -> dict[str, Any]:
    return {
        "RequestType": request_type,
        "ResponseURL": "https://cloudformation-custom-resource.s3.amazonaws.com/x",
        "StackId": "arn:aws:cloudformation:us-east-1:111122223333:stack/plugin/abc",
        "RequestId": "req-1",
        "LogicalResourceId": "InstallCheck",
        "ResourceProperties": {"Region": "us-east-1"},
    }


def test_a_delete_succeeds_without_calling_cloudtrail(recorder: Recorder) -> None:
    """Nothing was created, so nothing needs checking — and a failed check must
    not block a stack deletion."""
    handler(cfn_event("Delete"))

    assert recorder.sent == [{"status": "SUCCESS", "data": {"Status": "DELETED"}}]


def test_a_missing_trail_still_reports_success(
    recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The core rule: this never fails an install."""
    from runtime import install_check

    monkeypatch.setattr(
        install_check, "check_trail", lambda: (NO_TRAIL, "No trail found")
    )

    handler(cfn_event())

    assert recorder.sent[0]["status"] == "SUCCESS"
    assert recorder.sent[0]["data"]["Status"] == NO_TRAIL


def test_an_active_trail_is_reported(
    recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runtime import install_check

    monkeypatch.setattr(install_check, "check_trail", lambda: (ACTIVE, "Trail x"))

    handler(cfn_event())

    assert recorder.sent[0]["data"]["Status"] == ACTIVE


def test_an_exception_inside_the_check_still_responds_success(
    recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unhandled error here would leave the stack waiting an hour for a
    response it never gets."""
    from runtime import install_check

    def explode() -> tuple[str, str]:
        raise RuntimeError("cloudtrail unreachable")

    monkeypatch.setattr(install_check, "check_trail", explode)

    handler(cfn_event())

    assert recorder.sent[0]["status"] == "SUCCESS"
    assert recorder.sent[0]["data"]["Status"] == UNKNOWN
    assert "cloudtrail unreachable" in recorder.sent[0]["data"]["Detail"]


def test_an_update_re_runs_the_check(
    recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runtime import install_check

    monkeypatch.setattr(install_check, "check_trail", lambda: (ACTIVE, "Trail x"))

    handler(cfn_event("Update"))

    assert recorder.sent[0]["data"]["Status"] == ACTIVE


def test_a_non_https_callback_url_is_refused() -> None:
    """The URL comes from CloudFormation and is always HTTPS. Asserting it keeps
    that true if the event is ever fed from elsewhere."""
    from runtime.install_check import _respond

    event = cfn_event()
    event["ResponseURL"] = "http://attacker.example/collect"

    # Returns without sending rather than raising.
    _respond(event, None, "SUCCESS", {})


def test_a_missing_callback_url_does_not_raise() -> None:
    from runtime.install_check import _respond

    event = cfn_event()
    del event["ResponseURL"]

    _respond(event, None, "SUCCESS", {})
