"""Tests for snapshot compression and the oversize path.

Nested rollup aggregates every descendant stack into the root's snapshot, so the
400KB DynamoDB item limit is reachable by a large nested application even though
CloudFormation caps a single stack at 500 resources.

Two behaviours are pinned here. Snapshots written before compression must keep
loading, because if they did not, the first deployment after this change would
find no history for any stack and report a baseline instead of a delta — a silent,
one-off loss of every stack's "before". And a snapshot that still will not fit must
*invalidate* stored history rather than leave a stale snapshot in place, because
diffing against a stale snapshot reports a two-deployment delta as a
one-deployment delta, which is a wrong number rather than a missing one.
"""

from __future__ import annotations

import gzip
import json
from decimal import Decimal
from typing import Any

import pytest

from conftest import REGION, snap_resource, snapshot
from state import (
    MAX_ITEM_BYTES,
    DynamoStateStore,
    SnapshotTooLarge,
    StackSnapshot,
)

STACK_ID = "arn:aws:cloudformation:us-east-1:111122223333:stack/payments-api-prod/abc-123"


def wide_snapshot(count: int) -> StackSnapshot:
    """A snapshot with enough resources to matter, shaped like a real one."""
    return snapshot(
        *[
            snap_resource(
                f"AppServer{i:04d}",
                dimensions={
                    "instanceType": "t3.large",
                    "operatingSystem": "Linux",
                    "tenancy": "Shared",
                    "productFamily": "Compute Instance",
                },
                physical_id=f"i-0abcdef{i:010d}",
            )
            for i in range(count)
        ]
    )


class FakeTable:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.deleted: list[str] = []

    def get_item(
        self, Key: dict[str, Any], ConsistentRead: bool = False
    ) -> dict[str, Any]:
        item = self.items.get(Key["pk"])
        return {"Item": item} if item else {}

    def put_item(self, Item: dict[str, Any]) -> None:
        self.items[Item["pk"]] = Item

    def delete_item(self, Key: dict[str, Any]) -> None:
        self.deleted.append(Key["pk"])
        self.items.pop(Key["pk"], None)


class Binary:
    """Mirrors boto3's Binary wrapper, which is what a real read returns."""

    def __init__(self, value: bytes) -> None:
        self.value = value


# -- round trip -----------------------------------------------------------


def test_a_snapshot_round_trips_through_compression() -> None:
    original = wide_snapshot(20)

    restored = StackSnapshot.from_item(original.to_item())

    assert len(restored.resources) == 20
    assert restored.stack_id == original.stack_id
    assert restored.monthly_cost == original.monthly_cost
    assert [r.logical_id for r in restored.resources] == [
        r.logical_id for r in original.resources
    ]


def test_resource_detail_survives_the_round_trip() -> None:
    """Dimensions and components are what the diff compares. If compression lost
    them, every resource would look changed."""
    original = wide_snapshot(3)

    restored = StackSnapshot.from_item(original.to_item())

    before = original.resources[0]
    after = restored.resources[0]
    assert after.dimensions == before.dimensions
    assert after.physical_id == before.physical_id
    assert [c.query_key for c in after.components] == [
        c.query_key for c in before.components
    ]


def test_the_blob_is_stored_compressed() -> None:
    item = wide_snapshot(10).to_item()

    assert isinstance(item["resourcesGzip"], bytes)
    assert "resources" not in item
    # Decompresses to the JSON it came from.
    assert isinstance(json.loads(gzip.decompress(item["resourcesGzip"])), list)


def test_a_boto3_binary_wrapper_is_accepted() -> None:
    """A real read returns Binary, not bytes. Handling only bytes would work in
    every test and fail on the first live load."""
    item = wide_snapshot(5).to_item()
    item["resourcesGzip"] = Binary(item["resourcesGzip"])

    restored = StackSnapshot.from_item(item)

    assert len(restored.resources) == 5


def test_summary_attributes_stay_plain_and_queryable() -> None:
    """Only the per-resource detail is opaque. Totals and identity remain
    readable in the console without decompressing anything."""
    item = wide_snapshot(7).to_item()

    assert item["pk"] == STACK_ID
    assert item["stackName"] == "payments-api-prod"
    assert item["region"] == REGION
    assert item["resourceCount"] == 7
    assert Decimal(item["monthlyCost"]) > 0


