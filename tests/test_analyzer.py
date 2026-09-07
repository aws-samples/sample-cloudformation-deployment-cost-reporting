"""Analyzer orchestration, end to end with faked AWS reads."""

from dataclasses import dataclass, field
from decimal import Decimal

from analyzer import (
    Analyzer,
    AnalyzerConfig,
    StackDescription,
    StackResourceSummary,
    parse_stack_event,
)
from conftest import REGION, build_catalog
from resolver import load_template
from state import InMemoryStateStore

ROOT_ID = "arn:aws:cloudformation:us-east-1:111122223333:stack/payments-api-prod/abc-123"
CHILD_ID = "arn:aws:cloudformation:us-east-1:111122223333:stack/payments-api-prod-Db/def-456"
GRANDCHILD_ID = (
    "arn:aws:cloudformation:us-east-1:111122223333:stack/payments-api-prod-Db-Vol/ghi-789"
)

ROOT_TEMPLATE = """
Parameters:
  InstanceType: {Type: String, Default: t3.large}
Resources:
  Vpc:
    Type: AWS::EC2::VPC
    Properties: {CidrBlock: 10.0.0.0/16}
  AppServer:
    Type: AWS::EC2::Instance
    Properties:
      InstanceType: !Ref InstanceType
  UploadBucket:
    Type: AWS::S3::Bucket
    DeletionPolicy: Retain
    Properties: {BucketName: uploads}
"""

NESTED_ROOT_TEMPLATE = """
Resources:
  AppServer:
    Type: AWS::EC2::Instance
    Properties: {InstanceType: t3.large}
  DbStack:
    Type: AWS::CloudFormation::Stack
    Properties: {TemplateURL: https://example/db.yaml}
"""

CHILD_TEMPLATE = """
Resources:
  Database:
    Type: AWS::RDS::DBInstance
    Properties:
      DBInstanceClass: db.t4g.medium
      Engine: postgres
      AllocatedStorage: 20
"""


@dataclass
class FakeStack:
    template: str
    parameters: dict = field(default_factory=dict)
    resources: list = field(default_factory=list)
    tags: dict = field(default_factory=dict)
    stack_name: str = "payments-api-prod"
    root_id: str | None = None
    parent_id: str | None = None
    status: str = "UPDATE_COMPLETE"


class FakeReader:
    """Serves canned CloudFormation reads, and can be made to fail."""

    def __init__(self, stacks: dict):
        self.stacks = stacks
        self.template_failures: set = set()
        self.describe_failures: set = set()

    def describe_stack(self, stack_id):
        if stack_id in self.describe_failures:
            return None
        stack = self.stacks.get(stack_id)
        if stack is None:
            return None
        return StackDescription(
            stack_id=stack_id,
            stack_name=stack.stack_name,
            status=stack.status,
            parameters=dict(stack.parameters),
            tags=dict(stack.tags),
            root_id=stack.root_id,
            parent_id=stack.parent_id,
        )

    def get_template(self, stack_id):
        if stack_id in self.template_failures:
            return None
        stack = self.stacks.get(stack_id)
        return load_template(stack.template) if stack else None

    def list_resources(self, stack_id):
        stack = self.stacks.get(stack_id)
        if stack is None:
            return []
        return [
            StackResourceSummary(
                logical_id=logical,
                physical_id=physical,
                resource_type=rtype,
                status=status,
            )
            for logical, physical, rtype, status in stack.resources
        ]


def simple_root(**kwargs):
    return FakeStack(
        template=ROOT_TEMPLATE,
        parameters={"InstanceType": "t3.large"},
        resources=[
            ("Vpc", "vpc-1", "AWS::EC2::VPC", "CREATE_COMPLETE"),
            ("AppServer", "i-1", "AWS::EC2::Instance", "CREATE_COMPLETE"),
            ("UploadBucket", "uploads", "AWS::S3::Bucket", "CREATE_COMPLETE"),
        ],
        tags={"Environment": "production", "Project": "payments"},
        **kwargs,
    )


def event(status="UPDATE_COMPLETE", stack_id=ROOT_ID, token="token-1"):
    return parse_stack_event(
        {
            "detail-type": "CloudFormation Stack Status Change",
            "source": "aws.cloudformation",
            "id": "event-1",
            "account": "111122223333",
            "region": REGION,
            "time": "2026-08-10T17:06:18Z",
            "detail": {
                "stack-id": stack_id,
                "status-details": {"status": status},
                "client-request-token": token,
            },
        }
    )


