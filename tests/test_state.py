"""Snapshot model and state store."""

from decimal import Decimal

import pytest

from conftest import REGION, STACK_ID, build_catalog, make_resource, snap_resource, snapshot
from pricing import Confidence, PricingClass, PricingEngine
from state import (
    DynamoStateStore,
    InMemoryStateStore,
    SnapshotResource,
    StackSnapshot,
    StateStoreError,
    total_of,
)

# -- fingerprints ---------------------------------------------------------


def test_fingerprint_follows_pricing_dimensions():
    small = snap_resource("Web", dimensions={"instanceType": "t3.large"})
    large = snap_resource("Web", dimensions={"instanceType": "t3.xlarge"})

    assert small.fingerprint != large.fingerprint


def test_fingerprint_ignores_everything_that_does_not_affect_cost():
    """The reason properties are not compared wholesale.

    Two resources with identical pricing dimensions but different descriptions
    must fingerprint identically, or a tag edit becomes a cost change.
    """
    left = snap_resource("Web", dimensions={"instanceType": "t3.large"})
    right = snap_resource("Web", dimensions={"instanceType": "t3.large"})
    right.description = "renamed entirely"
    right.physical_id = "i-different"

    assert left.fingerprint == right.fingerprint


def test_fingerprint_reflects_quantity():
    small = snap_resource("Vol", dimensions={"volumeType": "gp3"}, quantity="100")
    large = snap_resource("Vol", dimensions={"volumeType": "gp3"}, quantity="500")

    assert small.fingerprint != large.fingerprint


def test_fingerprint_is_order_independent():
    left = snap_resource("Db", dimensions={"a": "1", "b": "2"})
    right = snap_resource("Db", dimensions={"b": "2", "a": "1"})

    assert left.fingerprint == right.fingerprint


def test_resources_without_components_fingerprint_empty():
    bucket = snap_resource(
        "Bucket", "AWS::S3::Bucket", pricing_class=PricingClass.USAGE_BASED
    )
    assert bucket.fingerprint == ()
    assert not bucket.is_priced


# -- snapshot totals ------------------------------------------------------


def test_snapshot_total_sums_only_priced_resources():
    snap = snapshot(
        snap_resource("Web", dimensions={"instanceType": "t3.large"}, monthly="60.74"),
        snap_resource("Nat", "AWS::EC2::NatGateway", dimensions={}, monthly="32.85"),
        snap_resource("Bucket", "AWS::S3::Bucket", pricing_class=PricingClass.USAGE_BASED),
        snap_resource("Vpc", "AWS::EC2::VPC", pricing_class=PricingClass.FREE),
    )
    assert snap.monthly_list == Decimal("93.59")


def test_unavailable_confidence_is_excluded_from_totals():
    snap = snapshot(
        snap_resource(
            "Web",
            dimensions={"instanceType": "t3.large"},
            monthly="60.74",
            confidence=Confidence.UNAVAILABLE,
        )
    )
    assert snap.monthly_list == Decimal(0)


def test_by_logical_id_indexes_resources():
    snap = snapshot(snap_resource("Web"), snap_resource("Db"))
    assert set(snap.by_logical_id) == {"Web", "Db"}


def test_empty_snapshot_is_flagged():
    assert snapshot().is_empty
    assert not snapshot(snap_resource("Web")).is_empty


def test_total_of_can_report_discounted_figures():
    resources = [snap_resource("Web", monthly="100.00")]
    resources[0].monthly_cost = Decimal("85.00")

    assert total_of(resources) == Decimal("100.00")
    assert total_of(resources, discounted=True) == Decimal("85.00")


def test_captured_at_defaults_to_now():
    assert snapshot(snap_resource("Web")).captured_at


# -- serialisation --------------------------------------------------------


