"""Sync rules, orchestration, and rule verification."""

import json
from decimal import Decimal

from pricing import (
    SYNC_RULES,
    Boto3ProductSource,
    PriceQuery,
    PriceSync,
    RuleStatus,
    StaticPriceCatalog,
    SyncRule,
    parse_product,
    rules_needing_verification,
    verify_rules,
)

REGION = "us-east-1"


def ec2_product(
    sku,
    instance_type="t3.large",
    capacity_status="Used",
    pre_installed="NA",
    licence="No License required",
    tenancy="Shared",
    price="0.0832",
    family="Compute Instance",
    market_option="OnDemand",
):
    return json.dumps(
        {
            "product": {
                "productFamily": family,
                "sku": sku,
                "attributes": {
                    "instanceType": instance_type,
                    "operatingSystem": "Linux",
                    "tenancy": tenancy,
                    "capacitystatus": capacity_status,
                    "preInstalledSw": pre_installed,
                    "licenseModel": licence,
                    "marketoption": market_option,
                    "usagetype": f"BoxUsage:{instance_type}",
                },
            },
            "serviceCode": "AmazonEC2",
            "version": "20260801000000",
            "terms": {
                "OnDemand": {
                    f"{sku}.T": {
                        "priceDimensions": {
                            f"{sku}.T.D": {
                                "unit": "Hrs",
                                "beginRange": "0",
                                "pricePerUnit": {"USD": price},
                            }
                        }
                    }
                }
            },
        }
    )


class FakeSource:
    """Serves canned product lists keyed by (service, family)."""

    def __init__(self, products):
        self._products = products
        self.calls = []

    def iter_products(self, service_code, region, product_family, group=""):
        self.calls.append((service_code, region, product_family, group))
        return iter(self._products.get((service_code, group or product_family), []))


EC2_RULE = next(r for r in SYNC_RULES if r.name == "EC2 instance hours")


# -- rule matching --------------------------------------------------------


def test_rule_matches_a_canonical_product():
    parsed = parse_product(ec2_product("A"))
    assert EC2_RULE.matches(parsed)


def test_rule_rejects_a_capacity_reservation_row():
    """These rows exist per instance type and several are priced at zero."""
    parsed = parse_product(
        ec2_product("B", capacity_status="AllocatedCapacityReservation", price="0.0")
    )
    assert not EC2_RULE.matches(parsed)


def test_rule_rejects_bundled_software_rows():
    parsed = parse_product(ec2_product("C", pre_installed="SQL Std"))
    assert not EC2_RULE.matches(parsed)


def test_rule_rejects_dedicated_tenancy():
    parsed = parse_product(ec2_product("D", tenancy="Dedicated"))
    assert not EC2_RULE.matches(parsed)


def test_rule_rejects_a_different_product_family():
    parsed = parse_product(ec2_product("E", family="Storage"))
    assert not EC2_RULE.matches(parsed)


def test_reject_contains_accepts_multiple_fragments():
    """A rule can reject a row if an attribute contains any of several fragments,
    e.g. ElastiCache's Extended Support / Sync Durability / Outposts variants."""
    rule = SyncRule(
        name="cache",
        service_code="AmazonElastiCache",
        product_family="Cache Instance",
        reject_contains={"usagetype": ("ExtendedSupport", "SyncDurability", "Outpost")},
    )

    def cache_product(usagetype):
        return parse_product(
            json.dumps(
                {
                    "product": {
                        "productFamily": "Cache Instance",
                        "sku": "C1",
                        "attributes": {"usagetype": usagetype},
                    },
                    "serviceCode": "AmazonElastiCache",
                    "version": "v",
                    "terms": {
                        "OnDemand": {
                            "C1.T": {
                                "priceDimensions": {
                                    "C1.T.D": {
                                        "unit": "Hrs",
                                        "beginRange": "0",
                                        "pricePerUnit": {"USD": "0.10"},
                                    }
                                }
                            }
                        }
                    },
                }
            )
        )

    assert rule.matches(cache_product("NodeUsage:cache.m5.large"))
    assert not rule.matches(cache_product("USE1-ExtendedSupportYr3-NodeUsage:cache.m5.large"))
    assert not rule.matches(cache_product("USE1-SyncDurability-NodeUsage:cache.m5.large"))
    assert not rule.matches(cache_product("USE1-Outpost-NodeUsage:cache.m5.large"))


