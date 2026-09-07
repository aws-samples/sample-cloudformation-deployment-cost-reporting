"""End-to-end resolution against realistic templates.

These are the tests that matter most. Individual intrinsics are easy to get
right in isolation; real templates combine them in ways invented examples don't.
"""

from resolver import Inclusion, PseudoContext, TemplateResolver, load_template

STACK_ID = (
    "arn:aws:cloudformation:us-east-1:111122223333:stack/payments-api-prod/abc-123"
)

WEB_APP_TEMPLATE = """
AWSTemplateFormatVersion: '2010-09-09'
Description: Web tier with database

Parameters:
  Environment:
    Type: String
    AllowedValues: [dev, staging, production]
    Default: dev
  InstanceType:
    Type: String
    Default: t3.micro
  DataVolumeSize:
    Type: Number
    Default: 20
  DBAllocatedStorage:
    Type: Number
    Default: 20

Mappings:
  EnvConfig:
    dev:
      DBClass: db.t4g.micro
      NodeCount: 1
    production:
      DBClass: db.r6g.xlarge
      NodeCount: 3
  RegionAmis:
    us-east-1:
      HVM64: ami-0abc123
    eu-west-1:
      HVM64: ami-0def456

Conditions:
  IsProd: !Equals [!Ref Environment, production]
  IsNotProd: !Not [!Condition IsProd]
  NeedsReplica: !And
    - !Condition IsProd
    - !Not [!Equals [!Ref InstanceType, t3.micro]]

Resources:
  AppServer:
    Type: AWS::EC2::Instance
    Properties:
      InstanceType: !Ref InstanceType
      ImageId: !FindInMap [RegionAmis, !Ref 'AWS::Region', HVM64]
      SubnetId: !ImportValue shared-private-subnet
      BlockDeviceMappings:
        - DeviceName: /dev/xvda
          Ebs:
            VolumeSize: !Ref DataVolumeSize
            VolumeType: gp3
            Encrypted: true
      Tags:
        - Key: Name
          Value: !Sub '${AWS::StackName}-app'
        - Key: Environment
          Value: !Ref Environment

  DataVolume:
    Type: AWS::EC2::Volume
    Properties:
      Size: !Ref DataVolumeSize
      VolumeType: !If [IsProd, io2, gp3]
      Iops: !If [IsProd, 5000, !Ref 'AWS::NoValue']
      AvailabilityZone: !Select [0, !GetAZs '']

  Database:
    Type: AWS::RDS::DBInstance
    Properties:
      DBInstanceClass: !FindInMap [EnvConfig, !Ref Environment, DBClass]
      Engine: postgres
      AllocatedStorage: !Ref DBAllocatedStorage
      MultiAZ: !If [IsProd, true, !Ref 'AWS::NoValue']
      StorageEncrypted: true

  ReadReplica:
    Type: AWS::RDS::DBInstance
    Condition: NeedsReplica
    Properties:
      DBInstanceClass: !FindInMap [EnvConfig, !Ref Environment, DBClass]
      SourceDBInstanceIdentifier: !Ref Database

  NatGateway:
    Type: AWS::EC2::NatGateway
    Properties:
      SubnetId: !ImportValue shared-public-subnet
      AllocationId: !GetAtt NatEip.AllocationId

  NatEip:
    Type: AWS::EC2::EIP
    Properties:
      Domain: vpc

  UploadBucket:
    Type: AWS::S3::Bucket
    DeletionPolicy: Retain
    Properties:
      BucketName: !Sub '${AWS::StackName}-uploads-${AWS::AccountId}'
      VersioningConfiguration:
        Status: !If [IsProd, Enabled, Suspended]

  DevOnlyBastion:
    Type: AWS::EC2::Instance
    Condition: IsNotProd
    Properties:
      InstanceType: t3.nano
      ImageId: !FindInMap [RegionAmis, !Ref 'AWS::Region', HVM64]
"""


def resolve_all(parameters):
    template = load_template(WEB_APP_TEMPLATE)
    context = PseudoContext.from_stack_id(STACK_ID)
    resolver = TemplateResolver(template, parameters, context)
    return {r.logical_id: r for r in resolver.resolve_resources()}


# -- dev environment ------------------------------------------------------


