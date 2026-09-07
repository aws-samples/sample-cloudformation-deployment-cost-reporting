"""Intrinsic function resolution, one behaviour at a time."""

import base64

import pytest

from resolver import (
    NO_VALUE,
    Inclusion,
    PseudoContext,
    ResolvedVia,
    TemplateResolver,
    UnresolvedReason,
)

CONTEXT = PseudoContext(
    region="us-east-1",
    account_id="111122223333",
    stack_name="payments-api-prod",
    stack_id="arn:aws:cloudformation:us-east-1:111122223333:stack/payments-api-prod/abc",
)


def resolve(node, parameters=None, template=None):
    """Resolve one expression in isolation."""
    base = {"Parameters": {"InstanceType": {"Type": "String"}}}
    base.update(template or {})
    resolver = TemplateResolver(base, parameters or {}, CONTEXT)
    return resolver.resolve_value(node)


def resolve_properties(properties, parameters=None, template=None):
    """Resolve a resource's Properties block and return the resource."""
    full = {
        "Parameters": {},
        "Resources": {
            "Target": {"Type": "AWS::EC2::Instance", "Properties": properties}
        },
    }
    full.update(template or {})
    full.setdefault("Resources", {})["Target"] = {
        "Type": "AWS::EC2::Instance",
        "Properties": properties,
    }
    resolver = TemplateResolver(full, parameters or {}, CONTEXT)
    return resolver.resolve_resource("Target", full["Resources"]["Target"])


# -- Ref ------------------------------------------------------------------


def test_ref_to_parameter():
    result = resolve({"Ref": "InstanceType"}, {"InstanceType": "t3.large"})
    assert result.value == "t3.large"
    assert result.via is ResolvedVia.PARAMETER
    assert result.ref == "InstanceType"


@pytest.mark.parametrize(
    "name,expected",
    [
        ("AWS::Region", "us-east-1"),
        ("AWS::AccountId", "111122223333"),
        ("AWS::StackName", "payments-api-prod"),
        ("AWS::Partition", "aws"),
        ("AWS::URLSuffix", "amazonaws.com"),
    ],
)
def test_ref_to_pseudo_parameters(name, expected):
    result = resolve({"Ref": name})
    assert result.value == expected
    assert result.via is ResolvedVia.PSEUDO


def test_ref_no_value_resolves_to_sentinel():
    result = resolve({"Ref": "AWS::NoValue"})
    assert result.is_resolved
    assert result.value is NO_VALUE


def test_ref_to_resource_is_distinguished_from_missing_parameter():
    template = {"Resources": {"Bucket": {"Type": "AWS::S3::Bucket"}}}
    result = resolve({"Ref": "Bucket"}, template=template)
    assert not result.is_resolved
    assert result.reason is UnresolvedReason.RESOURCE_REFERENCE


def test_ref_to_notification_arns_is_account_specific():
    result = resolve({"Ref": "AWS::NotificationARNs"})
    assert result.reason is UnresolvedReason.ACCOUNT_SPECIFIC


def test_ref_with_non_string_argument_is_malformed():
    assert resolve({"Ref": ["a"]}).reason is UnresolvedReason.MALFORMED


# -- structurally unresolvable -------------------------------------------


def test_getatt_is_runtime_attribute():
    result = resolve({"Fn::GetAtt": ["Server", "PrivateIp"]})
    assert result.reason is UnresolvedReason.RUNTIME_ATTRIBUTE


def test_import_value_is_cross_stack():
    result = resolve({"Fn::ImportValue": "shared-vpc-id"})
    assert result.reason is UnresolvedReason.CROSS_STACK_IMPORT


def test_get_azs_is_account_specific():
    result = resolve({"Fn::GetAZs": "us-east-1"})
    assert result.reason is UnresolvedReason.ACCOUNT_SPECIFIC


def test_unsupported_function_is_named():
    result = resolve({"Fn::Cidr": ["10.0.0.0/16", 6, 8]})
    assert result.reason is UnresolvedReason.UNSUPPORTED_FUNCTION
    assert "Fn::Cidr" in result.detail


# -- Fn::FindInMap --------------------------------------------------------