def test_require_contains_matches_on_substring():
    rule = SyncRule(
        name="nat",
        service_code="AmazonEC2",
        product_family="NAT Gateway",
        require_contains={"usagetype": "NatGateway-Hours"},
    )
    hours = parse_product(
        json.dumps(
            {
                "product": {
                    "productFamily": "NAT Gateway",
                    "sku": "N1",
                    "attributes": {"usagetype": "USE1-NatGateway-Hours"},
                },
                "serviceCode": "AmazonEC2",
                "version": "v",
                "terms": {
                    "OnDemand": {
                        "N1.T": {
                            "priceDimensions": {
                                "N1.T.D": {
                                    "unit": "Hrs",
                                    "beginRange": "0",
                                    "pricePerUnit": {"USD": "0.045"},
                                }
                            }
                        }
                    }
                },
            }
        )
    )
    bytes_row = parse_product(
        json.dumps(
            {
                "product": {
                    "productFamily": "NAT Gateway",
                    "sku": "N2",
                    "attributes": {"usagetype": "USE1-NatGateway-Bytes"},
                },
                "serviceCode": "AmazonEC2",
                "version": "v",
                "terms": {
                    "OnDemand": {
                        "N2.T": {
                            "priceDimensions": {
                                "N2.T.D": {
                                    "unit": "GB",
                                    "beginRange": "0",
                                    "pricePerUnit": {"USD": "0.045"},
                                }
                            }
                        }
                    }
                },
            }
        )
    )

    assert rule.matches(hours)
    assert not rule.matches(bytes_row)


# -- key building ---------------------------------------------------------


def test_build_query_matches_what_the_mapper_asks_for():
    """The whole point: sync keys must equal engine lookup keys."""
    parsed = parse_product(ec2_product("A"))
    built = EC2_RULE.build_query(parsed, REGION)

    expected = PriceQuery.of(
        "AmazonEC2",
        REGION,
        productFamily="Compute Instance",
        instanceType="t3.large",
        operatingSystem="Linux",
        tenancy="Shared",
    )
    assert built == expected


def test_build_query_applies_value_translation():
    rule = SyncRule(
        name="rds storage",
        service_code="AmazonRDS",
        product_family="Database Storage",
        key_from={"volumeType": "volumeType"},
        value_map={"volumeType": {"General Purpose": "gp2"}},
    )
    parsed = parse_product(
        json.dumps(
            {
                "product": {
                    "productFamily": "Database Storage",
                    "sku": "S1",
                    "attributes": {"volumeType": "General Purpose"},
                },
                "serviceCode": "AmazonRDS",
                "version": "v",
                "terms": {
                    "OnDemand": {
                        "S1.T": {
                            "priceDimensions": {
                                "S1.T.D": {
                                    "unit": "GB-Mo",
                                    "beginRange": "0",
                                    "pricePerUnit": {"USD": "0.115"},
                                }
                            }
                        }
                    }
                },
            }
        )
    )
    query = rule.build_query(parsed, REGION)
    assert ("volumeType", "gp2") in query.attributes


def test_build_query_returns_none_when_a_key_attribute_is_absent():
    rule = SyncRule(
        name="x",
        service_code="AmazonEC2",
        product_family="Compute Instance",
        key_from={"instanceType": "nonexistentAttribute"},
    )
    parsed = parse_product(ec2_product("A"))
    assert rule.build_query(parsed, REGION) is None


def test_aws_attributes_used_covers_keys_and_filters():
    assert "capacitystatus" in EC2_RULE.aws_attributes_used
    assert "instanceType" in EC2_RULE.aws_attributes_used


