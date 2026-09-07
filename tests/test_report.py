"""Canonical report assembly, SNS subject, and message attributes."""

from decimal import Decimal

from analyzer import (
    SCHEMA_VERSION,
    ReportPhase,
    build_report,
    console_url,
    message_attributes,
    render_email_body,
    render_email_html,
    subject_line,
)
from conftest import snap_resource, snapshot
from pricing import PricingBasis, PricingClass
from state import DeltaAction, diff_deletion, diff_snapshots


def sample_delta():
    before = snapshot(
        snap_resource("Web", dimensions={"instanceType": "t3.large"}, monthly="60.74")
    )
    after = snapshot(
        snap_resource("Web", dimensions={"instanceType": "t3.xlarge"}, monthly="121.47"),
        snap_resource("Bucket", "AWS::S3::Bucket", pricing_class=PricingClass.USAGE_BASED),
    )
    return diff_snapshots(before, after, action=DeltaAction.UPDATE)


def test_report_carries_schema_phase_and_context():
    report = build_report(
        sample_delta(),
        phase=ReportPhase.CONFIRMED,
        status="UPDATE_COMPLETE",
        client_request_token="token-1",
        tags={"Environment": "production", "Project": "payments"},
        pricing_basis=PricingBasis(price_list_version="20260801000000"),
        root_stack_id=None,
        is_nested_stack=False,
        report_id="fixed-id",
        analyzed_at="2026-08-10T17:06:41Z",
    )

    assert report["schemaVersion"] == SCHEMA_VERSION
    assert report["reportId"] == "fixed-id"
    assert report["reportPhase"] == "CONFIRMED"
    assert report["status"] == "UPDATE_COMPLETE"
    assert report["clientRequestToken"] == "token-1"
    assert report["analyzedAt"] == "2026-08-10T17:06:41Z"
    assert report["isNestedStack"] is False
    assert report["tags"]["Environment"] == "production"


def test_report_includes_the_delta_payload():
    report = build_report(sample_delta())

    assert report["action"] == "UPDATE"
    assert report["direction"] == "INCREASE"
    assert report["totals"]["changedMonthly"] == 60.73
    assert report["coverage"]["resourcesUsageBased"] == 1
    assert report["reconciles"] is True
    assert "changed" in report


def test_report_includes_the_pricing_basis_and_currency():
    report = build_report(
        sample_delta(),
        pricing_basis=PricingBasis(
            price_list_version="20260801000000", discount_percent=Decimal(15)
        ),
    )

    assert report["pricingBasis"]["hoursPerMonth"] == 730
    assert report["pricingBasis"]["discountPercent"] == 15.0
    assert "Savings Plans" in report["pricingBasis"]["note"]
    assert report["currency"] == "USD"


def test_notes_are_included_only_when_present():
    assert "notes" not in build_report(sample_delta())

    report = build_report(sample_delta(), notes=["Something worth saying"])
    assert report["notes"] == ["Something worth saying"]


def test_report_ids_are_unique_by_default():
    assert build_report(sample_delta())["reportId"] != build_report(sample_delta())["reportId"]


def test_console_url_encodes_the_stack_arn():
    url = console_url(
        "arn:aws:cloudformation:us-east-1:111122223333:stack/my-stack/abc", "us-east-1"
    )
    assert url.startswith("https://us-east-1.console.aws.amazon.com/cloudformation/home")
    assert "arn%3Aaws%3A" in url
    assert " " not in url


def test_report_carries_a_console_deep_link():
    assert "cloudformation" in build_report(sample_delta())["consoleUrl"]


# -- SNS subject ---------------------------------------------------------


def test_subject_leads_with_the_cost():
    """SNS truncates at 100 characters, so the figure must survive (S27)."""
    # The instance resize is the only movement; the added bucket is usage-based.
    subject = subject_line(build_report(sample_delta()))
    assert subject.startswith("[+$60.73/mo]")
    assert "payments-api-prod" in subject
    assert "update" in subject


def test_subject_shows_a_saving_as_negative():
    delta = diff_deletion(snapshot(snap_resource("Web", monthly="60.74")))
    subject = subject_line(build_report(delta))
    assert subject.startswith("[-$60.74/mo]")


def test_subject_for_a_baseline_reports_the_current_total():
    delta = diff_snapshots(None, snapshot(snap_resource("Web", monthly="60.74")))
    subject = subject_line(build_report(delta))
    assert "baseline" in subject
    assert "60.74" in subject