def build(stacks=None, store=None, config=None):
    reader = FakeReader(stacks if stacks is not None else {ROOT_ID: simple_root()})
    store = store if store is not None else InMemoryStateStore()
    return (
        Analyzer(reader, build_catalog(), store, config or AnalyzerConfig()),
        reader,
        store,
    )


# -- creation -------------------------------------------------------------


def test_creation_reports_everything_as_added():
    analyzer, _, store = build()
    outcome = analyzer.analyze(event("CREATE_COMPLETE"))

    assert outcome.reported
    assert outcome.report["action"] == "CREATE"
    assert {r["logicalId"] for r in outcome.report["added"]} == {
        "Vpc",
        "AppServer",
        "UploadBucket",
    }
    # 0.0832 * 730
    assert outcome.report["totals"]["netMonthly"] == 60.74
    assert outcome.snapshot_saved
    assert store.load(ROOT_ID) is not None


def test_tags_and_basis_reach_the_report():
    analyzer, _, _ = build()
    report = analyzer.analyze(event("CREATE_COMPLETE")).report

    assert report["tags"]["Environment"] == "production"
    assert report["pricingBasis"]["priceListVersion"] == "2026-08-09"
    assert report["status"] == "CREATE_COMPLETE"
    assert report["clientRequestToken"] == "token-1"


def test_physical_ids_are_captured_for_replacement_detection():
    analyzer, _, store = build()
    analyzer.analyze(event("CREATE_COMPLETE"))

    assert store.load(ROOT_ID).by_logical_id["AppServer"].physical_id == "i-1"


# -- baseline -------------------------------------------------------------


def test_first_sighting_of_an_existing_stack_is_a_baseline():
    """Installing into a mature account must not report a false increase (S12)."""
    analyzer, _, _ = build()
    outcome = analyzer.analyze(event("UPDATE_COMPLETE"))

    assert outcome.report["action"] == "BASELINE"
    assert outcome.report["isBaseline"] is True
    assert outcome.report["totals"]["netMonthly"] == 0
    assert outcome.report["totals"]["currentStackMonthly"] == 60.74
    assert any("First time" in note for note in outcome.notes)


# -- update ---------------------------------------------------------------


def test_second_deployment_reports_a_resize():
    stacks = {ROOT_ID: simple_root()}
    analyzer, reader, _ = build(stacks)
    analyzer.analyze(event("CREATE_COMPLETE"))

    reader.stacks[ROOT_ID].parameters = {"InstanceType": "t3.xlarge"}
    outcome = analyzer.analyze(event("UPDATE_COMPLETE", token="token-2"))

    assert outcome.report["action"] == "UPDATE"
    changed = outcome.report["changed"]
    assert [c["logicalId"] for c in changed] == ["AppServer"]
    assert changed[0]["changedDimensions"] == ["instanceType"]
    assert outcome.report["totals"]["netMonthly"] == 60.74


def test_redeploying_unchanged_reports_no_movement():
    analyzer, _, _ = build()
    analyzer.analyze(event("CREATE_COMPLETE"))
    outcome = analyzer.analyze(event("UPDATE_COMPLETE", token="token-2"))

    assert outcome.report["totals"]["netMonthly"] == 0
    assert outcome.report["unchanged"] == 3


# -- resource status filtering ------------------------------------------

def test_resources_that_failed_to_create_are_not_priced():
    """After a rollback the template still declares them, but they do not exist."""
    stack = simple_root()
    stack.resources = [
        ("Vpc", "vpc-1", "AWS::EC2::VPC", "CREATE_COMPLETE"),
        ("AppServer", None, "AWS::EC2::Instance", "CREATE_FAILED"),
        ("UploadBucket", "uploads", "AWS::S3::Bucket", "CREATE_COMPLETE"),
    ]
    analyzer, _, _ = build({ROOT_ID: stack})
    outcome = analyzer.analyze(event("ROLLBACK_COMPLETE"))

    # Nothing chargeable survived the rollback.
    assert outcome.report["totals"]["currentStackMonthly"] == 0

    # Only the VPC and the bucket remain, so AppServer never entered the
    # inventory at all rather than being priced and then zeroed.
    coverage = outcome.report["coverage"]
    assert coverage["resourcesTotal"] == 2
    assert coverage["resourcesPriced"] == 0
    assert coverage["resourcesFree"] == 1
    assert coverage["resourcesUsageBased"] == 1


