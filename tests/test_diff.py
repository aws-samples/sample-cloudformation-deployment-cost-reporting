"""The diff engine."""

from decimal import Decimal

from conftest import snap_resource, snapshot
from pricing import Confidence, PricingClass, money
from state import DeltaAction, Direction, diff_deletion, diff_snapshots

# -- baseline (S12) -------------------------------------------------------


def test_no_history_produces_a_baseline_not_a_flood_of_additions():
    """Installing into a mature account must not report every resource as new."""
    after = snapshot(
        snap_resource("Web", dimensions={"instanceType": "t3.large"}, monthly="60.74"),
        snap_resource("Db", "AWS::RDS::DBInstance", dimensions={"instanceType": "db.t4g.medium"}, monthly="47.45"),
    )
    delta = diff_snapshots(None, after, action=DeltaAction.UPDATE)

    assert delta.action is DeltaAction.BASELINE
    assert delta.is_baseline
    assert delta.added == []
    assert delta.removed == []
    assert delta.changed == []
    assert delta.net_monthly == Decimal(0)
    assert delta.current_monthly == Decimal("108.19")


def test_baseline_carries_the_full_inventory():
    after = snapshot(snap_resource("Web", monthly="60.74"))
    delta = diff_snapshots(None, after, action=DeltaAction.UPDATE)

    assert len(delta.inventory) == 1
    payload = delta.to_dict()
    assert payload["inventory"][0]["logicalId"] == "Web"
    assert "added" not in payload


def test_a_genuine_creation_is_not_a_baseline():
    """CREATE with no history really is everything being new."""
    after = snapshot(snap_resource("Web", monthly="60.74"))
    delta = diff_snapshots(None, after, action=DeltaAction.CREATE)

    assert delta.action is DeltaAction.CREATE
    assert not delta.is_baseline
    assert [r.logical_id for r in delta.added] == ["Web"]
    assert delta.net_monthly == Decimal("60.74")


def test_delete_without_history_degrades_to_baseline():
    delta = diff_snapshots(None, snapshot(), action=DeltaAction.DELETE)
    assert delta.action is DeltaAction.BASELINE


# -- additions and removals ----------------------------------------------


def test_added_resource_is_detected():
    before = snapshot(snap_resource("Web", monthly="60.74"))
    after = snapshot(
        snap_resource("Web", monthly="60.74"),
        snap_resource("Db", "AWS::RDS::DBInstance", monthly="47.45"),
    )
    delta = diff_snapshots(before, after)

    assert [r.logical_id for r in delta.added] == ["Db"]
    assert delta.added_monthly == Decimal("47.45")
    assert delta.net_monthly == Decimal("47.45")
    assert delta.direction is Direction.INCREASE


def test_removed_resource_produces_a_saving():
    before = snapshot(
        snap_resource("Web", monthly="60.74"),
        snap_resource("Legacy", monthly="30.20"),
    )
    after = snapshot(snap_resource("Web", monthly="60.74"))
    delta = diff_snapshots(before, after)

    assert [r.logical_id for r in delta.removed] == ["Legacy"]
    assert delta.removed_monthly == Decimal("30.20")
    assert delta.net_monthly == Decimal("-30.20")
    assert delta.direction is Direction.DECREASE


def test_unchanged_resources_are_counted_not_listed():
    before = snapshot(
        snap_resource("A", dimensions={"instanceType": "t3.large"}),
        snap_resource("B", dimensions={"instanceType": "t3.large"}),
    )
    after = snapshot(
        snap_resource("A", dimensions={"instanceType": "t3.large"}),
        snap_resource("B", dimensions={"instanceType": "t3.large"}),
    )
    delta = diff_snapshots(before, after)

    assert delta.unchanged == 2
    assert not delta.has_movement
    assert delta.direction is Direction.NEUTRAL


def test_results_are_sorted_for_stable_reports():
    before = snapshot()
    after = snapshot(
        snap_resource("Zebra", monthly="1.00"),
        snap_resource("Alpha", monthly="1.00"),
    )
    delta = diff_snapshots(before, after)
    assert [r.logical_id for r in delta.added] == ["Alpha", "Zebra"]


# -- resize detection ----------------------------------------------------


def test_instance_resize_is_reported_with_both_sides_priced():
    before = snapshot(
        snap_resource("Web", dimensions={"instanceType": "t3.large"}, monthly="60.74")
    )
    after = snapshot(
        snap_resource("Web", dimensions={"instanceType": "t3.xlarge"}, monthly="121.47")
    )
    delta = diff_snapshots(before, after)

    assert len(delta.changed) == 1
    change = delta.changed[0]
    assert change.logical_id == "Web"
    assert change.delta_monthly_list == Decimal("60.73")
    assert change.changed_dimensions == ["instanceType"]
    assert delta.changed_monthly == Decimal("60.73")


