"""Resolver and pricing together, against a realistic template.

Steps 1 and 2 joined up. This is what the analyzer will do once the state store
and diff engine exist.
"""

from decimal import Decimal

from conftest import REGION, build_catalog
from pricing import Confidence, PricingClass, PricingEngine, money
from resolver import PseudoContext, TemplateResolver, load_template

STACK_ID = (
    "arn:aws:cloudformation:us-east-1:111122223333:stack/payments-api-prod/abc-123"
)

TEMPLATE = """
Parameters:
  Environment:
    Type: String
    Default: dev
  InstanceType:
    Type: String
    Default: t3.large
  DataVolumeSize:
    Type: Number
    Default: 100

Mappings:
  EnvConfig:
    dev:
      DBClass: db.t4g.medium
    production:
      DBClass: db.r6g.xlarge

Conditions:
  IsProd: !Equals [!Ref Environment, production]

Resources:
  Vpc:
    Type: AWS::EC2::VPC
    Properties:
      CidrBlock: 10.0.0.0/16

  AppSubnet:
    Type: AWS::EC2::Subnet
    Properties:
      VpcId: !Ref Vpc
      CidrBlock: 10.0.1.0/24

  AppSecurityGroup:
    Type: AWS::EC2::SecurityGroup
    Properties:
      GroupDescription: App tier
      VpcId: !Ref Vpc

  AppRole:
    Type: AWS::IAM::Role
    Properties:
      AssumeRolePolicyDocument:
        Version: '2012-10-17'
        Statement:
          - Effect: Allow
            Principal: {Service: ec2.amazonaws.com}
            Action: sts:AssumeRole

  AppServer:
    Type: AWS::EC2::Instance
    Properties:
      InstanceType: !Ref InstanceType
      SubnetId: !Ref AppSubnet
      BlockDeviceMappings:
        - DeviceName: /dev/xvda
          Ebs:
            VolumeSize: !Ref DataVolumeSize
            VolumeType: gp3

  Database:
    Type: AWS::RDS::DBInstance
    Properties:
      DBInstanceClass: !FindInMap [EnvConfig, !Ref Environment, DBClass]
      Engine: postgres
      AllocatedStorage: 20
      MultiAZ: !If [IsProd, true, !Ref 'AWS::NoValue']

  ProdReplica:
    Type: AWS::RDS::DBInstance
    Condition: IsProd
    Properties:
      DBInstanceClass: db.r6g.large
      Engine: postgres
      AllocatedStorage: 20

  NatGateway:
    Type: AWS::EC2::NatGateway
    Properties:
      SubnetId: !Ref AppSubnet
      AllocationId: !GetAtt NatEip.AllocationId

  NatEip:
    Type: AWS::EC2::EIP
    Properties:
      Domain: vpc

  LoadBalancer:
    Type: AWS::ElasticLoadBalancingV2::LoadBalancer
    Properties:
      Type: application
      Subnets:
        - !Ref AppSubnet

  UploadBucket:
    Type: AWS::S3::Bucket
    Properties:
      BucketName: !Sub '${AWS::StackName}-uploads'

  ApiHandler:
    Type: AWS::Lambda::Function
    Properties:
      MemorySize: 512
      Runtime: python3.12
      Role: !GetAtt AppRole.Arn
"""


def price_stack(parameters, discount=0):
    resolver = TemplateResolver(
        load_template(TEMPLATE), parameters, PseudoContext.from_stack_id(STACK_ID)
    )
    engine = PricingEngine(build_catalog(), REGION, discount_percent=discount)
    inventory = engine.price_inventory(resolver.resolve_resources())
    return inventory, {r.logical_id: r for r in inventory.resources}


# -- dev ------------------------------------------------------------------


def test_dev_stack_total():
    """Every deterministic component, summed.

    EC2 t3.large   0.0832 * 730 = 60.736
    EBS gp3 100GB  0.08   * 100 =  8.000
    RDS instance   0.065  * 730 = 47.450
    RDS storage    0.115  *  20 =  2.300
    NAT Gateway    0.045  * 730 = 32.850
    Public IPv4    0.005  * 730 =  3.650
    ALB            0.0225 * 730 = 16.425
                                 -------
                                 171.411
    """
    inventory, _ = price_stack({"Environment": "dev"})
    assert money(inventory.monthly_list) == Decimal("171.41")


def test_dev_resolves_the_instance_type_from_a_parameter():
    _, resources = price_stack({"Environment": "dev", "InstanceType": "t3.xlarge"})
    assert "t3.xlarge" in resources["AppServer"].description


def test_dev_database_class_comes_from_the_mapping():
    _, resources = price_stack({"Environment": "dev"})
    assert "db.t4g.medium" in resources["Database"].description
    assert "Single-AZ" in resources["Database"].description


def test_replica_is_absent_from_pricing_in_dev():
    """Excluded by condition, so it never reaches the inventory at all."""
    _, resources = price_stack({"Environment": "dev"})
    assert "ProdReplica" not in resources


