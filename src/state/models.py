"""Stack snapshots.

What the plugin remembers about a stack between deployments. Without this there
is no "before", and therefore no delta — only a current total (S11).

The central decision here is **what gets compared**. Comparing resolved template
properties wholesale would be wrong: editing a tag, a description, or an IAM
policy document would surface as a cost change with a delta of zero. So a
resource's identity for diffing purposes is its *pricing dimensions* — the
lookup keys and quantities that actually determine what it costs.

That means a change is only reported when it moves money.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pricing import Confidence, PricedInventory, PricedResource, PricingClass, money

#: Query attributes that describe structure rather than anything a reader would
#: recognise as a change, so they are excluded from the human-facing dimension
#: comparison.
_STRUCTURAL_ATTRIBUTES = frozenset({"productFamily", "usageFamily", "group"})

#: DynamoDB rejects any item over 400KB. The margin covers the summary attributes
#: stored alongside the compressed blob, which are small but not free.
MAX_ITEM_BYTES = 380_000


class SnapshotTooLarge(Exception):
    """A snapshot will not fit in a DynamoDB item even compressed.

    Raised rather than letting DynamoDB return a ``ValidationException``, which
    says nothing about which stack or how far over the limit it was.
    """


def _compress(payload: str) -> bytes:
    # mtime=0 so the same resources always produce identical bytes. Without it
    # gzip embeds a timestamp and two snapshots of an unchanged stack would
    # differ, which makes stored items impossible to compare directly.
    return gzip.compress(payload.encode("utf-8"), mtime=0)


def _decompress(value: Any) -> str:
    """Read a compressed resources blob.

    boto3 hands back a ``Binary`` wrapper rather than ``bytes``, and a plain fake
    in a test hands back ``bytes``, so both are accepted.
    """
    raw = getattr(value, "value", value)
    return gzip.decompress(raw).decode("utf-8")


def _item_size(item: dict[str, Any]) -> int:
    """Approximate the item size the way DynamoDB measures it.

    DynamoDB counts the UTF-8 length of every attribute *name* plus its value,
    with binary counted as its raw byte length. This is an estimate — the exact
    accounting includes per-attribute overhead AWS does not publish — which is
    why ``MAX_ITEM_BYTES`` sits 20KB below the real 400KB ceiling rather than at
    it.
    """
    total = 0
    for name, value in item.items():
        total += len(name.encode("utf-8"))
        if value is None:
            total += 1
        elif isinstance(value, bytes):
            total += len(value)
        elif isinstance(value, str):
            total += len(value.encode("utf-8"))
        else:
            total += len(str(value).encode("utf-8"))
    return total


@dataclass
class SnapshotComponent:
    """One priced line, as remembered."""

    label: str
    query_key: str
    quantity: Decimal
    monthly_list: Decimal
    unit_price: Decimal | None = None

    def to_item(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "queryKey": self.query_key,
            "quantity": str(self.quantity),
            "monthlyList": str(self.monthly_list),
            "unitPrice": str(self.unit_price) if self.unit_price is not None else None,
        }

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> SnapshotComponent:
        unit_price = item.get("unitPrice")
        return cls(
            label=str(item.get("label") or ""),
            query_key=str(item.get("queryKey") or ""),
            quantity=Decimal(str(item.get("quantity") or 0)),
            monthly_list=Decimal(str(item.get("monthlyList") or 0)),
            unit_price=Decimal(str(unit_price)) if unit_price is not None else None,
        )


@dataclass
class SnapshotResource:
    """One resource, priced and remembered."""

    logical_id: str
    resource_type: str
    pricing_class: PricingClass
    description: str = ""
    confidence: Confidence = Confidence.HIGH
    physical_id: str | None = None
    monthly_list: Decimal = Decimal(0)
    monthly_cost: Decimal = Decimal(0)
    components: list[SnapshotComponent] = field(default_factory=list)
    dimensions: dict[str, str] = field(default_factory=dict)
    excluded: list[str] = field(default_factory=list)
    reason: str | None = None
    deletion_policy: str | None = None
    update_replace_policy: str | None = None

    @property
    def is_priced(self) -> bool:
        return (
            self.pricing_class is PricingClass.DETERMINISTIC
            and self.confidence is not Confidence.UNAVAILABLE
        )

    @property
    def is_retained(self) -> bool:
        """True when removing this resource from a stack does not stop the bill.

        ``DeletionPolicy: Retain`` leaves the resource in place, so it outlives
        the stack and keeps charging. Counting it as a saving would overstate
        every teardown that keeps a bucket or a database behind.

        ``Snapshot`` is not treated as retention: the resource itself is deleted
        and only a snapshot remains, which is a different and much smaller cost.
        """
        return (self.deletion_policy or "").strip().lower() == "retain"

    @property
    def is_retained_on_replacement(self) -> bool:
        return (self.update_replace_policy or "").strip().lower() == "retain"

    @property
    def fingerprint(self) -> tuple[tuple[str, str], ...]:
        """What must change for the cost to change.

        Derived from the priced components rather than the template properties,
        so a tag edit produces an identical fingerprint and is correctly reported
        as no change at all.
        """
        return tuple(
            sorted((c.query_key, str(c.quantity)) for c in self.components)
        )

    def to_item(self) -> dict[str, Any]:
        return {
            "logicalId": self.logical_id,
            "resourceType": self.resource_type,
            "pricingClass": self.pricing_class.value,
            "description": self.description,
            "confidence": self.confidence.value,
            "physicalId": self.physical_id,
            "monthlyList": str(self.monthly_list),
            "monthlyCost": str(self.monthly_cost),
            "components": [c.to_item() for c in self.components],
            "dimensions": dict(self.dimensions),
            "excluded": list(self.excluded),
            "reason": self.reason,
            "deletionPolicy": self.deletion_policy,
            "updateReplacePolicy": self.update_replace_policy,
        }

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> SnapshotResource:
        return cls(
            logical_id=str(item.get("logicalId") or ""),
            resource_type=str(item.get("resourceType") or ""),
            pricing_class=PricingClass(item.get("pricingClass") or "UNSUPPORTED"),
            description=str(item.get("description") or ""),
            confidence=Confidence(item.get("confidence") or "UNAVAILABLE"),
            physical_id=item.get("physicalId"),
            monthly_list=Decimal(str(item.get("monthlyList") or 0)),
            monthly_cost=Decimal(str(item.get("monthlyCost") or 0)),
            components=[
                SnapshotComponent.from_item(c) for c in item.get("components") or []
            ],
            dimensions=dict(item.get("dimensions") or {}),
            excluded=list(item.get("excluded") or []),
            reason=item.get("reason"),
            deletion_policy=item.get("deletionPolicy"),
            update_replace_policy=item.get("updateReplacePolicy"),
        )

    @classmethod
    def from_priced(
        cls, priced: PricedResource, physical_id: str | None = None
    ) -> SnapshotResource:
        components = [
            SnapshotComponent(
                label=component.label,
                query_key=component.query.key,
                quantity=component.quantity,
                monthly_list=component.monthly_list,
                unit_price=component.unit_price,
            )
            for component in priced.components
        ]

        dimensions: dict[str, str] = {}
        for component in priced.components:
            for key, value in component.query.attributes:
                if key not in _STRUCTURAL_ATTRIBUTES:
                    dimensions[key] = value

        return cls(
            logical_id=priced.logical_id,
            resource_type=priced.resource_type,
            pricing_class=priced.pricing_class,
            description=priced.description,
            confidence=priced.confidence,
            physical_id=physical_id,
            monthly_list=priced.monthly_list,
            monthly_cost=priced.monthly_cost,
            components=components,
            dimensions=dimensions,
            excluded=list(priced.excluded),
            reason=priced.reason,
            deletion_policy=priced.deletion_policy,
            update_replace_policy=priced.update_replace_policy,
        )


@dataclass
class StackSnapshot:
    """A stack's priced inventory at one point in time."""

    stack_id: str
    stack_name: str
    account: str
    region: str
    resources: list[SnapshotResource] = field(default_factory=list)
    captured_at: str = ""
    client_request_token: str | None = None
    event_time: str | None = None
    status: str | None = None
    price_list_version: str | None = None
    discount_percent: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        if not self.captured_at:
            self.captured_at = datetime.now(UTC).isoformat()

    @property
    def by_logical_id(self) -> dict[str, SnapshotResource]:
        return {r.logical_id: r for r in self.resources}

    @property
    def monthly_list(self) -> Decimal:
        return sum((r.monthly_list for r in self.resources if r.is_priced), Decimal(0))

    @property
    def monthly_cost(self) -> Decimal:
        return sum((r.monthly_cost for r in self.resources if r.is_priced), Decimal(0))

    @property
    def is_empty(self) -> bool:
        return not self.resources

    def to_item(self) -> dict[str, Any]:
        """Shape written to the state store.

        Resources are stored as one gzipped JSON document under
        ``resourcesGzip``. A single stack is capped at 500 resources by
        CloudFormation and would fit uncompressed, but nested rollup aggregates
        every descendant into the root's snapshot, and a large nested application
        exceeds 400KB uncompressed.

        Compression is unconditional rather than applied above a threshold. A
        threshold would mean the compressed path only ever executes on the
        stacks already closest to failing — the exact opposite of where you want
        your least-exercised code. This way a three-resource stack and a
        fifteen-hundred-resource stack take the same path.

        Every summary attribute stays plain and queryable. Only the per-resource
        detail is opaque, and :meth:`from_item` is the one thing that needs to
        read it.

        Raises:
            SnapshotTooLarge: Compressed and still over the limit.
        """
        item: dict[str, Any] = {
            "pk": self.stack_id,
            "stackName": self.stack_name,
            "account": self.account,
            "region": self.region,
            "capturedAt": self.captured_at,
            "clientRequestToken": self.client_request_token,
            "eventTime": self.event_time,
            "status": self.status,
            "priceListVersion": self.price_list_version,
            "discountPercent": str(self.discount_percent),
            "monthlyList": str(money(self.monthly_list)),
            "monthlyCost": str(money(self.monthly_cost)),
            "resourceCount": len(self.resources),
            "resourcesGzip": _compress(
                json.dumps([r.to_item() for r in self.resources])
            ),
        }

        size = _item_size(item)
        if size > MAX_ITEM_BYTES:
            raise SnapshotTooLarge(
                f"Snapshot for {self.stack_name or self.stack_id} is {size:,} bytes "
                f"compressed, over the {MAX_ITEM_BYTES:,} byte limit, "
                f"with {len(self.resources):,} resources"
            )

        return item

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> StackSnapshot:
        payload = cls._read_resources(item)

        return cls(
            stack_id=str(item.get("pk") or ""),
            stack_name=str(item.get("stackName") or ""),
            account=str(item.get("account") or ""),
            region=str(item.get("region") or ""),
            resources=[SnapshotResource.from_item(r) for r in payload],
            captured_at=str(item.get("capturedAt") or ""),
            client_request_token=item.get("clientRequestToken"),
            event_time=item.get("eventTime"),
            status=item.get("status"),
            price_list_version=item.get("priceListVersion"),
            discount_percent=Decimal(str(item.get("discountPercent") or 0)),
        )

    @staticmethod
    def _read_resources(item: dict[str, Any]) -> list[dict[str, Any]]:
        """Read the resources blob in either the compressed or the legacy form.

        Snapshots written before compression exist and must keep loading. If they
        did not, the first deployment after this change would find no readable
        history for any stack and report a baseline instead of a delta — a silent
        one-off loss of every stack's "before".
        """
        compressed = item.get("resourcesGzip")
        if compressed:
            return list(json.loads(_decompress(compressed)))

        legacy = item.get("resources")
        if isinstance(legacy, str):
            return list(json.loads(legacy)) if legacy else []
        return list(legacy or [])

    @classmethod
    def from_inventory(
        cls,
        inventory: PricedInventory,
        stack_id: str,
        stack_name: str,
        account: str,
        region: str,
        physical_ids: dict[str, str] | None = None,
        client_request_token: str | None = None,
        event_time: str | None = None,
        status: str | None = None,
    ) -> StackSnapshot:
        """Build a snapshot from a priced inventory.

        ``physical_ids`` comes from ``ListStackResources``, which is where
        template-derived data meets stack-derived data. Physical IDs are what
        make replacement detectable: a resource can keep its logical ID while
        being destroyed and recreated.
        """
        physical_ids = physical_ids or {}
        resources = [
            SnapshotResource.from_priced(
                priced, physical_id=physical_ids.get(priced.logical_id)
            )
            for priced in inventory.resources
        ]

        basis = inventory.basis
        return cls(
            stack_id=stack_id,
            stack_name=stack_name,
            account=account,
            region=region,
            resources=resources,
            client_request_token=client_request_token,
            event_time=event_time,
            status=status,
            price_list_version=basis.price_list_version if basis else None,
            discount_percent=basis.discount_percent if basis else Decimal(0),
        )

    @classmethod
    def empty_like(cls, other: StackSnapshot, status: str | None = None) -> StackSnapshot:
        """An empty snapshot for the same stack.

        Used for deletion, where every resource is removed and the correct delta
        is negative. The stored snapshot supplies the "before", because once a
        stack is gone its resources cannot be inspected.
        """
        return cls(
            stack_id=other.stack_id,
            stack_name=other.stack_name,
            account=other.account,
            region=other.region,
            resources=[],
            status=status,
            price_list_version=other.price_list_version,
            discount_percent=other.discount_percent,
        )


def total_of(resources: Iterable[SnapshotResource], discounted: bool = False) -> Decimal:
    """Sum the priced resources in a collection."""
    return sum(
        (
            (r.monthly_cost if discounted else r.monthly_list)
            for r in resources
            if r.is_priced
        ),
        Decimal(0),
    )
