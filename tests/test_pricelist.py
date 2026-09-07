"""Price List response parsing."""

import json
from decimal import Decimal

from pricing import ParsedProduct, ParseSkip, parse_product


def product(
    sku="EC2T3LARGE",
    family="Compute Instance",
    attributes=None,
    on_demand=True,
    price="0.0832000000",
    unit="Hrs",
    version="20260801000000",
):
    payload = {
        "product": {
            "productFamily": family,
            "attributes": attributes
            if attributes is not None
            else {
                "instanceType": "t3.large",
                "operatingSystem": "Linux",
                "tenancy": "Shared",
                "capacitystatus": "Used",
                "preInstalledSw": "NA",
                "licenseModel": "No License required",
                "regionCode": "us-east-1",
                "usagetype": "BoxUsage:t3.large",
            },
            "sku": sku,
        },
        "serviceCode": "AmazonEC2",
        "version": version,
        "terms": {},
    }
    if on_demand:
        payload["terms"]["OnDemand"] = {
            f"{sku}.JRTCKXETXF": {
                "effectiveDate": "2026-08-01T00:00:00Z",
                "priceDimensions": {
                    f"{sku}.JRTCKXETXF.6YS6EN2CT7": {
                        "unit": unit,
                        "beginRange": "0",
                        "endRange": "Inf",
                        "pricePerUnit": {"USD": price},
                    }
                },
            }
        }
    return json.dumps(payload)


def test_well_formed_product_parses():
    result = parse_product(product())

    assert isinstance(result, ParsedProduct)
    assert result.sku == "EC2T3LARGE"
    assert result.service_code == "AmazonEC2"
    assert result.product_family == "Compute Instance"
    assert result.unit_price == Decimal("0.0832000000")
    assert result.unit == "Hrs"
    assert result.version == "20260801000000"
    assert result.attributes["instanceType"] == "t3.large"


def test_already_decoded_mapping_is_accepted():
    result = parse_product(json.loads(product()))
    assert isinstance(result, ParsedProduct)


def test_lowest_tier_is_chosen_when_a_product_is_tiered():
    payload = json.loads(product())
    dimensions = payload["terms"]["OnDemand"]["EC2T3LARGE.JRTCKXETXF"]["priceDimensions"]
    dimensions["second"] = {
        "unit": "Hrs",
        "beginRange": "1000",
        "endRange": "Inf",
        "pricePerUnit": {"USD": "0.0100000000"},
    }

    result = parse_product(payload)
    assert result.unit_price == Decimal("0.0832000000")


def test_tier_order_in_the_payload_does_not_matter():
    payload = json.loads(product())
    term = payload["terms"]["OnDemand"]["EC2T3LARGE.JRTCKXETXF"]
    term["priceDimensions"] = {
        "high": {
            "unit": "Hrs",
            "beginRange": "500",
            "pricePerUnit": {"USD": "0.0100000000"},
        },
        "low": {
            "unit": "Hrs",
            "beginRange": "0",
            "pricePerUnit": {"USD": "0.0832000000"},
        },
    }

    assert parse_product(payload).unit_price == Decimal("0.0832000000")


def test_multiple_offer_terms_resolve_deterministically():
    payload = json.loads(product())
    payload["terms"]["OnDemand"]["AAAAAAAAAA"] = {
        "priceDimensions": {
            "a": {"unit": "Hrs", "beginRange": "0", "pricePerUnit": {"USD": "0.5"}}
        }
    }
    # Sorted term codes mean the same answer every run.
    assert parse_product(payload).unit_price == Decimal("0.5")


def test_reserved_only_product_is_skipped_without_alarm():
    result = parse_product(product(on_demand=False))
    assert isinstance(result, ParseSkip)
    assert "OnDemand" in result.reason
    assert result.sku == "EC2T3LARGE"


def test_zero_price_is_parsed_not_discarded():
    """Some rows legitimately cost nothing; filtering them is the rule's job."""
    result = parse_product(product(price="0.0000000000"))
    assert isinstance(result, ParsedProduct)
    assert result.unit_price == Decimal(0)


def test_invalid_json_is_skipped():
    result = parse_product("{not json")
    assert isinstance(result, ParseSkip)
    assert "not valid json" in result.reason.lower()


def test_non_mapping_payload_is_skipped():
    result = parse_product(json.dumps(["a", "list"]))
    assert isinstance(result, ParseSkip)


def test_missing_product_block_is_skipped():
    result = parse_product(json.dumps({"serviceCode": "AmazonEC2"}))
    assert isinstance(result, ParseSkip)
    assert "product block" in result.reason


def test_missing_sku_is_skipped():
    payload = json.loads(product())
    del payload["product"]["sku"]
    result = parse_product(payload)
    assert isinstance(result, ParseSkip)
    assert "sku" in result.reason


def test_missing_attributes_is_skipped():
    payload = json.loads(product())
    del payload["product"]["attributes"]
    result = parse_product(payload)
    assert isinstance(result, ParseSkip)
    assert "attributes" in result.reason


def test_missing_terms_is_skipped():
    payload = json.loads(product())
    del payload["terms"]
    result = parse_product(payload)
    assert isinstance(result, ParseSkip)
    assert "terms" in result.reason


def test_unparseable_price_is_skipped_not_coerced():
    payload = json.loads(product())
    term = payload["terms"]["OnDemand"]["EC2T3LARGE.JRTCKXETXF"]
    term["priceDimensions"]["EC2T3LARGE.JRTCKXETXF.6YS6EN2CT7"]["pricePerUnit"] = {
        "USD": "not-a-number"
    }
    result = parse_product(payload)
    assert isinstance(result, ParseSkip)
    assert "Unparseable price" in result.reason


def test_non_usd_only_product_is_skipped():
    payload = json.loads(product())
    term = payload["terms"]["OnDemand"]["EC2T3LARGE.JRTCKXETXF"]
    term["priceDimensions"]["EC2T3LARGE.JRTCKXETXF.6YS6EN2CT7"]["pricePerUnit"] = {
        "CNY": "0.5"
    }
    assert isinstance(parse_product(payload), ParseSkip)


def test_empty_price_dimensions_is_skipped():
    payload = json.loads(product())
    payload["terms"]["OnDemand"]["EC2T3LARGE.JRTCKXETXF"]["priceDimensions"] = {}
    assert isinstance(parse_product(payload), ParseSkip)


def test_attribute_values_are_stringified():
    payload = json.loads(product())
    payload["product"]["attributes"]["vcpu"] = 2
    result = parse_product(payload)
    assert result.attributes["vcpu"] == "2"


def test_free_tier_band_is_skipped_in_favour_of_the_paid_rate():
    """DynamoDB-style products put a $0 free allowance in the lowest band and the
    real per-unit rate in a higher band. The paid rate must win; pricing at the
    $0 free band would zero the resource out entirely."""
    payload = json.loads(product(price="0.0000000000"))
    dimensions = payload["terms"]["OnDemand"]["EC2T3LARGE.JRTCKXETXF"]["priceDimensions"]
    dimensions["paid"] = {
        "unit": "Hrs",
        "beginRange": "18600",
        "endRange": "Inf",
        "pricePerUnit": {"USD": "0.00013"},
    }

    result = parse_product(payload)

    assert isinstance(result, ParsedProduct)
    assert result.unit_price == Decimal("0.00013")
