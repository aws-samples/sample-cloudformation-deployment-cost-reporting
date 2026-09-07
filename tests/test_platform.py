"""AMI platform resolution, and its effect on price."""

from decimal import Decimal

import pytest

from conftest import REGION, build_catalog, make_resource
from pricing import (
    ASSUMED_LINUX,
    AssumedPlatformResolver,
    Confidence,
    Ec2PlatformResolver,
    Platform,
    PricingEngine,
    classify,
    money,
    platform_from_details,
)


class FakeEc2:
    """Serves canned DescribeImages responses, and can be made to fail."""

    def __init__(self, images: dict | None = None):
        self.images = images or {}
        self.calls: list[str] = []
        self.raise_on_call = False

    def describe_images(self, ImageIds):
        image_id = ImageIds[0]
        self.calls.append(image_id)
        if self.raise_on_call:
            raise RuntimeError("AccessDenied")
        details = self.images.get(image_id)
        if details is None:
            return {"Images": []}
        return {"Images": [{"ImageId": image_id, "PlatformDetails": details}]}


# -- interpreting PlatformDetails ----------------------------------------


@pytest.mark.parametrize(
    "details,operating_system,software",
    [
        ("Linux/UNIX", "Linux", "NA"),
        ("Windows", "Windows", "NA"),
        ("Red Hat Enterprise Linux", "RHEL", "NA"),
        ("SUSE Linux", "SUSE", "NA"),
        ("Ubuntu Pro", "Ubuntu Pro", "NA"),
        ("Windows with SQL Server Standard", "Windows", "SQL Std"),
        ("Windows with SQL Server Web", "Windows", "SQL Web"),
        ("Windows with SQL Server Enterprise", "Windows", "SQL Ent"),
        ("Linux with SQL Server Standard", "Linux", "SQL Std"),
    ],
)
def test_known_platform_strings_map_exactly(details, operating_system, software):
    platform = platform_from_details(details)

    assert platform.operating_system == operating_system
    assert platform.pre_installed_sw == software
    assert platform.source == "AMI"
    assert not platform.is_assumed


def test_an_unlisted_windows_variant_is_caught_by_the_heuristic():
    """Getting Windows wrong is the expensive mistake, so the string is searched."""
    platform = platform_from_details("Windows Server 2025 Datacenter Edition")

    assert platform.operating_system == "Windows"
    assert platform.source == "AMI"


def test_the_heuristic_also_picks_up_bundled_software():
    platform = platform_from_details("Windows Server 2030 with SQL Server Enterprise")

    assert platform.operating_system == "Windows"
    assert platform.pre_installed_sw == "SQL Ent"


def test_a_wholly_unrecognised_platform_is_declared_assumed():
    """Uncertainty reaches the report rather than hiding behind a number."""
    platform = platform_from_details("Something Entirely New")

    assert platform.is_assumed
    assert platform.operating_system == "Linux"
    # The raw string is kept so a surprising figure can be traced.
    assert platform.detail == "Something Entirely New"


def test_missing_platform_details_falls_back():
    assert platform_from_details(None).is_assumed
    assert platform_from_details("").is_assumed


def test_bundled_software_is_flagged():
    assert platform_from_details("Windows with SQL Server Web").has_bundled_software
    assert not platform_from_details("Windows").has_bundled_software


# -- the resolvers -------------------------------------------------------


def test_the_assumed_resolver_needs_no_aws_access():
    resolver = AssumedPlatformResolver()
    assert resolver.resolve("ami-anything") is ASSUMED_LINUX
    assert resolver.resolve(None) is ASSUMED_LINUX


def test_a_custom_fallback_can_be_supplied():
    windows = Platform(operating_system="Windows", source="ASSUMED")
    assert AssumedPlatformResolver(windows).resolve("ami-1") is windows


def test_ec2_resolver_reads_platform_details():
    client = FakeEc2({"ami-win": "Windows"})
    platform = Ec2PlatformResolver(client).resolve("ami-win")

    assert platform.operating_system == "Windows"
    assert platform.source == "AMI"


def test_lookups_are_cached_per_ami():
    """A stack usually reuses one image across every instance."""
    client = FakeEc2({"ami-1": "Linux/UNIX"})
    resolver = Ec2PlatformResolver(client)

    for _ in range(5):
        resolver.resolve("ami-1")

    assert client.calls == ["ami-1"]
    assert resolver.calls == 1


def test_distinct_amis_are_each_fetched_once():
    client = FakeEc2({"ami-1": "Linux/UNIX", "ami-2": "Windows"})
    resolver = Ec2PlatformResolver(client)

    resolver.resolve("ami-1")
    resolver.resolve("ami-2")
    resolver.resolve("ami-1")

    assert resolver.calls == 2


def test_failures_are_retried_instead_of_cached_for_the_container():
    client = FakeEc2()
    client.raise_on_call = True
    resolver = Ec2PlatformResolver(client)

    resolver.resolve("ami-gone")
    resolver.resolve("ami-gone")

    assert resolver.calls == 2
    assert resolver.failures == ["ami-gone", "ami-gone"]


