"""Resource type to pricing dimensions.

One mapper per priceable resource type. Each decides which properties it needs,
builds the price queries, and declares what it is deliberately not pricing.

The important asymmetry, discovered while testing the resolver: a resource can
have every property unresolved and still be fully priceable. A NAT Gateway's
hourly charge depends on nothing but its existence. So coverage is judged per
*pricing dimension*, never per property — a mapper asks only for what it needs
and ignores the rest.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from resolver import ResolvedResource

from .models import (
    HOURS_PER_MONTH,
    PriceQuery,
    PricingClass,
)
from .platform import AssumedPlatformResolver, PlatformResolver

# -- specs ----------------------------------------------------------------


@dataclass(frozen=True)
class ComponentSpec:
    """One cost component, before a price has been looked up."""

    label: str
    query: PriceQuery
    quantity: Decimal
    excluded: tuple[str, ...] = ()


@dataclass(frozen=True)
class Classification:
    """What the engine needs to know about a resource before pricing it."""

    pricing_class: PricingClass
    components: tuple[ComponentSpec, ...] = ()
    assumptions: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()
    description: str = ""
    reason: str | None = None


Mapper = Callable[[ResolvedResource, str], Classification]


# -- coercion helpers -----------------------------------------------------


def _number(value: Any) -> Decimal | None:
    """Coerce a resolved property to Decimal, or None if it isn't numeric."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value.strip())
        except (InvalidOperation, ValueError):
            return None
    return None