def test_a_downsize_produces_a_negative_delta():
    before = snapshot(
        snap_resource("Web", dimensions={"instanceType": "t3.xlarge"}, monthly="121.47")
    )
    after = snapshot(
        snap_resource("Web", dimensions={"instanceType": "t3.large"}, monthly="60.74")
    )
    delta = diff_snapshots(before, after)

    assert delta.changed[0].delta_monthly_list == Decimal("-60.73")
    assert delta.direction is Direction.DECREASE


def test_a_tag_edit_is_not_a_change():
    """The reason fingerprints are used instead of properties."""
    before = snapshot(
        snap_resource("Web", dimensions={"instanceType": "t3.large"}, monthly="60.74")
    )
    after = snapshot(
        snap_resource("Web", dimensions={"instanceType": "t3.large"}, monthly="60.74")
    )
    after.resources[0].description = "Now with a different tag"

    delta = diff_snapshots(before, after)

    assert delta.changed == []
    assert delta.unchanged == 1
    assert delta.net_monthly == Decimal(0)


def test_a_volume_resize_is_named_as_a_quantity_change():
    """Dimensions match, quantity moved."""
    before = snapshot(
        snap_resource(
            "Vol", "AWS::EC2::Volume", dimensions={"volumeType": "gp3"},
            quantity="100", monthly="8.00",
        )
    )
    after = snapshot(
        snap_resource(
            "Vol", "AWS::EC2::Volume", dimensions={"volumeType": "gp3"},
            quantity="500", monthly="40.00",
        )
    )
    delta = diff_snapshots(before, after)

    assert delta.changed[0].changed_dimensions == ["quantity"]
    assert delta.changed[0].delta_monthly_list == Decimal("32.00")


def test_multiple_dimension_changes_are_all_named():
    before = snapshot(
        snap_resource(
            "Db", "AWS::RDS::DBInstance",
            dimensions={"instanceType": "db.t4g.medium", "deploymentOption": "Single-AZ"},
            monthly="47.45",
        )
    )
    after = snapshot(
        snap_resource(
            "Db", "AWS::RDS::DBInstance",
            dimensions={"instanceType": "db.r6g.xlarge", "deploymentOption": "Multi-AZ"},
            monthly="700.80",
        )
    )
    delta = diff_snapshots(before, after)

    assert delta.changed[0].changed_dimensions == ["deploymentOption", "instanceType"]


# -- replacement ---------------------------------------------------------


def test_replacement_is_flagged_when_the_physical_id_changes():
    """A resource can keep its logical ID while being destroyed and recreated."""
    before = snapshot(
        snap_resource(
            "Web", dimensions={"instanceType": "t3.large"},
            monthly="60.74", physical_id="i-old",
        )
    )
    after = snapshot(
        snap_resource(
            "Web", dimensions={"instanceType": "t3.large"},
            monthly="60.74", physical_id="i-new",
        )
    )
    delta = diff_snapshots(before, after)

    assert delta.changed[0].replacement
    assert delta.changed[0].to_dict()["replacement"] is True


def test_same_physical_id_is_not_a_replacement():
    before = snapshot(
        snap_resource("Vol", dimensions={"volumeType": "gp3"}, quantity="100", physical_id="vol-1")
    )
    after = snapshot(
        snap_resource("Vol", dimensions={"volumeType": "gp3"}, quantity="200", physical_id="vol-1")
    )
    assert not diff_snapshots(before, after).changed[0].replacement


def test_missing_physical_ids_do_not_imply_replacement():
    before = snapshot(snap_resource("Web", dimensions={"instanceType": "t3.large"}))
    after = snapshot(snap_resource("Web", dimensions={"instanceType": "t3.xlarge"}))
    assert not diff_snapshots(before, after).changed[0].replacement


# -- pricing class transitions -------------------------------------------


