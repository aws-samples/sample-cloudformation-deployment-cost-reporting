"""DynamoDB-backed price cache.

Satisfies the same :class:`~pricing.catalog.PriceCatalog` protocol as the
in-memory implementation, so the engine and its tests never need to know which
one is in use.

Prices are stored as strings, not DynamoDB numbers. Round-tripping money through
anything float-shaped introduces drift that is visible in a cost report.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .models import CURRENCY, PriceQuery, PriceRecord

#: Partition key holding the snapshot's own metadata.
METADATA_KEY = "__meta__"


class PriceCatalogError(RuntimeError):
    """Price cache could not be read consistently and analysis should retry."""


class DynamoPriceCatalog:
    """Reads prices written by the sync job.

    Args:
        table: A boto3 DynamoDB ``Table`` resource, or anything with the same
            ``get_item`` interface.
        fallback: Optional catalog consulted when the table has no entry. Lets a
            static snapshot keep reports flowing if the cache is incomplete.
    """

    def __init__(self, table: Any, fallback: Any | None = None) -> None:
        self._table = table
        self._fallback = fallback
        # A single invocation prices many resources and the same instance type
        # recurs constantly, so lookups are memoised for the container's life.
        self._memo: dict[str, PriceRecord | None] = {}
        self._version: str | None = None
        self._generation: str | None = None
        self.misses: list[str] = []

    @property
    def version(self) -> str:
        if self._version is None:
            self.refresh()
        return self._version or "unknown"

    def refresh(self) -> None:
        """Adopt an atomically activated generation and invalidate warm caches."""
        version, generation = self._read_metadata()
        if generation != self._generation or version != self._version:
            self._memo.clear()
            self.misses.clear()
            self._generation = generation
            self._version = version

    def _read_metadata(self) -> tuple[str, str | None]:
        try:
            response = self._table.get_item(
                Key={"pk": METADATA_KEY}, ConsistentRead=True
            )
        except Exception as exc:
            raise PriceCatalogError("Could not read active price generation") from exc

        item = response.get("Item") or {}
        version = str(item.get("priceListVersion") or "unknown")
        generation = item.get("activeGeneration")
        return version, str(generation) if generation else None

    def lookup(self, query: PriceQuery) -> PriceRecord | None:
        if self._version is None:
            self.refresh()
        key = query.key
        if key in self._memo:
            return self._memo[key]

        record = self._fetch(key)

        if record is None and self._fallback is not None:
            record = self._fallback.lookup(query)

        if record is None:
            self.misses.append(key)

        self._memo[key] = record
        return record

    def _fetch(self, key: str) -> PriceRecord | None:
        storage_key = f"{self._generation}#{key}" if self._generation else key
        try:
            response = self._table.get_item(
                Key={"pk": storage_key}, ConsistentRead=True
            )
        except Exception as exc:
            raise PriceCatalogError(f"Could not read cached price {key}") from exc

        item = response.get("Item")
        if not item:
            return None

        try:
            unit_price = Decimal(str(item["unitPrice"]))
        except (KeyError, InvalidOperation, ValueError) as exc:
            raise PriceCatalogError(f"Cached price {key} is malformed") from exc

        return PriceRecord(
            unit_price=unit_price,
            unit=str(item.get("unit") or ""),
            price_list_version=str(item.get("priceListVersion") or "unknown"),
            currency=str(item.get("currency") or CURRENCY),
            sku=item.get("sku"),
        )

    def __bool__(self) -> bool:
        return True


@dataclass
class DynamoPriceWriter:
    """Sink for :meth:`~pricing.sync.PriceSync.run`.

    Buffers through DynamoDB's batch writer so a full sync is not one request per
    price.
    """

    table: Any
    generation: str = ""

    def __post_init__(self) -> None:
        self._manager: Any | None = None
        self._batch: Any | None = None
        self.written = 0

    def __enter__(self) -> DynamoPriceWriter:
        # The manager is held, not recreated on exit. Calling batch_writer()
        # again would open a second batch and leave the first one unflushed.
        #
        # overwrite_by_pkeys de-duplicates buffered writes sharing a pk, keeping
        # the last. Several Price List products can resolve to the same cache key,
        # and without this two writes for one pk can land in the same 25-item
        # batch, which BatchWriteItem rejects with "Provided list of item keys
        # contains duplicates". This matches the in-memory catalog's
        # last-writer-wins behaviour on a repeated key.
        self._manager = self.table.batch_writer(overwrite_by_pkeys=["pk"])
        self._batch = self._manager.__enter__()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        if self._manager is not None:
            self._manager.__exit__(*exc_info)
            self._manager = None
            self._batch = None

    def __call__(self, query: PriceQuery, record: PriceRecord) -> None:
        target = self._batch if self._batch is not None else self.table
        target.put_item(
            Item={
                "pk": f"{self.generation}#{query.key}" if self.generation else query.key,
                "generation": self.generation,
                "serviceCode": query.service_code,
                "region": query.region,
                # String, not Number: money must not round-trip through a float.
                "unitPrice": str(record.unit_price),
                "unit": record.unit,
                "currency": record.currency,
                "priceListVersion": record.price_list_version,
                **({"sku": record.sku} if record.sku else {}),
            }
        )
        self.written += 1

    def write_metadata(self, price_list_version: str) -> None:
        """Atomically activate this complete generation for all readers."""
        self.table.put_item(
            Item={
                "pk": METADATA_KEY,
                "activeGeneration": self.generation,
                "priceListVersion": price_list_version,
                "syncedAt": datetime.now(UTC).isoformat(),
                "priceCount": str(self.written),
            }
        )
