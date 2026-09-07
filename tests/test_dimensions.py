"""Resource classification and dimension extraction, one type at a time."""

from decimal import Decimal

from conftest import REGION, make_resource
from pricing import HOURS_PER_MONTH, PricingClass, classify, supported_types
from resolver import ResolutionStatus, Resolved, UnresolvedReason


def classify_of(resource_type, properties=None, resolution=None):
    return classify(
        make_resource(
            resource_type=resource_type, properties=properties, resolution=resolution
        ),
        REGION,
    )


def labels(classification):
    return [component.label for component in classification.components]


# -- EC2 ------------------------------------------------------------------


def test_ec2_instance_is_priced_by_the_hour():
    result = classify_of("AWS::EC2::Instance", {"InstanceType": "t3.large"})

    assert result.pricing_class is PricingClass.DETERMINISTIC
    assert len(result.components) == 1
    assert result.components[0].quantity == HOURS_PER_MONTH
    assert result.components[0].query.attributes


def test_ec2_operating_system_assumption_is_declared_not_hidden():
    """OS is not in a template, so the assumption has to be visible."""
    result = classify_of("AWS::EC2::Instance", {"InstanceType": "t3.large"})
    assert any("Linux" in assumption for assumption in result.assumptions)


def test_ec2_without_instance_type_is_unresolved_not_unsupported():
    result = classify_of("AWS::EC2::Instance", {})
    assert result.pricing_class is PricingClass.UNRESOLVED
    assert "InstanceType" in result.reason


def test_ec2_unresolved_reason_is_carried_through_from_the_resolver():
    """The report should say why a property is missing, not just that it is."""
    resolution = {
        "InstanceType": Resolved(
            status=ResolutionStatus.UNRESOLVED,
            reason=UnresolvedReason.CROSS_STACK_IMPORT,
        )
    }
    result = classify_of("AWS::EC2::Instance", {}, resolution)
    assert "CROSS_STACK_IMPORT" in result.reason


def test_ec2_inline_block_devices_are_priced_as_volumes():
    """Inline block devices are real volumes; missing them undercounts."""
    result = classify_of(
        "AWS::EC2::Instance",
        {
            "InstanceType": "t3.large",
            "BlockDeviceMappings": [
                {"DeviceName": "/dev/xvda", "Ebs": {"VolumeSize": 100, "VolumeType": "gp3"}}
            ],
        },
    )
    assert len(result.components) == 2
    assert any("storage" in label for label in labels(result))


def test_ec2_block_devices_without_ebs_are_ignored():
    result = classify_of(
        "AWS::EC2::Instance",
        {
            "InstanceType": "t3.large",
            "BlockDeviceMappings": [{"DeviceName": "/dev/sdb", "VirtualName": "ephemeral0"}],
        },
    )
    assert len(result.components) == 1


# -- EBS ------------------------------------------------------------------


def test_gp2_volume_charges_storage_only():
    result = classify_of("AWS::EC2::Volume", {"Size": 100, "VolumeType": "gp2"})
    assert len(result.components) == 1
    assert result.components[0].quantity == Decimal(100)


def test_volume_type_defaults_to_gp2():
    result = classify_of("AWS::EC2::Volume", {"Size": 50})
    assert "gp2" in result.description


def test_gp3_below_the_included_iops_adds_nothing():
    result = classify_of(
        "AWS::EC2::Volume", {"Size": 100, "VolumeType": "gp3", "Iops": 3000}
    )
    assert len(result.components) == 1


def test_gp3_above_the_included_iops_charges_only_the_excess():
    """gp3 includes 3000 IOPS free, so only the excess is billable."""
    result = classify_of(
        "AWS::EC2::Volume", {"Size": 100, "VolumeType": "gp3", "Iops": 5000}
    )
    iops_component = next(c for c in result.components if "IOPS" in c.label)
    assert iops_component.quantity == Decimal(2000)