def _flag(value: Any) -> bool:
    """Interpret a resolved property as a boolean.

    Templates express booleans as real booleans or as the strings CloudFormation
    stringifies them to, so both have to be understood.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return False


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    return text or None


def _unresolved(resource: ResolvedResource, prop: str) -> Classification:
    """A type we can price, missing a property we need.

    The resolver's reason is carried through so the report can say *why* the
    property is missing rather than just that it is.
    """
    record = resource.resolution.get(prop)
    cause = ""
    if record is not None and not record.is_resolved and record.reason is not None:
        cause = f" ({record.reason.value})"

    return Classification(
        PricingClass.UNRESOLVED,
        description=resource.resource_type,
        reason=f"{prop} could not be resolved{cause}",
    )


# -- EBS, shared by volumes and inline block devices ----------------------

#: gp3 includes this much at no extra charge.
_GP3_FREE_IOPS = Decimal(3000)
_GP3_FREE_THROUGHPUT_MBPS = Decimal(125)


def _ebs_components(
    spec: dict[str, Any], region: str, prefix: str
) -> tuple[list[ComponentSpec], list[str]]:
    """Build storage, IOPS, and throughput components for one EBS volume.

    Shared between ``AWS::EC2::Volume`` and the block devices declared inline on
    an EC2 instance. Inline devices are real volumes and bill separately; missing
    them would undercount every instance that declares one.

    Returns the priced components and a list of dimensions deliberately left
    unpriced (io2 provisioned IOPS, whose tiered rate cannot be a single number).
    """
    volume_type = _text(spec.get("VolumeType")) or "gp2"
    # AWS::EC2::Volume calls it Size; EC2 block devices call it VolumeSize.
    size = _number(spec.get("Size")) or _number(spec.get("VolumeSize"))

    components: list[ComponentSpec] = []
    excluded: list[str] = []

    if size is not None and size > 0:
        components.append(
            ComponentSpec(
                label=f"{prefix} storage ({volume_type}, {size}GB)",
                query=PriceQuery.of(
                    "AmazonEC2",
                    region,
                    productFamily="Storage",
                    volumeType=volume_type,
                ),
                quantity=size,
            )
        )

    iops = _number(spec.get("Iops"))

    if volume_type == "gp3":
        if iops is not None and iops > _GP3_FREE_IOPS:
            components.append(
                ComponentSpec(
                    label=f"{prefix} provisioned IOPS above {_GP3_FREE_IOPS}",
                    query=PriceQuery.of(
                        "AmazonEC2",
                        region,
                        productFamily="System Operation",
                        volumeType=volume_type,
                    ),
                    quantity=iops - _GP3_FREE_IOPS,
                )
            )
        throughput = _number(spec.get("Throughput"))
        if throughput is not None and throughput > _GP3_FREE_THROUGHPUT_MBPS:
            components.append(
                ComponentSpec(
                    label=f"{prefix} provisioned throughput above "
                    f"{_GP3_FREE_THROUGHPUT_MBPS}MB/s",
                    query=PriceQuery.of(
                        "AmazonEC2",
                        region,
                        productFamily="Provisioned Throughput",
                        volumeType=volume_type,
                    ),
                    quantity=throughput - _GP3_FREE_THROUGHPUT_MBPS,
                )
            )

    elif volume_type == "io1" and iops is not None and iops > 0:
        components.append(
            ComponentSpec(
                label=f"{prefix} provisioned IOPS",
                query=PriceQuery.of(
                    "AmazonEC2",
                    region,
                    productFamily="System Operation",
                    volumeType=volume_type,
                ),
                quantity=iops,
            )
        )

    elif volume_type == "io2" and iops is not None and iops > 0:
        # io2 provisioned IOPS is tiered (rate drops in bands as IOPS climb), so
        # there is no single unit price to apply. Named rather than guessed at.
        excluded.append(f"{prefix} provisioned IOPS (io2 tiered pricing)")

    return components, excluded


# -- mappers --------------------------------------------------------------


def _ec2_instance(
    resource: ResolvedResource,
    region: str,
    platforms: PlatformResolver | None = None,
) -> Classification:
    instance_type = _text(resource.properties.get("InstanceType"))
    if instance_type is None:
        return _unresolved(resource, "InstanceType")

    resolver = platforms or AssumedPlatformResolver()
    platform = resolver.resolve(_text(resource.properties.get("ImageId")))

    # preInstalledSw is only sent when it is not the default. The price cache is
    # keyed without it for ordinary images, so including "NA" would miss every
    # lookup. When software *is* bundled the key deliberately will not match
    # until a sync rule exists for it, which surfaces as UNAVAILABLE rather than
    # as the much cheaper base rate.
    software = (
        {"preInstalledSw": platform.pre_installed_sw}
        if platform.has_bundled_software
        else {}
    )
    tenancy_value = (_text(resource.properties.get("Tenancy")) or "default").lower()
    tenancy = {
        "default": "Shared",
        "dedicated": "Dedicated",
        "host": "Host",
    }.get(tenancy_value)
    if tenancy is None:
        return _unresolved(resource, "Tenancy")

    components = [
        ComponentSpec(
            label=(
                f"EC2 {instance_type} instance hours "
                f"({platform.operating_system})"
            ),
            query=PriceQuery.of(
                "AmazonEC2",
                region,
                productFamily="Compute Instance",
                instanceType=instance_type,
                operatingSystem=platform.operating_system,
                tenancy=tenancy,
                **software,
            ),
            quantity=HOURS_PER_MONTH,
        )
    ]

    block_excluded: list[str] = [
        "AMI-provided storage outside explicit BlockDeviceMappings"
    ]
    block_devices = resource.properties.get("BlockDeviceMappings")
    if isinstance(block_devices, list):
        for index, mapping in enumerate(block_devices):
            if not isinstance(mapping, dict):
                continue
            ebs = mapping.get("Ebs")
            if isinstance(ebs, dict):
                device = _text(mapping.get("DeviceName")) or f"device {index}"
                comps, excl = _ebs_components(ebs, region, f"EBS {device}")
                components.extend(comps)
                block_excluded.extend(excl)

    assumptions = [f"{tenancy} tenancy"]
    if platform.is_assumed:
        assumptions.insert(
            0,
            f"{platform.operating_system} OS assumed — AMI could not be resolved",
        )
    else:
        assumptions.insert(0, f"{platform.detail} (from AMI)")

    description = f"{instance_type} {platform.operating_system}"
    if platform.has_bundled_software:
        description += f" + {platform.pre_installed_sw}"

    return Classification(
        PricingClass.DETERMINISTIC,
        components=tuple(components),
        assumptions=tuple(assumptions),
        excluded=tuple(block_excluded),
        description=description,
    )


def _ebs_volume(resource: ResolvedResource, region: str) -> Classification:
    if _number(resource.properties.get("Size")) is None:
        return _unresolved(resource, "Size")

    volume_type = _text(resource.properties.get("VolumeType")) or "gp2"
    size = _number(resource.properties.get("Size"))
    components, excluded = _ebs_components(resource.properties, region, "Volume")

    return Classification(
        PricingClass.DETERMINISTIC,
        components=tuple(components),
        excluded=tuple(excluded),
        description=f"{volume_type} {size}GB",
    )


#: Template engine identifiers to Price List engine names.
_RDS_ENGINES = {
    "postgres": "PostgreSQL",
    "mysql": "MySQL",
    "mariadb": "MariaDB",
    "oracle-se2": "Oracle",
    "oracle-se2-cdb": "Oracle",
    "oracle-ee": "Oracle",
    "oracle-ee-cdb": "Oracle",
    "sqlserver-ex": "SQL Server",
    "sqlserver-web": "SQL Server",
    "sqlserver-se": "SQL Server",
    "sqlserver-ee": "SQL Server",
    "aurora-postgresql": "Aurora PostgreSQL",
    "aurora-mysql": "Aurora MySQL",
}


def _rds_instance(resource: ResolvedResource, region: str) -> Classification:
    instance_class = _text(resource.properties.get("DBInstanceClass"))
    if instance_class is None:
        return _unresolved(resource, "DBInstanceClass")

    engine_raw = _text(resource.properties.get("Engine"))
    if engine_raw is None:
        return _unresolved(resource, "Engine")

    engine_key = engine_raw.lower()

    if engine_key.startswith("aurora"):
        # Aurora ships two instance rows at different prices — Standard and
        # I/O-Optimized — and which one applies is a property of the DB cluster,
        # not the instance being priced here. It cannot be disambiguated from a
        # DBInstance alone, so pricing it would be a guess. Verified against the
        # live us-east-1 Price List (2026-08).
        return Classification(
            PricingClass.UNSUPPORTED,
            description=f"{instance_class} {engine_raw}",
            reason=(
                "Aurora instance pricing depends on the cluster's storage "
                "configuration (Standard vs I/O-Optimized), set on the DB cluster "
                "rather than the instance"
            ),
        )

    engine = _RDS_ENGINES.get(engine_key, engine_raw)

    multi_az = _flag(resource.properties.get("MultiAZ"))
    deployment = "Multi-AZ" if multi_az else "Single-AZ"

    components = [
        ComponentSpec(
            label=f"RDS {instance_class} {engine} instance hours ({deployment})",
            query=PriceQuery.of(
                "AmazonRDS",
                region,
                productFamily="Database Instance",
                instanceType=instance_class,
                databaseEngine=engine,
                deploymentOption=deployment,
            ),
            quantity=HOURS_PER_MONTH,
        )
    ]

    excluded: list[str] = []

    storage = _number(resource.properties.get("AllocatedStorage"))
    storage_type = _text(resource.properties.get("StorageType")) or "gp2"
    if storage is not None and storage > 0:
        components.append(
            ComponentSpec(
                label=f"RDS storage ({storage_type}, {storage}GB, {deployment})",
                query=PriceQuery.of(
                    "AmazonRDS",
                    region,
                    productFamily="Database Storage",
                    volumeType=storage_type,
                    deploymentOption=deployment,
                ),
                quantity=storage,
            )
        )
    else:
        excluded.append("Storage (AllocatedStorage not resolved)")

    iops = _number(resource.properties.get("Iops"))
    if storage_type in ("io1", "io2") and iops is not None and iops > 0:
        components.append(
            ComponentSpec(
                label=f"RDS provisioned IOPS ({deployment})",
                query=PriceQuery.of(
                    "AmazonRDS",
                    region,
                    productFamily="Provisioned IOPS",
                    deploymentOption=deployment,
                ),
                quantity=iops,
            )
        )
    elif storage_type == "gp3" and iops is not None and iops > 0:
        excluded.append("gp3 provisioned IOPS")
    else:
        excluded.append("I/O requests")

    throughput = _number(resource.properties.get("StorageThroughput"))
    if storage_type == "gp3" and throughput is not None and throughput > 0:
        excluded.append("gp3 storage throughput")

    return Classification(
        PricingClass.DETERMINISTIC,
        components=tuple(components),
        excluded=tuple(excluded),
        description=f"{instance_class} {engine}, {deployment}",
    )


def _nat_gateway(resource: ResolvedResource, region: str) -> Classification:
    """Split resource (S18): hourly is knowable, data processing is not."""
    return Classification(
        PricingClass.DETERMINISTIC,
        components=(
            ComponentSpec(
                label="NAT Gateway hours",
                query=PriceQuery.of(
                    "AmazonEC2", region, productFamily="NAT Gateway", usageFamily="Hours"
                ),
                quantity=HOURS_PER_MONTH,
                excluded=("Data processing per GB",),
            ),
        ),
        excluded=("Data processing per GB",),
        description="NAT Gateway (hourly only)",
    )


_LOAD_BALANCER_FAMILIES = {
    "application": "Load Balancer-Application",
    "network": "Load Balancer-Network",
    "gateway": "Load Balancer-Gateway",
}


def _load_balancer(resource: ResolvedResource, region: str) -> Classification:
    """Split resource (S18): hourly is knowable, LCUs are not."""
    lb_type = (_text(resource.properties.get("Type")) or "application").lower()
    family = _LOAD_BALANCER_FAMILIES.get(lb_type)

    if family is None:
        return Classification(
            PricingClass.UNSUPPORTED,
            description=f"Load balancer type {lb_type!r}",
            reason=f"Unrecognised load balancer type {lb_type!r}",
        )

    unit_label = "LCU" if lb_type != "gateway" else "GLCU"

    return Classification(
        PricingClass.DETERMINISTIC,
        components=(
            ComponentSpec(
                label=f"{lb_type.title()} Load Balancer hours",
                query=PriceQuery.of("AWSELB", region, productFamily=family),
                quantity=HOURS_PER_MONTH,
                excluded=(f"{unit_label} usage",),
            ),
        ),
        excluded=(f"{unit_label} usage",),
        description=f"{lb_type.title()} Load Balancer (hourly only)",
    )


def _elasticache_cluster(resource: ResolvedResource, region: str) -> Classification:
    node_type = _text(resource.properties.get("CacheNodeType"))
    if node_type is None:
        return _unresolved(resource, "CacheNodeType")

    engine = _text(resource.properties.get("Engine")) or "redis"
    nodes = _number(resource.properties.get("NumCacheNodes")) or Decimal(1)

    return Classification(
        PricingClass.DETERMINISTIC,
        components=(
            ComponentSpec(
                label=f"ElastiCache {node_type} × {nodes} node hours",
                query=PriceQuery.of(
                    "AmazonElastiCache",
                    region,
                    productFamily="Cache Instance",
                    instanceType=node_type,
                    cacheEngine=engine.capitalize(),
                ),
                quantity=HOURS_PER_MONTH * nodes,
            ),
        ),
        description=f"{node_type} {engine}, {nodes} node(s)",
    )


def _elasticache_replication_group(
    resource: ResolvedResource, region: str
) -> Classification:
    """A Redis or Valkey replication group.

    Priceable without cross-resource resolution — unlike an Auto Scaling group or
    an Aurora cluster, every input is a property of this resource. What makes it
    awkward is that the node count can be expressed three different ways, and
    getting it wrong scales the error by the whole group.

    Cluster mode disabled
        ``NumCacheClusters`` is the total, primary plus replicas.

    Cluster mode enabled
        ``NumNodeGroups`` shards, each with ``ReplicasPerNodeGroup`` replicas plus
        its own primary, so the total is ``shards * (1 + replicas)``.

    Explicit shard layout
        ``NodeGroupConfiguration`` is a list with one entry per shard, used
        instead of ``NumNodeGroups``.

    A group with three shards and two replicas each is nine nodes, not three and
    not one. Defaulting to a single node would under-report it by 89%, which is
    the kind of error that reads as plausible.
    """
    node_type = _text(resource.properties.get("CacheNodeType"))
    if node_type is None:
        return _unresolved(resource, "CacheNodeType")

    engine = _text(resource.properties.get("Engine")) or "redis"

    nodes, shape = _replication_group_nodes(resource)

    return Classification(
        PricingClass.DETERMINISTIC,
        components=(
            ComponentSpec(
                label=f"ElastiCache {node_type} × {nodes} node hours",
                query=PriceQuery.of(
                    "AmazonElastiCache",
                    region,
                    productFamily="Cache Instance",
                    instanceType=node_type,
                    cacheEngine=engine.capitalize(),
                ),
                quantity=HOURS_PER_MONTH * nodes,
            ),
        ),
        # Backup storage beyond the free allowance and data transfer are both
        # usage-based, so they are named rather than guessed at.
        excluded=("Backup storage", "Data transfer"),
        description=f"{node_type} {engine}, {shape}",
    )


def _replication_group_nodes(resource: ResolvedResource) -> tuple[Decimal, str]:
    """Total billable nodes, and a phrase describing how that was arrived at.

    The description is returned alongside the number because a reader seeing
    "9 nodes" for a resource whose template says ``NumNodeGroups: 3`` needs to be
    able to check the arithmetic without reading this function.
    """
    replicas = _number(resource.properties.get("ReplicasPerNodeGroup"))

    shards = _number(resource.properties.get("NumNodeGroups"))
    if shards is None:
        configuration = resource.properties.get("NodeGroupConfiguration")
        if isinstance(configuration, list) and configuration:
            shards = Decimal(len(configuration))

    if shards is not None:
        per_shard = Decimal(1) + (replicas or Decimal(0))
        total = shards * per_shard
        if replicas:
            return total, f"{shards} shard(s) × {per_shard} node(s) = {total} nodes"
        return total, f"{shards} shard(s), no replicas = {total} nodes"

    # Cluster mode disabled. NumCacheClusters already counts the primary.
    clusters = _number(resource.properties.get("NumCacheClusters"))
    if clusters is not None:
        return clusters, f"{clusters} node(s)"

    # Neither form given. ElastiCache creates a single primary, and saying so is
    # better than silently assuming it.
    if replicas:
        total = Decimal(1) + replicas
        return total, f"1 primary + {replicas} replica(s) = {total} nodes"

    return Decimal(1), "1 node (count not specified)"


def _elastic_ip(resource: ResolvedResource, region: str) -> Classification:
    """All public IPv4 addresses carry an hourly charge, attached or not.

    Priced under AmazonVPC by group (these products have no productFamily),
    matching the sync rule. Verified against the live us-east-1 Price List.
    """
    return Classification(
        PricingClass.DETERMINISTIC,
        components=(
            ComponentSpec(
                label="Public IPv4 address hours",
                query=PriceQuery.of(
                    "AmazonVPC", region, group="VPCPublicIPv4Address"
                ),
                quantity=HOURS_PER_MONTH,
            ),
        ),
        description="Public IPv4 address",
    )


def _dynamodb_table(resource: ResolvedResource, region: str) -> Classification:
    """Deterministic or usage-based depending on billing mode."""
    billing_mode = (_text(resource.properties.get("BillingMode")) or "PROVISIONED").upper()

    if billing_mode == "PAY_PER_REQUEST":
        return Classification(
            PricingClass.USAGE_BASED,
            description="DynamoDB table (on-demand)",
            reason="On-demand billing depends on request volume",
        )

    throughput = resource.properties.get("ProvisionedThroughput")
    if not isinstance(throughput, dict):
        return _unresolved(resource, "ProvisionedThroughput")

    rcu = _number(throughput.get("ReadCapacityUnits"))
    wcu = _number(throughput.get("WriteCapacityUnits"))
    if rcu is None:
        return _unresolved(resource, "ProvisionedThroughput.ReadCapacityUnits")
    if wcu is None:
        return _unresolved(resource, "ProvisionedThroughput.WriteCapacityUnits")

    # Provisioned global secondary indexes are billed independently from the
    # table. Aggregate their capacity so the deterministic total is complete.
    for index, gsi in enumerate(resource.properties.get("GlobalSecondaryIndexes") or []):
        if not isinstance(gsi, dict):
            continue
        gsi_throughput = gsi.get("ProvisionedThroughput")
        if not isinstance(gsi_throughput, dict):
            return _unresolved(resource, f"GlobalSecondaryIndexes[{index}].ProvisionedThroughput")
        gsi_rcu = _number(gsi_throughput.get("ReadCapacityUnits"))
        gsi_wcu = _number(gsi_throughput.get("WriteCapacityUnits"))
        if gsi_rcu is None or gsi_wcu is None:
            return _unresolved(resource, f"GlobalSecondaryIndexes[{index}].ProvisionedThroughput")
        rcu += gsi_rcu
        wcu += gsi_wcu

    return Classification(
        PricingClass.DETERMINISTIC,
        components=(
            ComponentSpec(
                label=f"DynamoDB provisioned read capacity ({rcu} RCU)",
                query=PriceQuery.of(
                    "AmazonDynamoDB",
                    region,
                    productFamily="Provisioned IOPS",
                    group="DDB-ReadUnits",
                ),
                quantity=rcu * HOURS_PER_MONTH,
            ),
            ComponentSpec(
                label=f"DynamoDB provisioned write capacity ({wcu} WCU)",
                query=PriceQuery.of(
                    "AmazonDynamoDB",
                    region,
                    productFamily="Provisioned IOPS",
                    group="DDB-WriteUnits",
                ),
                quantity=wcu * HOURS_PER_MONTH,
            ),
        ),
        excluded=("Table storage", "Backup and restore"),
        description=f"DynamoDB provisioned ({rcu} RCU / {wcu} WCU)",
    )


_MAPPERS: dict[str, Mapper] = {
    "AWS::EC2::Instance": _ec2_instance,
    "AWS::EC2::Volume": _ebs_volume,
    "AWS::RDS::DBInstance": _rds_instance,
    "AWS::EC2::NatGateway": _nat_gateway,
    "AWS::ElasticLoadBalancingV2::LoadBalancer": _load_balancer,
    "AWS::ElastiCache::CacheCluster": _elasticache_cluster,
    "AWS::ElastiCache::ReplicationGroup": _elasticache_replication_group,
    "AWS::EC2::EIP": _elastic_ip,
    "AWS::DynamoDB::Table": _dynamodb_table,
}


# -- usage-based (S16: listed, never estimated) ---------------------------

_USAGE_BASED: dict[str, str] = {
    "AWS::S3::Bucket": "Cost depends on stored volume and request count",
    "AWS::Lambda::Function": "Cost depends on invocation count and duration",
    "AWS::CloudFront::Distribution": "Cost depends on data transfer and requests",
    "AWS::ApiGateway::RestApi": "Cost depends on request count",
    "AWS::ApiGatewayV2::Api": "Cost depends on request count",
    "AWS::SQS::Queue": "Cost depends on request count",
    "AWS::SNS::Topic": "Cost depends on publish and delivery volume",
    "AWS::Logs::LogGroup": "Cost depends on ingested and stored log volume",
    "AWS::StepFunctions::StateMachine": "Cost depends on state transitions",
    "AWS::Athena::WorkGroup": "Cost depends on data scanned",
    "AWS::SES::ConfigurationSet": "Cost depends on messages sent",
    "AWS::Kinesis::Stream": "Cost depends on shard hours and put payload units",
    "AWS::EFS::FileSystem": "Cost depends on stored volume and throughput",
}


# -- free (excluded from the coverage denominator) ------------------------

_FREE: frozenset[str] = frozenset(
    {
        # Networking scaffolding
        "AWS::EC2::VPC",
        "AWS::EC2::Subnet",
        "AWS::EC2::RouteTable",
        "AWS::EC2::Route",
        "AWS::EC2::InternetGateway",
        "AWS::EC2::VPCGatewayAttachment",
        "AWS::EC2::SubnetRouteTableAssociation",
        "AWS::EC2::SecurityGroup",
        "AWS::EC2::SecurityGroupIngress",
        "AWS::EC2::SecurityGroupEgress",
        "AWS::EC2::NetworkAcl",
        "AWS::EC2::NetworkAclEntry",
        "AWS::EC2::SubnetNetworkAclAssociation",
        "AWS::EC2::DHCPOptions",
        "AWS::EC2::VPCDHCPOptionsAssociation",
        "AWS::EC2::EIPAssociation",
        "AWS::EC2::LaunchTemplate",
        "AWS::EC2::KeyPair",
        "AWS::EC2::PlacementGroup",
        # Identity
        "AWS::IAM::Role",
        "AWS::IAM::Policy",
        "AWS::IAM::ManagedPolicy",
        "AWS::IAM::InstanceProfile",
        "AWS::IAM::User",
        "AWS::IAM::Group",
        "AWS::IAM::ServiceLinkedRole",
        # Load balancing scaffolding — the balancer itself is priced
        "AWS::ElasticLoadBalancingV2::TargetGroup",
        "AWS::ElasticLoadBalancingV2::Listener",
        "AWS::ElasticLoadBalancingV2::ListenerRule",
        "AWS::ElasticLoadBalancingV2::ListenerCertificate",
        # Database scaffolding
        "AWS::RDS::DBSubnetGroup",
        "AWS::RDS::DBParameterGroup",
        "AWS::RDS::DBClusterParameterGroup",
        "AWS::RDS::OptionGroup",
        "AWS::ElastiCache::SubnetGroup",
        "AWS::ElastiCache::ParameterGroup",
        # Policies and attachments
        "AWS::S3::BucketPolicy",
        "AWS::SNS::TopicPolicy",
        "AWS::SNS::Subscription",
        "AWS::SQS::QueuePolicy",
        "AWS::Lambda::Permission",
        "AWS::Lambda::EventInvokeConfig",
        "AWS::Lambda::EventSourceMapping",
        "AWS::Lambda::Version",
        "AWS::Lambda::Alias",
        # Orchestration
        "AWS::CloudFormation::WaitConditionHandle",
        "AWS::CloudFormation::WaitCondition",
        # A nested stack is not itself billable; its contents are priced by
        # walking into the child stack.
        "AWS::CloudFormation::Stack",
        "AWS::AutoScaling::ScalingPolicy",
        "AWS::AutoScaling::LifecycleHook",
        "AWS::ApplicationAutoScaling::ScalableTarget",
        "AWS::ApplicationAutoScaling::ScalingPolicy",
        "AWS::Events::Rule",
        "AWS::Logs::SubscriptionFilter",
        "AWS::Logs::MetricFilter",
        # Certificates and parameters
        "AWS::CertificateManager::Certificate",
        "AWS::SSM::Parameter",
    }
)


def classify(
    resource: ResolvedResource,
    region: str,
    platforms: PlatformResolver | None = None,
) -> Classification:
    """Classify and build pricing dimensions for one resolved resource.

    Args:
        resource: A resolved resource from the template.
        region: Region the stack is deployed in.
        platforms: Optional AMI platform resolver. Without it, EC2 instances fall
            back to the documented Linux assumption.
    """
    resource_type = resource.resource_type

    # EC2 is dispatched directly because it is the only mapper that needs the
    # platform resolver. Threading it through the other eleven, which have no use
    # for it, would be noise. If a second capability ever needs injecting —
    # cross-resource lookups for Auto Scaling groups, say — this becomes a
    # context object passed to every mapper.
    if resource_type == "AWS::EC2::Instance":
        return _ec2_instance(resource, region, platforms)

    if resource_type == "AWS::SSM::Parameter":
        tier = (_text(resource.properties.get("Tier")) or "Standard").lower()
        if tier == "standard":
            return Classification(PricingClass.FREE, description="SSM standard parameter")
        return Classification(
            PricingClass.UNSUPPORTED,
            description=f"SSM {tier} parameter",
            reason="Advanced/intelligent-tiering parameter charges are not mapped",
        )

    if resource_type == "AWS::CertificateManager::Certificate":
        if resource.properties.get("CertificateAuthorityArn"):
            return Classification(
                PricingClass.UNSUPPORTED,
                description="Private CA certificate",
                reason="Private CA issuance charges are not mapped",
            )
        return Classification(PricingClass.FREE, description="ACM public certificate")

    mapper = _MAPPERS.get(resource_type)
    if mapper is not None:
        return mapper(resource, region)

    if resource_type in _USAGE_BASED:
        return Classification(
            PricingClass.USAGE_BASED,
            description=resource_type,
            reason=_USAGE_BASED[resource_type],
        )

    if resource_type in _FREE:
        return Classification(PricingClass.FREE, description=resource_type)

    return Classification(
        PricingClass.UNSUPPORTED,
        description=resource_type,
        reason=f"No pricing mapping for {resource_type or 'unknown type'}",
    )


def supported_types() -> frozenset[str]:
    """Resource types with a deterministic pricing mapper."""
    return frozenset(_MAPPERS)
