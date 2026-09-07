"""Path B — pre-deploy estimates from change sets.

One invariant carries the whole path: **an estimate must never persist a
snapshot.** A change set is a proposal. Storing it would make the subsequent
confirmed report diff against a state that was never deployed, and if the change
set were abandoned the stored state would be permanently wrong — every later
report for that stack silently measured from a fiction.

The code comments say so; these tests are what stops someone adding
``store.save()`` for symmetry with the confirmed path.
"""

from __future__ import annotations

from typing import Any

import pytest

from analyzer import (
    Analyzer,
    AnalyzerConfig,
    ChangeSetDescription,
    ChangeSetNotReady,
    StackDescription,
    StackResourceSummary,
    parse_change_set_event,
)
from conftest import REGION, build_catalog
from resolver import load_template
from state import InMemoryStateStore

STACK_ID = "arn:aws:cloudformation:us-east-1:111122223333:stack/payments-api-prod/abc-123"
CHANGE_SET_ID = "arn:aws:cloudformation:us-east-1:111122223333:changeSet/deploy-42/cs-abc"

CURRENT_TEMPLATE = """
Resources:
  AppServer:
    Type: AWS::EC2::Instance
    Properties: {InstanceType: t3.large}
"""

PROPOSED_TEMPLATE = """
Resources:
  AppServer:
    Type: AWS::EC2::Instance
    Properties: {InstanceType: t3.large}
  Database:
    Type: AWS::RDS::DBInstance
    Properties:
      DBInstanceClass: db.r6g.large
      Engine: postgres
      AllocatedStorage: 100
"""


class FakeReader:
    """Serves canned reads for both the stack and its change set."""

    def __init__(
        self,
        change_set: ChangeSetDescription | None,
        proposed_template: str | None = PROPOSED_TEMPLATE,
        current_template: str = CURRENT_TEMPLATE,
        stack_status: str = "CREATE_COMPLETE",
        resources: list[tuple[str, str, str, str]] | None = None,
    ) -> None:
        self._change_set = change_set
        self._proposed = proposed_template
        self._current = current_template
        self._status = stack_status
        self._resources = (
            resources
            if resources is not None
            else [("AppServer", "i-1", "AWS::EC2::Instance", "CREATE_COMPLETE")]
        )
        self.template_calls: list[str] = []

    def describe_stack(self, stack_id: str) -> StackDescription | None:
        return StackDescription(
            stack_id=stack_id,
            stack_name="payments-api-prod",
            status=self._status,
            parameters={},
            tags={},
            root_id=None,
            parent_id=None,
        )

    def get_template(self, stack_id: str) -> dict[str, Any] | None:
        self.template_calls.append("current")
        return load_template(self._current)

    def list_resources(self, stack_id: str) -> list[StackResourceSummary]:
        return [
            StackResourceSummary(
                logical_id=logical, physical_id=physical, resource_type=rtype, status=status
            )
            for logical, physical, rtype, status in self._resources
        ]

    def describe_change_set(self, change_set_id: str) -> ChangeSetDescription | None:
        return self._change_set

    def get_change_set_template(
        self, stack_id: str, change_set_id: str
    ) -> dict[str, Any] | None:
        self.template_calls.append("changeset")
        return load_template(self._proposed) if self._proposed else None


def change_set(
    status: str = "CREATE_COMPLETE", execution_status: str = "AVAILABLE", **kwargs: Any
) -> ChangeSetDescription:
    return ChangeSetDescription(
        change_set_id=CHANGE_SET_ID,
        stack_id=STACK_ID,
        stack_name="payments-api-prod",
        status=status,
        execution_status=execution_status,
        **kwargs,
    )


def cloudtrail_event() -> Any:
    return parse_change_set_event(
        {
            "detail-type": "AWS API Call via CloudTrail",
            "source": "aws.cloudformation",
            "id": "event-1",
            "account": "111122223333",
            "region": REGION,
            "time": "2026-08-10T17:06:18Z",
            "detail": {
                "eventSource": "cloudformation.amazonaws.com",
                "eventName": "CreateChangeSet",
                "awsRegion": REGION,
                "userIdentity": {"accountId": "111122223333"},
                "responseElements": {"id": CHANGE_SET_ID, "stackId": STACK_ID},
            },
        }
    )


def build(reader: FakeReader, store: Any = None, config: Any = None) -> tuple[Any, Any]:
    store = store if store is not None else InMemoryStateStore()
    analyzer = Analyzer(reader, build_catalog(), store, config or AnalyzerConfig())
    return analyzer, store