def test_gp3_above_the_included_throughput_charges_only_the_excess():
    result = classify_of(
        "AWS::EC2::Volume",
        {"Size": 100, "VolumeType": "gp3", "Throughput": 250},
    )
    throughput = next(c for c in result.components if "throughput" in c.label)
    assert throughput.quantity == Decimal(125)


def test_io2_provisioned_iops_is_excluded_because_it_is_tiered():
    """io2 IOPS pricing is tiered, so it is named as excluded rather than priced.

    The storage component is still priced; only the tiered IOPS is left out.
    """
    result = classify_of(
        "AWS::EC2::Volume", {"Size": 100, "VolumeType": "io2", "Iops": 5000}
    )
    assert any("storage" in c.label for c in result.components)
    assert not any("IOPS" in c.label for c in result.components)
    assert any("io2 tiered" in item for item in result.excluded)


def test_volume_without_size_is_unresolved():
    result = classify_of("AWS::EC2::Volume", {"VolumeType": "gp3"})
    assert result.pricing_class is PricingClass.UNRESOLVED


def test_string_numbers_are_coerced():
    result = classify_of("AWS::EC2::Volume", {"Size": "250", "VolumeType": "gp2"})
    assert result.components[0].quantity == Decimal(250)


# -- RDS ------------------------------------------------------------------


def test_rds_prices_instance_and_storage_separately():
    result = classify_of(
        "AWS::RDS::DBInstance",
        {
            "DBInstanceClass": "db.t4g.medium",
            "Engine": "postgres",
            "AllocatedStorage": 100,
        },
    )
    assert len(result.components) == 2
    assert any("instance hours" in label for label in labels(result))
    assert any("storage" in label for label in labels(result))


def test_rds_engine_name_is_mapped_to_the_price_list_vocabulary():
    result = classify_of(
        "AWS::RDS::DBInstance",
        {"DBInstanceClass": "db.t4g.medium", "Engine": "postgres"},
    )
    assert "PostgreSQL" in result.description


def test_rds_multi_az_changes_the_deployment_dimension():
    single = classify_of(
        "AWS::RDS::DBInstance",
        {"DBInstanceClass": "db.t4g.medium", "Engine": "postgres"},
    )
    multi = classify_of(
        "AWS::RDS::DBInstance",
        {"DBInstanceClass": "db.t4g.medium", "Engine": "postgres", "MultiAZ": True},
    )
    assert "Single-AZ" in single.description
    assert "Multi-AZ" in multi.description
    assert single.components[0].query != multi.components[0].query


def test_rds_multi_az_accepts_a_stringified_boolean():
    result = classify_of(
        "AWS::RDS::DBInstance",
        {"DBInstanceClass": "db.t4g.medium", "Engine": "postgres", "MultiAZ": "true"},
    )
    assert "Multi-AZ" in result.description


def test_rds_always_excludes_io_requests():
    result = classify_of(
        "AWS::RDS::DBInstance",
        {"DBInstanceClass": "db.t4g.medium", "Engine": "postgres", "AllocatedStorage": 20},
    )
    assert any("I/O" in item for item in result.excluded)


def test_aurora_is_unsupported_because_its_config_is_not_on_the_instance():
    """Aurora Standard and I/O-Optimized differ in price, and which applies is a
    property of the DB cluster, not the instance, so it cannot be priced here."""
    result = classify_of(
        "AWS::RDS::DBInstance",
        {
            "DBInstanceClass": "db.r6g.large",
            "Engine": "aurora-postgresql",
            "AllocatedStorage": 100,
        },
    )
    assert result.pricing_class is PricingClass.UNSUPPORTED
    assert result.components == ()
    assert "Aurora" in (result.reason or "")


def test_rds_without_engine_is_unresolved():
    result = classify_of("AWS::RDS::DBInstance", {"DBInstanceClass": "db.t4g.medium"})
    assert result.pricing_class is PricingClass.UNRESOLVED
    assert "Engine" in result.reason