def test_an_empty_resource_list_is_authoritative():
    """A successful empty list means a full rollback created no resources."""
    stack = simple_root()
    stack.resources = []
    analyzer, _, _ = build({ROOT_ID: stack})
    outcome = analyzer.analyze(event("CREATE_COMPLETE"))

    assert outcome.report["totals"]["currentStackMonthly"] == 0.0


# -- deletion and retention ---------------------------------------------


def test_deleting_a_stack_reports_the_saving():
    analyzer, _, store = build()
    analyzer.analyze(event("CREATE_COMPLETE"))

    outcome = analyzer.analyze(event("DELETE_COMPLETE", token="token-2"))

    assert outcome.report["action"] == "DELETE"
    assert outcome.report["direction"] == "DECREASE"
    assert outcome.report["totals"]["netMonthly"] == -60.74
    assert store.load(ROOT_ID) is None


def test_retained_resources_are_excluded_from_the_saving():
    """A Retain policy means the resource outlives the stack and keeps billing.

    Counting it as a saving would overstate the teardown, and the bill would not
    move by the amount reported.
    """
    stack = FakeStack(
        template="""
Resources:
  KeptVolume:
    Type: AWS::EC2::Volume
    DeletionPolicy: Retain
    Properties: {Size: 100, VolumeType: gp3}
  DoomedServer:
    Type: AWS::EC2::Instance
    Properties: {InstanceType: t3.large}
""",
        resources=[
            ("KeptVolume", "vol-1", "AWS::EC2::Volume", "CREATE_COMPLETE"),
            ("DoomedServer", "i-1", "AWS::EC2::Instance", "CREATE_COMPLETE"),
        ],
    )
    analyzer, _, _ = build({ROOT_ID: stack})
    analyzer.analyze(event("CREATE_COMPLETE"))

    outcome = analyzer.analyze(event("DELETE_COMPLETE", token="token-2"))
    totals = outcome.report["totals"]

    # Only the instance is a real saving; the volume survives.
    assert totals["netMonthly"] == -60.74
    assert totals["retainedMonthly"] == 8.00
    assert [r["logicalId"] for r in outcome.report["retained"]] == ["KeptVolume"]
    assert any("retained" in note for note in outcome.notes)
    assert outcome.report["reconciles"] is True


def test_deleting_without_history_reports_honestly_rather_than_guessing():
    analyzer, _, _ = build()
    outcome = analyzer.analyze(event("DELETE_COMPLETE"))

    assert outcome.reported
    assert outcome.report["action"] == "DELETE"
    assert outcome.report["totals"]["netMonthly"] == 0
    assert any("prior cost is unknown" in note for note in outcome.notes)


# -- nested stacks -------------------------------------------------------


def test_a_nested_child_event_is_skipped_in_favour_of_the_root():
    """Otherwise one deployment produces a message per child stack (S13)."""
    stacks = {
        ROOT_ID: simple_root(),
        CHILD_ID: FakeStack(
            template=CHILD_TEMPLATE, parent_id=ROOT_ID, root_id=ROOT_ID
        ),
    }
    analyzer, _, store = build(stacks)
    outcome = analyzer.analyze(event("UPDATE_COMPLETE", stack_id=CHILD_ID))

    assert not outcome.reported
    assert "Nested child stack" in outcome.skipped
    assert not outcome.snapshot_saved
    assert len(store) == 0


def test_the_root_report_includes_nested_stack_resources():
    stacks = {
        ROOT_ID: FakeStack(
            template=NESTED_ROOT_TEMPLATE,
            resources=[
                ("AppServer", "i-1", "AWS::EC2::Instance", "CREATE_COMPLETE"),
                ("DbStack", CHILD_ID, "AWS::CloudFormation::Stack", "CREATE_COMPLETE"),
            ],
        ),
        CHILD_ID: FakeStack(
            template=CHILD_TEMPLATE,
            parent_id=ROOT_ID,
            root_id=ROOT_ID,
            resources=[("Database", "db-1", "AWS::RDS::DBInstance", "CREATE_COMPLETE")],
        ),
    }
    analyzer, _, _ = build(stacks)
    outcome = analyzer.analyze(event("CREATE_COMPLETE"))

    logical_ids = {r["logicalId"] for r in outcome.report["added"]}
    # Child resources are prefixed with the nesting path.
    assert "DbStack/Database" in logical_ids
    assert "AppServer" in logical_ids

    # EC2 60.736 + RDS instance 47.45 + RDS storage 2.30
    assert outcome.report["totals"]["netMonthly"] == 110.49