def test_an_empty_snapshot_round_trips() -> None:
    restored = StackSnapshot.from_item(snapshot().to_item())

    assert restored.resources == []


def test_compression_is_deterministic() -> None:
    """gzip embeds an mtime by default, which would make two snapshots of an
    unchanged stack differ byte for byte and defeat any direct comparison."""
    first = wide_snapshot(10).to_item()["resourcesGzip"]
    second = wide_snapshot(10).to_item()["resourcesGzip"]

    assert first == second


# -- the actual benefit ---------------------------------------------------


def test_compression_buys_roughly_an_order_of_magnitude() -> None:
    """The whole fix rests on this ratio, so it is measured rather than assumed.

    Snapshot JSON is highly repetitive — the same dimension keys, query keys, and
    resource types repeat per resource — which is close to the best case for
    gzip.
    """
    snap = wide_snapshot(500)
    uncompressed = len(
        json.dumps([r.to_item() for r in snap.resources]).encode("utf-8")
    )
    compressed = len(snap.to_item()["resourcesGzip"])

    assert compressed * 5 < uncompressed, (
        f"Only {uncompressed / compressed:.1f}x compression; "
        "the headroom assumption no longer holds"
    )


def test_a_snapshot_that_previously_would_not_fit_now_does() -> None:
    """The regression this fix exists for.

    1,500 resources is a plausible nested application and exceeds 400KB as plain
    JSON. Before compression this raised a DynamoDB ValidationException and the
    snapshot was lost.
    """
    snap = wide_snapshot(1500)
    uncompressed = len(
        json.dumps([r.to_item() for r in snap.resources]).encode("utf-8")
    )

    assert uncompressed > 400_000, "Fixture is too small to prove anything"

    item = snap.to_item()

    assert len(item["resourcesGzip"]) < MAX_ITEM_BYTES


# -- backward compatibility ----------------------------------------------


def test_a_legacy_uncompressed_snapshot_still_loads() -> None:
    """Snapshots written before this change exist in the table.

    If they stopped loading, the next deployment for every stack would find no
    history and report a baseline — losing every stack's "before" exactly once,
    silently.
    """
    original = wide_snapshot(12)
    legacy = original.to_item()
    del legacy["resourcesGzip"]
    legacy["resources"] = json.dumps([r.to_item() for r in original.resources])

    restored = StackSnapshot.from_item(legacy)

    assert len(restored.resources) == 12
    assert restored.monthly_cost == original.monthly_cost


def test_a_legacy_snapshot_stored_as_a_list_still_loads() -> None:
    """Defensive: an item written by a hand-run script may hold a real list
    rather than a JSON string."""
    original = wide_snapshot(4)
    legacy = original.to_item()
    del legacy["resourcesGzip"]
    legacy["resources"] = [r.to_item() for r in original.resources]

    assert len(StackSnapshot.from_item(legacy).resources) == 4


def test_an_item_with_neither_form_loads_as_empty() -> None:
    item = wide_snapshot(3).to_item()
    del item["resourcesGzip"]

    assert StackSnapshot.from_item(item).resources == []


def test_the_compressed_form_wins_when_both_are_present() -> None:
    """During a migration an item could carry both. The compressed one is the
    one this code writes, so it is authoritative."""
    original = wide_snapshot(6)
    item = original.to_item()
    item["resources"] = json.dumps([])

    assert len(StackSnapshot.from_item(item).resources) == 6


# -- the oversize path ----------------------------------------------------


def test_an_impossible_snapshot_raises_a_named_error() -> None:
    """Rather than letting DynamoDB return a ValidationException, which names
    neither the stack nor how far over it was."""
    # Random-ish physical IDs defeat compression, which is what it takes to
    # exceed the limit even gzipped.
    resources = [
        snap_resource(
            f"R{i:05d}",
            dimensions={"instanceType": f"custom-{i}-{i * 7919}", "tenancy": "Shared"},
            physical_id=f"i-{i:012d}{'abcdef0123456789' * 8}",
        )
        for i in range(30000)
    ]

    with pytest.raises(SnapshotTooLarge, match="over the"):
        snapshot(*resources).to_item()