def test_rds_missing_storage_is_noted_as_excluded_not_silently_zero():
    result = classify_of(
        "AWS::RDS::DBInstance",
        {"DBInstanceClass": "db.t4g.medium", "Engine": "postgres"},
    )
    assert any("AllocatedStorage" in item for item in result.excluded)


# -- split resources (S18) ------------------------------------------------


def test_nat_gateway_needs_no_properties_but_declares_its_exclusion():
    """Hourly cost depends on existence alone; data processing does not."""
    result = classify_of("AWS::EC2::NatGateway", {})

    assert result.pricing_class is PricingClass.DETERMINISTIC
    assert result.components[0].quantity == HOURS_PER_MONTH
    assert result.excluded == ("Data processing per GB",)


def test_load_balancer_defaults_to_application_and_excludes_lcus():
    result = classify_of("AWS::ElasticLoadBalancingV2::LoadBalancer", {})

    assert result.pricing_class is PricingClass.DETERMINISTIC
    assert "Application" in result.description
    assert any("LCU" in item for item in result.excluded)


def test_network_load_balancer_uses_its_own_family():
    application = classify_of("AWS::ElasticLoadBalancingV2::LoadBalancer", {})
    network = classify_of(
        "AWS::ElasticLoadBalancingV2::LoadBalancer", {"Type": "network"}
    )
    assert application.components[0].query != network.components[0].query


def test_gateway_load_balancer_excludes_glcus():
    result = classify_of(
        "AWS::ElasticLoadBalancingV2::LoadBalancer", {"Type": "gateway"}
    )
    assert any("GLCU" in item for item in result.excluded)


def test_unknown_load_balancer_type_is_unsupported():
    result = classify_of(
        "AWS::ElasticLoadBalancingV2::LoadBalancer", {"Type": "quantum"}
    )
    assert result.pricing_class is PricingClass.UNSUPPORTED


# -- ElastiCache, EIP -----------------------------------------------------


def test_elasticache_multiplies_hours_by_node_count():
    result = classify_of(
        "AWS::ElastiCache::CacheCluster",
        {"CacheNodeType": "cache.t4g.micro", "Engine": "redis", "NumCacheNodes": 3},
    )
    assert result.components[0].quantity == HOURS_PER_MONTH * 3


def test_elasticache_node_count_defaults_to_one():
    result = classify_of(
        "AWS::ElastiCache::CacheCluster", {"CacheNodeType": "cache.t4g.micro"}
    )
    assert result.components[0].quantity == HOURS_PER_MONTH


def test_elastic_ip_is_charged_hourly_regardless_of_attachment():
    result = classify_of("AWS::EC2::EIP", {"Domain": "vpc"})
    assert result.pricing_class is PricingClass.DETERMINISTIC
    assert result.components[0].quantity == HOURS_PER_MONTH


# -- DynamoDB, class depends on properties --------------------------------


def test_provisioned_dynamodb_prices_read_and_write_capacity():
    result = classify_of(
        "AWS::DynamoDB::Table",
        {
            "BillingMode": "PROVISIONED",
            "ProvisionedThroughput": {"ReadCapacityUnits": 5, "WriteCapacityUnits": 10},
        },
    )
    assert result.pricing_class is PricingClass.DETERMINISTIC
    assert len(result.components) == 2
    read = next(c for c in result.components if "read" in c.label)
    assert read.quantity == HOURS_PER_MONTH * 5


def test_billing_mode_defaults_to_provisioned():
    result = classify_of(
        "AWS::DynamoDB::Table",
        {"ProvisionedThroughput": {"ReadCapacityUnits": 1, "WriteCapacityUnits": 1}},
    )
    assert result.pricing_class is PricingClass.DETERMINISTIC


def test_on_demand_dynamodb_is_usage_based():
    """Same resource type, different class depending on billing mode."""
    result = classify_of("AWS::DynamoDB::Table", {"BillingMode": "PAY_PER_REQUEST"})
    assert result.pricing_class is PricingClass.USAGE_BASED
    assert "request volume" in result.reason


