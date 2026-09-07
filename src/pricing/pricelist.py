"""AWS Price List response parsing.

``GetProducts`` returns ``PriceList`` as a list of JSON strings, each holding one
product's metadata plus its pricing terms. The envelope is documented and stable,
so it is parsed strictly here; anything unexpected is skipped with a reason
rather than guessed at.

Only ``OnDemand`` terms are read. Reserved and Savings Plans terms are ignored
because commitment coverage is dynamic per resource and a template cannot reveal
whether a new resource lands under existing unused commitment.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .models import CURRENCY


@dataclass(frozen=True)
class ParsedProduct:
    """One product with its On-Demand unit price."""

    sku: str
    service_code: str
    product_family: str
    attributes: Mapping[str, str]
    unit_price: Decimal
    unit: str
    version: str
    currency: str = CURRENCY


@dataclass(frozen=True)
class ParseSkip:
    """A product that could not be turned into a price, and why."""

    reason: str
    sku: str | None = None


def _lowest_tier(price_dimensions: Mapping[str, Any]) -> dict[str, Any] | None:
    """Pick the marginal chargeable pricing tier.

    Tiered products list several dimensions distinguished by ``beginRange``. The
    lowest band is normally the right one, but some products put a $0 free-tier
    allowance in the lowest band and the real per-unit rate in a higher one
    (DynamoDB provisioned capacity does this: 25 units free, then a rate). Pricing
    at the $0 band would zero the resource out, so the lowest *positively priced*
    band is used, falling back to the lowest band only if every band is free.
    """

    def begin_range(dimension: dict[str, Any]) -> Decimal:
        try:
            return Decimal(str(dimension.get("beginRange", "0")))
        except (InvalidOperation, ValueError):
            return Decimal(0)

    def is_priced(dimension: dict[str, Any]) -> bool:
        per_unit = dimension.get("pricePerUnit")
        if not isinstance(per_unit, dict) or CURRENCY not in per_unit:
            return False
        try:
            return Decimal(str(per_unit[CURRENCY])) > 0
        except (InvalidOperation, ValueError):
            return False

    tiers = [d for d in price_dimensions.values() if isinstance(d, dict)]
    if not tiers:
        return None

    priced = [d for d in tiers if is_priced(d)]
    return min(priced or tiers, key=begin_range)


def parse_product(raw: str | Mapping[str, Any]) -> ParsedProduct | ParseSkip:
    """Parse one entry from a ``GetProducts`` ``PriceList``.

    Args:
        raw: A JSON string, or the already-decoded mapping.

    Returns:
        A :class:`ParsedProduct`, or a :class:`ParseSkip` explaining why not.
    """
    if isinstance(raw, str):
        try:
            payload: Any = json.loads(raw)
        except json.JSONDecodeError as exc:
            return ParseSkip(f"Not valid JSON: {exc}")
    else:
        payload = raw

    if not isinstance(payload, dict):
        return ParseSkip("Product entry is not a mapping")

    product = payload.get("product")
    if not isinstance(product, dict):
        return ParseSkip("Missing product block")

    sku = product.get("sku")
    if not isinstance(sku, str) or not sku:
        return ParseSkip("Missing sku")

    attributes = product.get("attributes")
    if not isinstance(attributes, dict):
        return ParseSkip("Missing product attributes", sku=sku)

    terms = payload.get("terms")
    if not isinstance(terms, dict):
        return ParseSkip("Missing terms block", sku=sku)

    on_demand = terms.get("OnDemand")
    if not isinstance(on_demand, dict) or not on_demand:
        # Reserved-only products are expected and unremarkable.
        return ParseSkip("No OnDemand terms", sku=sku)

    # Sorted for determinism when a product carries more than one offer term.
    for _, term in sorted(on_demand.items()):
        if not isinstance(term, dict):
            continue
        dimensions = term.get("priceDimensions")
        if not isinstance(dimensions, dict) or not dimensions:
            continue

        tier = _lowest_tier(dimensions)
        if tier is None:
            continue

        per_unit = tier.get("pricePerUnit")
        if not isinstance(per_unit, dict) or CURRENCY not in per_unit:
            continue

        try:
            unit_price = Decimal(str(per_unit[CURRENCY]))
        except (InvalidOperation, ValueError):
            return ParseSkip(
                f"Unparseable price {per_unit.get(CURRENCY)!r}", sku=sku
            )

        return ParsedProduct(
            sku=sku,
            service_code=str(payload.get("serviceCode") or ""),
            product_family=str(product.get("productFamily") or ""),
            attributes={str(k): str(v) for k, v in attributes.items()},
            unit_price=unit_price,
            unit=str(tier.get("unit") or ""),
            version=str(payload.get("version") or ""),
        )

    return ParseSkip("No usable OnDemand price dimension", sku=sku)