def seeded_store() -> Any:
    """A store holding the stack's real last-deployed state.

    Produced by running a confirmed analysis, so the seed is whatever the
    analyzer itself would have written — not a hand-built snapshot that might
    drift from the real shape.

    Needed because an estimate with no stored history is a *baseline*, which
    reports an inventory rather than a delta. That is correct behaviour, but it
    is not the case that exercises the addition path.
    """
    from analyzer import parse_stack_event

    store = InMemoryStateStore()
    analyzer, store = build(FakeReader(None), store=store)
    analyzer.analyze(
        parse_stack_event(
            {
                "detail-type": "CloudFormation Stack Status Change",
                "source": "aws.cloudformation",
                "id": "event-0",
                "account": "111122223333",
                "region": REGION,
                "time": "2026-08-10T17:00:00Z",
                "detail": {
                    "stack-id": STACK_ID,
                    "status-details": {"status": "CREATE_COMPLETE"},
                    "client-request-token": "token-0",
                },
            }
        )
    )
    assert store.load(STACK_ID) is not None
    return store


# -- the invariant --------------------------------------------------------


def test_an_estimate_never_persists_a_snapshot() -> None:
    """The one thing that must not change.

    Storing a proposal makes every later confirmed report diff against something
    that was never deployed.
    """
    analyzer, store = build(FakeReader(change_set()))

    outcome = analyzer.analyze_change_set(cloudtrail_event())

    assert outcome.reported
    assert outcome.snapshot_saved is False
    assert store.load(STACK_ID) is None


def test_an_estimate_does_not_overwrite_existing_history() -> None:
    """Worse than writing to an empty store: silently replacing the real
    last-deployed state with a proposal.

    If this regressed, the *next* confirmed report would diff the deployed stack
    against a change set that may never have been executed — and report a delta
    for a change that never happened.
    """
    store = seeded_store()
    deployed = store.load(STACK_ID)

    estimator, store = build(FakeReader(change_set()), store=store)
    estimator.analyze_change_set(cloudtrail_event())

    after = store.load(STACK_ID)
    assert after is not None
    assert {r.logical_id for r in after.resources} == {
        r.logical_id for r in deployed.resources
    }
    # The proposal's new resource must not have leaked into stored state.
    assert "Database" not in {r.logical_id for r in after.resources}


# -- pricing the proposal -------------------------------------------------


def test_the_estimate_prices_the_change_set_template_not_the_current_one() -> None:
    """The whole point: report what the deployment *will* cost.

    The current template has one instance; the change set adds a database. If the
    estimate read the current template it would report no change at all.
    """
    reader = FakeReader(change_set())
    analyzer, _ = build(reader, store=seeded_store())

    outcome = analyzer.analyze_change_set(cloudtrail_event())

    assert "changeset" in reader.template_calls
    added = {r["logicalId"] for r in outcome.report["added"]}
    assert "Database" in added


def test_the_estimate_is_marked_as_an_estimate() -> None:
    """A reader must be able to tell a proposal from a confirmed figure."""
    analyzer, _ = build(FakeReader(change_set()))

    outcome = analyzer.analyze_change_set(cloudtrail_event())

    assert outcome.report["reportPhase"] == "ESTIMATE"


def test_the_report_says_the_figures_are_not_confirmed() -> None:
    """Stated in the notes so it survives into email and Slack, not just the
    JSON field."""
    analyzer, _ = build(FakeReader(change_set()))

    outcome = analyzer.analyze_change_set(cloudtrail_event())

    assert any("not confirmed" in note for note in outcome.report["notes"])


def test_proposed_additions_are_not_filtered_against_current_resources() -> None:
    """A change set proposes resources that do not exist yet. Filtering against
    the live resource list would drop exactly the additions the estimate exists
    to report."""
    reader = FakeReader(
        change_set(),
        # The live stack has only AppServer. Database does not exist yet, which is
        # precisely why it must survive collection.
        resources=[("AppServer", "i-1", "AWS::EC2::Instance", "CREATE_COMPLETE")],
    )
    analyzer, _ = build(reader, store=seeded_store())

    outcome = analyzer.analyze_change_set(cloudtrail_event())

    assert any(r["logicalId"] == "Database" for r in outcome.report["added"])
    assert outcome.report["totals"]["netMonthly"] > 0


