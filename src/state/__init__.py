"""Stack state and change detection.

Steps 3 and 4 of the build order. Remembers what a stack cost, and works out what
changed when it is deployed again.

Typical use::

    from state import DynamoStateStore, DeltaAction, diff_snapshots, StackSnapshot

    store = DynamoStateStore(table)
    before = store.load(stack_id)

    after = StackSnapshot.from_inventory(
        inventory, stack_id, stack_name, account, region, physical_ids
    )

    delta = diff_snapshots(before, after, action=DeltaAction.UPDATE)
    store.save(after)

Three rules enforced here rather than left to callers:

* Change is detected on pricing fingerprints, so a tag edit is not a cost change.
* No history yields a baseline, never an invented delta (S12).
* A snapshot that cannot be stored *invalidates* history rather than leaving the
  previous one in place. A stale snapshot describes the deployment before last, so
  diffing against it reports a two-deployment delta as one — a wrong number rather
  than a missing one.
"""

from .diff import (
    ChangedResource,
    DeltaAction,
    Direction,
    StackDelta,
    diff_deletion,
    diff_snapshots,
)
from .models import (
    MAX_ITEM_BYTES,
    SnapshotComponent,
    SnapshotResource,
    SnapshotTooLarge,
    StackSnapshot,
    total_of,
)
from .store import DynamoStateStore, InMemoryStateStore, StateStore, StateStoreError

__all__ = [
    "MAX_ITEM_BYTES",
    "ChangedResource",
    "DeltaAction",
    "Direction",
    "DynamoStateStore",
    "InMemoryStateStore",
    "SnapshotComponent",
    "SnapshotResource",
    "SnapshotTooLarge",
    "StackDelta",
    "StackSnapshot",
    "StateStore",
    "StateStoreError",
    "diff_deletion",
    "diff_snapshots",
    "total_of",
]
