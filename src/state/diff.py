"""The diff engine.

Compares two snapshots and produces what was added, removed, and resized, with
the cost of each.

Two decisions carry most of the weight:

**Change is detected on pricing fingerprints, not properties.** A resource is
"changed" only when a lookup key or quantity moved. Editing a tag, a description,
or a policy document produces an identical fingerprint and is correctly reported
as no change, rather than a change with a delta of zero.

**No history means a baseline, not a delta.** The first time the plugin sees a
pre-existing stack it has no "before". Diffing against nothing would report every
existing resource as newly added, so installing into a mature account would open
with a flood of large, wrong increases (S12). The one exception is a genuine
``CREATE``, where everything really is new.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any

from pricing import Confidence, Coverage, PricingClass, coverage_of, money, weakest

from .models import SnapshotResource, StackSnapshot, total_of


class DeltaAction(str, Enum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    ROLLBACK = "ROLLBACK"
    #: First sighting of a stack that already existed. Inventory, not change.
    BASELINE = "BASELINE"


class Direction(str, Enum):
    INCREASE = "INCREASE"
    DECREASE = "DECREASE"
    NEUTRAL = "NEUTRAL"


@dataclass
class ChangedResource:
    """A resource that survived but whose cost moved."""

    before: SnapshotResource
    after: SnapshotResource
    changed_dimensions: list[str] = field(default_factory=list)
    replacement: bool = False

    @property
    def logical_id(self) -> str:
        return self.after.logical_id

    @property
    def resource_type(self) -> str:
        return self.after.resource_type

    @property
    def delta_monthly_list(self) -> Decimal:
        return self._after_list - self._before_list

    @property
    def delta_monthly_cost(self) -> Decimal:
        after = self.after.monthly_cost if self.after.is_priced else Decimal(0)
        before = self.before.monthly_cost if self.before.is_priced else Decimal(0)
        return after - before

    @property
    def _before_list(self) -> Decimal:
        return self.before.monthly_list if self.before.is_priced else Decimal(0)

    @property
    def _after_list(self) -> Decimal:
        return self.after.monthly_list if self.after.is_priced else Decimal(0)

    @property
    def confidence(self) -> Confidence:
        """A delta is only as trustworthy as its weaker side."""
        return weakest([self.before.confidence, self.after.confidence])

    @property
    def pricing_class_changed(self) -> bool:
        """True when the resource moved between pricing buckets.

        Switching a DynamoDB table to on-demand, for instance, turns a known
        monthly figure into a usage-based one.
        """
        return self.before.pricing_class is not self.after.pricing_class

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "logicalId": self.logical_id,
            "resourceType": self.resource_type,
            "before": {
                "description": self.before.description,
                "monthlyCost": float(money(self.before.monthly_cost)),
            },
            "after": {
                "description": self.after.description,
                "monthlyCost": float(money(self.after.monthly_cost)),
            },
            "deltaMonthly": float(money(self.delta_monthly_cost)),
            "replacement": self.replacement,
            "changedDimensions": list(self.changed_dimensions),
            "confidence": self.confidence.value,
        }
        if self.pricing_class_changed:
            payload["pricingClassChanged"] = {
                "from": self.before.pricing_class.value,
                "to": self.after.pricing_class.value,
            }
        return payload


@dataclass
class StackDelta:
    """What one stack operation did to the bill."""

    action: DeltaAction
    stack_id: str
    stack_name: str
    account: str
    region: str
    after: StackSnapshot
    before: StackSnapshot | None = None
    added: list[SnapshotResource] = field(default_factory=list)
    removed: list[SnapshotResource] = field(default_factory=list)
    changed: list[ChangedResource] = field(default_factory=list)
    #: Left the stack but survives it, so no saving was realised.
    retained: list[SnapshotResource] = field(default_factory=list)
    unchanged: int = 0

    # -- flags ------------------------------------------------------------

    @property
    def is_baseline(self) -> bool:
        return self.action is DeltaAction.BASELINE

    @property
    def has_movement(self) -> bool:
        return bool(self.added or self.removed or self.changed or self.retained)

    # -- totals -----------------------------------------------------------

    @property
    def added_monthly(self) -> Decimal:
        return total_of(self.added, discounted=True)

    @property
    def removed_monthly(self) -> Decimal:
        return total_of(self.removed, discounted=True)

    @property
    def retained_monthly(self) -> Decimal:
        """Cost that left the stack but did not leave the bill."""
        return total_of(self.retained, discounted=True)

    @property
    def changed_monthly(self) -> Decimal:
        return sum((c.delta_monthly_cost for c in self.changed), Decimal(0))

    @property
    def net_monthly(self) -> Decimal:
        return self.added_monthly - self.removed_monthly + self.changed_monthly

    @property
    def net_annual(self) -> Decimal:
        return self.net_monthly * 12

    @property
    def previous_monthly(self) -> Decimal:
        return self.before.monthly_cost if self.before else Decimal(0)

    @property
    def current_monthly(self) -> Decimal:
        return self.after.monthly_cost

    @property
    def direction(self) -> Direction:
        if self.net_monthly > 0:
            return Direction.INCREASE
        if self.net_monthly < 0:
            return Direction.DECREASE
        return Direction.NEUTRAL

    @property
    def reconciles(self) -> bool:
        """Self-check: the delta must explain the change in the total.

        ``added - removed + changed`` equals ``current - previous`` plus whatever
        was retained. Unchanged resources contribute nothing to either side, and
        retained resources leave the stack total without leaving the bill, so
        they show up as a difference between the two figures rather than as a
        saving.

        If this is ever false, the diff has lost or double-counted a resource.
        """
        if self.is_baseline:
            return self.net_monthly == 0
        expected = self.current_monthly - self.previous_monthly + self.retained_monthly
        return money(self.net_monthly) == money(expected)

    # -- reporting --------------------------------------------------------

    @property
    def inventory(self) -> list[SnapshotResource]:
        """Full priced inventory. The payload of a baseline report."""
        return list(self.after.resources)

    @property
    def coverage(self) -> Coverage:
        """Coverage of the resulting stack.

        A deletion leaves nothing behind, so its coverage is reported against
        what was removed — otherwise a delete report would claim 100% coverage of
        an empty set, which says nothing useful.
        """
        if self.after.is_empty and self.before is not None:
            return coverage_of(self.before.resources)
        return coverage_of(self.after.resources)

    @property
    def unpriced(self) -> dict[str, list[SnapshotResource]]:
        source = (
            self.before.resources
            if self.after.is_empty and self.before is not None
            else self.after.resources
        )
        buckets: dict[str, list[SnapshotResource]] = {
            "usageBased": [],
            "unsupported": [],
            "unresolved": [],
        }
        for resource in source:
            if resource.pricing_class is PricingClass.USAGE_BASED:
                buckets["usageBased"].append(resource)
            elif resource.pricing_class is PricingClass.UNSUPPORTED:
                buckets["unsupported"].append(resource)
            elif resource.pricing_class is PricingClass.UNRESOLVED or not (
                resource.pricing_class is PricingClass.FREE or resource.is_priced
            ):
                buckets["unresolved"].append(resource)
        return buckets

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "action": self.action.value,
            "isBaseline": self.is_baseline,
            "stackId": self.stack_id,
            "stackName": self.stack_name,
            "account": self.account,
            "region": self.region,
            "direction": self.direction.value,
            "totals": {
                "addedMonthly": float(money(self.added_monthly)),
                "removedMonthly": float(money(self.removed_monthly)),
                "changedMonthly": float(money(self.changed_monthly)),
                "retainedMonthly": float(money(self.retained_monthly)),
                "netMonthly": float(money(self.net_monthly)),
                "netAnnual": float(money(self.net_annual)),
                "previousStackMonthly": float(money(self.previous_monthly)),
                "currentStackMonthly": float(money(self.current_monthly)),
            },
            "coverage": self.coverage.to_dict(),
            "unpriced": {
                bucket: [
                    {
                        "logicalId": r.logical_id,
                        "resourceType": r.resource_type,
                        "reason": r.reason,
                    }
                    for r in resources
                ]
                for bucket, resources in self.unpriced.items()
            },
            "reconciles": self.reconciles,
        }

        if self.is_baseline:
            payload["inventory"] = [
                {
                    "logicalId": r.logical_id,
                    "resourceType": r.resource_type,
                    "description": r.description,
                    "monthlyCost": float(money(r.monthly_cost)),
                }
                for r in self.inventory
                if r.is_priced
            ]
        else:
            payload["added"] = [_resource_dict(r) for r in self.added]
            payload["removed"] = [_resource_dict(r) for r in self.removed]
            payload["changed"] = [c.to_dict() for c in self.changed]
            payload["unchanged"] = self.unchanged
            if self.retained:
                payload["retained"] = [
                    {
                        **_resource_dict(r),
                        "deletionPolicy": r.deletion_policy,
                        "updateReplacePolicy": r.update_replace_policy,
                    }
                    for r in self.retained
                ]

        return payload


def _resource_dict(resource: SnapshotResource) -> dict[str, Any]:
    return {
        "logicalId": resource.logical_id,
        "resourceType": resource.resource_type,
        "description": resource.description,
        "monthlyCost": float(money(resource.monthly_cost))
        if resource.is_priced
        else 0.0,
        "confidence": resource.confidence.value,
        "excluded": list(resource.excluded),
    }


def _changed_dimensions(
    before: SnapshotResource, after: SnapshotResource
) -> list[str]:
    """Name what moved, for the report."""
    keys = set(before.dimensions) | set(after.dimensions)
    differing = sorted(
        key for key in keys if before.dimensions.get(key) != after.dimensions.get(key)
    )
    if differing:
        return differing
    if before.pricing_class is not after.pricing_class:
        return ["pricingClass"]
    if before.fingerprint != after.fingerprint:
        # Dimensions match but the fingerprint did not, so a quantity moved — a
        # volume resized, or a node count changed.
        return ["quantity"]
    if money(before.monthly_cost) != money(after.monthly_cost):
        # The resource shape is unchanged, but the current price generation or
        # configured discount changed its effective monthly cost.
        return ["unitPrice"]
    if _is_replacement(before, after):
        return ["physicalId"]
    return []


def _is_replacement(before: SnapshotResource, after: SnapshotResource) -> bool:
    """A resource can keep its logical ID while being destroyed and recreated."""
    return bool(
        before.physical_id
        and after.physical_id
        and before.physical_id != after.physical_id
    )


def diff_snapshots(
    before: StackSnapshot | None,
    after: StackSnapshot,
    action: DeltaAction = DeltaAction.UPDATE,
) -> StackDelta:
    """Compare two snapshots.

    Args:
        before: The last stored snapshot, or None when the stack has no history.
        after: The snapshot just computed.
        action: What the CloudFormation event said happened. Used to tell a
            genuine ``CREATE`` (where everything really is new) from a first
            sighting of a pre-existing stack (where nothing has changed yet).

    Returns:
        A :class:`StackDelta`. When ``before`` is None and the action is not
        ``CREATE``, the result is a baseline rather than a delta.
    """
    resolved_action = action
    if before is None and action is not DeltaAction.CREATE:
        # No history, and this was not a creation. Any delta would be invented.
        resolved_action = DeltaAction.BASELINE

    delta = StackDelta(
        action=resolved_action,
        stack_id=after.stack_id,
        stack_name=after.stack_name,
        account=after.account,
        region=after.region,
        after=after,
        before=before,
    )

    if resolved_action is DeltaAction.BASELINE:
        return delta

    previous = before.by_logical_id if before else {}
    current = after.by_logical_id

    for logical_id, resource in current.items():
        prior = previous.get(logical_id)
        if prior is None:
            delta.added.append(resource)
            continue
        replacement = _is_replacement(prior, resource)
        if replacement and resource.is_retained_on_replacement:
            # CloudFormation leaves the old physical resource billing and creates
            # the replacement. Model that as one addition plus one retained item
            # so the realised bill increase and reconciliation are both correct.
            delta.added.append(resource)
            delta.retained.append(prior)
            continue
        cost_changed = money(prior.monthly_cost) != money(resource.monthly_cost)
        if prior.fingerprint != resource.fingerprint or cost_changed or replacement:
            delta.changed.append(
                ChangedResource(
                    before=prior,
                    after=resource,
                    changed_dimensions=_changed_dimensions(prior, resource),
                    replacement=replacement,
                )
            )
        else:
            delta.unchanged += 1

    for logical_id, resource in previous.items():
        if logical_id in current:
            continue
        # A retained resource has left the stack but not the bill, so it is not a
        # saving. Reporting it as one would overstate every teardown that keeps a
        # bucket or a database behind.
        if resource.is_retained:
            delta.retained.append(resource)
        else:
            delta.removed.append(resource)

    delta.added.sort(key=lambda r: r.logical_id)
    delta.removed.sort(key=lambda r: r.logical_id)
    delta.retained.sort(key=lambda r: r.logical_id)
    delta.changed.sort(key=lambda c: c.logical_id)

    return delta


def diff_deletion(before: StackSnapshot) -> StackDelta:
    """Delta for a stack that has been deleted.

    Every resource is removed and the net figure is negative. The stored snapshot
    is what makes this possible: once a stack is gone its resources cannot be
    inspected, so without history there is nothing to report a saving against.
    """
    return diff_snapshots(
        before,
        StackSnapshot.empty_like(before, status="DELETE_COMPLETE"),
        action=DeltaAction.DELETE,
    )
