"""Engine orchestration: lookup, multiply, roll up confidence, apply discount."""

from decimal import Decimal

from conftest import REGION, build_catalog, make_resource
from pricing import (
    Confidence,
    PricingClass,
    PricingEngine,
    StaticPriceCatalog,
    money,
)
from resolver import Inclusion


def engine(catalog=None, discount=0):
    # Explicit None check, not `catalog or ...`. An empty catalog is a valid
    # catalog and must not be swapped out.
    if catalog is None:
        catalog = build_catalog()
    return PricingEngine(catalog, REGION, discount_percent=discount)


# -- straightforward pricing ---------------------------------------------


def test_ec2_monthly_cost_is_hourly_times_730(catalog):
    resource = make_resource(properties={"InstanceType": "t3.large"})
    priced = engine(catalog).price_resource(resource)

    # 0.0832 * 730 = 60.736
    assert money(priced.monthly_list) == Decimal("60.74")
    assert priced.confidence is Confidence.MEDIUM
    assert any("AMI-provided storage" in item for item in priced.excluded)
    assert priced.is_priced


def test_instance_with_a_volume_sums_both_components(catalog):
    resource = make_resource(
        properties={
            "InstanceType": "t3.large",
            "BlockDeviceMappings": [
                {"DeviceName": "/dev/xvda", "Ebs": {"VolumeSize": 100, "VolumeType": "gp3"}}
            ],
        }
    )
    priced = engine(catalog).price_resource(resource)

    # 60.736 instance + (0.08 * 100) storage
    assert money(priced.monthly_list) == Decimal("68.74")
    assert len(priced.components) == 2


def test_rds_sums_instance_and_storage(catalog):
    resource = make_resource(
        resource_type="AWS::RDS::DBInstance",
        properties={
            "DBInstanceClass": "db.t4g.medium",
            "Engine": "postgres",
            "AllocatedStorage": 20,
        },
    )
    priced = engine(catalog).price_resource(resource)

    # (0.065 * 730) + (0.115 * 20) = 47.45 + 2.30
    assert money(priced.monthly_list) == Decimal("49.75")


def test_multi_az_costs_more_than_single_az(catalog):
    def price(multi_az):
        return engine(catalog).price_resource(
            make_resource(
                resource_type="AWS::RDS::DBInstance",
                properties={
                    "DBInstanceClass": "db.t4g.medium",
                    "Engine": "postgres",
                    "MultiAZ": multi_az,
                    "AllocatedStorage": 20,
                },
            )
        )

    assert price(True).monthly_list > price(False).monthly_list


def test_dynamodb_provisioned_capacity_cost(catalog):
    resource = make_resource(
        resource_type="AWS::DynamoDB::Table",
        properties={
            "ProvisionedThroughput": {"ReadCapacityUnits": 5, "WriteCapacityUnits": 5}
        },
    )
    priced = engine(catalog).price_resource(resource)

    # (0.00013 * 5 * 730) + (0.00065 * 5 * 730) = 0.4745 + 2.37125
    assert money(priced.monthly_list) == Decimal("2.85")


def test_elasticache_node_count_scales_cost(catalog):
    one = engine(catalog).price_resource(
        make_resource(
            resource_type="AWS::ElastiCache::CacheCluster",
            properties={"CacheNodeType": "cache.t4g.micro", "NumCacheNodes": 1},
        )
    )
    three = engine(catalog).price_resource(
        make_resource(
            resource_type="AWS::ElastiCache::CacheCluster",
            properties={"CacheNodeType": "cache.t4g.micro", "NumCacheNodes": 3},
        )
    )
    assert three.monthly_list == one.monthly_list * 3


def test_elastic_ip_hourly_charge(catalog):
    priced = engine(catalog).price_resource(
        make_resource(resource_type="AWS::EC2::EIP", properties={"Domain": "vpc"})
    )
    # 0.005 * 730
    assert money(priced.monthly_list) == Decimal("3.65")


# -- split resources downgrade confidence (S18) ---------------------------


def test_nat_gateway_is_priced_but_only_medium_confidence(catalog):
    """The hourly figure is real; the data-processing charge is not included."""
    priced = engine(catalog).price_resource(
        make_resource(resource_type="AWS::EC2::NatGateway")
    )

    assert money(priced.monthly_list) == Decimal("32.85")
    assert priced.confidence is Confidence.MEDIUM
    assert priced.excluded == ("Data processing per GB",)
    assert priced.is_priced


def test_load_balancer_is_medium_confidence(catalog):
    priced = engine(catalog).price_resource(
        make_resource(resource_type="AWS::ElasticLoadBalancingV2::LoadBalancer")
    )
    # 0.0225 * 730 = 16.425
    assert money(priced.monthly_list) == Decimal("16.43")
    assert priced.confidence is Confidence.MEDIUM