# -- orchestration --------------------------------------------------------


def test_sync_writes_the_canonical_row_and_drops_the_decoy():
    """The reservation row is priced at zero; picking it would be silently wrong."""
    source = FakeSource(
        {
            ("AmazonEC2", "Compute Instance"): [
                ec2_product("GOOD", price="0.0832"),
                ec2_product(
                    "DECOY", capacity_status="AllocatedCapacityReservation", price="0.0"
                ),
            ]
        }
    )
    catalog = StaticPriceCatalog(version="test")
    report = PriceSync(rules=[EC2_RULE]).run(
        source, REGION, lambda q, r: catalog.put(q, r.unit_price, r.unit, r.sku)
    )

    assert report.products_seen == 2
    assert report.prices_written == 1
    assert report.unmatched == 1

    record = catalog.lookup(
        PriceQuery.of(
            "AmazonEC2",
            REGION,
            productFamily="Compute Instance",
            instanceType="t3.large",
            operatingSystem="Linux",
            tenancy="Shared",
        )
    )
    assert record.unit_price == Decimal("0.0832")


def test_sync_records_the_price_list_version():
    source = FakeSource({("AmazonEC2", "Compute Instance"): [ec2_product("A")]})
    report = PriceSync(rules=[EC2_RULE]).run(source, REGION, lambda q, r: None)
    assert report.version == "20260801000000"


def test_sync_reports_a_rule_that_matched_nothing():
    """A silently empty cache is the failure this catches."""
    source = FakeSource(
        {("AmazonEC2", "Compute Instance"): [ec2_product("X", tenancy="Dedicated")]}
    )
    report = PriceSync(rules=[EC2_RULE]).run(source, REGION, lambda q, r: None)

    assert report.rules_with_no_matches == ["EC2 instance hours"]
    assert not report.healthy


def test_sync_reports_collisions_when_filters_are_too_loose():
    """Two products claiming one key means a price is being overwritten."""
    loose = SyncRule(
        name="loose",
        service_code="AmazonEC2",
        product_family="Compute Instance",
        key_from={"instanceType": "instanceType"},
    )
    source = FakeSource(
        {
            ("AmazonEC2", "Compute Instance"): [
                ec2_product("ONE", price="0.0832"),
                ec2_product("TWO", capacity_status="UnusedCapacityReservation", price="0.0"),
            ]
        }
    )
    report = PriceSync(rules=[loose]).run(source, REGION, lambda q, r: None)

    assert report.collisions
    assert "ONE" in report.collisions[0]


def test_sync_counts_skips_by_reason():
    source = FakeSource(
        {("AmazonEC2", "Compute Instance"): ["{not json", ec2_product("A")]}
    )
    report = PriceSync(rules=[EC2_RULE]).run(source, REGION, lambda q, r: None)

    assert sum(report.skipped.values()) == 1
    assert report.prices_written == 1


def test_sync_fetches_each_service_and_family_only_once():
    """Fetching per rule would re-download the same products repeatedly."""
    rule_a = SyncRule(
        name="a",
        service_code="AmazonEC2",
        product_family="Compute Instance",
        key_from={"instanceType": "instanceType"},
        require={"capacitystatus": "Used"},
    )
    rule_b = SyncRule(
        name="b",
        service_code="AmazonEC2",
        product_family="Compute Instance",
        key_static={"variant": "b"},
        key_from={"instanceType": "instanceType"},
        require={"capacitystatus": "Used"},
    )
    source = FakeSource({("AmazonEC2", "Compute Instance"): [ec2_product("A")]})
    report = PriceSync(rules=[rule_a, rule_b]).run(source, REGION, lambda q, r: None)

    assert len(source.calls) == 1
    # One product satisfied both rules, so two prices were written.
    assert report.prices_written == 2


def test_healthy_requires_prices_and_no_empty_rules():
    source = FakeSource({("AmazonEC2", "Compute Instance"): [ec2_product("A")]})
    report = PriceSync(rules=[EC2_RULE]).run(source, REGION, lambda q, r: None)
    assert report.healthy


