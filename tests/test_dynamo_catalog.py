"""DynamoDB-backed cache, and the full sync-to-price chain."""

import json
from decimal import Decimal

import pytest

from conftest import REGION, make_resource
from pricing import (
    METADATA_KEY,
    Confidence,
    DynamoPriceCatalog,
    DynamoPriceWriter,
    PriceCatalogError,
    PriceQuery,
    PriceRecord,
    PriceSync,
    PricingEngine,
    StaticPriceCatalog,
    SyncRule,
    money,
)

EC2_QUERY = PriceQuery.of(
    "AmazonEC2",
    REGION,
    productFamily="Compute Instance",
    instanceType="t3.large",
    operatingSystem="Linux",
    tenancy="Shared",
)


class FakeBatch:
    def __init__(self, table, overwrite_by_pkeys=None):
        self._table = table
        self._overwrite_by_pkeys = overwrite_by_pkeys
        self._seen = set()
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.closed = True
        return False

    def put_item(self, Item):
        # Mirror BatchWriteItem: a batch containing two writes for the same key
        # is rejected, unless the writer asked to de-duplicate on that key.
        pk = Item["pk"]
        if not self._overwrite_by_pkeys and pk in self._seen:
            raise RuntimeError("Provided list of item keys contains duplicates")
        self._seen.add(pk)
        self._table.put_item(Item=Item)


class FakeTable:
    def __init__(self, items=None):
        self.items = dict(items or {})
        self.get_calls = 0
        self.raise_on_get = False
        self.batches = []

    def get_item(self, Key, ConsistentRead=False):
        self.get_calls += 1
        if self.raise_on_get:
            raise RuntimeError("throttled")
        item = self.items.get(Key["pk"])
        return {"Item": item} if item is not None else {}

    def put_item(self, Item):
        self.items[Item["pk"]] = Item

    def batch_writer(self, overwrite_by_pkeys=None):
        batch = FakeBatch(self, overwrite_by_pkeys=overwrite_by_pkeys)
        self.batches.append(batch)
        return batch


# -- reading --------------------------------------------------------------


def test_lookup_returns_a_stored_price():
    table = FakeTable(
        {
            EC2_QUERY.key: {
                "pk": EC2_QUERY.key,
                "unitPrice": "0.0832",
                "unit": "Hrs",
                "priceListVersion": "20260801000000",
                "sku": "ABC",
            }
        }
    )
    record = DynamoPriceCatalog(table).lookup(EC2_QUERY)

    assert record.unit_price == Decimal("0.0832")
    assert record.unit == "Hrs"
    assert record.sku == "ABC"


def test_missing_entry_is_a_recorded_miss_not_an_error():
    catalog = DynamoPriceCatalog(FakeTable())
    assert catalog.lookup(EC2_QUERY) is None
    assert catalog.misses == [EC2_QUERY.key]


def test_lookups_are_memoised_within_a_container():
    """One invocation prices many resources and instance types recur."""
    table = FakeTable(
        {EC2_QUERY.key: {"pk": EC2_QUERY.key, "unitPrice": "0.0832", "unit": "Hrs"}}
    )
    catalog = DynamoPriceCatalog(table)

    catalog.lookup(EC2_QUERY)
    catalog.lookup(EC2_QUERY)
    catalog.lookup(EC2_QUERY)

    # One metadata-generation read plus one price read; repeated lookups stay in memory.
    assert table.get_calls == 2


def test_misses_are_memoised_until_a_new_generation_is_activated():
    table = FakeTable()
    catalog = DynamoPriceCatalog(table)

    catalog.lookup(EC2_QUERY)
    catalog.lookup(EC2_QUERY)
    assert table.get_calls == 2  # metadata plus one price miss

    table.items[METADATA_KEY] = {
        "pk": METADATA_KEY,
        "activeGeneration": "g2",
        "priceListVersion": "v2",
    }
    table.items[f"g2#{EC2_QUERY.key}"] = {
        "pk": f"g2#{EC2_QUERY.key}",
        "unitPrice": "0.0832",
        "unit": "Hrs",
    }
    catalog.refresh()

    assert catalog.lookup(EC2_QUERY) is not None
    assert catalog.version == "v2"