def test_subject_is_truncated_to_the_sns_limit():
    delta = diff_snapshots(
        None,
        snapshot(snap_resource("Web", monthly="60.74"), stack_id="arn:aws:cloudformation:us-east-1:111122223333:stack/" + "x" * 200 + "/abc"),
        action=DeltaAction.CREATE,
    )
    assert len(subject_line(build_report(delta))) <= 100


def test_subject_formats_large_numbers_with_separators():
    before = snapshot()
    after = snapshot(snap_resource("Big", monthly="12345.67"))
    subject = subject_line(build_report(diff_snapshots(before, after)))
    assert "12,345.67" in subject


# -- SNS message attributes ----------------------------------------------


def test_message_attributes_let_subscribers_filter_without_code():
    report = build_report(
        sample_delta(),
        tags={"Environment": "production"},
        pricing_basis=PricingBasis(price_list_version="v"),
    )
    attributes = message_attributes(report)

    assert attributes["account"]["StringValue"] == "111122223333"
    assert attributes["region"]["StringValue"] == "us-east-1"
    assert attributes["action"]["StringValue"] == "UPDATE"
    assert attributes["direction"]["StringValue"] == "INCREASE"
    assert attributes["environment"]["StringValue"] == "production"
    assert attributes["isBaseline"]["StringValue"] == "false"


def test_net_monthly_is_a_numeric_attribute_so_thresholds_work():
    attributes = message_attributes(build_report(sample_delta()))
    assert attributes["netMonthly"]["DataType"] == "Number"
    assert float(attributes["netMonthly"]["StringValue"]) == 60.73


def test_empty_attributes_are_omitted_rather_than_sent_blank():
    report = build_report(sample_delta(), tags={})
    assert "environment" not in message_attributes(report)


def test_baseline_is_flagged_in_attributes():
    delta = diff_snapshots(None, snapshot(snap_resource("Web", monthly="60.74")))
    attributes = message_attributes(build_report(delta))
    assert attributes["isBaseline"]["StringValue"] == "true"


# -- email body ----------------------------------------------------------
#
# The email subscriber receives plain text, so this is what an operator reads.
# These build the report dict directly (rather than through a diff) so each
# assertion pins one rendering decision independently of the diff internals.


def _email_report(**overrides):
    report = {
        "stackName": "payments-api-prod",
        "account": "111122223333",
        "region": "us-east-1",
        "action": "UPDATE",
        "direction": "INCREASE",
        "reportPhase": "CONFIRMED",
        "analyzedAt": "2026-08-10T17:06:18.123456Z",
        "totals": {
            "netMonthly": 120.5,
            "netAnnual": 1446.0,
            "addedMonthly": 140.0,
            "removedMonthly": 19.5,
            "changedMonthly": 0.0,
            "previousStackMonthly": 379.5,
            "currentStackMonthly": 500.0,
        },
        "coverage": {
            "resourcesPriced": 6,
            "resourcesUsageBased": 1,
            "resourcesUnsupported": 1,
            "resourcesUnresolved": 0,
            "resourcesFree": 12,
        },
        "pricingBasis": {
            "rateType": "On-Demand",
            "hoursPerMonth": 730,
            "discountPercent": 0,
            "priceListVersion": "20260801000000",
        },
        "added": [
            {
                "logicalId": "Database",
                "resourceType": "AWS::RDS::DBInstance",
                "description": "db.r6g.large PostgreSQL",
                "monthlyCost": 140.0,
                "excluded": ["I/O requests"],
            }
        ],
        "removed": [
            {
                "logicalId": "OldCache",
                "resourceType": "AWS::ElastiCache::CacheCluster",
                "description": "cache.t3.micro",
                "monthlyCost": 19.5,
            }
        ],
        "changed": [],
        "retained": [],
        "unpriced": {"usageBased": [{"logicalId": "Queue"}], "unsupported": [{"logicalId": "Widget"}]},
        "consoleUrl": "https://us-east-1.console.aws.amazon.com/cloudformation/home",
    }
    report.update(overrides)
    return report


def test_email_body_leads_with_the_stack_and_net_change():
    body = render_email_body(_email_report())
    assert "payments-api-prod — UPDATE" in body
    assert "Net change: +$120.50/month" in body
    assert "$1,446.00/year" in body


def test_email_body_lists_added_and_removed_with_subtotals():
    body = render_email_body(_email_report())
    assert "Added (+$140.00/mo)" in body
    assert "Database — db.r6g.large PostgreSQL" in body
    assert "Removed (-$19.50/mo)" in body
    assert "OldCache" in body