def test_scaffolding_is_free_and_does_not_dent_coverage():
    inventory, resources = price_stack({"Environment": "dev"})

    for logical_id in ("Vpc", "AppSubnet", "AppSecurityGroup", "AppRole"):
        assert resources[logical_id].pricing_class is PricingClass.FREE

    coverage = inventory.coverage
    assert coverage.total == 11
    assert coverage.free == 4
    assert coverage.priced == 5
    assert coverage.usage_based == 2
    # 5 of 7 chargeable. Counting the 4 free resources would report 45.5% on a
    # stack where everything chargeable that can be priced, is.
    assert coverage.priced_percent == 71.4


def test_usage_based_resources_are_listed_not_estimated():
    inventory, resources = price_stack({"Environment": "dev"})

    assert resources["UploadBucket"].pricing_class is PricingClass.USAGE_BASED
    assert resources["ApiHandler"].pricing_class is PricingClass.USAGE_BASED
    assert resources["UploadBucket"].monthly_list == Decimal(0)
    assert len(inventory.by_class(PricingClass.USAGE_BASED)) == 2


def test_split_resources_report_medium_confidence():
    _, resources = price_stack({"Environment": "dev"})

    assert resources["NatGateway"].confidence is Confidence.MEDIUM
    assert resources["LoadBalancer"].confidence is Confidence.MEDIUM
    assert resources["AppServer"].confidence is Confidence.MEDIUM


def test_nat_gateway_prices_even_though_both_properties_are_unresolvable():
    """SubnetId is a Ref to a resource, AllocationId is a GetAtt.

    Neither resolves, and neither matters: the hourly charge depends only on the
    NAT Gateway existing.
    """
    _, resources = price_stack({"Environment": "dev"})
    nat = resources["NatGateway"]

    assert nat.is_priced
    assert money(nat.monthly_list) == Decimal("32.85")


def test_instance_and_its_inline_volume_are_both_counted():
    _, resources = price_stack({"Environment": "dev"})
    server = resources["AppServer"]

    assert len(server.components) == 2
    assert money(server.monthly_list) == Decimal("68.74")


# -- production -----------------------------------------------------------


def test_production_costs_more_and_includes_the_replica():
    dev, _ = price_stack({"Environment": "dev"})
    prod, resources = price_stack({"Environment": "production"})

    assert prod.monthly_list > dev.monthly_list
    assert "ProdReplica" in resources
    assert resources["ProdReplica"].is_priced


def test_production_database_is_multi_az():
    _, resources = price_stack({"Environment": "production"})
    database = resources["Database"]

    assert "Multi-AZ" in database.description
    # 0.96 * 730 instance + 0.230 * 20 storage
    assert money(database.monthly_list) == Decimal("705.40")


def test_production_coverage_gains_one_priced_resource():
    dev, _ = price_stack({"Environment": "dev"})
    prod, _ = price_stack({"Environment": "production"})

    assert prod.coverage.priced == dev.coverage.priced + 1


# -- discount -------------------------------------------------------------


def test_discount_applies_across_the_whole_stack():
    inventory, _ = price_stack({"Environment": "dev"}, discount=15)

    assert money(inventory.monthly_list) == Decimal("171.41")
    # 171.411 * 0.85 = 145.699
    assert money(inventory.monthly_cost) == Decimal("145.70")


def test_list_price_is_preserved_alongside_the_discounted_figure():
    _, resources = price_stack({"Environment": "dev"}, discount=15)
    server = resources["AppServer"]

    assert server.monthly_list > server.monthly_cost
    payload = server.to_dict()
    assert payload["monthlyCostList"] > payload["monthlyCost"]


# -- report shape ---------------------------------------------------------


def test_inventory_serialises_with_everything_a_report_needs():
    inventory, _ = price_stack({"Environment": "dev"})
    payload = inventory.to_dict()

    assert payload["monthlyCostList"] == 171.41
    assert payload["coverage"]["pricedPercent"] == 71.4
    assert payload["pricingBasis"]["hoursPerMonth"] == 730
    assert len(payload["resources"]) == 11


def test_every_resource_appears_in_the_output_including_free_ones():
    """Nothing silently dropped (S19).

    ProdReplica is the one absentee, and only because its condition was false so
    it was never created.
    """
    inventory, _ = price_stack({"Environment": "dev"})
    logical_ids = {r.logical_id for r in inventory.resources}

    assert logical_ids == {
        "Vpc",
        "AppSubnet",
        "AppSecurityGroup",
        "AppRole",
        "AppServer",
        "Database",
        "NatGateway",
        "NatEip",
        "LoadBalancer",
        "UploadBucket",
        "ApiHandler",
    }


def test_price_list_version_is_stamped_on_priced_resources():
    _, resources = price_stack({"Environment": "dev"})
    assert resources["AppServer"].price_list_version == "2026-08-09"