def test_empty_source_is_not_healthy():
    report = PriceSync(rules=[EC2_RULE]).run(FakeSource({}), REGION, lambda q, r: None)
    assert not report.healthy
    assert report.prices_written == 0


def test_report_serialises():
    source = FakeSource({("AmazonEC2", "Compute Instance"): [ec2_product("A")]})
    payload = PriceSync(rules=[EC2_RULE]).run(source, REGION, lambda q, r: None).to_dict()

    assert payload["pricesWritten"] == 1
    assert payload["healthy"] is True
    assert payload["version"] == "20260801000000"


# -- boto3 source paging --------------------------------------------------


class FakePricingClient:
    def __init__(self, pages):
        self._pages = pages
        self.requests = []

    def get_products(self, **request):
        self.requests.append(request)
        return self._pages[len(self.requests) - 1]


def test_boto3_source_follows_pagination():
    client = FakePricingClient(
        [
            {"PriceList": ["a", "b"], "NextToken": "more"},
            {"PriceList": ["c"]},
        ]
    )
    source = Boto3ProductSource(client)
    products = list(source.iter_products("AmazonEC2", REGION, "Compute Instance"))

    assert products == ["a", "b", "c"]
    assert client.requests[1]["NextToken"] == "more"


def test_boto3_source_filters_by_region_and_family():
    client = FakePricingClient([{"PriceList": []}])
    list(Boto3ProductSource(client).iter_products("AmazonEC2", REGION, "Compute Instance"))

    filters = client.requests[0]["Filters"]
    fields = {f["Field"]: f["Value"] for f in filters}
    assert fields["regionCode"] == REGION
    assert fields["productFamily"] == "Compute Instance"
    assert all(f["Type"] == "TERM_MATCH" for f in filters)


# -- rule verification ----------------------------------------------------


class FakeAttributeSource:
    def __init__(self, names, values=None):
        self._names = names
        self._values = values or {}
        self.name_calls = 0

    def attribute_names(self, service_code):
        self.name_calls += 1
        return frozenset(self._names.get(service_code, set()))

    def attribute_values(self, service_code, attribute):
        return frozenset(self._values.get((service_code, attribute), set()))


def test_verification_flags_attribute_names_that_do_not_exist():
    """Turns an untestable guess into a mechanical check."""
    source = FakeAttributeSource({"AmazonEC2": {"instanceType", "operatingSystem"}})
    results = verify_rules(source, rules=[EC2_RULE])

    assert not results[0].ok
    assert "capacitystatus" in results[0].missing_attributes
    assert "tenancy" in results[0].missing_attributes


def test_verification_passes_when_every_attribute_and_value_is_published():
    source = FakeAttributeSource(
        {
            "AmazonEC2": {
                "instanceType",
                "operatingSystem",
                "tenancy",
                "capacitystatus",
                "preInstalledSw",
                "licenseModel",
                "marketoption",
            }
        },
        {
            ("AmazonEC2", "capacitystatus"): {"Used", "AllocatedCapacityReservation"},
            ("AmazonEC2", "preInstalledSw"): {"NA", "SQL Std"},
            ("AmazonEC2", "licenseModel"): {"No License required"},
            ("AmazonEC2", "tenancy"): {"Shared", "Dedicated"},
            ("AmazonEC2", "marketoption"): {"OnDemand", "CapacityBlock"},
        },
    )
    results = verify_rules(source, rules=[EC2_RULE])
    assert results[0].ok


def test_verification_flags_a_required_value_that_is_not_published():
    """Catches casing differences, which differ between EC2 and RDS."""
    source = FakeAttributeSource(
        {
            "AmazonEC2": {
                "instanceType",
                "operatingSystem",
                "tenancy",
                "capacitystatus",
                "preInstalledSw",
                "licenseModel",
            }
        },
        {("AmazonEC2", "licenseModel"): {"No license required"}},
    )
    results = verify_rules(source, rules=[EC2_RULE])

    assert not results[0].ok
    assert "licenseModel" in results[0].unexpected_values