def test_switching_dynamodb_to_on_demand_shows_as_a_change():
    """A known monthly figure becoming usage-based is a real cost change."""
    before = snapshot(
        snap_resource(
            "Table", "AWS::DynamoDB::Table",
            dimensions={"group": "DDB-ReadUnits"}, monthly="2.85",
        )
    )
    after = snapshot(
        snap_resource(
            "Table", "AWS::DynamoDB::Table",
            pricing_class=PricingClass.USAGE_BASED,
            reason="On-demand billing depends on request volume",
        )
    )
    delta = diff_snapshots(before, after)

    change = delta.changed[0]
    assert change.pricing_class_changed
    assert change.delta_monthly_list == Decimal("-2.85")

    payload = change.to_dict()
    assert payload["pricingClassChanged"] == {
        "from": "DETERMINISTIC",
        "to": "USAGE_BASED",
    }


def test_a_reclassification_with_no_components_either_side_is_not_a_change():
    """After a mapping update, FREE and UNSUPPORTED both price at nothing.

    Reporting that as a change would spam every stack after a plugin upgrade.
    """
    before = snapshot(
        snap_resource("Thing", "AWS::Some::Thing", pricing_class=PricingClass.UNSUPPORTED)
    )
    after = snapshot(
        snap_resource("Thing", "AWS::Some::Thing", pricing_class=PricingClass.FREE)
    )
    delta = diff_snapshots(before, after)

    assert delta.changed == []
    assert delta.unchanged == 1


# -- conditions ----------------------------------------------------------


def test_a_condition_turning_false_reads_as_a_removal():
    """The pricing engine drops excluded resources, so they leave the snapshot."""
    before = snapshot(
        snap_resource("Web", monthly="60.74"),
        snap_resource("ProdReplica", "AWS::RDS::DBInstance", monthly="175.20"),
    )
    after = snapshot(snap_resource("Web", monthly="60.74"))
    delta = diff_snapshots(before, after)

    assert [r.logical_id for r in delta.removed] == ["ProdReplica"]
    assert delta.net_monthly == Decimal("-175.20")


# -- deletion ------------------------------------------------------------


def test_deleting_a_stack_reports_the_whole_saving():
    """The headline feature: proving teardown value."""
    before = snapshot(
        snap_resource("Web", monthly="60.74"),
        snap_resource("Db", "AWS::RDS::DBInstance", monthly="47.45"),
        snap_resource("Nat", "AWS::EC2::NatGateway", monthly="32.85"),
        snap_resource("Bucket", "AWS::S3::Bucket", pricing_class=PricingClass.USAGE_BASED),
    )
    delta = diff_deletion(before)

    assert delta.action is DeltaAction.DELETE
    assert len(delta.removed) == 4
    assert delta.net_monthly == Decimal("-141.04")
    assert delta.direction is Direction.DECREASE
    assert delta.current_monthly == Decimal(0)
    assert delta.previous_monthly == Decimal("141.04")


def test_delete_coverage_is_reported_against_what_was_removed():
    """An empty stack has nothing to cover, which would say nothing useful."""
    before = snapshot(
        snap_resource("Web", monthly="60.74"),
        snap_resource("Bucket", "AWS::S3::Bucket", pricing_class=PricingClass.USAGE_BASED),
        snap_resource("Vpc", "AWS::EC2::VPC", pricing_class=PricingClass.FREE),
    )
    coverage = diff_deletion(before).coverage

    assert coverage.priced == 1
    assert coverage.usage_based == 1
    assert coverage.free == 1
    assert coverage.priced_percent == 50.0


# -- rollback ------------------------------------------------------------


def test_a_rollback_that_restored_state_nets_to_zero():
    state = snapshot(snap_resource("Web", dimensions={"instanceType": "t3.large"}))
    delta = diff_snapshots(
        state,
        snapshot(snap_resource("Web", dimensions={"instanceType": "t3.large"})),
        action=DeltaAction.ROLLBACK,
    )

    assert delta.action is DeltaAction.ROLLBACK
    assert delta.net_monthly == Decimal(0)
    assert not delta.has_movement


# -- reconciliation invariant --------------------------------------------


def test_the_delta_explains_the_change_in_the_total():
    """Self-check: added − removed + changed must equal current − previous.

    If this is ever false the diff has lost or double-counted a resource.
    """
    before = snapshot(
        snap_resource("Keep", dimensions={"instanceType": "t3.large"}, monthly="60.74"),
        snap_resource("Resize", dimensions={"instanceType": "t3.large"}, monthly="60.74"),
        snap_resource("Drop", monthly="30.20"),
    )
    after = snapshot(
        snap_resource("Keep", dimensions={"instanceType": "t3.large"}, monthly="60.74"),
        snap_resource("Resize", dimensions={"instanceType": "t3.xlarge"}, monthly="121.47"),
        snap_resource("New", "AWS::RDS::DBInstance", monthly="47.45"),
    )
    delta = diff_snapshots(before, after)

    assert delta.reconciles
    assert money(delta.net_monthly) == money(
        delta.current_monthly - delta.previous_monthly
    )