def test_nat_gateway_prices_despite_every_property_being_unresolved(catalog):
    """The finding that shaped the engine: coverage is per dimension, not per property.

    A NAT Gateway can have nothing resolvable and still be fully priceable.
    Treating unresolved properties as disqualifying would discard real cost.
    """
    resource = make_resource(
        resource_type="AWS::EC2::NatGateway",
        properties={},
        resolution={},
    )
    priced = engine(catalog).price_resource(resource)
    assert priced.is_priced
    assert priced.monthly_list > 0


# -- missing prices (S16, S19) -------------------------------------------


def test_missing_price_yields_unavailable_and_is_excluded_from_totals():
    """Never guessed, never dropped from the listing."""
    empty = StaticPriceCatalog(version="empty")
    resource = make_resource(properties={"InstanceType": "t3.large"})
    priced = engine(empty).price_resource(resource)

    assert priced.confidence is Confidence.UNAVAILABLE
    assert not priced.is_priced
    assert priced.monthly_list == Decimal(0)
    # Still described, and the reason names what was missing.
    assert "No price found" in priced.reason
    assert priced.components


def test_partially_priced_resource_keeps_the_known_figure_and_flags_the_gap():
    """One component priced, one not.

    Discarding the whole resource would throw away a known $60.74 and understate
    the total. Keeping it at MEDIUM with the gap named is both more accurate and
    more honest.
    """
    catalog = build_catalog()
    resource = make_resource(
        properties={
            "InstanceType": "t3.large",
            "BlockDeviceMappings": [
                # io1 storage is deliberately absent from the fixture catalog.
                {"DeviceName": "/dev/sdb", "Ebs": {"VolumeSize": 500, "VolumeType": "io1"}}
            ],
        }
    )
    priced = engine(catalog).price_resource(resource)

    assert priced.is_priced
    assert money(priced.monthly_list) == Decimal("60.74")
    assert priced.confidence is Confidence.MEDIUM
    assert priced.reason.startswith("Partially priced")
    assert "io1" in priced.reason


def test_wholly_unpriced_resource_says_so_without_the_partial_wording():
    priced = engine(StaticPriceCatalog(version="empty")).price_resource(
        make_resource(properties={"InstanceType": "t3.large"})
    )
    assert priced.reason.startswith("No price found")
    assert not priced.is_priced


def test_catalog_records_misses_for_the_mapping_backlog():
    catalog = StaticPriceCatalog(version="empty")
    engine(catalog).price_resource(make_resource(properties={"InstanceType": "t3.large"}))
    assert catalog.misses
    assert "t3.large" in catalog.misses[0]


# -- non-deterministic classes -------------------------------------------


def test_usage_based_resource_is_listed_with_a_reason_not_a_number(catalog):
    priced = engine(catalog).price_resource(
        make_resource(resource_type="AWS::S3::Bucket", properties={"BucketName": "x"})
    )
    assert priced.pricing_class is PricingClass.USAGE_BASED
    assert not priced.is_priced
    assert priced.monthly_list == Decimal(0)
    assert "stored volume" in priced.reason


def test_free_resource_carries_no_cost_and_no_complaint(catalog):
    priced = engine(catalog).price_resource(
        make_resource(resource_type="AWS::EC2::VPC", properties={})
    )
    assert priced.pricing_class is PricingClass.FREE
    assert not priced.counts_toward_coverage
    assert priced.reason is None


def test_unsupported_resource_is_named(catalog):
    priced = engine(catalog).price_resource(
        make_resource(resource_type="AWS::Braket::QuantumTask")
    )
    assert priced.pricing_class is PricingClass.UNSUPPORTED
    assert "AWS::Braket::QuantumTask" in priced.reason


def test_unresolved_property_is_its_own_bucket(catalog):
    priced = engine(catalog).price_resource(make_resource(properties={}))
    assert priced.pricing_class is PricingClass.UNRESOLVED
    assert "InstanceType" in priced.reason


# -- discount (question 1) -----------------------------------------------


def test_discount_reduces_cost_but_preserves_list_price(catalog):
    resource = make_resource(properties={"InstanceType": "t3.large"})
    priced = engine(catalog, discount=20).price_resource(resource)

    assert money(priced.monthly_list) == Decimal("60.74")
    assert money(priced.monthly_cost) == Decimal("48.59")
    assert priced.discount_percent == Decimal(20)


def test_zero_discount_makes_the_two_figures_identical(catalog):
    priced = engine(catalog).price_resource(
        make_resource(properties={"InstanceType": "t3.large"})
    )
    assert priced.monthly_list == priced.monthly_cost