def test_verification_caches_metadata_per_service():
    source = FakeAttributeSource({"AmazonEC2": set()})
    ec2_rules = [r for r in SYNC_RULES if r.service_code == "AmazonEC2"]
    verify_rules(source, rules=ec2_rules)

    assert len(ec2_rules) > 1
    assert source.name_calls == 1


def test_verification_serialises():
    source = FakeAttributeSource({"AmazonEC2": set()})
    payload = verify_rules(source, rules=[EC2_RULE])[0].to_dict()

    assert payload["rule"] == "EC2 instance hours"
    assert payload["ok"] is False
    assert payload["missingAttributes"]


# -- honesty about the rule table ----------------------------------------


def test_every_rule_the_mappers_need_has_a_sync_rule():
    services = {rule.service_code for rule in SYNC_RULES}
    assert services == {
        "AmazonEC2",
        "AmazonRDS",
        "AWSELB",
        "AmazonElastiCache",
        "AmazonDynamoDB",
        "AmazonVPC",
    }


def test_all_rules_are_confirmed_against_a_live_account():
    """Every rule's attribute mapping has been verified against a live Price List
    response (see scripts/verify_pricing_rules.py) and marked CONFIRMED.

    This replaces the earlier "all unverified" assertion. The dataclass default
    deliberately stays NEEDS_VERIFICATION so a newly added rule is never silently
    assumed correct — it must be verified and opt in to CONFIRMED explicitly.
    """
    assert all(r.status is RuleStatus.CONFIRMED for r in SYNC_RULES)
    assert rules_needing_verification() == ()

    fresh = SyncRule(name="new", service_code="AmazonEC2", product_family="X")
    assert fresh.status is RuleStatus.NEEDS_VERIFICATION


def test_rds_storage_maps_gp3_and_io2_distinctly():
    """The old rule guessed 'General Purpose' covered gp2 and gp3; verifying
    against the live Price List showed gp3/io2 have their own rows, so they are
    mapped distinctly rather than collapsing onto gp2/io1."""
    rds_storage = next(r for r in SYNC_RULES if r.name == "RDS storage")
    volume_map = rds_storage.value_map["volumeType"]
    assert volume_map["General Purpose-GP3"] == "gp3"
    assert volume_map["Provisioned IOPS-IO2"] == "io2"
    assert "General Purpose-Aurora" not in volume_map


def test_price_scale_normalises_the_written_rate():
    """gp3 throughput is listed per GiBps-month but the mapper works in MiBps, so
    the rule scales the rate by 1/1024 before it is cached."""
    rule = SyncRule(
        name="gp3 throughput",
        service_code="AmazonEC2",
        product_family="Provisioned Throughput",
        key_static={"productFamily": "Provisioned Throughput"},
        key_from={"volumeType": "volumeApiName"},
        price_scale=Decimal(1) / Decimal(1024),
    )
    product = json.dumps(
        {
            "product": {
                "productFamily": "Provisioned Throughput",
                "sku": "T1",
                "attributes": {
                    "volumeApiName": "gp3",
                    "usagetype": "EBS:VolumeP-Throughput.gp3",
                },
            },
            "serviceCode": "AmazonEC2",
            "version": "v",
            "terms": {
                "OnDemand": {
                    "T1.T": {
                        "priceDimensions": {
                            "T1.T.D": {
                                "unit": "GiBps-mo",
                                "beginRange": "0",
                                "pricePerUnit": {"USD": "40.96"},
                            }
                        }
                    }
                }
            },
        }
    )
    source = FakeSource({("AmazonEC2", "Provisioned Throughput"): [product]})
    captured: list[Decimal] = []
    PriceSync(rules=[rule]).run(source, REGION, lambda q, r: captured.append(r.unit_price))

    assert captured == [Decimal("0.04")]