def test_email_body_marks_excluded_dimensions():
    assert "(I/O requests excluded)" in render_email_body(_email_report())


def test_email_body_shows_a_resize_with_both_sides():
    body = render_email_body(
        _email_report(
            added=[],
            removed=[],
            totals={"netMonthly": 60.74, "netAnnual": 728.9, "changedMonthly": 60.74},
            changed=[
                {
                    "logicalId": "Api",
                    "before": {"description": "t3.large"},
                    "after": {"description": "t3.xlarge"},
                    "deltaMonthly": 60.74,
                    "replacement": True,
                }
            ],
        )
    )
    assert "Resized (+$60.74/mo)" in body
    assert "t3.large -> t3.xlarge" in body
    assert "[replaced]" in body


def test_email_body_states_coverage_and_pricing_basis():
    body = render_email_body(_email_report())
    assert "Coverage: priced 6 of 8" in body
    assert "On-Demand" in body
    assert "730h/mo" in body
    assert "no discount" in body
    assert "Price list version 20260801000000" in body


def test_email_body_names_unpriced_resources():
    body = render_email_body(_email_report())
    assert "Not priced (usage-based): 1 — Queue" in body
    assert "Not priced (no mapping): 1" in body


def test_email_body_carries_the_console_link():
    assert "View stack: https://" in render_email_body(_email_report())


def test_email_body_for_a_baseline_shows_inventory_not_a_delta():
    body = render_email_body(
        _email_report(
            isBaseline=True,
            inventory=[
                {"logicalId": "Api", "description": "t3.large", "monthlyCost": 60.74}
            ],
        )
    )
    assert "now tracking" in body
    assert "Current inventory" in body
    assert "Net change" not in body
    assert "Stack total: $500.00/month" in body


def test_email_body_renders_a_sparse_report_without_crashing():
    assert render_email_body({"stackName": "api"})


def test_email_body_collapses_zero_cost_resources_and_sorts_by_cost():
    """The added/removed lists include free scaffolding at $0.00. Itemising it
    buries the resources that cost money, so $0.00 entries are collapsed to a
    count and the priced ones are ordered largest-first."""
    body = render_email_body(
        _email_report(
            added=[
                {"logicalId": "Cheap", "description": "gp3 10GB", "monthlyCost": 0.8},
                {"logicalId": "Pricey", "description": "NAT Gateway", "monthlyCost": 32.85},
                {"logicalId": "Vpc", "resourceType": "AWS::EC2::VPC", "monthlyCost": 0.0},
                {"logicalId": "SubnetA", "resourceType": "AWS::EC2::Subnet", "monthlyCost": 0.0},
            ],
            removed=[],
        )
    )

    # Free/$0 resources are collapsed, not itemised line by line.
    assert "Vpc" not in body
    assert "SubnetA" not in body
    assert "and 2 more at $0.00/mo" in body
    # Priced resources are ordered largest-first.
    assert body.index("Pricey") < body.index("Cheap")


# -- HTML email (SES) -----------------------------------------------------


def test_email_html_is_a_full_document_with_the_headline():
    body = render_email_html(_email_report())
    assert body.startswith("<!DOCTYPE html>")
    assert body.rstrip().endswith("</html>")
    assert "payments-api-prod" in body
    assert "+$120.50/mo" in body
    assert "+$1,446.00/yr" in body


def test_email_html_colours_the_header_by_direction():
    # Red for an increase, green for a saving — the colour is driven by the
    # direction field, not the sign of the fixture's totals.
    assert "#d13212" in render_email_html(_email_report(direction="INCREASE"))
    assert "#037f0c" in render_email_html(_email_report(direction="DECREASE"))


def test_email_html_lists_priced_resources_with_subtotals():
    body = render_email_html(_email_report())
    assert "Database" in body
    assert "db.r6g.large PostgreSQL" in body
    assert "+$140.00/mo" in body  # added subtotal
    assert "OldCache" in body


def test_email_html_escapes_untrusted_text():
    body = render_email_html(
        _email_report(
            added=[
                {"logicalId": "Db", "description": "m5 <prod> & staging", "monthlyCost": 5.0}
            ]
        )
    )
    assert "&lt;prod&gt; &amp; staging" in body
    assert "<prod>" not in body


