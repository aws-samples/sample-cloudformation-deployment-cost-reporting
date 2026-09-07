"""Parameter typing and defaults.

DescribeStacks hands back every value as a string, so coercion is what makes a
VolumeSize of "100" multiplyable by a per-GB rate.
"""

from resolver import ParameterStore, ResolvedVia, UnresolvedReason

TEMPLATE = {
    "Parameters": {
        "InstanceType": {"Type": "String", "Default": "t3.micro"},
        "VolumeSize": {"Type": "Number", "Default": "20"},
        "Ratio": {"Type": "Number", "Default": "1.5"},
        "Subnets": {"Type": "CommaDelimitedList", "Default": "a,b,c"},
        "Weights": {"Type": "List<Number>", "Default": "1,2,3"},
        "SubnetIds": {"Type": "List<AWS::EC2::Subnet::Id>"},
        "NoDefault": {"Type": "String"},
    }
}


def test_supplied_value_beats_default():
    store = ParameterStore.build(TEMPLATE, {"InstanceType": "t3.xlarge"})
    result = store.get("InstanceType")
    assert result.value == "t3.xlarge"
    assert result.via is ResolvedVia.PARAMETER


def test_default_used_when_not_supplied():
    store = ParameterStore.build(TEMPLATE, {})
    result = store.get("InstanceType")
    assert result.value == "t3.micro"
    assert result.via is ResolvedVia.PARAMETER_DEFAULT


def test_number_coerced_to_int_and_float():
    store = ParameterStore.build(TEMPLATE, {})
    volume = store.get("VolumeSize")
    ratio = store.get("Ratio")

    assert volume.value == 20
    assert isinstance(volume.value, int)
    assert ratio.value == 1.5
    assert isinstance(ratio.value, float)


def test_supplied_number_string_is_coerced():
    store = ParameterStore.build(TEMPLATE, {"VolumeSize": "500"})
    assert store.get("VolumeSize").value == 500


def test_comma_delimited_list_is_split_and_stripped():
    store = ParameterStore.build(TEMPLATE, {"Subnets": "one, two , three"})
    assert store.get("Subnets").value == ["one", "two", "three"]


def test_list_of_number_coerces_each_item():
    store = ParameterStore.build(TEMPLATE, {})
    assert store.get("Weights").value == [1, 2, 3]


def test_aws_typed_list_is_split():
    store = ParameterStore.build(TEMPLATE, {"SubnetIds": "subnet-a,subnet-b"})
    assert store.get("SubnetIds").value == ["subnet-a", "subnet-b"]


def test_declared_but_unsupplied_parameter_is_unresolved():
    store = ParameterStore.build(TEMPLATE, {})
    result = store.get("NoDefault")
    assert not result.is_resolved
    assert result.reason is UnresolvedReason.MISSING_PARAMETER
    assert "no supplied value and no default" in result.detail


def test_undeclared_parameter_reports_distinctly():
    store = ParameterStore.build(TEMPLATE, {})
    result = store.get("Nonexistent")
    assert not result.is_resolved
    assert result.reason is UnresolvedReason.MISSING_PARAMETER
    assert "not a declared parameter" in result.detail


def test_non_numeric_value_for_number_type_is_preserved_not_crashed():
    """Bad input should degrade to low confidence downstream, not raise."""
    store = ParameterStore.build(TEMPLATE, {"VolumeSize": "not-a-number"})
    assert store.get("VolumeSize").value == "not-a-number"


def test_empty_comma_delimited_list_is_empty_not_a_blank_item():
    store = ParameterStore.build(TEMPLATE, {"Subnets": ""})
    assert store.get("Subnets").value == []


def test_template_without_parameters_block():
    store = ParameterStore.build({}, {})
    assert not store.get("Anything").is_resolved
