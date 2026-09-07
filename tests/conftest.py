import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pricing import PriceQuery, StaticPriceCatalog
from resolver import Inclusion, PseudoContext, Resolved, ResolvedResource

REGION = "us-east-1"


@pytest.fixture
def context() -> PseudoContext:
    return PseudoContext(
        region=REGION,
        account_id="111122223333",
        stack_name="payments-api-prod",
        stack_id=(
            "arn:aws:cloudformation:us-east-1:111122223333:"
            "stack/payments-api-prod/abc-123"
        ),
    )


def make_resource(
    logical_id: str = "Target",
    resource_type: str = "AWS::EC2::Instance",
    properties: dict | None = None,
    resolution: dict[str, Resolved] | None = None,
    inclusion: Inclusion = Inclusion.INCLUDED,
) -> ResolvedResource:
    """Build a ResolvedResource directly, for unit tests that don't need YAML."""
    return ResolvedResource(
        logical_id=logical_id,
        resource_type=resource_type,
        properties=properties or {},
        resolution=resolution or {},
        inclusion=inclusion,
    )


def build_catalog(region: str = REGION, version: str = "2026-08-09") -> StaticPriceCatalog:
    """A catalog with approximately realistic us-east-1 On-Demand rates.

    Values are close enough to real prices that expected totals in tests read
    plausibly, but they are fixtures, not a source of truth.
    """
    catalog = StaticPriceCatalog(version=version)

    def ec2_instance(
        instance_type: str, hourly: str, operating_system: str = "Linux"
    ) -> None:
        catalog.put(
            PriceQuery.of(
                "AmazonEC2",
                region,
                productFamily="Compute Instance",
                instanceType=instance_type,
                operatingSystem=operating_system,
                tenancy="Shared",
            ),
            hourly,
            "Hrs",
        )

    ec2_instance("t3.micro", "0.0104")
    ec2_instance("t3.large", "0.0832")
    ec2_instance("t3.xlarge", "0.1664")
    ec2_instance("m6g.large", "0.0770")
    ec2_instance("t3.nano", "0.0052")
    ec2_instance("t4g.small", "0.0134")

    # Windows and RHEL carry a licence premium, which is the whole reason the OS
    # cannot be assumed.
    ec2_instance("t3.large", "0.1880", operating_system="Windows")
    ec2_instance("t3.large", "0.1432", operating_system="RHEL")

    # EBS
    for volume_type, per_gb in (("gp2", "0.10"), ("gp3", "0.08"), ("io2", "0.125"), ("st1", "0.045")):
        catalog.put(
            PriceQuery.of("AmazonEC2", region, productFamily="Storage", volumeType=volume_type),
            per_gb,
            "GB-Mo",
        )

    catalog.put(
        PriceQuery.of(
            "AmazonEC2", region, productFamily="System Operation", volumeType="gp3"
        ),
        "0.005",
        "IOPS-Mo",
    )
    catalog.put(
        PriceQuery.of(
            "AmazonEC2", region, productFamily="System Operation", volumeType="io2"
        ),
        "0.065",
        "IOPS-Mo",
    )
    catalog.put(
        PriceQuery.of(
            "AmazonEC2", region, productFamily="Provisioned Throughput", volumeType="gp3"
        ),
        "0.040",
        "MBps-Mo",
    )

    # RDS
    def rds_instance(instance_class: str, engine: str, deployment: str, hourly: str) -> None:
        catalog.put(
            PriceQuery.of(
                "AmazonRDS",
                region,
                productFamily="Database Instance",
                instanceType=instance_class,
                databaseEngine=engine,
                deploymentOption=deployment,
            ),
            hourly,
            "Hrs",
        )

    rds_instance("db.t4g.medium", "PostgreSQL", "Single-AZ", "0.065")
    rds_instance("db.t4g.medium", "PostgreSQL", "Multi-AZ", "0.130")
    rds_instance("db.t4g.micro", "PostgreSQL", "Single-AZ", "0.016")
    rds_instance("db.r6g.xlarge", "PostgreSQL", "Multi-AZ", "0.960")
    rds_instance("db.r6g.xlarge", "PostgreSQL", "Single-AZ", "0.480")
    rds_instance("db.r6g.large", "PostgreSQL", "Single-AZ", "0.240")
    rds_instance("db.r6g.large", "Aurora PostgreSQL", "Single-AZ", "0.260")

    for deployment, per_gb in (("Single-AZ", "0.115"), ("Multi-AZ", "0.230")):
        catalog.put(
            PriceQuery.of(
                "AmazonRDS",
                region,
                productFamily="Database Storage",
                volumeType="gp2",
                deploymentOption=deployment,
            ),
            per_gb,
            "GB-Mo",
        )

    # NAT Gateway, load balancers, IPv4
    catalog.put(
        PriceQuery.of("AmazonEC2", region, productFamily="NAT Gateway", usageFamily="Hours"),
        "0.045",
        "Hrs",
    )
    catalog.put(
        PriceQuery.of("AWSELB", region, productFamily="Load Balancer-Application"),
        "0.0225",
        "Hrs",
    )
    catalog.put(
        PriceQuery.of("AWSELB", region, productFamily="Load Balancer-Network"),
        "0.0225",
        "Hrs",
    )
    catalog.put(
        PriceQuery.of("AmazonVPC", region, group="VPCPublicIPv4Address"),
        "0.005",
        "Hrs",
    )

    # ElastiCache
    catalog.put(
        PriceQuery.of(
            "AmazonElastiCache",
            region,
            productFamily="Cache Instance",
            instanceType="cache.t4g.micro",
            cacheEngine="Redis",
        ),
        "0.016",
        "Hrs",
    )

    # DynamoDB provisioned capacity
    catalog.put(
        PriceQuery.of(
            "AmazonDynamoDB", region, productFamily="Provisioned IOPS", group="DDB-ReadUnits"
        ),
        "0.00013",
        "RCU-Hrs",
    )
    catalog.put(
        PriceQuery.of(
            "AmazonDynamoDB", region, productFamily="Provisioned IOPS", group="DDB-WriteUnits"
        ),
        "0.00065",
        "WCU-Hrs",
    )

    return catalog