def test_reconciliation_holds_for_creation():
    delta = diff_snapshots(
        None, snapshot(snap_resource("Web", monthly="60.74")), action=DeltaAction.CREATE
    )
    assert delta.reconciles


def test_reconciliation_holds_for_deletion():
    assert diff_deletion(snapshot(snap_resource("Web", monthly="60.74"))).reconciles


def test_reconciliation_holds_when_unpriced_resources_are_present():
    before = snapshot(
        snap_resource("Web", monthly="60.74"),
        snap_resource("Bucket", "AWS::S3::Bucket", pricing_class=PricingClass.USAGE_BASED),
    )
    after = snapshot(
        snap_resource("Web", monthly="60.74"),
        snap_resource("Bucket", "AWS::S3::Bucket", pricing_class=PricingClass.USAGE_BASED),
        snap_resource("Lambda", "AWS::Lambda::Function", pricing_class=PricingClass.USAGE_BASED),
    )
    delta = diff_snapshots(before, after)

    assert delta.reconciles
    assert delta.net_monthly == Decimal(0)
    assert [r.logical_id for r in delta.added] == ["Lambda"]


def test_baseline_reconciles_trivially():
    assert diff_snapshots(None, snapshot(snap_resource("Web", monthly="60.74"))).reconciles


# -- confidence ----------------------------------------------------------


def test_a_delta_is_only_as_trustworthy_as_its_weaker_side():
    before = snapshot(
        snap_resource("Nat", "AWS::EC2::NatGateway", dimensions={"a": "1"}, monthly="32.85", confidence=Confidence.MEDIUM)
    )
    after = snapshot(
        snap_resource("Nat", "AWS::EC2::NatGateway", dimensions={"a": "2"}, monthly="40.00", confidence=Confidence.HIGH)
    )
    assert diff_snapshots(before, after).changed[0].confidence is Confidence.MEDIUM


def test_unpriced_side_contributes_zero_rather_than_breaking_the_delta():
    before = snapshot(
        snap_resource(
            "Web", dimensions={"instanceType": "t3.large"},
            monthly="60.74", confidence=Confidence.UNAVAILABLE,
        )
    )
    after = snapshot(
        snap_resource("Web", dimensions={"instanceType": "t3.xlarge"}, monthly="121.47")
    )
    change = diff_snapshots(before, after).changed[0]

    # The before figure was never trustworthy, so it counts as zero.
    assert change.delta_monthly_list == Decimal("121.47")


# -- report shape --------------------------------------------------------


def test_delta_serialises_with_everything_a_report_needs():
    before = snapshot(snap_resource("Web", dimensions={"instanceType": "t3.large"}, monthly="60.74"))
    after = snapshot(
        snap_resource("Web", dimensions={"instanceType": "t3.xlarge"}, monthly="121.47"),
        snap_resource("Nat", "AWS::EC2::NatGateway", monthly="32.85", confidence=Confidence.MEDIUM, excluded=["Data processing per GB"]),
        snap_resource("Bucket", "AWS::S3::Bucket", pricing_class=PricingClass.USAGE_BASED, reason="Depends on volume"),
    )
    payload = diff_snapshots(before, after, action=DeltaAction.UPDATE).to_dict()

    assert payload["action"] == "UPDATE"
    assert payload["direction"] == "INCREASE"
    assert payload["totals"]["addedMonthly"] == 32.85
    assert payload["totals"]["changedMonthly"] == 60.73
    assert payload["totals"]["netMonthly"] == 93.58
    assert payload["totals"]["netAnnual"] == 1122.96
    assert payload["totals"]["previousStackMonthly"] == 60.74
    assert payload["totals"]["currentStackMonthly"] == 154.32
    assert payload["reconciles"] is True
    assert payload["unpriced"]["usageBased"][0]["logicalId"] == "Bucket"
    assert payload["unchanged"] == 0

    # Looked up by name, since results are sorted by logical ID.
    added = {r["logicalId"]: r for r in payload["added"]}
    assert added["Nat"]["excluded"] == ["Data processing per GB"]
    assert added["Bucket"]["monthlyCost"] == 0.0


def test_net_annual_is_twelve_times_monthly():
    before = snapshot()
    after = snapshot(snap_resource("Web", monthly="100.00"))
    delta = diff_snapshots(before, after)

    assert delta.net_annual == Decimal("1200.00")
