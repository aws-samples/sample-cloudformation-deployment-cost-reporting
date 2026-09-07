"""Conventions, rounding, coverage arithmetic."""

from decimal import Decimal

import pytest

from pricing import (
    HOURS_PER_MONTH,
    Confidence,
    Coverage,
    PriceQuery,
    apply_discount,
    money,
    weakest,
)


def test_hours_per_month_is_the_aws_standard():
    assert Decimal("730") == HOURS_PER_MONTH


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("60.736", "60.74"),
        ("60.734", "60.73"),
        ("16.425", "16.43"),
        ("0.4745", "0.47"),
        ("0", "0.00"),
    ],
)
def test_money_rounds_half_up_to_cents(raw, expected):
    assert money(Decimal(raw)) == Decimal(expected)


def test_zero_discount_leaves_list_price_untouched():
    assert apply_discount(Decimal("100.00"), Decimal(0)) == Decimal("100.00")


def test_negative_discount_is_ignored_rather_than_inflating():
    assert apply_discount(Decimal("100.00"), Decimal(-10)) == Decimal("100.00")


def test_discount_is_applied_proportionally():
    assert money(apply_discount(Decimal("100.00"), Decimal(15))) == Decimal("85.00")


def test_confidence_rolls_up_to_the_weakest():
    assert weakest([Confidence.HIGH, Confidence.MEDIUM]) is Confidence.MEDIUM
    assert (
        weakest([Confidence.HIGH, Confidence.MEDIUM, Confidence.UNAVAILABLE])
        is Confidence.UNAVAILABLE
    )
    assert weakest([Confidence.HIGH, Confidence.HIGH]) is Confidence.HIGH


def test_no_components_means_no_confidence():
    assert weakest([]) is Confidence.UNAVAILABLE


def test_price_query_key_is_order_independent():
    left = PriceQuery.of("AmazonEC2", "us-east-1", instanceType="t3.large", tenancy="Shared")
    right = PriceQuery.of("AmazonEC2", "us-east-1", tenancy="Shared", instanceType="t3.large")
    assert left.key == right.key
    assert left == right


def test_price_query_drops_none_attributes():
    query = PriceQuery.of("AmazonEC2", "us-east-1", instanceType="t3.large", iops=None)
    assert "iops" not in query.key


def test_price_query_is_hashable_so_it_can_be_cached():
    query = PriceQuery.of("AmazonEC2", "us-east-1", instanceType="t3.large")
    assert {query: 1}[query] == 1


def test_coverage_excludes_free_resources_from_the_denominator():
    """A stack that is mostly VPC plumbing should not look badly covered.

    One priced, one usage-based, one unsupported, five free. Counting the free
    resources would report 12.5% on a stack that is in fact fully priced for
    everything chargeable.
    """
    coverage = Coverage(total=8, priced=1, usage_based=1, unsupported=1, free=5)
    assert coverage.denominator == 3
    assert coverage.priced_percent == 33.3


def test_coverage_of_an_entirely_free_stack_is_full_not_zero():
    coverage = Coverage(total=4, free=4)
    assert coverage.denominator == 0
    assert coverage.priced_percent == 100.0


def test_coverage_serialises_every_bucket():
    payload = Coverage(total=3, priced=2, unresolved=1).to_dict()
    assert payload["resourcesTotal"] == 3
    assert payload["resourcesPriced"] == 2
    assert payload["resourcesUnresolved"] == 1
    assert payload["resourcesFree"] == 0
    assert payload["pricedPercent"] == 66.7