def test_a_table_error_is_retryable_not_a_cached_miss():
    table = FakeTable()
    table.raise_on_get = True
    catalog = DynamoPriceCatalog(table)

    with pytest.raises(PriceCatalogError):
        catalog.lookup(EC2_QUERY)


def test_malformed_stored_price_is_a_cache_error():
    table = FakeTable(
        {EC2_QUERY.key: {"pk": EC2_QUERY.key, "unitPrice": "not-a-number"}}
    )
    with pytest.raises(PriceCatalogError):
        DynamoPriceCatalog(table).lookup(EC2_QUERY)


def test_version_comes_from_the_metadata_item():
    table = FakeTable(
        {METADATA_KEY: {"pk": METADATA_KEY, "priceListVersion": "20260801000000"}}
    )
    assert DynamoPriceCatalog(table).version == "20260801000000"


def test_version_is_unknown_when_no_snapshot_metadata_exists():
    assert DynamoPriceCatalog(FakeTable()).version == "unknown"


def test_fallback_catalog_is_consulted_on_a_miss():
    """Lets a static snapshot keep reports flowing if the cache is incomplete."""
    fallback = StaticPriceCatalog(version="fallback")
    fallback.put(EC2_QUERY, "0.0832", "Hrs")
    catalog = DynamoPriceCatalog(FakeTable(), fallback=fallback)

    record = catalog.lookup(EC2_QUERY)
    assert record.unit_price == Decimal("0.0832")


def test_table_is_preferred_over_the_fallback():
    fallback = StaticPriceCatalog(version="fallback")
    fallback.put(EC2_QUERY, "9.99", "Hrs")
    table = FakeTable(
        {EC2_QUERY.key: {"pk": EC2_QUERY.key, "unitPrice": "0.0832", "unit": "Hrs"}}
    )

    record = DynamoPriceCatalog(table, fallback=fallback).lookup(EC2_QUERY)
    assert record.unit_price == Decimal("0.0832")


def test_catalog_is_truthy_even_when_the_table_is_empty():
    assert bool(DynamoPriceCatalog(FakeTable()))


# -- writing --------------------------------------------------------------


def test_writer_stores_price_as_a_string():
    """Money must not round-trip through anything float-shaped."""
    table = FakeTable()
    writer = DynamoPriceWriter(table)
    writer(EC2_QUERY, PriceRecord(Decimal("0.0832"), "Hrs", "20260801000000", sku="ABC"))

    item = table.items[EC2_QUERY.key]
    assert item["unitPrice"] == "0.0832"
    assert isinstance(item["unitPrice"], str)
    assert item["serviceCode"] == "AmazonEC2"
    assert item["region"] == REGION
    assert item["sku"] == "ABC"


def test_writer_omits_sku_when_absent():
    table = FakeTable()
    DynamoPriceWriter(table)(EC2_QUERY, PriceRecord(Decimal("1"), "Hrs", "v"))
    assert "sku" not in table.items[EC2_QUERY.key]


def test_writer_uses_one_batch_and_closes_it():
    """A second batch_writer() call would leave the first batch unflushed."""
    table = FakeTable()
    with DynamoPriceWriter(table) as writer:
        writer(EC2_QUERY, PriceRecord(Decimal("0.0832"), "Hrs", "v"))

    assert len(table.batches) == 1
    assert table.batches[0].closed
    assert writer.written == 1