def test_basis_records_the_conventions_in_force(catalog):
    basis = engine(catalog, discount=15).basis.to_dict()

    assert basis["hoursPerMonth"] == 730
    assert basis["rateType"] == "On-Demand"
    assert basis["discountPercent"] == 15.0
    assert basis["priceListVersion"] == "2026-08-09"
    assert basis["operatingSystemAssumption"] == "Linux"
    assert "Savings Plans" in basis["note"]


# -- inventory ------------------------------------------------------------


def test_inventory_skips_resources_whose_condition_was_false(catalog):
    """An excluded resource was never created, so it has no cost."""
    resources = [
        make_resource("Kept", properties={"InstanceType": "t3.large"}),
        make_resource(
            "Dropped",
            properties={"InstanceType": "t3.xlarge"},
            inclusion=Inclusion.EXCLUDED,
        ),
    ]
    inventory = engine(catalog).price_inventory(resources)

    assert [r.logical_id for r in inventory.resources] == ["Kept"]
    assert money(inventory.monthly_list) == Decimal("60.74")


def test_inventory_totals_only_include_priced_resources(catalog):
    resources = [
        make_resource("Server", properties={"InstanceType": "t3.large"}),
        make_resource("Bucket", "AWS::S3::Bucket", {"BucketName": "x"}),
        make_resource("Vpc", "AWS::EC2::VPC"),
        make_resource("Exotic", "AWS::Braket::QuantumTask"),
    ]
    inventory = engine(catalog).price_inventory(resources)

    assert money(inventory.monthly_list) == Decimal("60.74")
    assert len(inventory.resources) == 4


def test_inventory_coverage_excludes_free_resources(catalog):
    resources = [
        make_resource("Server", properties={"InstanceType": "t3.large"}),
        make_resource("Bucket", "AWS::S3::Bucket", {"BucketName": "x"}),
        make_resource("Vpc", "AWS::EC2::VPC"),
        make_resource("Subnet", "AWS::EC2::Subnet"),
        make_resource("Sg", "AWS::EC2::SecurityGroup"),
    ]
    coverage = engine(catalog).price_inventory(resources).coverage

    assert coverage.total == 5
    assert coverage.priced == 1
    assert coverage.usage_based == 1
    assert coverage.free == 3
    # Free resources are out of the denominator, so 1 of 2, not 1 of 5.
    assert coverage.priced_percent == 50.0


def test_deterministic_resource_without_a_price_counts_as_unresolved():
    """A priceable type with no available price is a gap, not a success."""
    resources = [make_resource("Server", properties={"InstanceType": "t3.large"})]
    coverage = engine(StaticPriceCatalog(version="empty")).price_inventory(resources).coverage

    assert coverage.priced == 0
    assert coverage.unresolved == 1


def test_inventory_can_be_filtered_by_class(catalog):
    resources = [
        make_resource("Server", properties={"InstanceType": "t3.large"}),
        make_resource("Bucket", "AWS::S3::Bucket", {"BucketName": "x"}),
    ]
    inventory = engine(catalog).price_inventory(resources)

    assert len(inventory.by_class(PricingClass.USAGE_BASED)) == 1
    assert len(inventory.priced) == 1


def test_inventory_serialises_for_the_report(catalog):
    resources = [
        make_resource("Server", properties={"InstanceType": "t3.large"}),
        make_resource("Bucket", "AWS::S3::Bucket", {"BucketName": "x"}),
    ]
    payload = engine(catalog, discount=10).price_inventory(resources).to_dict()

    # 60.736 list, 60.736 * 0.90 = 54.6624 discounted
    assert payload["monthlyCostList"] == 60.74
    assert payload["monthlyCost"] == 54.66
    assert payload["coverage"]["resourcesPriced"] == 1
    assert payload["pricingBasis"]["discountPercent"] == 10.0
    assert len(payload["resources"]) == 2


def test_priced_resource_payload_omits_cost_fields_when_not_priced(catalog):
    priced = engine(catalog).price_resource(
        make_resource("Bucket", "AWS::S3::Bucket", {"BucketName": "x"})
    )
    payload = priced.to_dict()

    assert "monthlyCost" not in payload
    assert payload["reason"]
    assert payload["pricingClass"] == "USAGE_BASED"


def test_region_is_part_of_the_lookup(catalog):
    """Prices vary by region, so a catalog built for one must not answer another."""
    other_region = PricingEngine(catalog, "eu-west-1")
    priced = other_region.price_resource(make_resource(properties={"InstanceType": "t3.large"}))
    assert priced.confidence is Confidence.UNAVAILABLE


def test_empty_inventory_is_fully_covered_not_divided_by_zero(catalog):
    inventory = engine(catalog).price_inventory([])
    assert inventory.monthly_list == Decimal(0)
    assert inventory.coverage.priced_percent == 100.0