MAPPING_TEMPLATE = {
    "Mappings": {
        "RegionAmis": {
            "us-east-1": {"ami": "ami-east", "size": "t3.large"},
            "eu-west-1": {"ami": "ami-west"},
        }
    }
}


def test_find_in_map_with_region_lookup():
    result = resolve(
        {"Fn::FindInMap": ["RegionAmis", {"Ref": "AWS::Region"}, "ami"]},
        template=MAPPING_TEMPLATE,
    )
    assert result.value == "ami-east"
    assert result.via is ResolvedVia.MAPPING


def test_find_in_map_missing_key_is_reported_with_the_missing_level():
    result = resolve(
        {"Fn::FindInMap": ["RegionAmis", "eu-west-1", "size"]},
        template=MAPPING_TEMPLATE,
    )
    assert result.reason is UnresolvedReason.MISSING_MAPPING
    assert "second-level key" in result.detail


def test_find_in_map_missing_map_is_reported():
    result = resolve(
        {"Fn::FindInMap": ["Nonexistent", "a", "b"]}, template=MAPPING_TEMPLATE
    )
    assert result.reason is UnresolvedReason.MISSING_MAPPING
    assert "no map" in result.detail


def test_find_in_map_default_value_is_honoured():
    result = resolve(
        {
            "Fn::FindInMap": [
                "RegionAmis",
                "eu-west-1",
                "size",
                {"DefaultValue": "t3.micro"},
            ]
        },
        template=MAPPING_TEMPLATE,
    )
    assert result.value == "t3.micro"


def test_find_in_map_with_unresolvable_key():
    result = resolve(
        {"Fn::FindInMap": ["RegionAmis", {"Fn::ImportValue": "x"}, "ami"]},
        template=MAPPING_TEMPLATE,
    )
    assert result.reason is UnresolvedReason.NESTED_UNRESOLVED


# -- Fn::If ---------------------------------------------------------------

CONDITION_TEMPLATE = {
    "Parameters": {"Environment": {"Type": "String"}},
    "Conditions": {
        "IsProd": {"Fn::Equals": [{"Ref": "Environment"}, "production"]}
    },
}


def test_if_takes_true_branch():
    result = resolve(
        {"Fn::If": ["IsProd", "db.r6g.xlarge", "db.t4g.medium"]},
        {"Environment": "production"},
        CONDITION_TEMPLATE,
    )
    assert result.value == "db.r6g.xlarge"
    assert result.via is ResolvedVia.CONDITION


def test_if_takes_false_branch():
    result = resolve(
        {"Fn::If": ["IsProd", "db.r6g.xlarge", "db.t4g.medium"]},
        {"Environment": "dev"},
        CONDITION_TEMPLATE,
    )
    assert result.value == "db.t4g.medium"


def test_if_with_unknown_condition_is_reported():
    result = resolve({"Fn::If": ["Nope", "a", "b"]}, {}, CONDITION_TEMPLATE)
    assert result.reason is UnresolvedReason.NESTED_UNRESOLVED


def test_if_resolving_to_no_value_drops_the_property():
    """The canonical Fn::If + AWS::NoValue pattern must remove the key entirely.

    An absent property falls back to the AWS service default, which is a
    different cost from an explicit null.
    """
    resource = resolve_properties(
        {
            "DBInstanceClass": "db.t4g.medium",
            "MultiAZ": {"Fn::If": ["IsProd", True, {"Ref": "AWS::NoValue"}]},
        },
        {"Environment": "dev"},
        CONDITION_TEMPLATE,
    )
    assert resource.properties == {"DBInstanceClass": "db.t4g.medium"}
    assert "MultiAZ" not in resource.properties


def test_if_keeps_the_property_when_the_branch_has_a_value():
    resource = resolve_properties(
        {"MultiAZ": {"Fn::If": ["IsProd", True, {"Ref": "AWS::NoValue"}]}},
        {"Environment": "production"},
        CONDITION_TEMPLATE,
    )
    assert resource.properties["MultiAZ"] is True


# -- Fn::Sub --------------------------------------------------------------