def test_provisioned_dynamodb_without_throughput_is_unresolved():
    result = classify_of("AWS::DynamoDB::Table", {"BillingMode": "PROVISIONED"})
    assert result.pricing_class is PricingClass.UNRESOLVED


def test_dynamodb_storage_is_excluded():
    result = classify_of(
        "AWS::DynamoDB::Table",
        {"ProvisionedThroughput": {"ReadCapacityUnits": 1, "WriteCapacityUnits": 1}},
    )
    assert any("storage" in item.lower() for item in result.excluded)


# -- classification buckets -----------------------------------------------


def test_usage_based_types_carry_a_reason():
    result = classify_of("AWS::S3::Bucket", {"BucketName": "example"})
    assert result.pricing_class is PricingClass.USAGE_BASED
    assert "stored volume" in result.reason


def test_lambda_is_usage_based():
    result = classify_of("AWS::Lambda::Function", {"MemorySize": 512})
    assert result.pricing_class is PricingClass.USAGE_BASED


def test_scaffolding_resources_are_free_not_unsupported():
    """Free is a distinct bucket so coverage stays meaningful."""
    for resource_type in (
        "AWS::EC2::VPC",
        "AWS::EC2::Subnet",
        "AWS::EC2::SecurityGroup",
        "AWS::IAM::Role",
        "AWS::ElasticLoadBalancingV2::TargetGroup",
        "AWS::RDS::DBSubnetGroup",
    ):
        assert classify_of(resource_type, {}).pricing_class is PricingClass.FREE


def test_unknown_types_are_unsupported_and_named():
    result = classify_of("AWS::Braket::QuantumTask", {})
    assert result.pricing_class is PricingClass.UNSUPPORTED
    assert "AWS::Braket::QuantumTask" in result.reason


def test_empty_resource_type_is_unsupported_not_crashing():
    result = classify_of("", {})
    assert result.pricing_class is PricingClass.UNSUPPORTED


def test_supported_types_are_the_nine_from_the_spec():
    """Kept exact rather than a subset check.

    Adding a mapper is a documentation change as much as a code change: the
    coverage claim in the spec and the count in the reports both follow from this
    set. An assertion that only checked membership would let the two drift.
    """
    assert supported_types() == {
        "AWS::EC2::Instance",
        "AWS::EC2::Volume",
        "AWS::RDS::DBInstance",
        "AWS::EC2::NatGateway",
        "AWS::ElasticLoadBalancingV2::LoadBalancer",
        "AWS::ElastiCache::CacheCluster",
        "AWS::ElastiCache::ReplicationGroup",
        "AWS::EC2::EIP",
        "AWS::DynamoDB::Table",
    }


# -- ElastiCache replication groups ---------------------------------------
#
# The node count is the whole difficulty. A replication group expresses it three
# different ways, and getting it wrong scales the error by the size of the group —
# a nine-node cluster priced as one node under-reports by 89%, which is exactly
# the magnitude that still reads as plausible.


def replication_group(**properties):
    return classify_of("AWS::ElastiCache::ReplicationGroup", properties)


def nodes_priced(result):
    """Node count recovered from the component quantity."""
    from pricing.dimensions import HOURS_PER_MONTH

    return result.components[0].quantity / HOURS_PER_MONTH


def test_a_replication_group_is_priced():
    result = replication_group(CacheNodeType="cache.t4g.micro", Engine="redis")

    assert result.pricing_class is PricingClass.DETERMINISTIC
    assert result.components


def test_cluster_mode_disabled_uses_num_cache_clusters():
    """NumCacheClusters already counts the primary, so it is used as given rather
    than incremented."""
    result = replication_group(CacheNodeType="cache.t4g.micro", NumCacheClusters=3)

    assert nodes_priced(result) == 3


def test_cluster_mode_enabled_multiplies_shards_by_nodes_per_shard():
    """Three shards with two replicas each is nine nodes: each shard has a primary
    plus its replicas."""
    result = replication_group(
        CacheNodeType="cache.r6g.large", NumNodeGroups=3, ReplicasPerNodeGroup=2
    )

    assert nodes_priced(result) == 9


