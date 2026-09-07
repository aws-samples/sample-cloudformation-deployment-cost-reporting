"""Steps 1 to 4 joined up.

Template in, cost delta out, through every real component: the CloudFormation
loader, the parameter resolver, the pricing engine, the state store, and the diff
engine. This is what the analyzer will do once the event wiring exists.
"""

from decimal import Decimal

from conftest import REGION, build_catalog
from pricing import PricingEngine
from resolver import PseudoContext, TemplateResolver, load_template
from state import (
    DeltaAction,
    Direction,
    InMemoryStateStore,
    StackSnapshot,
    diff_deletion,
    diff_snapshots,
)

STACK_ID = (
    "arn:aws:cloudformation:us-east-1:111122223333:stack/payments-api-prod/abc-123"
)

TEMPLATE = """
Parameters:
  Environment:
    Type: String
    Default: dev
  InstanceType:
    Type: String
    Default: t3.large
  VolumeSize:
    Type: Number
    Default: 100

Mappings:
  EnvConfig:
    dev:
      DBClass: db.t4g.medium
    production:
      DBClass: db.r6g.xlarge

Conditions:
  IsProd: !Equals [!Ref Environment, production]

Resources:
  Vpc:
    Type: AWS::EC2::VPC
    Properties:
      CidrBlock: 10.0.0.0/16

  AppServer:
    Type: AWS::EC2::Instance
    Properties:
      InstanceType: !Ref InstanceType
      Tags:
        - Key: Environment
          Value: !Ref Environment

  DataVolume:
    Type: AWS::EC2::Volume
    Properties:
      Size: !Ref VolumeSize
      VolumeType: gp3

  Database:
    Type: AWS::RDS::DBInstance
    Properties:
      DBInstanceClass: !FindInMap [EnvConfig, !Ref Environment, DBClass]
      Engine: postgres
      AllocatedStorage: 20
      MultiAZ: !If [IsProd, true, !Ref 'AWS::NoValue']

  ProdReplica:
    Type: AWS::RDS::DBInstance
    Condition: IsProd
    Properties:
      DBInstanceClass: db.r6g.large
      Engine: postgres
      AllocatedStorage: 20

  UploadBucket:
    Type: AWS::S3::Bucket
    Properties:
      BucketName: uploads
"""


def build_snapshot(parameters, physical_ids=None, discount=0):
    """Resolve, price, and snapshot the template under given parameters."""
    resolver = TemplateResolver(
        load_template(TEMPLATE), parameters, PseudoContext.from_stack_id(STACK_ID)
    )
    engine = PricingEngine(build_catalog(), REGION, discount_percent=discount)
    inventory = engine.price_inventory(resolver.resolve_resources())

    return StackSnapshot.from_inventory(
        inventory,
        stack_id=STACK_ID,
        stack_name="payments-api-prod",
        account="111122223333",
        region=REGION,
        physical_ids=physical_ids,
    )


# -- first deployment -----------------------------------------------------


def test_first_deployment_reports_everything_as_added():
    after = build_snapshot({"Environment": "dev"})
    delta = diff_snapshots(None, after, action=DeltaAction.CREATE)

    assert delta.action is DeltaAction.CREATE
    # EC2 60.736 + gp3 100GB 8.00 + RDS 47.45 + storage 2.30
    assert delta.net_monthly == Decimal("118.486")
    assert {r.logical_id for r in delta.added} == {
        "Vpc",
        "AppServer",
        "DataVolume",
        "Database",
        "UploadBucket",
    }
    assert delta.reconciles


def test_a_preexisting_stack_starts_from_a_baseline():
    """The plugin was installed after this stack already existed."""
    after = build_snapshot({"Environment": "dev"})
    delta = diff_snapshots(None, after, action=DeltaAction.UPDATE)

    assert delta.is_baseline
    assert delta.net_monthly == Decimal(0)
    assert delta.current_monthly == Decimal("118.486")


# -- resize --------------------------------------------------------------


def test_resizing_the_instance_reports_only_that_resource_as_changed():
    before = build_snapshot({"Environment": "dev", "InstanceType": "t3.large"})
    after = build_snapshot({"Environment": "dev", "InstanceType": "t3.xlarge"})
    delta = diff_snapshots(before, after)

    assert [c.logical_id for c in delta.changed] == ["AppServer"]
    assert delta.changed[0].changed_dimensions == ["instanceType"]
    # 0.1664 * 730 − 0.0832 * 730
    assert delta.changed[0].delta_monthly_list == Decimal("60.736")
    assert delta.added == []
    assert delta.removed == []
    assert delta.reconciles


def test_growing_the_volume_is_reported_as_a_quantity_change():
    before = build_snapshot({"Environment": "dev", "VolumeSize": 100})
    after = build_snapshot({"Environment": "dev", "VolumeSize": 500})
    delta = diff_snapshots(before, after)

    assert [c.logical_id for c in delta.changed] == ["DataVolume"]
    assert delta.changed[0].changed_dimensions == ["quantity"]
    # (500 − 100) * 0.08
    assert delta.changed[0].delta_monthly_list == Decimal("32.00")


def test_redeploying_the_same_template_reports_no_movement():
    before = build_snapshot({"Environment": "dev"})
    after = build_snapshot({"Environment": "dev"})
    delta = diff_snapshots(before, after)

    assert not delta.has_movement
    assert delta.net_monthly == Decimal(0)
    assert delta.direction is Direction.NEUTRAL