def test_snapshot_round_trips_through_the_store_format():
    original = snapshot(
        snap_resource(
            "Web",
            dimensions={"instanceType": "t3.large"},
            monthly="60.74",
            physical_id="i-0abc",
        ),
        snap_resource("Bucket", "AWS::S3::Bucket", pricing_class=PricingClass.USAGE_BASED),
    )
    original.client_request_token = "token-1"
    original.status = "UPDATE_COMPLETE"
    original.price_list_version = "20260801000000"
    original.discount_percent = Decimal(15)

    restored = StackSnapshot.from_item(original.to_item())

    assert restored.stack_id == original.stack_id
    assert restored.client_request_token == "token-1"
    assert restored.status == "UPDATE_COMPLETE"
    assert restored.price_list_version == "20260801000000"
    assert restored.discount_percent == Decimal(15)
    assert restored.monthly_list == original.monthly_list
    assert len(restored.resources) == 2


def test_fingerprints_survive_a_round_trip():
    """Otherwise every stored resource would look changed on the next deploy."""
    original = snapshot(
        snap_resource("Web", dimensions={"instanceType": "t3.large"}, quantity="730")
    )
    restored = StackSnapshot.from_item(original.to_item())

    assert restored.resources[0].fingerprint == original.resources[0].fingerprint


def test_decimals_are_stored_as_strings():
    """Money must not round-trip through anything float-shaped."""
    item = snapshot(snap_resource("Web", monthly="60.74")).to_item()
    assert isinstance(item["monthlyList"], str)
    assert isinstance(item["discountPercent"], str)


def test_item_carries_a_resource_count_for_cheap_inspection():
    item = snapshot(snap_resource("A"), snap_resource("B")).to_item()
    assert item["resourceCount"] == 2


def test_resource_round_trips_with_every_field():
    original = snap_resource(
        "Nat",
        "AWS::EC2::NatGateway",
        dimensions={},
        monthly="32.85",
        confidence=Confidence.MEDIUM,
        excluded=["Data processing per GB"],
        reason="Partially priced",
    )
    restored = SnapshotResource.from_item(original.to_item())

    assert restored.confidence is Confidence.MEDIUM
    assert restored.excluded == ["Data processing per GB"]
    assert restored.reason == "Partially priced"
    assert restored.monthly_list == Decimal("32.85")


def test_from_item_tolerates_a_decoded_resource_list():
    """Some stores hand back a list rather than a JSON string."""
    item = snapshot(snap_resource("Web")).to_item()
    item["resources"] = [snap_resource("Web").to_item()]
    assert len(StackSnapshot.from_item(item).resources) == 1


# -- building from a priced inventory ------------------------------------


def test_snapshot_is_built_from_a_priced_inventory():
    engine = PricingEngine(build_catalog(), REGION)
    inventory = engine.price_inventory(
        [
            make_resource("Web", properties={"InstanceType": "t3.large"}),
            make_resource("Bucket", "AWS::S3::Bucket", {"BucketName": "x"}),
        ]
    )
    snap = StackSnapshot.from_inventory(
        inventory,
        stack_id=STACK_ID,
        stack_name="payments-api-prod",
        account="111122223333",
        region=REGION,
        physical_ids={"Web": "i-0abc123"},
    )

    assert snap.monthly_list == Decimal("60.736")
    assert snap.by_logical_id["Web"].physical_id == "i-0abc123"
    assert snap.by_logical_id["Web"].dimensions["instanceType"] == "t3.large"
    assert snap.price_list_version == "2026-08-09"


def test_structural_query_attributes_are_left_out_of_dimensions():
    """productFamily is plumbing, not something a reader would call a change."""
    engine = PricingEngine(build_catalog(), REGION)
    inventory = engine.price_inventory(
        [make_resource("Web", properties={"InstanceType": "t3.large"})]
    )
    snap = StackSnapshot.from_inventory(
        inventory, STACK_ID, "s", "111122223333", REGION
    )

    dimensions = snap.by_logical_id["Web"].dimensions
    assert "productFamily" not in dimensions
    assert "instanceType" in dimensions


def test_multi_component_resource_keeps_every_component():
    engine = PricingEngine(build_catalog(), REGION)
    inventory = engine.price_inventory(
        [
            make_resource(
                "Web",
                properties={
                    "InstanceType": "t3.large",
                    "BlockDeviceMappings": [
                        {"DeviceName": "/dev/xvda", "Ebs": {"VolumeSize": 100, "VolumeType": "gp3"}}
                    ],
                },
            )
        ]
    )
    snap = StackSnapshot.from_inventory(inventory, STACK_ID, "s", "111122223333", REGION)

    assert len(snap.by_logical_id["Web"].components) == 2