def test_dev_ec2_pricing_properties_resolve():
    resources = resolve_all({"Environment": "dev", "InstanceType": "t3.small"})
    server = resources["AppServer"]

    assert server.properties["InstanceType"] == "t3.small"
    assert server.properties["ImageId"] == "ami-0abc123"

    ebs = server.properties["BlockDeviceMappings"][0]["Ebs"]
    assert ebs["VolumeSize"] == 20
    assert ebs["VolumeType"] == "gp3"


def test_sub_inside_a_tag_list_resolves():
    resources = resolve_all({"Environment": "dev"})
    tags = resources["AppServer"].properties["Tags"]
    assert {"Key": "Name", "Value": "payments-api-prod-app"} in tags
    assert {"Key": "Environment", "Value": "dev"} in tags


def test_import_value_is_the_only_unresolved_path_on_the_server():
    """Everything that affects EC2 cost resolves; only the subnet doesn't."""
    resources = resolve_all({"Environment": "dev"})
    assert resources["AppServer"].unresolved_paths == ["SubnetId"]


def test_dev_volume_takes_gp3_and_drops_iops():
    resources = resolve_all({"Environment": "dev"})
    volume = resources["DataVolume"].properties

    assert volume["VolumeType"] == "gp3"
    # Iops resolved to AWS::NoValue, so the key must be gone entirely.
    assert "Iops" not in volume


def test_dev_database_class_comes_from_the_mapping():
    resources = resolve_all({"Environment": "dev"})
    database = resources["Database"].properties

    assert database["DBInstanceClass"] == "db.t4g.micro"
    assert database["AllocatedStorage"] == 20
    assert "MultiAZ" not in database


def test_replica_is_excluded_in_dev():
    resources = resolve_all({"Environment": "dev", "InstanceType": "t3.large"})
    assert resources["ReadReplica"].inclusion is Inclusion.EXCLUDED
    assert not resources["ReadReplica"].is_priceable


def test_bastion_is_included_in_dev_only():
    resources = resolve_all({"Environment": "dev"})
    bastion = resources["DevOnlyBastion"]
    assert bastion.inclusion is Inclusion.INCLUDED
    assert bastion.properties["InstanceType"] == "t3.nano"


# -- production environment -----------------------------------------------


def test_production_database_upgrades_and_gains_multi_az():
    resources = resolve_all({"Environment": "production"})
    database = resources["Database"].properties

    assert database["DBInstanceClass"] == "db.r6g.xlarge"
    assert database["MultiAZ"] is True


def test_production_volume_switches_to_io2_with_iops():
    resources = resolve_all({"Environment": "production"})
    volume = resources["DataVolume"].properties

    assert volume["VolumeType"] == "io2"
    assert volume["Iops"] == 5000


def test_replica_included_when_compound_condition_is_true():
    """NeedsReplica is And[IsProd, Not[InstanceType == t3.micro]]."""
    resources = resolve_all({"Environment": "production", "InstanceType": "t3.large"})
    replica = resources["ReadReplica"]

    assert replica.inclusion is Inclusion.INCLUDED
    assert replica.properties["DBInstanceClass"] == "db.r6g.xlarge"


def test_replica_excluded_in_production_on_the_default_instance_type():
    resources = resolve_all({"Environment": "production", "InstanceType": "t3.micro"})
    assert resources["ReadReplica"].inclusion is Inclusion.EXCLUDED


def test_bastion_is_excluded_in_production():
    resources = resolve_all({"Environment": "production"})
    assert resources["DevOnlyBastion"].inclusion is Inclusion.EXCLUDED


def test_replica_source_ref_to_resource_is_reported_not_guessed():
    resources = resolve_all({"Environment": "production", "InstanceType": "t3.large"})
    replica = resources["ReadReplica"]

    assert "SourceDBInstanceIdentifier" in replica.unresolved_paths
    record = replica.resolution["SourceDBInstanceIdentifier"]
    assert record.reason.value == "RESOURCE_REFERENCE"


# -- resources whose cost is knowable despite unresolved properties -------


def test_nat_gateway_cost_drivers_do_not_depend_on_unresolved_properties():
    """NAT hourly cost needs nothing from the template beyond existence.

    Both its properties are unresolvable, yet it is still priceable. This is why
    coverage is judged per pricing dimension, not per property.
    """
    resources = resolve_all({"Environment": "dev"})
    nat = resources["NatGateway"]

    assert sorted(nat.unresolved_paths) == ["AllocationId", "SubnetId"]
    assert nat.is_priceable


def test_eip_resolves_completely():
    resources = resolve_all({"Environment": "dev"})
    assert resources["NatEip"].fully_resolved


