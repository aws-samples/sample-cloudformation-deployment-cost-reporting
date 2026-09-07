"""Core types and conventions for pricing.

Money is ``Decimal`` throughout. Float accumulation drift is visible in a cost
report and undermines the arithmetic people are meant to be able to check.

The conventions in this module are the ones stated in every report's footer
(S20). They are pinned here so a figure produced today can be reproduced later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Any

# -- conventions (S20) ----------------------------------------------------

#: AWS's standard month for hourly resources.
HOURS_PER_MONTH = Decimal("730")

#: Rate type. Commitment-based pricing is deliberately not modelled; coverage is
#: dynamic and per-resource, so a template cannot reveal whether a new instance
#: lands under existing unused commitment or is charged in full.
RATE_TYPE = "On-Demand"

CURRENCY = "USD"

#: Operating system is not derivable from a template. ``ImageId`` is an opaque
#: AMI identifier, and resolving it to a platform needs an API call against the
#: account. Linux is assumed and stated, the same way 730 hours is.
DEFAULT_OPERATING_SYSTEM = "Linux"
DEFAULT_TENANCY = "Shared"

_CENTS = Decimal("0.01")


def money(value: Decimal) -> Decimal:
    """Round to cents, half-up, the way an invoice would."""
    return value.quantize(_CENTS, rounding=ROUND_HALF_UP)


def apply_discount(list_cost: Decimal, discount_percent: Decimal) -> Decimal:
    """Apply a flat negotiated discount to a list price.

    A blunt instrument by design. It cannot model Savings Plans or Reserved
    Instances, and reports say so rather than implying the figure is a bill.
    """
    if discount_percent <= 0:
        return list_cost
    factor = (Decimal(100) - Decimal(discount_percent)) / Decimal(100)
    return list_cost * factor


# -- classification -------------------------------------------------------


class PricingClass(str, Enum):
    """How a resource relates to cost.

    ``FREE`` exists to keep the coverage metric honest. A template is mostly VPC
    plumbing, security groups, and IAM roles; counting those as gaps would show
    40% coverage on a perfectly well-priced stack and make the tool look broken.
    Free resources are excluded from the coverage denominator entirely.
    """

    DETERMINISTIC = "DETERMINISTIC"
    USAGE_BASED = "USAGE_BASED"
    FREE = "FREE"
    UNSUPPORTED = "UNSUPPORTED"
    #: Type is known and priceable, but a property it needs did not resolve.
    UNRESOLVED = "UNRESOLVED"


class Confidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    UNAVAILABLE = "UNAVAILABLE"


#: Worst-wins ordering, so a resource is only as trustworthy as its weakest part.
_CONFIDENCE_RANK = {
    Confidence.UNAVAILABLE: 0,
    Confidence.MEDIUM: 1,
    Confidence.HIGH: 2,
}


def weakest(values: list[Confidence]) -> Confidence:
    if not values:
        return Confidence.UNAVAILABLE
    return min(values, key=lambda c: _CONFIDENCE_RANK[c])


# -- price lookup ---------------------------------------------------------


@dataclass(frozen=True)
class PriceQuery:
    """A normalised request for one unit price.

    Attribute names here are the plugin's own vocabulary, not AWS's. The sync
    job's job is to translate the Price List API's naming into these keys, which
    means an upstream rename is absorbed in one place instead of rippling
    through every mapper.
    """

    service_code: str
    region: str
    attributes: tuple[tuple[str, str], ...] = ()

    @classmethod
    def of(cls, service_code: str, region: str, **attributes: Any) -> PriceQuery:
        normalised = tuple(
            sorted((key, str(value)) for key, value in attributes.items() if value is not None)
        )
        return cls(service_code=service_code, region=region, attributes=normalised)

    @property
    def key(self) -> str:
        """Canonical cache key."""
        joined = ",".join(f"{k}={v}" for k, v in self.attributes)
        return f"{self.service_code}#{self.region}#{joined}"

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return self.key


@dataclass(frozen=True)
class PriceRecord:
    """One unit price, as held in the cache."""

    unit_price: Decimal
    unit: str
    price_list_version: str
    currency: str = CURRENCY
    sku: str | None = None


# -- results --------------------------------------------------------------


@dataclass
class CostComponent:
    """One priced line within a resource.

    Resources are frequently more than one charge. An RDS instance bills for
    compute and storage separately; an EC2 instance with block devices bills for
    the instance and each volume. Modelling a resource as a single number would
    silently drop whichever part was not chosen.
    """

    label: str
    query: PriceQuery
    quantity: Decimal
    unit: str = ""
    unit_price: Decimal | None = None
    monthly_list: Decimal = Decimal(0)
    confidence: Confidence = Confidence.HIGH
    excluded: tuple[str, ...] = ()
    price_list_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "unit": self.unit,
            "quantity": float(self.quantity),
            "unitPrice": float(self.unit_price) if self.unit_price is not None else None,
            "monthlyList": float(money(self.monthly_list)),
            "confidence": self.confidence.value,
            "excluded": list(self.excluded),
        }


@dataclass
class PricedResource:
    """The pricing outcome for a single resolved resource."""

    logical_id: str
    resource_type: str
    pricing_class: PricingClass
    description: str = ""
    components: list[CostComponent] = field(default_factory=list)
    monthly_list: Decimal = Decimal(0)
    monthly_cost: Decimal = Decimal(0)
    discount_percent: Decimal = Decimal(0)
    confidence: Confidence = Confidence.HIGH
    assumptions: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()
    reason: str | None = None
    price_list_version: str | None = None
    currency: str = CURRENCY
    #: Carried through from the template because it decides whether removing a
    #: resource is actually a saving. A ``Retain`` policy means the resource
    #: outlives its stack and keeps billing.
    deletion_policy: str | None = None
    #: Applied when CloudFormation replaces (rather than deletes) a resource.
    update_replace_policy: str | None = None

    @property
    def is_priced(self) -> bool:
        """True when this resource contributes a figure to the totals."""
        return (
            self.pricing_class is PricingClass.DETERMINISTIC
            and self.confidence is not Confidence.UNAVAILABLE
        )

    @property
    def counts_toward_coverage(self) -> bool:
        """Free resources are excluded from the coverage denominator."""
        return self.pricing_class is not PricingClass.FREE

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "logicalId": self.logical_id,
            "resourceType": self.resource_type,
            "pricingClass": self.pricing_class.value,
            "description": self.description,
            "confidence": self.confidence.value,
            "currency": self.currency,
        }

        if self.is_priced:
            payload["monthlyCostList"] = float(money(self.monthly_list))
            payload["monthlyCost"] = float(money(self.monthly_cost))
            payload["discountPercent"] = float(self.discount_percent)
            payload["components"] = [c.to_dict() for c in self.components]

        if self.assumptions:
            payload["assumptions"] = list(self.assumptions)
        if self.excluded:
            payload["excluded"] = list(self.excluded)
        if self.reason:
            payload["reason"] = self.reason
        if self.price_list_version:
            payload["priceListVersion"] = self.price_list_version

        return payload


def coverage_of(items: Any) -> Coverage:
    """Count coverage buckets across anything priced.

    Works on both :class:`PricedResource` and the state store's snapshot
    resources, since both expose ``pricing_class`` and ``is_priced``. Shared so
    the two cannot drift apart and report different percentages for the same
    stack.
    """
    counts = Coverage()
    for item in items:
        counts.total += 1
        pricing_class = item.pricing_class
        if pricing_class is PricingClass.FREE:
            counts.free += 1
        elif pricing_class is PricingClass.USAGE_BASED:
            counts.usage_based += 1
        elif pricing_class is PricingClass.UNSUPPORTED:
            counts.unsupported += 1
        elif pricing_class is PricingClass.UNRESOLVED:
            counts.unresolved += 1
        elif item.is_priced:
            counts.priced += 1
        else:
            # A priceable type with no available price is a gap, not a success.
            counts.unresolved += 1
    return counts


@dataclass
class Coverage:
    """Per-report coverage figures (S17)."""

    total: int = 0
    priced: int = 0
    usage_based: int = 0
    unsupported: int = 0
    unresolved: int = 0
    free: int = 0

    @property
    def denominator(self) -> int:
        """Everything except free resources."""
        return self.priced + self.usage_based + self.unsupported + self.unresolved

    @property
    def priced_percent(self) -> float:
        if self.denominator == 0:
            return 100.0
        return round(self.priced / self.denominator * 100, 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "resourcesTotal": self.total,
            "resourcesPriced": self.priced,
            "resourcesUsageBased": self.usage_based,
            "resourcesUnsupported": self.unsupported,
            "resourcesUnresolved": self.unresolved,
            "resourcesFree": self.free,
            "pricedPercent": self.priced_percent,
        }