def test_sub_with_pseudo_and_parameter():
    result = resolve(
        {"Fn::Sub": "${AWS::StackName}-${InstanceType}-node"},
        {"InstanceType": "t3.large"},
    )
    assert result.value == "payments-api-prod-t3.large-node"


def test_sub_with_local_variable_map():
    result = resolve(
        {"Fn::Sub": ["${Prefix}/${Suffix}", {"Prefix": "logs", "Suffix": "app"}]}
    )
    assert result.value == "logs/app"


def test_sub_local_variables_take_precedence_over_parameters():
    result = resolve(
        {"Fn::Sub": ["${InstanceType}", {"InstanceType": "overridden"}]},
        {"InstanceType": "t3.large"},
    )
    assert result.value == "overridden"


def test_sub_escape_produces_a_literal():
    result = resolve({"Fn::Sub": "${!NotAVariable}-${AWS::Region}"})
    assert result.value == "${NotAVariable}-us-east-1"


def test_sub_with_attribute_reference_is_unresolved():
    result = resolve({"Fn::Sub": "http://${Server.PublicDnsName}/health"})
    assert not result.is_resolved
    assert result.reason is UnresolvedReason.NESTED_UNRESOLVED
    assert "runtime attribute" in result.detail


def test_sub_names_every_token_it_could_not_resolve():
    result = resolve({"Fn::Sub": "${Missing}-${AlsoMissing}"})
    assert "Missing" in result.detail
    assert "AlsoMissing" in result.detail


def test_sub_with_unresolvable_local_variable():
    result = resolve({"Fn::Sub": ["${A}", {"A": {"Fn::ImportValue": "x"}}]})
    assert result.reason is UnresolvedReason.NESTED_UNRESOLVED


def test_sub_malformed_is_reported():
    assert resolve({"Fn::Sub": 42}).reason is UnresolvedReason.MALFORMED


# -- list functions -------------------------------------------------------


def test_select_by_index():
    result = resolve({"Fn::Select": [1, ["a", "b", "c"]]})
    assert result.value == "b"


def test_select_accepts_a_string_index():
    result = resolve({"Fn::Select": ["2", ["a", "b", "c"]]})
    assert result.value == "c"


def test_select_supports_negative_index():
    result = resolve({"Fn::Select": [-1, ["a", "b", "c"]]})
    assert result.value == "c"


def test_select_out_of_range_is_reported():
    result = resolve({"Fn::Select": [9, ["a"]]})
    assert result.reason is UnresolvedReason.MALFORMED
    assert "out of range" in result.detail


def test_select_over_split_composes():
    result = resolve(
        {"Fn::Select": [1, {"Fn::Split": [",", {"Ref": "InstanceType"}]}]},
        {"InstanceType": "a,b,c"},
    )
    assert result.value == "b"


def test_select_from_unresolvable_list():
    result = resolve({"Fn::Select": [0, {"Fn::GetAZs": ""}]})
    assert result.reason is UnresolvedReason.NESTED_UNRESOLVED


def test_join_stringifies_mixed_items():
    result = resolve({"Fn::Join": ["-", ["a", 1, True]]})
    assert result.value == "a-1-true"


def test_join_with_nested_ref():
    result = resolve(
        {"Fn::Join": [":", [{"Ref": "AWS::Region"}, {"Ref": "InstanceType"}]]},
        {"InstanceType": "t3.large"},
    )
    assert result.value == "us-east-1:t3.large"


def test_join_with_unresolvable_member_fails_the_whole_join():
    result = resolve({"Fn::Join": ["-", ["a", {"Fn::ImportValue": "x"}]]})
    assert result.reason is UnresolvedReason.NESTED_UNRESOLVED


def test_select_does_not_shift_index_when_a_member_is_unresolvable():
    """The dangerous variant: dropping a member would return the wrong element.

    With ["a", <unresolved>, "c"], silently dropping the middle item makes
    index 1 return "c". That is a confident wrong answer rather than a reported
    gap, so the whole expression must fail instead.
    """
    result = resolve({"Fn::Select": [1, ["a", {"Fn::ImportValue": "x"}, "c"]]})
    assert not result.is_resolved
    assert result.reason is UnresolvedReason.NESTED_UNRESOLVED
    assert result.value != "c"