def test_nested_stacks_are_free_and_do_not_dent_coverage():
    stacks = {
        ROOT_ID: FakeStack(
            template=NESTED_ROOT_TEMPLATE,
            resources=[
                ("AppServer", "i-1", "AWS::EC2::Instance", "CREATE_COMPLETE"),
                ("DbStack", CHILD_ID, "AWS::CloudFormation::Stack", "CREATE_COMPLETE"),
            ],
        ),
        CHILD_ID: FakeStack(
            template=CHILD_TEMPLATE,
            parent_id=ROOT_ID,
            resources=[("Database", "db-1", "AWS::RDS::DBInstance", "CREATE_COMPLETE")],
        ),
    }
    analyzer, _, _ = build(stacks)
    coverage = analyzer.analyze(event("CREATE_COMPLETE")).report["coverage"]

    assert coverage["resourcesFree"] == 1
    assert coverage["resourcesPriced"] == 2
    assert coverage["pricedPercent"] == 100.0


def test_an_unreadable_child_is_noted_and_the_root_still_reports():
    stacks = {
        ROOT_ID: FakeStack(
            template=NESTED_ROOT_TEMPLATE,
            resources=[
                ("AppServer", "i-1", "AWS::EC2::Instance", "CREATE_COMPLETE"),
                ("DbStack", CHILD_ID, "AWS::CloudFormation::Stack", "CREATE_COMPLETE"),
            ],
        ),
    }
    analyzer, _, _ = build(stacks)
    outcome = analyzer.analyze(event("CREATE_COMPLETE"))

    assert outcome.reported
    assert any("Could not read nested stack DbStack" in n for n in outcome.notes)
    assert outcome.report["totals"]["netMonthly"] == 60.74


def test_rollup_can_be_turned_off():
    stacks = {
        ROOT_ID: FakeStack(
            template=NESTED_ROOT_TEMPLATE,
            resources=[
                ("AppServer", "i-1", "AWS::EC2::Instance", "CREATE_COMPLETE"),
                ("DbStack", CHILD_ID, "AWS::CloudFormation::Stack", "CREATE_COMPLETE"),
            ],
        ),
        CHILD_ID: FakeStack(
            template=CHILD_TEMPLATE,
            parent_id=ROOT_ID,
            resources=[("Database", "db-1", "AWS::RDS::DBInstance", "CREATE_COMPLETE")],
        ),
    }
    analyzer, _, _ = build(
        stacks, config=AnalyzerConfig(rollup_nested_stacks=False)
    )
    outcome = analyzer.analyze(event("CREATE_COMPLETE"))

    logical_ids = {r["logicalId"] for r in outcome.report["added"]}
    assert "DbStack/Database" not in logical_ids
    assert outcome.report["totals"]["netMonthly"] == 60.74


def test_nesting_depth_is_capped():
    stacks = {
        ROOT_ID: FakeStack(
            template=NESTED_ROOT_TEMPLATE,
            resources=[
                ("AppServer", "i-1", "AWS::EC2::Instance", "CREATE_COMPLETE"),
                ("DbStack", CHILD_ID, "AWS::CloudFormation::Stack", "CREATE_COMPLETE"),
            ],
        ),
        CHILD_ID: FakeStack(
            template=NESTED_ROOT_TEMPLATE,
            parent_id=ROOT_ID,
            resources=[
                ("AppServer", "i-2", "AWS::EC2::Instance", "CREATE_COMPLETE"),
                ("DbStack", GRANDCHILD_ID, "AWS::CloudFormation::Stack", "CREATE_COMPLETE"),
            ],
        ),
        GRANDCHILD_ID: FakeStack(
            template=CHILD_TEMPLATE,
            parent_id=CHILD_ID,
            resources=[("Database", "db-1", "AWS::RDS::DBInstance", "CREATE_COMPLETE")],
        ),
    }
    analyzer, _, _ = build(stacks, config=AnalyzerConfig(max_nested_depth=1))
    outcome = analyzer.analyze(event("CREATE_COMPLETE"))

    assert any("depth limit" in note for note in outcome.notes)
    logical_ids = {r["logicalId"] for r in outcome.report["added"]}
    assert "DbStack/DbStack/Database" not in logical_ids