def test_the_error_names_the_stack_and_the_resource_count() -> None:
    """The message is the only thing an operator has to act on."""
    resources = [
        snap_resource(
            f"R{i:05d}",
            dimensions={"instanceType": f"custom-{i}-{i * 7919}", "tenancy": "Shared"},
            physical_id=f"i-{i:012d}{'abcdef0123456789' * 8}",
        )
        for i in range(30000)
    ]

    with pytest.raises(SnapshotTooLarge) as caught:
        snapshot(*resources).to_item()

    message = str(caught.value)
    assert "payments-api-prod" in message
    assert "30,000 resources" in message


def test_an_oversized_snapshot_invalidates_stored_history() -> None:
    """The key decision.

    Leaving the old snapshot in place would make the next report diff against the
    deployment *before* last, reporting a two-deployment delta as one. Deleting it
    means the next report is a baseline, which is honest about not knowing.
    """
    table = FakeTable()
    store = DynamoStateStore(table)
    store.save(wide_snapshot(10))
    assert STACK_ID in table.items

    huge = wide_snapshot(10)
    huge.resources = [
        snap_resource(
            f"R{i:05d}",
            dimensions={"instanceType": f"custom-{i}-{i * 7919}", "tenancy": "Shared"},
            physical_id=f"i-{i:012d}{'abcdef0123456789' * 8}",
        )
        for i in range(30000)
    ]

    store.save(huge)

    assert STACK_ID not in table.items
    assert table.deleted == [STACK_ID]


def test_the_next_load_after_an_oversized_save_returns_no_history() -> None:
    """Proves the invalidation actually produces a baseline rather than just
    clearing a dict."""
    table = FakeTable()
    store = DynamoStateStore(table)
    store.save(wide_snapshot(10))

    huge = wide_snapshot(10)
    huge.resources = [
        snap_resource(
            f"R{i:05d}",
            dimensions={"instanceType": f"custom-{i}-{i * 7919}", "tenancy": "Shared"},
            physical_id=f"i-{i:012d}{'abcdef0123456789' * 8}",
        )
        for i in range(30000)
    ]
    store.save(huge)

    assert store.load(STACK_ID) is None


def test_an_oversized_save_is_counted() -> None:
    """Silent degradation is the thing to avoid. The counter is what a metric or
    a log line can report on."""
    store = DynamoStateStore(FakeTable())

    huge = wide_snapshot(1)
    huge.resources = [
        snap_resource(
            f"R{i:05d}",
            dimensions={"instanceType": f"custom-{i}-{i * 7919}", "tenancy": "Shared"},
            physical_id=f"i-{i:012d}{'abcdef0123456789' * 8}",
        )
        for i in range(30000)
    ]
    store.save(huge)

    assert store.oversized_snapshots == 1


def test_an_oversized_save_does_not_raise() -> None:
    """The report has already been computed. Raising would retry the whole
    analysis and duplicate the notification to recover a snapshot that will fail
    again on the retry."""
    store = DynamoStateStore(FakeTable())

    huge = wide_snapshot(1)
    huge.resources = [
        snap_resource(
            f"R{i:05d}",
            dimensions={"instanceType": f"custom-{i}-{i * 7919}", "tenancy": "Shared"},
            physical_id=f"i-{i:012d}{'abcdef0123456789' * 8}",
        )
        for i in range(30000)
    ]

    store.save(huge)


# -- store integration ----------------------------------------------------


def test_a_normal_save_and_load_round_trips_through_the_store() -> None:
    table = FakeTable()
    store = DynamoStateStore(table)
    original = wide_snapshot(50)

    store.save(original)
    restored = store.load(STACK_ID)

    assert restored is not None
    assert len(restored.resources) == 50
    assert restored.monthly_cost == original.monthly_cost


def test_the_size_guard_sits_below_the_real_dynamo_limit() -> None:
    """The estimate cannot account for undocumented per-attribute overhead, so
    the margin is deliberate."""
    assert MAX_ITEM_BYTES < 400_000