def test_bucket_name_sub_uses_account_and_stack_name():
    resources = resolve_all({"Environment": "dev"})
    bucket = resources["UploadBucket"].properties
    assert bucket["BucketName"] == "payments-api-prod-uploads-111122223333"
    assert bucket["VersioningConfiguration"]["Status"] == "Suspended"


def test_deletion_policy_survives_resolution():
    resources = resolve_all({"Environment": "dev"})
    assert resources["UploadBucket"].deletion_policy == "Retain"


def test_get_azs_inside_select_is_reported_as_account_specific():
    resources = resolve_all({"Environment": "dev"})
    record = resources["DataVolume"].resolution["AvailabilityZone"]
    assert not record.is_resolved
    # Select failed because GetAZs beneath it could not resolve.
    assert record.reason.value == "NESTED_UNRESOLVED"


# -- SAM-shaped template --------------------------------------------------

SAM_PROCESSED_TEMPLATE = """
AWSTemplateFormatVersion: '2010-09-09'
Parameters:
  MemorySize:
    Type: Number
    Default: 512
  TableRcu:
    Type: Number
    Default: 5

Resources:
  ApiHandler:
    Type: AWS::Lambda::Function
    Properties:
      FunctionName: !Sub '${AWS::StackName}-handler'
      MemorySize: !Ref MemorySize
      Timeout: 30
      Runtime: python3.12
      Role: !GetAtt ApiHandlerRole.Arn
      Architectures:
        - arm64

  ApiHandlerRole:
    Type: AWS::IAM::Role
    Properties:
      AssumeRolePolicyDocument:
        Version: '2012-10-17'
        Statement:
          - Effect: Allow
            Principal:
              Service: lambda.amazonaws.com
            Action: sts:AssumeRole

  SessionTable:
    Type: AWS::DynamoDB::Table
    Properties:
      BillingMode: PROVISIONED
      ProvisionedThroughput:
        ReadCapacityUnits: !Ref TableRcu
        WriteCapacityUnits: !Ref TableRcu
      AttributeDefinitions:
        - AttributeName: pk
          AttributeType: S
      KeySchema:
        - AttributeName: pk
          KeyType: HASH
"""


def test_sam_processed_lambda_resolves_memory_and_architecture():
    template = load_template(SAM_PROCESSED_TEMPLATE)
    resolver = TemplateResolver(template, {}, PseudoContext.from_stack_id(STACK_ID))
    resources = {r.logical_id: r for r in resolver.resolve_resources()}

    handler = resources["ApiHandler"].properties
    assert handler["MemorySize"] == 512
    assert handler["Architectures"] == ["arm64"]
    assert handler["FunctionName"] == "payments-api-prod-handler"
    # Role is a GetAtt, unresolvable and irrelevant to cost.
    assert "Role" not in handler
    assert resources["ApiHandler"].unresolved_paths == ["Role"]


def test_provisioned_dynamodb_capacity_resolves():
    template = load_template(SAM_PROCESSED_TEMPLATE)
    resolver = TemplateResolver(
        template, {"TableRcu": "25"}, PseudoContext.from_stack_id(STACK_ID)
    )
    resources = {r.logical_id: r for r in resolver.resolve_resources()}

    throughput = resources["SessionTable"].properties["ProvisionedThroughput"]
    assert throughput == {"ReadCapacityUnits": 25, "WriteCapacityUnits": 25}


def test_iam_policy_document_passes_through_untouched():
    """Nested literal structures must survive the walk intact."""
    template = load_template(SAM_PROCESSED_TEMPLATE)
    resolver = TemplateResolver(template, {}, PseudoContext.from_stack_id(STACK_ID))
    resources = {r.logical_id: r for r in resolver.resolve_resources()}

    document = resources["ApiHandlerRole"].properties["AssumeRolePolicyDocument"]
    assert document["Version"] == "2012-10-17"
    assert document["Statement"][0]["Principal"]["Service"] == "lambda.amazonaws.com"


def test_every_resource_is_returned_even_when_excluded():
    """Coverage reporting needs the full set, not just the priceable ones."""
    resources = resolve_all({"Environment": "production"})
    assert set(resources) == {
        "AppServer",
        "DataVolume",
        "Database",
        "ReadReplica",
        "NatGateway",
        "NatEip",
        "UploadBucket",
        "DevOnlyBastion",
    }