# -- failure guards ------------------------------------------------------


def test_a_template_failure_skips_the_report_and_persists_nothing():
    """Persisting partial data would make the next deploy report false deletions."""
    analyzer, reader, store = build()
    analyzer.analyze(event("CREATE_COMPLETE"))
    saved = store.load(ROOT_ID)

    reader.template_failures.add(ROOT_ID)
    outcome = analyzer.analyze(event("UPDATE_COMPLETE", token="token-2"))

    assert not outcome.reported
    assert "nothing was persisted" in outcome.skipped
    assert not outcome.snapshot_saved
    # The previous snapshot is untouched.
    assert store.load(ROOT_ID) is saved


def test_a_describe_failure_skips_the_report():
    analyzer, reader, store = build()
    reader.describe_failures.add(ROOT_ID)
    outcome = analyzer.analyze(event("CREATE_COMPLETE"))

    assert not outcome.reported
    assert "describe" in outcome.skipped
    assert len(store) == 0


def test_an_unknown_stack_is_skipped():
    analyzer, _, _ = build(stacks={})
    assert not analyzer.analyze(event("CREATE_COMPLETE")).reported


# -- notification threshold ---------------------------------------------


def test_a_change_below_the_threshold_is_not_reported_but_is_still_recorded():
    """Suppressing a message must not suppress the state update.

    Otherwise the next diff measures from stale state and reports the suppressed
    change again, combined with the new one.
    """
    stacks = {ROOT_ID: simple_root()}
    analyzer, reader, store = build(
        stacks, config=AnalyzerConfig(notify_threshold=Decimal(100))
    )
    analyzer.analyze(event("CREATE_COMPLETE"))

    reader.stacks[ROOT_ID].parameters = {"InstanceType": "t3.xlarge"}
    outcome = analyzer.analyze(event("UPDATE_COMPLETE", token="token-2"))

    assert not outcome.reported
    assert "below the" in outcome.skipped
    assert outcome.snapshot_saved
    assert (
        store.load(ROOT_ID).by_logical_id["AppServer"].dimensions["instanceType"]
        == "t3.xlarge"
    )


def test_a_change_above_the_threshold_is_reported():
    stacks = {ROOT_ID: simple_root()}
    analyzer, reader, _ = build(
        stacks, config=AnalyzerConfig(notify_threshold=Decimal(10))
    )
    analyzer.analyze(event("CREATE_COMPLETE"))

    reader.stacks[ROOT_ID].parameters = {"InstanceType": "t3.xlarge"}
    assert analyzer.analyze(event("UPDATE_COMPLETE", token="token-2")).reported


def test_a_baseline_ignores_the_threshold():
    """A baseline has a net of zero but is the message that starts tracking."""
    analyzer, _, _ = build(config=AnalyzerConfig(notify_threshold=Decimal(1000)))
    assert analyzer.analyze(event("UPDATE_COMPLETE")).reported


# -- discount ------------------------------------------------------------


def test_discount_flows_into_the_report():
    analyzer, _, _ = build(config=AnalyzerConfig(discount_percent=Decimal(20)))
    report = analyzer.analyze(event("CREATE_COMPLETE")).report

    assert report["pricingBasis"]["discountPercent"] == 20.0
    assert report["totals"]["netMonthly"] == 48.59


# -- reconciliation ------------------------------------------------------


def test_reports_always_reconcile():
    stacks = {ROOT_ID: simple_root()}
    analyzer, reader, _ = build(stacks)

    assert analyzer.analyze(event("CREATE_COMPLETE")).report["reconciles"]

    reader.stacks[ROOT_ID].parameters = {"InstanceType": "t3.xlarge"}
    assert analyzer.analyze(event("UPDATE_COMPLETE", token="t2")).report["reconciles"]

    assert analyzer.analyze(event("DELETE_COMPLETE", token="t3")).report["reconciles"]