def test_unresolvable_member_nested_in_an_argument_mapping_fails_the_argument():
    result = resolve(
        {"Fn::Join": ["-", [{"Fn::Sub": "${Missing}"}, "tail"]]}
    )
    assert not result.is_resolved
    assert result.reason is UnresolvedReason.NESTED_UNRESOLVED


def test_argument_resolution_is_strict_but_property_resolution_stays_lenient():
    """Two modes, deliberately different.

    Within one expression a partial result is wrong. Across independent
    properties it is useful, so a single bad property must not discard its
    siblings.
    """
    resource = resolve_properties(
        {
            "InstanceType": "t3.large",
            "Broken": {"Fn::Join": ["-", ["a", {"Fn::ImportValue": "x"}]]},
            "Fine": {"Fn::Join": ["-", ["a", "b"]]},
        }
    )
    assert resource.properties["InstanceType"] == "t3.large"
    assert resource.properties["Fine"] == "a-b"
    assert "Broken" not in resource.properties
    assert resource.unresolved_paths == ["Broken"]


def test_split_produces_a_list():
    result = resolve({"Fn::Split": [",", "a,b,c"]})
    assert result.value == ["a", "b", "c"]


def test_base64_encodes_resolved_content():
    result = resolve({"Fn::Base64": {"Fn::Sub": "region=${AWS::Region}"}})
    assert base64.b64decode(result.value).decode() == "region=us-east-1"


# -- resource level -------------------------------------------------------


def test_resource_with_false_condition_is_excluded_from_pricing():
    """A conditional resource that is false was never created."""
    template = {
        "Parameters": {"Environment": {"Type": "String"}},
        "Conditions": {
            "IsProd": {"Fn::Equals": [{"Ref": "Environment"}, "production"]}
        },
        "Resources": {
            "Replica": {
                "Type": "AWS::RDS::DBInstance",
                "Condition": "IsProd",
                "Properties": {"DBInstanceClass": "db.r6g.large"},
            }
        },
    }
    resolver = TemplateResolver(template, {"Environment": "dev"}, CONTEXT)
    resource = resolver.resolve_resources()[0]

    assert resource.inclusion is Inclusion.EXCLUDED
    assert not resource.is_priceable


def test_resource_with_true_condition_is_included():
    template = {
        "Parameters": {"Environment": {"Type": "String"}},
        "Conditions": {
            "IsProd": {"Fn::Equals": [{"Ref": "Environment"}, "production"]}
        },
        "Resources": {
            "Replica": {
                "Type": "AWS::RDS::DBInstance",
                "Condition": "IsProd",
                "Properties": {"DBInstanceClass": "db.r6g.large"},
            }
        },
    }
    resolver = TemplateResolver(template, {"Environment": "production"}, CONTEXT)
    resource = resolver.resolve_resources()[0]

    assert resource.inclusion is Inclusion.INCLUDED
    assert resource.is_priceable


def test_unevaluable_condition_includes_the_resource_and_flags_it():
    """Under-reporting is the more dangerous error, so include and flag."""
    template = {
        "Conditions": {
            "Unknowable": {"Fn::Equals": [{"Fn::ImportValue": "x"}, "y"]}
        },
        "Resources": {
            "Server": {
                "Type": "AWS::EC2::Instance",
                "Condition": "Unknowable",
                "Properties": {"InstanceType": "t3.large"},
            }
        },
    }
    resolver = TemplateResolver(template, {}, CONTEXT)
    resource = resolver.resolve_resources()[0]

    assert resource.inclusion is Inclusion.UNKNOWN
    assert resource.is_priceable
    assert "$condition" in resource.unresolved_paths


def test_unresolved_property_is_omitted_but_reported():
    """Pricing sees the property missing; coverage sees why."""
    resource = resolve_properties(
        {
            "InstanceType": "t3.large",
            "SubnetId": {"Fn::ImportValue": "shared-subnet"},
        }
    )
    assert resource.properties["InstanceType"] == "t3.large"
    assert "SubnetId" not in resource.properties
    assert resource.unresolved_paths == ["SubnetId"]
    assert not resource.fully_resolved

    reason = resource.resolution["SubnetId"].reason
    assert reason is UnresolvedReason.CROSS_STACK_IMPORT