@pytest.fixture
def catalog() -> StaticPriceCatalog:
    return build_catalog()


# -- snapshot helpers -----------------------------------------------------

from decimal import Decimal

from pricing import Confidence, PricingClass
from state import SnapshotComponent, SnapshotResource, StackSnapshot

STACK_ID = "arn:aws:cloudformation:us-east-1:111122223333:stack/payments-api-prod/abc-123"


def snap_resource(
    logical_id: str,
    resource_type: str = "AWS::EC2::Instance",
    dimensions: dict | None = None,
    quantity: str = "730",
    monthly: str = "60.74",
    physical_id: str | None = None,
    pricing_class: PricingClass = PricingClass.DETERMINISTIC,
    confidence: Confidence = Confidence.HIGH,
    label: str = "instance hours",
    reason: str | None = None,
    excluded: list | None = None,
) -> SnapshotResource:
    """Build a snapshot resource whose fingerprint follows its dimensions."""
    dimensions = dimensions or {}
    components = []

    if pricing_class is PricingClass.DETERMINISTIC:
        key = "svc#us-east-1#" + ",".join(
            f"{k}={v}" for k, v in sorted(dimensions.items())
        )
        components.append(
            SnapshotComponent(
                label=label,
                query_key=key,
                quantity=Decimal(quantity),
                monthly_list=Decimal(monthly),
            )
        )

    return SnapshotResource(
        logical_id=logical_id,
        resource_type=resource_type,
        pricing_class=pricing_class,
        description=", ".join(f"{k}={v}" for k, v in sorted(dimensions.items())),
        confidence=confidence,
        physical_id=physical_id,
        monthly_list=Decimal(monthly) if pricing_class is PricingClass.DETERMINISTIC else Decimal(0),
        monthly_cost=Decimal(monthly) if pricing_class is PricingClass.DETERMINISTIC else Decimal(0),
        components=components,
        dimensions=dict(dimensions),
        excluded=excluded or [],
        reason=reason,
    )


def snapshot(*resources: SnapshotResource, stack_id: str = STACK_ID) -> StackSnapshot:
    return StackSnapshot(
        stack_id=stack_id,
        stack_name="payments-api-prod",
        account="111122223333",
        region=REGION,
        resources=list(resources),
    )