def test_email_html_collapses_zero_cost_resources():
    body = render_email_html(
        _email_report(
            added=[
                {"logicalId": "Pricey", "description": "NAT Gateway", "monthlyCost": 32.85},
                {"logicalId": "Vpc", "resourceType": "AWS::EC2::VPC", "monthlyCost": 0.0},
                {"logicalId": "SubnetA", "resourceType": "AWS::EC2::Subnet", "monthlyCost": 0.0},
            ],
            removed=[],
        )
    )
    assert "Vpc" not in body
    assert "SubnetA" not in body
    assert "2 more at $0.00/mo" in body


def test_email_html_states_coverage_and_pricing_basis():
    body = render_email_html(_email_report())
    assert "6 of 8" in body
    assert "12 free" in body
    assert "On-Demand" in body


def test_email_html_carries_the_console_button():
    body = render_email_html(_email_report())
    assert "https://us-east-1.console.aws.amazon.com" in body
    assert "View stack" in body


def test_email_html_for_a_baseline_shows_inventory_not_a_delta():
    body = render_email_html(
        _email_report(
            isBaseline=True,
            inventory=[{"logicalId": "Api", "description": "t3.large", "monthlyCost": 60.74}],
        )
    )
    assert "#0972d3" in body  # baseline is console blue, not red or green
    assert "Current inventory" in body
    assert "$500.00/mo" in body
    assert "/yr" not in body


def test_email_html_renders_a_sparse_report_without_crashing():
    body = render_email_html({"stackName": "api"})
    assert body.startswith("<!DOCTYPE html>")
    assert "api" in body


def _gappy_report(**overrides):
    """A report with coverage gaps in all three buckets."""
    defaults = {
        "coverage": {
            "resourcesPriced": 6,
            "resourcesUsageBased": 1,
            "resourcesUnsupported": 1,
            "resourcesUnresolved": 2,
            "resourcesFree": 12,
        },
        "unpriced": {
            "usageBased": [{"logicalId": "IngestQueue"}],
            "unsupported": [{"logicalId": "Widget"}],
            "unresolved": [{"logicalId": "Mystery"}, {"logicalId": "Mystery2"}],
        },
    }
    return _email_report(**{**defaults, **overrides})


def test_email_html_names_the_unpriced_resources():
    """A count alone says something is missing without saying what, which is what
    makes a coverage line untrustworthy."""
    body = render_email_html(_gappy_report())

    assert "IngestQueue" in body
    assert "Widget" in body
    assert "Mystery" in body
    assert "Mystery2" in body
    assert "Not priced" in body


def test_email_html_caps_a_long_unpriced_list():
    """A large stack must not produce an unbounded email."""
    body = render_email_html(
        _gappy_report(
            unpriced={"usageBased": [{"logicalId": f"Q{n}"} for n in range(12)]}
        )
    )

    assert "Q0" in body
    assert "Q11" not in body
    assert "and 7 more" in body


def test_email_html_omits_the_unpriced_block_when_coverage_is_complete():
    body = render_email_html(
        _email_report(unpriced={"usageBased": [], "unsupported": [], "unresolved": []})
    )

    assert "Not priced" not in body


def test_email_html_warns_when_a_report_fails_reconciliation():
    """The report is still delivered — withholding it would hide the problem — but
    the reader has to know the totals cannot be trusted."""
    body = render_email_html(_email_report(reconciles=False))

    assert "failed their arithmetic check" in body
    assert "unreliable" in body


def test_email_html_stays_quiet_when_a_report_reconciles():
    assert "arithmetic check" not in render_email_html(_email_report(reconciles=True))
    # Absent means it reconciled; only an explicit False is a failure.
    assert "arithmetic check" not in render_email_html(_email_report())


def test_email_body_names_every_unpriced_bucket():
    """The text alternative ships in the same SES message as the HTML, so it must
    not report a different set of gaps."""
    body = render_email_body(_gappy_report())

    assert "usage-based): 1 — IngestQueue" in body
    assert "no mapping): 1 — Widget" in body
    assert "unresolved properties): 2 — Mystery, Mystery2" in body


def test_email_body_warns_when_a_report_fails_reconciliation():
    body = render_email_body(_email_report(reconciles=False))

    assert "WARNING" in body
    assert "arithmetic check" in body


def test_email_body_stays_quiet_when_a_report_reconciles():
    assert "arithmetic check" not in render_email_body(_email_report())


def test_email_html_does_not_uppercase_a_subtotals_unit():
    """The section heading is uppercased; the subtotal beside it must not inherit
    that and render "/MO"."""
    body = render_email_html(_email_report())

    assert "+$140.00/mo" in body
    assert "/MO" not in body