def test_writer_deduplicates_a_repeated_key_within_a_batch():
    """Several Price List products can resolve to one cache key. Real
    BatchWriteItem rejects a batch that repeats a key, so the writer must open
    the batch with overwrite_by_pkeys=["pk"]. Without it the live sync fails with
    "Provided list of item keys contains duplicates"."""
    table = FakeTable()
    with DynamoPriceWriter(table) as writer:
        writer(EC2_QUERY, PriceRecord(Decimal("0.0832"), "Hrs", "v"))
        writer(EC2_QUERY, PriceRecord(Decimal("0.0999"), "Hrs", "v"))

    assert table.batches[0]._overwrite_by_pkeys == ["pk"]
    # Last write wins, matching the in-memory catalog's semantics.
    assert table.items[EC2_QUERY.key]["unitPrice"] == "0.0999"
    assert writer.written == 2


def test_writer_works_without_the_context_manager():
    table = FakeTable()
    writer = DynamoPriceWriter(table)
    writer(EC2_QUERY, PriceRecord(Decimal("0.0832"), "Hrs", "v"))

    assert table.batches == []
    assert table.items[EC2_QUERY.key]


def test_metadata_records_version_and_count():
    table = FakeTable()
    writer = DynamoPriceWriter(table)
    writer(EC2_QUERY, PriceRecord(Decimal("0.0832"), "Hrs", "v"))
    writer.write_metadata("20260801000000")

    meta = table.items[METADATA_KEY]
    assert meta["priceListVersion"] == "20260801000000"
    assert meta["priceCount"] == "1"
    assert meta["syncedAt"]


# -- the whole chain ------------------------------------------------------


def test_sync_to_dynamo_to_engine_prices_a_resource():
    """Price List JSON in, monthly cost out, through every real component.

    Parser, sync rule, DynamoDB writer, DynamoDB catalog, pricing engine. The
    only fakes are the AWS transports.
    """
    raw = json.dumps(
        {
            "product": {
                "productFamily": "Compute Instance",
                "sku": "EC2T3L",
                "attributes": {
                    "instanceType": "t3.large",
                    "operatingSystem": "Linux",
                    "tenancy": "Shared",
                    "capacitystatus": "Used",
                    "preInstalledSw": "NA",
                    "licenseModel": "No License required",
                    "usagetype": "BoxUsage:t3.large",
                },
            },
            "serviceCode": "AmazonEC2",
            "version": "20260801000000",
            "terms": {
                "OnDemand": {
                    "EC2T3L.T": {
                        "priceDimensions": {
                            "EC2T3L.T.D": {
                                "unit": "Hrs",
                                "beginRange": "0",
                                "pricePerUnit": {"USD": "0.0832"},
                            }
                        }
                    }
                }
            },
        }
    )

    class Source:
        def iter_products(self, service_code, region, product_family, group=""):
            if (service_code, product_family) == ("AmazonEC2", "Compute Instance"):
                yield raw

    ec2_rule = SyncRule(
        name="EC2 instance hours",
        service_code="AmazonEC2",
        product_family="Compute Instance",
        key_static={"productFamily": "Compute Instance"},
        key_from={
            "instanceType": "instanceType",
            "operatingSystem": "operatingSystem",
            "tenancy": "tenancy",
        },
        require={
            "capacitystatus": "Used",
            "preInstalledSw": "NA",
            "licenseModel": "No License required",
            "tenancy": "Shared",
        },
    )

    table = FakeTable()
    writer = DynamoPriceWriter(table)
    report = PriceSync(rules=[ec2_rule]).run(Source(), REGION, writer)
    writer.write_metadata(report.version or "unknown")

    assert report.healthy
    assert report.prices_written == 1

    catalog = DynamoPriceCatalog(table)
    engine = PricingEngine(catalog, REGION)
    priced = engine.price_resource(make_resource(properties={"InstanceType": "t3.large"}))

    assert priced.confidence is Confidence.MEDIUM
    assert any("AMI-provided storage" in item for item in priced.excluded)
    assert money(priced.monthly_list) == Decimal("60.74")
    assert priced.price_list_version == "20260801000000"
    assert catalog.version == "20260801000000"