def test_a_never_deployed_stack_estimates_a_create_not_a_baseline() -> None:
    """A stack in REVIEW_IN_PROGRESS exists only as a change set. Falling back to
    a baseline would report an inventory instead of the cost of creating it,
    which is useless as an estimate."""
    reader = FakeReader(
        change_set(), stack_status="REVIEW_IN_PROGRESS", resources=[]
    )
    analyzer, _ = build(reader)

    outcome = analyzer.analyze_change_set(cloudtrail_event())

    assert outcome.report["action"] == "CREATE"
    assert not outcome.report.get("isBaseline")


# -- timing ---------------------------------------------------------------


@pytest.mark.parametrize("status", ["CREATE_PENDING", "CREATE_IN_PROGRESS"])
def test_a_change_set_still_computing_is_retryable(status: str) -> None:
    """CreateChangeSet returns before the change set exists, so this is the
    normal case, not an edge one. Raising lets SQS provide the wait instead of
    the Lambda billing for a sleep."""
    analyzer, store = build(FakeReader(change_set(status=status)))

    with pytest.raises(ChangeSetNotReady):
        analyzer.analyze_change_set(cloudtrail_event())

    assert store.load(STACK_ID) is None


def test_a_failed_change_set_is_skipped_not_retried() -> None:
    """FAILED is usually CloudFormation saying the template produces no changes.
    Retrying would never succeed."""
    analyzer, _ = build(FakeReader(change_set(status="FAILED")))

    outcome = analyzer.analyze_change_set(cloudtrail_event())

    assert not outcome.reported
    assert outcome.skipped


def test_a_deleted_change_set_is_skipped() -> None:
    analyzer, _ = build(FakeReader(change_set(status="DELETE_COMPLETE")))

    outcome = analyzer.analyze_change_set(cloudtrail_event())

    assert not outcome.reported


# -- failure paths --------------------------------------------------------


def test_an_undescribable_change_set_is_skipped_not_estimated() -> None:
    """Better to report nothing than to price a change set we could not read."""
    analyzer, store = build(FakeReader(None))

    outcome = analyzer.analyze_change_set(cloudtrail_event())

    assert not outcome.reported
    assert outcome.skipped
    assert store.load(STACK_ID) is None


def test_an_unfetchable_template_is_skipped() -> None:
    analyzer, store = build(FakeReader(change_set(), proposed_template=None))

    outcome = analyzer.analyze_change_set(cloudtrail_event())

    assert not outcome.reported
    assert store.load(STACK_ID) is None


def test_a_nested_child_is_counted_in_the_root_estimate() -> None:
    """Under ROLLUP, a child stack's own change set must not produce a second
    report for cost already counted at the root."""

    class NestedReader(FakeReader):
        def describe_stack(self, stack_id: str) -> StackDescription | None:
            return StackDescription(
                stack_id=stack_id,
                stack_name="payments-api-prod-Db",
                status="CREATE_COMPLETE",
                parameters={},
                tags={},
                root_id="arn:aws:cloudformation:us-east-1:111122223333:stack/root/xyz",
                parent_id="arn:aws:cloudformation:us-east-1:111122223333:stack/root/xyz",
            )

    analyzer, _ = build(NestedReader(change_set()))

    outcome = analyzer.analyze_change_set(cloudtrail_event())

    assert not outcome.reported
    assert "root stack" in (outcome.skipped or "")


def test_separate_mode_reports_a_nested_child_estimate() -> None:
    class NestedReader(FakeReader):
        def describe_stack(self, stack_id: str) -> StackDescription | None:
            return StackDescription(
                stack_id=stack_id,
                stack_name="payments-api-prod-Db",
                status="CREATE_COMPLETE",
                parameters={},
                tags={},
                root_id="arn:aws:cloudformation:us-east-1:111122223333:stack/root/xyz",
                parent_id="arn:aws:cloudformation:us-east-1:111122223333:stack/root/xyz",
            )

    analyzer, _ = build(
        NestedReader(change_set()),
        config=AnalyzerConfig(rollup_nested_stacks=False),
    )

    outcome = analyzer.analyze_change_set(cloudtrail_event())

    assert outcome.reported


# -- coverage -------------------------------------------------------------


def test_an_estimate_still_states_its_coverage() -> None:
    """Coverage on every report, estimates included — a proposal with hidden gaps
    is exactly as misleading as a confirmed one."""
    analyzer, _ = build(FakeReader(change_set()))

    outcome = analyzer.analyze_change_set(cloudtrail_event())

    assert "coverage" in outcome.report
    assert outcome.report["coverage"]["resourcesPriced"] >= 1


def test_an_estimate_reconciles() -> None:
    analyzer, _ = build(FakeReader(change_set()))

    outcome = analyzer.analyze_change_set(cloudtrail_event())

    assert outcome.report["reconciles"] is True