def test_empty_like_preserves_identity_but_drops_resources():
    original = snapshot(snap_resource("Web"))
    empty = StackSnapshot.empty_like(original, status="DELETE_COMPLETE")

    assert empty.stack_id == original.stack_id
    assert empty.stack_name == original.stack_name
    assert empty.is_empty
    assert empty.status == "DELETE_COMPLETE"


# -- in-memory store ------------------------------------------------------


def test_in_memory_store_saves_and_loads():
    store = InMemoryStateStore()
    snap = snapshot(snap_resource("Web"))
    store.save(snap)

    assert store.load(STACK_ID) is snap
    assert len(store) == 1


def test_in_memory_store_returns_none_for_an_unknown_stack():
    assert InMemoryStateStore().load("arn:unknown") is None


def test_in_memory_store_deletes():
    store = InMemoryStateStore()
    store.save(snapshot(snap_resource("Web")))
    store.delete(STACK_ID)

    assert store.load(STACK_ID) is None


def test_empty_store_is_truthy():
    """Otherwise `store or fallback` silently swaps out a real empty store."""
    assert bool(InMemoryStateStore())


# -- DynamoDB store -------------------------------------------------------


class FakeTable:
    def __init__(self, items=None):
        self.items = dict(items or {})
        self.raise_on_get = False
        self.updates = []
        self.deletes = []

    def get_item(self, Key, ConsistentRead=False):
        if self.raise_on_get:
            raise RuntimeError("throttled")
        item = self.items.get(Key["pk"])
        return {"Item": item} if item is not None else {}

    def put_item(self, Item):
        self.items[Item["pk"]] = Item

    def delete_item(self, Key):
        self.deletes.append(Key["pk"])
        self.items.pop(Key["pk"], None)

    def update_item(self, **kwargs):
        self.updates.append(kwargs)


def test_dynamo_store_round_trips():
    table = FakeTable()
    store = DynamoStateStore(table)
    original = snapshot(snap_resource("Web", dimensions={"instanceType": "t3.large"}))
    store.save(original)

    restored = store.load(STACK_ID)
    assert restored is not None
    assert restored.by_logical_id["Web"].fingerprint == original.resources[0].fingerprint


def test_dynamo_store_returns_none_when_absent():
    assert DynamoStateStore(FakeTable()).load(STACK_ID) is None


def test_dynamo_read_failure_is_retryable():
    """A transient read failure must not be mistaken for absent history."""
    table = FakeTable()
    table.raise_on_get = True
    store = DynamoStateStore(table)

    with pytest.raises(StateStoreError):
        store.load(STACK_ID)
    assert store.load_failures == 1


def test_corrupt_snapshot_is_not_silently_treated_as_no_history():
    table = FakeTable({STACK_ID: {"pk": STACK_ID, "resources": "{not json"}})
    store = DynamoStateStore(table)

    with pytest.raises(StateStoreError):
        store.load(STACK_ID)
    assert store.load_failures == 1


def test_delete_expires_rather_than_removing_by_default():
    """A late or out-of-order event still needs history to diff against (S10)."""
    table = FakeTable()
    store = DynamoStateStore(table, retention_days=90)
    store.delete(STACK_ID)

    assert table.deletes == []
    assert len(table.updates) == 1
    assert ":expires" in table.updates[0]["ExpressionAttributeValues"]


def test_zero_retention_hard_deletes():
    table = FakeTable()
    DynamoStateStore(table, retention_days=0).delete(STACK_ID)
    assert table.deletes == [STACK_ID]


def test_delete_failure_is_retryable():
    class Broken(FakeTable):
        def update_item(self, **kwargs):
            raise RuntimeError("throttled")

    with pytest.raises(StateStoreError):
        DynamoStateStore(Broken()).delete(STACK_ID)
