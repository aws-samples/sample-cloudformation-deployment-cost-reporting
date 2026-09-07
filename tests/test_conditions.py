"""Condition evaluation.

Conditions decide two pricing-relevant things: which Fn::If branch is taken, and
whether a conditional resource exists at all.
"""

from resolver import PseudoContext, TemplateResolver, UnresolvedReason

CONTEXT = PseudoContext(
    region="us-east-1",
    account_id="111122223333",
    stack_name="test-stack",
    stack_id="arn:aws:cloudformation:us-east-1:111122223333:stack/test-stack/abc",
)


def build(conditions, parameters=None, declared=None):
    template = {
        "Parameters": declared
        or {"Environment": {"Type": "String"}, "Replicas": {"Type": "Number"}},
        "Conditions": conditions,
        "Resources": {},
    }
    return TemplateResolver(template, parameters or {}, CONTEXT)


def test_equals_against_a_parameter():
    resolver = build(
        {"IsProd": {"Fn::Equals": [{"Ref": "Environment"}, "production"]}},
        {"Environment": "production"},
    )
    assert resolver.conditions.evaluate("IsProd").value is True


def test_equals_is_false_when_values_differ():
    resolver = build(
        {"IsProd": {"Fn::Equals": [{"Ref": "Environment"}, "production"]}},
        {"Environment": "dev"},
    )
    assert resolver.conditions.evaluate("IsProd").value is False


def test_number_and_string_compare_equal():
    resolver = build(
        {"IsSingle": {"Fn::Equals": [{"Ref": "Replicas"}, "1"]}},
        {"Replicas": "1"},
    )
    assert resolver.conditions.evaluate("IsSingle").value is True


def test_zero_padded_string_does_not_equal_unpadded():
    """Numeric coercion here would break account IDs and padded identifiers."""
    resolver = build({"Same": {"Fn::Equals": ["01", "1"]}})
    assert resolver.conditions.evaluate("Same").value is False


def test_boolean_stringifies_lowercase():
    resolver = build({"Yes": {"Fn::Equals": [True, "true"]}})
    assert resolver.conditions.evaluate("Yes").value is True


def test_not_inverts():
    resolver = build(
        {
            "IsProd": {"Fn::Equals": [{"Ref": "Environment"}, "production"]},
            "IsNotProd": {"Fn::Not": [{"Condition": "IsProd"}]},
        },
        {"Environment": "dev"},
    )
    assert resolver.conditions.evaluate("IsNotProd").value is True


def test_and_requires_all_true():
    resolver = build(
        {
            "Both": {
                "Fn::And": [
                    {"Fn::Equals": [{"Ref": "Environment"}, "production"]},
                    {"Fn::Equals": [{"Ref": "Replicas"}, "3"]},
                ]
            }
        },
        {"Environment": "production", "Replicas": "3"},
    )
    assert resolver.conditions.evaluate("Both").value is True


def test_and_is_false_when_one_is_false():
    resolver = build(
        {
            "Both": {
                "Fn::And": [
                    {"Fn::Equals": [{"Ref": "Environment"}, "production"]},
                    {"Fn::Equals": [{"Ref": "Replicas"}, "3"]},
                ]
            }
        },
        {"Environment": "production", "Replicas": "1"},
    )
    assert resolver.conditions.evaluate("Both").value is False


def test_or_is_true_when_either_is_true():
    resolver = build(
        {
            "Either": {
                "Fn::Or": [
                    {"Fn::Equals": [{"Ref": "Environment"}, "production"]},
                    {"Fn::Equals": [{"Ref": "Environment"}, "staging"]},
                ]
            }
        },
        {"Environment": "staging"},
    )
    assert resolver.conditions.evaluate("Either").value is True


def test_or_short_circuits_past_an_unresolvable_branch():
    """A true branch makes Or true even if a sibling is unknowable.

    Evaluating greedily would report UNRESOLVED for a value that is in fact
    knowable, which would needlessly drop a resource out of pricing.
    """
    resolver = build(
        {
            "Either": {
                "Fn::Or": [
                    {"Fn::Equals": [{"Ref": "Environment"}, "production"]},
                    {"Fn::Equals": [{"Fn::ImportValue": "other-stack"}, "x"]},
                ]
            }
        },
        {"Environment": "production"},
    )
    outcome = resolver.conditions.evaluate("Either")
    assert outcome.is_resolved
    assert outcome.value is True


def test_and_short_circuits_on_false():
    resolver = build(
        {
            "Both": {
                "Fn::And": [
                    {"Fn::Equals": [{"Ref": "Environment"}, "production"]},
                    {"Fn::Equals": [{"Fn::ImportValue": "other-stack"}, "x"]},
                ]
            }
        },
        {"Environment": "dev"},
    )
    outcome = resolver.conditions.evaluate("Both")
    assert outcome.is_resolved
    assert outcome.value is False


def test_unresolvable_operand_without_short_circuit_is_reported():
    resolver = build(
        {
            "Both": {
                "Fn::And": [
                    {"Fn::Equals": [{"Ref": "Environment"}, "production"]},
                    {"Fn::Equals": [{"Fn::ImportValue": "other-stack"}, "x"]},
                ]
            }
        },
        {"Environment": "production"},
    )
    outcome = resolver.conditions.evaluate("Both")
    assert not outcome.is_resolved
    assert outcome.reason is UnresolvedReason.NESTED_UNRESOLVED


def test_nested_condition_references_chain():
    resolver = build(
        {
            "IsProd": {"Fn::Equals": [{"Ref": "Environment"}, "production"]},
            "IsNotProd": {"Fn::Not": [{"Condition": "IsProd"}]},
            "IsDevLike": {"Fn::Or": [{"Condition": "IsNotProd"}, False]},
        },
        {"Environment": "dev"},
    )
    assert resolver.conditions.evaluate("IsDevLike").value is True


def test_unknown_condition_is_reported():
    resolver = build({})
    outcome = resolver.conditions.evaluate("Missing")
    assert not outcome.is_resolved
    assert outcome.reason is UnresolvedReason.UNKNOWN_CONDITION


def test_circular_conditions_are_detected_not_recursed():
    resolver = build(
        {
            "A": {"Fn::Not": [{"Condition": "B"}]},
            "B": {"Fn::Not": [{"Condition": "A"}]},
        }
    )
    outcome = resolver.conditions.evaluate("A")
    assert not outcome.is_resolved
    assert outcome.reason is UnresolvedReason.CIRCULAR_CONDITION
    assert "->" in outcome.detail


def test_malformed_equals_is_reported():
    resolver = build({"Bad": {"Fn::Equals": ["only-one"]}})
    outcome = resolver.conditions.evaluate("Bad")
    assert not outcome.is_resolved
    assert outcome.reason is UnresolvedReason.MALFORMED


def test_non_boolean_operator_in_condition_position_is_reported():
    resolver = build({"Bad": {"Fn::Join": ["", ["a", "b"]]}})
    outcome = resolver.conditions.evaluate("Bad")
    assert not outcome.is_resolved
    assert outcome.reason is UnresolvedReason.MALFORMED
