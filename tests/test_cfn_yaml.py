"""Template loading, including CloudFormation's short-form intrinsic tags."""

import pytest

from resolver import TemplateParseError, load_template


def test_short_form_ref_becomes_long_form():
    template = load_template(
        """
        Resources:
          Server:
            Type: AWS::EC2::Instance
            Properties:
              InstanceType: !Ref InstanceType
        """
    )
    props = template["Resources"]["Server"]["Properties"]
    assert props["InstanceType"] == {"Ref": "InstanceType"}


def test_short_form_getatt_splits_on_first_dot_only():
    template = load_template(
        """
        Outputs:
          A:
            Value: !GetAtt Server.PrivateIp
          B:
            Value: !GetAtt Nested.Outputs.BucketName
        """
    )
    assert template["Outputs"]["A"]["Value"] == {
        "Fn::GetAtt": ["Server", "PrivateIp"]
    }
    # Only the first dot separates logical ID from attribute path.
    assert template["Outputs"]["B"]["Value"] == {
        "Fn::GetAtt": ["Nested", "Outputs.BucketName"]
    }


def test_short_form_sequence_and_scalar_functions():
    template = load_template(
        """
        Resources:
          Server:
            Type: AWS::EC2::Instance
            Properties:
              ImageId: !FindInMap [Amis, !Ref "AWS::Region", ami]
              Name: !Sub "${AWS::StackName}-server"
              Size: !If [IsProd, large, small]
              Zones: !Select [0, !GetAZs ""]
        """
    )
    props = template["Resources"]["Server"]["Properties"]
    assert props["ImageId"] == {
        "Fn::FindInMap": ["Amis", {"Ref": "AWS::Region"}, "ami"]
    }
    assert props["Name"] == {"Fn::Sub": "${AWS::StackName}-server"}
    assert props["Size"] == {"Fn::If": ["IsProd", "large", "small"]}
    assert props["Zones"] == {"Fn::Select": [0, {"Fn::GetAZs": ""}]}


def test_json_templates_load_through_the_same_entry_point():
    template = load_template(
        '{"Resources": {"Bucket": {"Type": "AWS::S3::Bucket"}}}'
    )
    assert template["Resources"]["Bucket"]["Type"] == "AWS::S3::Bucket"


def test_duplicate_keys_are_rejected():
    """A duplicated block would otherwise silently discard half a resource."""
    with pytest.raises(TemplateParseError, match="Duplicate key"):
        load_template(
            """
            Resources:
              Server:
                Type: AWS::EC2::Instance
                Properties:
                  InstanceType: t3.large
                Properties:
                  InstanceType: t3.xlarge
            """
        )


def test_unknown_intrinsic_tag_is_rejected():
    with pytest.raises(TemplateParseError, match="Unrecognised intrinsic"):
        load_template("Resources: !Bogus []")


@pytest.mark.parametrize("source", ["", "   \n  "])
def test_empty_template_is_rejected(source):
    with pytest.raises(TemplateParseError, match="empty"):
        load_template(source)


def test_non_mapping_template_is_rejected():
    with pytest.raises(TemplateParseError, match="mapping"):
        load_template("- just\n- a\n- list\n")


def test_malformed_yaml_is_rejected():
    with pytest.raises(TemplateParseError, match="not valid YAML"):
        load_template("Resources:\n  - [unclosed\n")


# -- loader safety --------------------------------------------------------
#
# The loader takes a custom Loader class, so its safety is asserted here rather
# than left as a claim in a comment. Templates arrive from CloudFormation, but
# their contents originate with whoever wrote them.


def test_double_bang_python_object_is_rejected():
    """`!!python/...` resolves to a tag with no SafeLoader constructor."""
    with pytest.raises(TemplateParseError):
        load_template("Resources: !!python/object/apply:os.system ['echo pwned']\n")


def test_single_bang_python_object_is_rejected():
    """Caught by the intrinsic allowlist, which raises on unknown tags."""
    with pytest.raises(TemplateParseError, match="Unrecognised intrinsic"):
        load_template("Resources: !python/object/apply:os.system ['echo pwned']\n")


def test_arbitrary_module_tags_are_rejected():
    with pytest.raises(TemplateParseError):
        load_template("Resources: !!python/name:builtins.eval\n")


def test_a_tag_resembling_an_intrinsic_is_still_rejected():
    """Only the exact allowlist is accepted, not anything starting with Ref."""
    with pytest.raises(TemplateParseError, match="Unrecognised intrinsic"):
        load_template("Resources: !RefAllTheThings foo\n")