def test_resolution_records_provenance_for_debugging():
    resource = resolve_properties(
        {"InstanceType": {"Ref": "InstanceType"}},
        {"InstanceType": "t3.xlarge"},
        {"Parameters": {"InstanceType": {"Type": "String"}}},
    )
    record = resource.resolution["InstanceType"]
    assert record.value == "t3.xlarge"
    assert record.via is ResolvedVia.PARAMETER
    assert record.raw == {"Ref": "InstanceType"}

    as_dict = record.to_dict()
    assert as_dict["status"] == "RESOLVED"
    assert as_dict["via"] == "PARAMETER"
    assert as_dict["ref"] == "InstanceType"


def test_nested_paths_are_recorded_with_indices():
    resource = resolve_properties(
        {
            "BlockDeviceMappings": [
                {
                    "DeviceName": "/dev/sda1",
                    "Ebs": {"VolumeSize": {"Fn::ImportValue": "size"}},
                }
            ]
        }
    )
    assert "BlockDeviceMappings[0].Ebs.VolumeSize" in resource.unresolved_paths


def test_deeply_nested_resolution_succeeds():
    resource = resolve_properties(
        {
            "BlockDeviceMappings": [
                {"Ebs": {"VolumeSize": {"Ref": "Size"}, "VolumeType": "gp3"}}
            ]
        },
        {"Size": 100},
        {"Parameters": {"Size": {"Type": "Number"}}},
    )
    ebs = resource.properties["BlockDeviceMappings"][0]["Ebs"]
    assert ebs == {"VolumeSize": 100, "VolumeType": "gp3"}
    assert resource.fully_resolved


def test_malformed_resource_body_is_reported_not_crashed():
    resolver = TemplateResolver({"Resources": {"Bad": "not-a-mapping"}}, {}, CONTEXT)
    resource = resolver.resolve_resources()[0]
    assert resource.resource_type == ""
    assert not resource.fully_resolved


def test_resource_without_properties_resolves_to_empty():
    resolver = TemplateResolver(
        {"Resources": {"Bucket": {"Type": "AWS::S3::Bucket"}}}, {}, CONTEXT
    )
    resource = resolver.resolve_resources()[0]
    assert resource.properties == {}
    assert resource.fully_resolved
    assert resource.is_priceable


def test_deletion_policy_is_captured():
    resolver = TemplateResolver(
        {
            "Resources": {
                "Bucket": {"Type": "AWS::S3::Bucket", "DeletionPolicy": "Retain"}
            }
        },
        {},
        CONTEXT,
    )
    assert resolver.resolve_resources()[0].deletion_policy == "Retain"


# -- pseudo context -------------------------------------------------------


def test_pseudo_context_derived_from_stack_arn():
    context = PseudoContext.from_stack_id(
        "arn:aws:cloudformation:eu-west-1:999988887777:stack/my-stack/def-456"
    )
    assert context.region == "eu-west-1"
    assert context.account_id == "999988887777"
    assert context.stack_name == "my-stack"
    assert context.partition == "aws"
    assert context.url_suffix == "amazonaws.com"


def test_china_partition_gets_the_right_url_suffix():
    context = PseudoContext.from_stack_id(
        "arn:aws-cn:cloudformation:cn-north-1:999988887777:stack/my-stack/def"
    )
    assert context.partition == "aws-cn"
    assert context.url_suffix == "amazonaws.com.cn"


def test_non_arn_stack_id_is_rejected():
    with pytest.raises(ValueError, match="Not a stack ARN"):
        PseudoContext.from_stack_id("my-stack")


def test_resolver_without_context_reports_pseudo_params_as_missing():
    resolver = TemplateResolver({"Resources": {}}, {})
    result = resolver.resolve_value({"Ref": "AWS::Region"})
    assert not result.is_resolved
    assert result.reason is UnresolvedReason.MISSING_PARAMETER
