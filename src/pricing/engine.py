"""The pricing engine.

Takes resolved resources and produces priced ones. Deliberately boring: classify,
look up, multiply, sum. No estimation, no interpolation, no filling in of gaps.

Three rules from the spec are enforced here rather than left to callers:

* A missing price makes one resource UNAVAILABLE and is excluded from totals; it
  never fails the report and never becomes a guess (S16, S19).
* Nothing is dropped silently. Every resource comes back classified (S19).
* Excluded usage components downgrade confidence to MEDIUM rather than being
  quietly ignored (S18).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal

from resolver import Inclusion, ResolvedResource

from .catalog import PriceCatalog
from .dimensions import Classification, ComponentSpec, classify
from .models import (
    CURRENCY,
    DEFAULT_OPERATING_SYSTEM,
    HOURS_PER_MONTH,
    RATE_TYPE,
    Confidence,
    CostComponent,
    Coverage,
    PricedResource,
    PricingClass,
    apply_discount,
    coverage_of,
    money,
    weakest,
)
from .platform import PlatformResolver


@dataclass
class PricingBasis:
    """The conventions in force, reproduced in every report footer (S20)."""

    price_list_version: str
    discount_percent: Decimal = Decimal(0)
    hours_per_month: Decimal = HOURS_PER_MONTH
    rate_type: str = RATE_TYPE
    currency: str = CURRENCY

    def to_dict(self) -> dict[str, object]:
        return {
            "priceListVersion": self.price_list_version,
            "hoursPerMonth": int(self.hours_per_month),
            "rateType": self.rate_type,
            "discountPercent": float(self.discount_percent),
            "currency": self.currency,
            "operatingSystemAssumption": DEFAULT_OPERATING_SYSTEM,
            "note": (
                "Marginal On-Demand cost. May be reduced by existing Savings "
                "Plans or Reserved Instances."
            ),
        }


@dataclass
class PricedInventory:
    """Every resource in a stack, priced or explained."""

    resources: list[PricedResource] = field(default_factory=list)
    basis: PricingBasis | None = None

    @property
    def monthly_list(self) -> Decimal:
        return sum(
            (r.monthly_list for r in self.resources if r.is_priced), Decimal(0)
        )

    @property
    def monthly_cost(self) -> Decimal:
        return sum(
            (r.monthly_cost for r in self.resources if r.is_priced), Decimal(0)
        )

    @property
    def coverage(self) -> Coverage:
        return coverage_of(self.resources)

    def by_class(self, pricing_class: PricingClass) -> list[PricedResource]:
        return [r for r in self.resources if r.pricing_class is pricing_class]

    @property
    def priced(self) -> list[PricedResource]:
        return [r for r in self.resources if r.is_priced]

    def to_dict(self) -> dict[str, object]:
        return {
            "monthlyCostList": float(money(self.monthly_list)),
            "monthlyCost": float(money(self.monthly_cost)),
            "coverage": self.coverage.to_dict(),
            "pricingBasis": self.basis.to_dict() if self.basis else None,
            "resources": [r.to_dict() for r in self.resources],
        }


class PricingEngine:
    """Prices resolved resources against a catalog.

    Args:
        catalog: Where unit prices come from.
        region: Region the stack is deployed in. Prices vary by region.
        discount_percent: Flat negotiated discount against list price. Zero
            means report list price unchanged.
    """

    def __init__(
        self,
        catalog: PriceCatalog,
        region: str,
        discount_percent: Decimal | float | int = 0,
        platforms: PlatformResolver | None = None,
    ) -> None:
        self._catalog = catalog
        self._region = region
        self._discount = Decimal(str(discount_percent))
        # Without a resolver, EC2 instances fall back to the documented Linux
        # assumption, so the engine still works with no EC2 permissions.
        self._platforms = platforms

    @property
    def basis(self) -> PricingBasis:
        return PricingBasis(
            price_list_version=self._catalog.version,
            discount_percent=self._discount,
        )

    def price_inventory(
        self, resources: Iterable[ResolvedResource]
    ) -> PricedInventory:
        """Price a whole stack, skipping resources that were never created."""
        priced = [
            self.price_resource(resource)
            for resource in resources
            # A false Condition means the resource does not exist, so pricing it
            # would invent cost that was never incurred.
            if resource.inclusion is not Inclusion.EXCLUDED
        ]
        return PricedInventory(resources=priced, basis=self.basis)

    def price_resource(self, resource: ResolvedResource) -> PricedResource:
        """Classify and price one resource."""
        classification = classify(resource, self._region, self._platforms)

        if classification.pricing_class is not PricingClass.DETERMINISTIC:
            return PricedResource(
                logical_id=resource.logical_id,
                resource_type=resource.resource_type,
                pricing_class=classification.pricing_class,
                description=classification.description or resource.resource_type,
                confidence=Confidence.UNAVAILABLE,
                reason=classification.reason,
                assumptions=classification.assumptions,
                excluded=classification.excluded,
                discount_percent=self._discount,
                deletion_policy=resource.deletion_policy,
                update_replace_policy=resource.update_replace_policy,
            )

        return self._price_components(resource, classification)

    # -- internals --------------------------------------------------------

    def _price_components(
        self, resource: ResolvedResource, classification: Classification
    ) -> PricedResource:
        components = [self._price_component(spec) for spec in classification.components]

        priced = [c for c in components if c.unit_price is not None]
        unpriced = [c for c in components if c.unit_price is None]

        versions = {c.price_list_version for c in priced if c.price_list_version}
        monthly_list = sum((c.monthly_list for c in components), Decimal(0))

        # Confidence, in order of severity:
        #
        # Nothing priced is not a partial answer, it is no answer — the resource
        # is excluded from totals entirely.
        #
        # Something priced but not everything keeps the known figure and flags
        # the hole. Discarding the whole resource would throw away real, known
        # cost and understate the total, which is the more dangerous direction
        # (S17, S19).
        if not priced:
            confidence = Confidence.UNAVAILABLE
        else:
            confidence = weakest([c.confidence for c in priced])
            if unpriced or classification.excluded:
                confidence = weakest([confidence, Confidence.MEDIUM])

        reason = None
        if unpriced:
            gaps = "; ".join(c.label for c in unpriced)
            prefix = "Partially priced — no price found for" if priced else "No price found for"
            reason = f"{prefix}: {gaps}"

        return PricedResource(
            logical_id=resource.logical_id,
            resource_type=resource.resource_type,
            pricing_class=PricingClass.DETERMINISTIC,
            description=classification.description or resource.resource_type,
            components=components,
            monthly_list=monthly_list,
            monthly_cost=apply_discount(monthly_list, self._discount),
            discount_percent=self._discount,
            confidence=confidence,
            assumptions=classification.assumptions,
            excluded=classification.excluded,
            reason=reason,
            price_list_version=sorted(versions)[0] if versions else None,
            deletion_policy=resource.deletion_policy,
            update_replace_policy=resource.update_replace_policy,
        )

    def _price_component(self, spec: ComponentSpec) -> CostComponent:
        record = self._catalog.lookup(spec.query)

        if record is None:
            # Priced at zero and flagged, never omitted. An omitted component
            # would quietly understate the total.
            return CostComponent(
                label=spec.label,
                query=spec.query,
                quantity=spec.quantity,
                confidence=Confidence.UNAVAILABLE,
                excluded=spec.excluded,
            )

        confidence = Confidence.MEDIUM if spec.excluded else Confidence.HIGH

        return CostComponent(
            label=spec.label,
            query=spec.query,
            quantity=spec.quantity,
            unit=record.unit,
            unit_price=record.unit_price,
            monthly_list=record.unit_price * spec.quantity,
            confidence=confidence,
            excluded=spec.excluded,
            price_list_version=record.price_list_version,
        )