def test_a_deregistered_or_unshared_ami_falls_back():
    """DescribeImages returns an empty list rather than an error."""
    resolver = Ec2PlatformResolver(FakeEc2())
    platform = resolver.resolve("ami-missing")

    assert platform.is_assumed
    assert resolver.failures == ["ami-missing"]


def test_an_access_error_falls_back_rather_than_failing_the_report():
    client = FakeEc2()
    client.raise_on_call = True
    assert Ec2PlatformResolver(client).resolve("ami-1").is_assumed


def test_no_image_id_makes_no_call():
    client = FakeEc2()
    resolver = Ec2PlatformResolver(client)

    assert resolver.resolve(None).is_assumed
    assert client.calls == []


def test_an_unrecognised_platform_string_is_recorded_as_a_failure():
    """So the sync backlog knows a new platform string needs mapping."""
    resolver = Ec2PlatformResolver(FakeEc2({"ami-odd": "Brand New OS"}))
    resolver.resolve("ami-odd")

    assert resolver.failures == ["ami-odd"]


# -- effect on classification -------------------------------------------


def instance(image_id=None, instance_type="t3.large"):
    properties = {"InstanceType": instance_type}
    if image_id:
        properties["ImageId"] = image_id
    return make_resource(properties=properties)


def test_a_windows_ami_changes_the_lookup_key():
    resolver = Ec2PlatformResolver(FakeEc2({"ami-win": "Windows"}))
    linux = classify(instance("ami-lnx"), REGION, Ec2PlatformResolver(FakeEc2({"ami-lnx": "Linux/UNIX"})))
    windows = classify(instance("ami-win"), REGION, resolver)

    assert linux.components[0].query != windows.components[0].query
    assert "Windows" in windows.description


def test_a_resolved_platform_is_not_reported_as_an_assumption():
    resolver = Ec2PlatformResolver(FakeEc2({"ami-win": "Windows"}))
    result = classify(instance("ami-win"), REGION, resolver)

    assert not any("assumed" in a for a in result.assumptions)
    assert any("Windows (from AMI)" in a for a in result.assumptions)


def test_an_unresolvable_ami_still_declares_the_assumption():
    result = classify(instance("ami-missing"), REGION, Ec2PlatformResolver(FakeEc2()))

    assert any("Linux OS assumed" in a for a in result.assumptions)
    assert "Linux" in result.description


def test_without_a_resolver_behaviour_is_unchanged():
    """The engine must still work with no EC2 permissions at all."""
    result = classify(instance("ami-win"), REGION)

    assert "Linux" in result.description
    assert any("assumed" in a for a in result.assumptions)


def test_bundled_software_is_added_to_the_lookup_key():
    """No sync rule covers SQL-bundled images, so this deliberately will not match.

    UNAVAILABLE is the honest outcome. Pricing it at the plain Windows rate would
    understate a SQL Server instance substantially.
    """
    resolver = Ec2PlatformResolver(
        FakeEc2({"ami-sql": "Windows with SQL Server Standard"})
    )
    result = classify(instance("ami-sql"), REGION, resolver)

    attributes = dict(result.components[0].query.attributes)
    assert attributes["preInstalledSw"] == "SQL Std"
    assert "SQL Std" in result.description


def test_ordinary_images_omit_pre_installed_sw_so_the_cache_key_matches():
    resolver = Ec2PlatformResolver(FakeEc2({"ami-1": "Linux/UNIX"}))
    result = classify(instance("ami-1"), REGION, resolver)

    assert "preInstalledSw" not in dict(result.components[0].query.attributes)


# -- effect on price -----------------------------------------------------


def price(image_id, platform_details=None):
    images = {image_id: platform_details} if platform_details else {}
    engine = PricingEngine(
        build_catalog(), REGION, platforms=Ec2PlatformResolver(FakeEc2(images))
    )
    return engine.price_resource(instance(image_id))


def test_a_windows_instance_costs_more_than_a_linux_one():
    """The reason this work was worth doing."""
    linux = price("ami-lnx", "Linux/UNIX")
    windows = price("ami-win", "Windows")

    assert money(linux.monthly_list) == Decimal("60.74")
    assert money(windows.monthly_list) == Decimal("137.24")
    assert windows.monthly_list > linux.monthly_list * 2


def test_rhel_is_priced_at_its_own_rate():
    assert money(price("ami-rhel", "Red Hat Enterprise Linux").monthly_list) == Decimal(
        "104.54"
    )


def test_an_unresolvable_ami_prices_at_the_assumed_linux_rate():
    priced = price("ami-missing")

    assert money(priced.monthly_list) == Decimal("60.74")
    assert priced.confidence is Confidence.MEDIUM
    assert any("assumed" in a for a in priced.assumptions)


def test_a_sql_bundled_instance_reports_unavailable_rather_than_too_cheap():
    priced = price("ami-sql", "Windows with SQL Server Standard")

    assert priced.confidence is Confidence.UNAVAILABLE
    assert not priced.is_priced
    assert "No price found" in priced.reason


def test_the_engine_defaults_to_no_ec2_access():
    engine = PricingEngine(build_catalog(), REGION)
    priced = engine.price_resource(instance("ami-win"))

    # No resolver, so the Linux assumption stands and the price is the Linux one.
    assert money(priced.monthly_list) == Decimal("60.74")
