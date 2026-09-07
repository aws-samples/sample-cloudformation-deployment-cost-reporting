"""Operating system resolution from an AMI.

A template never states an instance's operating system. ``ImageId`` is an opaque
AMI identifier, so until now Linux was assumed and the assumption declared. That
understates a Windows instance by roughly 2x, and understates a SQL-bundled one
by considerably more.

``ec2:DescribeImages`` closes the gap. ``PlatformDetails`` maps almost directly
onto the Price List ``operatingSystem`` and ``preInstalledSw`` attributes, which
is exactly what pricing needs.

Resolution is best-effort by design. A deregistered AMI, an AMI owned by another
account, or an unrecognised platform string all fall back to the documented
assumption rather than failing — and the fallback is recorded on the result so a
report can say which instances were assumed and which were verified.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class Platform:
    """An instance's operating system, as pricing dimensions."""

    operating_system: str
    #: Bundled commercial software. ``NA`` is the overwhelming default.
    pre_installed_sw: str = "NA"
    #: ``AMI`` when read from DescribeImages, ``ASSUMED`` when defaulted.
    source: str = "ASSUMED"
    #: Raw ``PlatformDetails``, kept for debugging an unexpected figure.
    detail: str | None = None

    @property
    def is_assumed(self) -> bool:
        return self.source == "ASSUMED"

    @property
    def has_bundled_software(self) -> bool:
        return self.pre_installed_sw != "NA"


#: The documented default when an AMI cannot be resolved.
ASSUMED_LINUX = Platform(operating_system="Linux", pre_installed_sw="NA", source="ASSUMED")


#: ``PlatformDetails`` to (operatingSystem, preInstalledSw).
#:
#: Values come from the EC2 API's published platform strings. Anything not listed
#: falls through to the heuristic below.
PLATFORM_DETAILS: dict[str, tuple[str, str]] = {
    "Linux/UNIX": ("Linux", "NA"),
    "Red Hat Enterprise Linux": ("RHEL", "NA"),
    "Red Hat Enterprise Linux with HA": ("RHEL", "NA"),
    "Red Hat BYOL Linux": ("Linux", "NA"),
    "SUSE Linux": ("SUSE", "NA"),
    "Ubuntu Pro": ("Ubuntu Pro", "NA"),
    "Windows": ("Windows", "NA"),
    "Windows BYOL": ("Windows", "NA"),
    "Windows with SQL Server Standard": ("Windows", "SQL Std"),
    "Windows with SQL Server Web": ("Windows", "SQL Web"),
    "Windows with SQL Server Enterprise": ("Windows", "SQL Ent"),
    "Linux with SQL Server Standard": ("Linux", "SQL Std"),
    "Linux with SQL Server Web": ("Linux", "SQL Web"),
    "Linux with SQL Server Enterprise": ("Linux", "SQL Ent"),
}


def platform_from_details(details: str | None) -> Platform:
    """Interpret a ``PlatformDetails`` string.

    Exact match first. Failing that, a substring heuristic, because getting
    Windows wrong is the expensive mistake and the string reliably contains the
    word. Anything still unrecognised is reported as assumed, so the uncertainty
    reaches the report rather than being hidden behind a confident number.
    """
    if not details:
        return ASSUMED_LINUX

    exact = PLATFORM_DETAILS.get(details)
    if exact is not None:
        return Platform(
            operating_system=exact[0],
            pre_installed_sw=exact[1],
            source="AMI",
            detail=details,
        )

    lowered = details.lower()

    software = "NA"
    if "sql server standard" in lowered:
        software = "SQL Std"
    elif "sql server web" in lowered:
        software = "SQL Web"
    elif "sql server enterprise" in lowered:
        software = "SQL Ent"

    for needle, operating_system in (
        ("windows", "Windows"),
        ("red hat", "RHEL"),
        ("rhel", "RHEL"),
        ("suse", "SUSE"),
        ("ubuntu pro", "Ubuntu Pro"),
        ("linux", "Linux"),
    ):
        if needle in lowered:
            return Platform(
                operating_system=operating_system,
                pre_installed_sw=software,
                source="AMI",
                detail=details,
            )

    # Recognised nothing. Declared as assumed so the report says so.
    return Platform(
        operating_system=ASSUMED_LINUX.operating_system,
        pre_installed_sw="NA",
        source="ASSUMED",
        detail=details,
    )


class PlatformResolver(Protocol):
    """Anything that can turn an AMI ID into pricing dimensions."""

    def resolve(self, image_id: str | None) -> Platform:
        ...


class AssumedPlatformResolver:
    """Always returns the documented assumption.

    The default, so the pricing engine works with no EC2 permissions at all.
    """

    def __init__(self, platform: Platform = ASSUMED_LINUX) -> None:
        self._platform = platform

    def resolve(self, image_id: str | None) -> Platform:
        return self._platform


class Ec2PlatformResolver:
    """Resolves through ``ec2:DescribeImages``, cached per AMI.

    Verified AMI results are cached for the container. Assumed fallbacks are not:
    a transient EC2 failure must not underprice that AMI until Lambda recycling.

    Args:
        client: A boto3 EC2 client, or anything with ``describe_images``.
        fallback: Returned when an AMI cannot be resolved.
    """

    def __init__(self, client: Any, fallback: Platform = ASSUMED_LINUX) -> None:
        self._client = client
        self._fallback = fallback
        self._cache: dict[str, Platform] = {}
        self.calls = 0
        self.failures: list[str] = []

    def resolve(self, image_id: str | None) -> Platform:
        if not image_id:
            return self._fallback

        cached = self._cache.get(image_id)
        if cached is not None:
            return cached

        platform = self._fetch(image_id)
        # Verified AMI metadata is stable and safe to retain for the container.
        # Assumed fallbacks can be caused by transient EC2 failures, so do not
        # make one throttle underprice that AMI until the Lambda is recycled.
        if not platform.is_assumed:
            self._cache[image_id] = platform
        return platform

    def _fetch(self, image_id: str) -> Platform:
        self.calls += 1
        try:
            response = self._client.describe_images(ImageIds=[image_id])
        except Exception:
            self.failures.append(image_id)
            return self._fallback

        images = response.get("Images") or []
        if not images:
            # Deregistered, or owned by an account that has not shared it.
            self.failures.append(image_id)
            return self._fallback

        details = images[0].get("PlatformDetails")
        platform = platform_from_details(details)

        if platform.is_assumed:
            self.failures.append(image_id)

        return platform