# -- promotion to production ---------------------------------------------


def test_promoting_to_production_adds_the_replica_and_resizes_the_database():
    before = build_snapshot({"Environment": "dev"})
    after = build_snapshot({"Environment": "production"})
    delta = diff_snapshots(before, after)

    # The replica's condition became true, so it is new.
    assert [r.logical_id for r in delta.added] == ["ProdReplica"]

    changed = {c.logical_id: c for c in delta.changed}
    # The database changed class and gained Multi-AZ in one move.
    assert "Database" in changed
    assert set(changed["Database"].changed_dimensions) == {
        "instanceType",
        "deploymentOption",
    }

    assert delta.direction is Direction.INCREASE
    assert delta.reconciles


def test_demoting_from_production_removes_the_replica():
    before = build_snapshot({"Environment": "production"})
    after = build_snapshot({"Environment": "dev"})
    delta = diff_snapshots(before, after)

    assert [r.logical_id for r in delta.removed] == ["ProdReplica"]
    assert delta.direction is Direction.DECREASE
    assert delta.reconciles


# -- tags do not move money ----------------------------------------------


def test_changing_only_a_tag_produces_no_change_at_all():
    """AppServer's Environment tag differs, its pricing dimensions do not.

    Comparing resolved properties would report this as a change with a delta of
    zero. Comparing pricing fingerprints correctly reports nothing.
    """
    before = build_snapshot({"Environment": "dev"})
    after = build_snapshot({"Environment": "staging"})

    tags_before = before.by_logical_id["AppServer"].description
    tags_after = after.by_logical_id["AppServer"].description
    assert tags_before == tags_after  # description is dimension-derived

    delta = diff_snapshots(before, after)
    app_server_changed = [c for c in delta.changed if c.logical_id == "AppServer"]
    assert app_server_changed == []


# -- replacement ---------------------------------------------------------


def test_replacement_is_detected_from_physical_ids():
    before = build_snapshot(
        {"Environment": "dev", "InstanceType": "t3.large"},
        physical_ids={"AppServer": "i-old"},
    )
    after = build_snapshot(
        {"Environment": "dev", "InstanceType": "t3.xlarge"},
        physical_ids={"AppServer": "i-new"},
    )
    delta = diff_snapshots(before, after)

    assert delta.changed[0].replacement


# -- deletion ------------------------------------------------------------


def test_deleting_the_stack_reports_the_full_saving():
    before = build_snapshot({"Environment": "dev"})
    delta = diff_deletion(before)

    assert delta.action is DeltaAction.DELETE
    assert delta.net_monthly == Decimal("-118.486")
    assert delta.direction is Direction.DECREASE
    assert delta.current_monthly == Decimal(0)
    assert delta.reconciles

    payload = delta.to_dict()
    assert payload["totals"]["netMonthly"] == -118.49
    assert len(payload["removed"]) == 5


# -- through the store ---------------------------------------------------


def test_the_store_carries_state_across_two_deployments():
    """Round-tripping through storage must not alter the diff."""
    store = InMemoryStateStore()

    first = build_snapshot({"Environment": "dev", "InstanceType": "t3.large"})
    store.save(first)

    second = build_snapshot({"Environment": "dev", "InstanceType": "t3.xlarge"})
    delta = diff_snapshots(store.load(STACK_ID), second)
    store.save(second)

    assert [c.logical_id for c in delta.changed] == ["AppServer"]
    assert delta.reconciles

    # A third deployment with no change sees nothing move.
    third = build_snapshot({"Environment": "dev", "InstanceType": "t3.xlarge"})
    assert not diff_snapshots(store.load(STACK_ID), third).has_movement


def test_serialisation_does_not_disturb_change_detection():
    """Fingerprints must survive the store format, or everything looks changed."""
    original = build_snapshot({"Environment": "dev"})
    restored = StackSnapshot.from_item(original.to_item())

    delta = diff_snapshots(restored, build_snapshot({"Environment": "dev"}))
    assert not delta.has_movement


# -- discount ------------------------------------------------------------


def test_discount_flows_through_to_the_delta():
    before = build_snapshot({"Environment": "dev", "InstanceType": "t3.large"}, discount=20)
    after = build_snapshot({"Environment": "dev", "InstanceType": "t3.xlarge"}, discount=20)
    delta = diff_snapshots(before, after)
    change = delta.changed[0]

    # List delta 60.736, discounted at 20%; report arithmetic uses the
    # discounted figure while preserving list price in the snapshot.
    assert change.delta_monthly_list == Decimal("60.736")
    assert change.delta_monthly_cost == Decimal("48.5888")
    assert delta.changed_monthly == Decimal("48.5888")
    assert delta.to_dict()["totals"]["netMonthly"] == 48.59


# -- coverage ------------------------------------------------------------


def test_coverage_survives_into_the_delta():
    before = build_snapshot({"Environment": "dev"})
    after = build_snapshot({"Environment": "dev", "InstanceType": "t3.xlarge"})
    coverage = diff_snapshots(before, after).coverage

    # Vpc free; UploadBucket usage-based; the rest priced.
    assert coverage.free == 1
    assert coverage.usage_based == 1
    assert coverage.priced == 3
    assert coverage.priced_percent == 75.0