def test_shards_with_no_replicas_are_one_node_each():
    result = replication_group(CacheNodeType="cache.t4g.micro", NumNodeGroups=4)

    assert nodes_priced(result) == 4


def test_an_explicit_shard_layout_is_counted_by_length():
    """NodeGroupConfiguration is used instead of NumNodeGroups, one entry per
    shard."""
    result = replication_group(
        CacheNodeType="cache.t4g.micro",
        NodeGroupConfiguration=[{"Slots": "0-5461"}, {"Slots": "5462-10922"}],
        ReplicasPerNodeGroup=1,
    )

    assert nodes_priced(result) == 4


def test_num_node_groups_wins_over_the_explicit_layout():
    """Both can appear. NumNodeGroups is the authoritative shard count."""
    result = replication_group(
        CacheNodeType="cache.t4g.micro",
        NumNodeGroups=5,
        NodeGroupConfiguration=[{"Slots": "0-16383"}],
    )

    assert nodes_priced(result) == 5


def test_replicas_alone_still_add_a_primary():
    """ReplicasPerNodeGroup without a shard count means one shard."""
    result = replication_group(CacheNodeType="cache.t4g.micro", ReplicasPerNodeGroup=2)

    assert nodes_priced(result) == 3


def test_an_unspecified_count_is_one_node_and_says_so():
    """A silent default is the thing to avoid. The description carries the
    assumption so a reader can see it was assumed rather than read."""
    result = replication_group(CacheNodeType="cache.t4g.micro")

    assert nodes_priced(result) == 1
    assert "not specified" in result.description


def test_the_description_shows_the_arithmetic():
    """A reader seeing nine nodes for a template that says NumNodeGroups: 3 needs
    to be able to check it without reading the mapper."""
    result = replication_group(
        CacheNodeType="cache.r6g.large", NumNodeGroups=3, ReplicasPerNodeGroup=2
    )

    assert "3 shard(s)" in result.description
    assert "9 nodes" in result.description


def test_a_missing_node_type_is_unresolved_not_guessed():
    """Without CacheNodeType there is no price to look up, and inventing one would
    be worse than reporting the gap."""
    result = replication_group(NumCacheClusters=2)

    assert result.pricing_class is PricingClass.UNRESOLVED
    assert "CacheNodeType" in (result.reason or "")


def test_the_engine_defaults_to_redis():
    result = replication_group(CacheNodeType="cache.t4g.micro")

    assert "redis" in result.description


def test_valkey_is_carried_through():
    """Valkey is a distinct price list engine, not a Redis alias."""
    result = replication_group(CacheNodeType="cache.t4g.micro", Engine="valkey")

    assert dict(result.components[0].query.attributes)["cacheEngine"] == "Valkey"


def test_usage_based_components_are_named_not_silently_omitted():
    """Backup storage and data transfer cannot be derived from a template. Stating
    them keeps the figure trustworthy."""
    result = replication_group(CacheNodeType="cache.t4g.micro")

    assert "Backup storage" in result.excluded
    assert "Data transfer" in result.excluded


def test_the_node_type_reaches_the_price_query():
    result = replication_group(CacheNodeType="cache.r6g.large", NumCacheClusters=2)

    attributes = dict(result.components[0].query.attributes)
    assert attributes["instanceType"] == "cache.r6g.large"
    assert attributes["productFamily"] == "Cache Instance"


def test_a_nine_node_group_costs_nine_times_a_one_node_group():
    """The arithmetic that matters, asserted as a ratio so it cannot pass by
    coincidence."""
    single = replication_group(CacheNodeType="cache.r6g.large", NumCacheClusters=1)
    nine = replication_group(
        CacheNodeType="cache.r6g.large", NumNodeGroups=3, ReplicasPerNodeGroup=2
    )

    assert nine.components[0].quantity == single.components[0].quantity * 9
