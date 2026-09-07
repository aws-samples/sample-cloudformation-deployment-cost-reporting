"""Price lookup.

The engine never calls the AWS Price List API directly. Live lookups per event
throttle under load and the payloads are large (S14), so prices are read from a
cache that a scheduled job refreshes daily.

This module defines the lookup interface plus an in-memory implementation. The
DynamoDB-backed implementation satisfies the same protocol, which keeps the
engine testable without AWS credentials.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from .models import PriceQuery, PriceRecord


class PriceCatalog(Protocol):
    """Anything that can answer "what does one unit of this cost?"."""

    def lookup(self, query: PriceQuery) -> PriceRecord | None:
        """Return the unit price, or None when the catalog has no entry."""
        ...

    @property
    def version(self) -> str:
        """Identifier for the price snapshot in use, stamped onto reports."""
        ...


class StaticPriceCatalog:
    """In-memory catalog.

    Used by tests, and as the fallback when the cache is unreachable so a report
    still goes out rather than failing silently.
    """

    def __init__(
        self,
        prices: dict[str, PriceRecord] | None = None,
        version: str = "static",
    ) -> None:
        self._prices: dict[str, PriceRecord] = dict(prices or {})
        self._version = version
        self.misses: list[str] = []

    @property
    def version(self) -> str:
        return self._version

    def put(
        self,
        query: PriceQuery,
        unit_price: str | Decimal,
        unit: str,
        sku: str | None = None,
    ) -> StaticPriceCatalog:
        """Register a price. Returns self so calls can be chained."""
        self._prices[query.key] = PriceRecord(
            unit_price=Decimal(str(unit_price)),
            unit=unit,
            price_list_version=self._version,
            sku=sku,
        )
        return self

    def lookup(self, query: PriceQuery) -> PriceRecord | None:
        record = self._prices.get(query.key)
        if record is None:
            # Recorded rather than raised. A missing price makes one resource
            # UNAVAILABLE; it should not fail the whole report. The list also
            # tells the mapping backlog what to add next.
            self.misses.append(query.key)
        return record

    def __len__(self) -> int:
        return len(self._prices)

    def __bool__(self) -> bool:
        """Always truthy, even when empty.

        Without this, ``__len__`` makes an empty catalog falsy, so the natural
        ``catalog or fallback`` idiom silently discards a deliberately empty
        catalog and substitutes a populated one. An empty catalog is still a
        valid catalog — it simply reports every lookup as a miss.
        """
        return True
