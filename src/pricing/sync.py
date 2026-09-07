"""Price cache synchronisation.

Pulls On-Demand prices from the AWS Price List API and writes them into the cache
the engine reads (S14). Runs on a schedule, never in the request path.

The translation problem
-----------------------
The engine's :class:`~pricing.models.PriceQuery` attribute names are the plugin's
own vocabulary. AWS's product attributes are not the same, are not identical
across services, and are only discoverable at runtime through ``DescribeServices``
and ``GetAttributeValues`` — they are not a documented fixed list.

So the mapping lives here as declarative data rather than scattered through the
mappers, and every rule carries an honest verification status. Run
:func:`verify_rules` against a real account to find out which assumed attribute
names actually exist before trusting the output.

Why the filters are deliberately tight
--------------------------------------
The Price List returns many rows for the same instance type, differing on
capacity reservation status, bundled software, and licence model — several priced
at ``0.00``. Matching too loosely means the last row wins and the cache holds a
plausible but wrong number, which shows up as a confidently incorrect cost
report. Matching too tightly means no rows match, which shows up as
``UNAVAILABLE`` in coverage and as an entry in
:attr:`SyncReport.rules_with_no_matches`.

One failure is silent, the other is loud. So filter tightly and read the report.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Protocol

from .models import PriceQuery, PriceRecord
from .pricelist import ParsedProduct, ParseSkip, parse_product


class RuleStatus(str, Enum):
    """How much the rule's attribute mapping can be trusted."""

    #: Verified against a live Price List response.
    CONFIRMED = "CONFIRMED"
    #: Derived from documentation and convention; needs checking.
    NEEDS_VERIFICATION = "NEEDS_VERIFICATION"


@dataclass(frozen=True)
class SyncRule:
    """How to turn Price List products into one kind of cached price.

    Args:
        name: Human label, used in reports.
        service_code: Price List service code, e.g. ``AmazonEC2``.
        product_family: Price List ``productFamily``, also used as an API filter
            to keep result sets small.
        key_static: Query attributes with fixed values.
        key_from: Query attribute name mapped to the AWS attribute it reads.
        require: AWS attributes that must match exactly for a row to be used.
        require_contains: AWS attributes that must contain the given substring.
        value_map: Per query attribute, a translation from the AWS value to ours.
        status: Whether the mapping has been verified.
        note: Anything a reader needs to know, especially known ambiguity.
    """

    name: str
    service_code: str
    product_family: str
    key_static: Mapping[str, str] = field(default_factory=dict)
    key_from: Mapping[str, str] = field(default_factory=dict)
    require: Mapping[str, str] = field(default_factory=dict)
    require_contains: Mapping[str, str] = field(default_factory=dict)
    #: Attribute -> fragment(s). A row is rejected if the attribute contains any
    #: fragment. Accepts a single string or a tuple of them.
    reject_contains: Mapping[str, str | tuple[str, ...]] = field(default_factory=dict)
    value_map: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    status: RuleStatus = RuleStatus.NEEDS_VERIFICATION
    note: str = ""
    #: Fetch products by this ``group`` value instead of by ``product_family``.
    #: A few products (public IPv4 addresses) carry no productFamily and can
    #: only be pulled from the API by group. Empty means fetch by product_family.
    fetch_group: str = ""
    #: Multiplier applied to the Price List rate before caching, to reconcile a
    #: unit mismatch between the Price List and the mapper's quantity. gp3
    #: throughput is listed per GiBps-month but templates express throughput in
    #: MiBps, so it is scaled by 1/1024 to a per-MiBps rate.
    price_scale: Decimal = Decimal(1)

    @property
    def fetch_filter(self) -> tuple[str, str]:
        """The (field, value) the API is filtered on to fetch this rule's rows."""
        if self.fetch_group:
            return ("group", self.fetch_group)
        return ("productFamily", self.product_family)

    @property
    def aws_attributes_used(self) -> frozenset[str]:
        """Every AWS attribute name this rule depends on."""
        return frozenset(
            {
                *self.key_from.values(),
                *self.require,
                *self.require_contains,
                *self.reject_contains,
            }
        )

    def matches(self, product: ParsedProduct) -> bool:
        if product.product_family != self.product_family:
            return False

        for attribute, expected in self.require.items():
            if product.attributes.get(attribute) != expected:
                return False

        for attribute, fragment in self.require_contains.items():
            value = product.attributes.get(attribute)
            if value is None or fragment not in value:
                return False

        # A row matching any reject fragment is discarded: it shares this rule's
        # key but is a different product (a Capacity Block, a pricing tier, an
        # Aurora config, an Extended Support surcharge), and letting it through
        # would collide or misprice.
        for attribute, fragments in self.reject_contains.items():
            value = product.attributes.get(attribute)
            if value is None:
                continue
            candidates = (fragments,) if isinstance(fragments, str) else fragments
            if any(fragment in value for fragment in candidates):
                return False

        return True

    def build_query(self, product: ParsedProduct, region: str) -> PriceQuery | None:
        """Build the cache key for a matching product, or None if incomplete."""
        attributes = dict(self.key_static)

        for ours, theirs in self.key_from.items():
            raw = product.attributes.get(theirs)
            if raw is None:
                return None
            translation = self.value_map.get(ours)
            attributes[ours] = translation.get(raw, raw) if translation else raw

        return PriceQuery.of(self.service_code, region, **attributes)


# -- rules ----------------------------------------------------------------
#
# Values marked NEEDS_VERIFICATION are the plugin's best understanding, not
# confirmed fact. Run verify_rules() against a real account before relying on
# any figure they produce.

SYNC_RULES: tuple[SyncRule, ...] = (
    SyncRule(
        status=RuleStatus.CONFIRMED,
        name="EC2 instance hours",
        service_code="AmazonEC2",
        product_family="Compute Instance",
        key_static={"productFamily": "Compute Instance"},
        key_from={
            "instanceType": "instanceType",
            "operatingSystem": "operatingSystem",
            "tenancy": "tenancy",
        },
        require={
            # Excludes capacity-reservation rows, several of which are $0.00.
            "capacitystatus": "Used",
            # Excludes rows with bundled commercial software.
            "preInstalledSw": "NA",
            "licenseModel": "No License required",
            "tenancy": "Shared",
            # Excludes Capacity Block rows. For GPU instances (p4d/p5/p6) a
            # marketoption=CapacityBlock row shares every attribute above and
            # otherwise collides with the real On-Demand price. Verified against
            # the us-east-1 Price List (2026-08): p5.48xlarge Linux/Shared had
            # both an OnDemand and a CapacityBlock row passing the other filters.
            "marketoption": "OnDemand",
        },
        note=(
            "capacitystatus, preInstalledSw and marketoption are the load-bearing "
            "filters. Without them multiple rows collide per instance type and a "
            "$0.00 reservation or Capacity Block row can win. The marketoption "
            "filter was added after a live us-east-1 Price List check (2026-08) "
            "showed GPU instances (p5.48xlarge) returning both an OnDemand and a "
            "CapacityBlock row that otherwise passed every filter."
        ),
    ),
    SyncRule(
        status=RuleStatus.CONFIRMED,
        name="EBS volume storage",
        service_code="AmazonEC2",
        product_family="Storage",
        key_static={"productFamily": "Storage"},
        # volumeApiName carries the API name (gp3); volumeType is the human
        # label ("General Purpose") and is not what templates contain.
        key_from={"volumeType": "volumeApiName"},
        note="Depends on volumeApiName existing; volumeType would not match templates.",
    ),
    SyncRule(
        status=RuleStatus.CONFIRMED,
        name="EBS provisioned IOPS",
        service_code="AmazonEC2",
        product_family="System Operation",
        key_static={"productFamily": "System Operation"},
        key_from={"volumeType": "volumeApiName"},
        require_contains={"usagetype": "IOPS"},
        # io2 provisioned IOPS is tiered ($0.065 / $0.0455 / $0.03185 per the
        # us-east-1 Price List, 2026-08), so it cannot be a single unit rate and
        # the three tier rows otherwise collide. io2 IOPS is left unpriced (the
        # mapper marks it excluded); io1 and gp3 IOPS are flat and unaffected.
        reject_contains={"usagetype": "io2"},
        note="io2 IOPS is tiered and deliberately not priced; io1/gp3 IOPS are flat.",
    ),
    SyncRule(
        status=RuleStatus.CONFIRMED,
        name="EBS provisioned throughput",
        service_code="AmazonEC2",
        product_family="Provisioned Throughput",
        key_static={"productFamily": "Provisioned Throughput"},
        key_from={"volumeType": "volumeApiName"},
        # Listed as $40.96 per GiBps-month (= $0.04 per MiBps-month) but templates
        # express throughput in MiBps, so scale to a per-MiBps rate. Verified
        # against the live us-east-1 Price List (2026-08).
        price_scale=Decimal(1) / Decimal(1024),
        note="Stored per-MiBps ($0.04) to match the template's MiBps throughput quantity.",
    ),
    SyncRule(
        status=RuleStatus.CONFIRMED,
        name="NAT Gateway hours",
        service_code="AmazonEC2",
        product_family="NAT Gateway",
        key_static={"productFamily": "NAT Gateway", "usageFamily": "Hours"},
        # The family contains both hourly and per-GB rows; only hours are priced.
        require_contains={"usagetype": "NatGateway-Hours"},
        # us-east-1 carries two hourly rows, NatGateway-Hours and
        # RegionalNatGateway-Hours, both $0.045; the Regional one is rejected so
        # the key resolves deterministically. Verified against the live us-east-1
        # Price List (2026-08).
        reject_contains={"usagetype": "Regional"},
        note="Per-GB data processing rows excluded; the Regional duplicate rejected.",
    ),
    SyncRule(
        status=RuleStatus.CONFIRMED,
        name="Public IPv4 address hours",
        service_code="AmazonVPC",
        product_family="",
        # These products carry no productFamily and are fetched by group instead.
        fetch_group="VPCPublicIPv4Address",
        key_static={"group": "VPCPublicIPv4Address"},
        # The group also holds Idle and ContiguousBlock rows; only the in-use
        # hourly charge is priced.
        require_contains={"usagetype": "PublicIPv4:InUseAddress"},
        note=(
            "Lives under AmazonVPC, not AmazonEC2 — verified against the live "
            "us-east-1 Price List (2026-08): group 'VPCPublicIPv4Address', "
            "usagetype 'USE1-PublicIPv4:InUseAddress', $0.005/hr. The earlier "
            "AmazonEC2 'IP Address' family was CarrierIP (Wavelength) only."
        ),
    ),
    SyncRule(
        status=RuleStatus.CONFIRMED,
        name="RDS instance hours",
        service_code="AmazonRDS",
        product_family="Database Instance",
        key_static={"productFamily": "Database Instance"},
        key_from={
            "instanceType": "instanceType",
            "databaseEngine": "databaseEngine",
            "deploymentOption": "deploymentOption",
        },
        require={"licenseModel": "No license required"},
        # Aurora ships two rows per instance (Standard 'InstanceUsage' vs
        # I/O-Optimized 'InstanceUsageIOOptimized') at different prices, and which
        # applies is set on the DB cluster, not the instance — so they collide and
        # can't be disambiguated here. Aurora is excluded and marked unsupported
        # in the mapper. Verified against the live us-east-1 Price List (2026-08).
        reject_contains={"databaseEngine": "Aurora"},
        note=(
            "licenseModel casing differs from EC2's ('No license required' vs "
            "'No License required'). Aurora engines excluded (unsupported); "
            "other commercial engines will need separate rules."
        ),
    ),
    SyncRule(
        status=RuleStatus.CONFIRMED,
        name="RDS storage",
        service_code="AmazonRDS",
        product_family="Database Storage",
        key_static={"productFamily": "Database Storage"},
        key_from={
            "volumeType": "volumeType",
            "deploymentOption": "deploymentOption",
        },
        value_map={
            # Real us-east-1 volumeType values (2026-08) mapped to the template's
            # StorageType names. gp3 and io2 have their own rows, so they no
            # longer collapse onto gp2/io1 as the old guessed map assumed.
            "volumeType": {
                "General Purpose": "gp2",
                "General Purpose-GP3": "gp3",
                "Provisioned IOPS": "io1",
                "Provisioned IOPS-IO2": "io2",
                "Magnetic": "standard",
            }
        },
        # Aurora storage (Aurora:StorageUsage, Aurora:IO-OptimizedStorageUsage) is
        # consumption-based and Aurora is unsupported; its 'General Purpose-Aurora'
        # rows were being mapped onto gp2 and overwriting the real gp2 rate.
        # Verified against the live us-east-1 Price List (2026-08).
        reject_contains={"usagetype": "Aurora"},
        note=(
            "volumeType mapped from the live Price List (2026-08): 'General "
            "Purpose'=gp2, 'General Purpose-GP3'=gp3, 'Provisioned IOPS'=io1, "
            "'Provisioned IOPS-IO2'=io2, 'Magnetic'=standard. Aurora rows excluded."
        ),
    ),
    SyncRule(
        status=RuleStatus.CONFIRMED,
        name="RDS provisioned IOPS",
        service_code="AmazonRDS",
        product_family="Provisioned IOPS",
        key_static={"productFamily": "Provisioned IOPS"},
        key_from={"deploymentOption": "deploymentOption"},
        # The family also carries gp3 baseline provisioned IOPS (RDS:GP3-PIOPS,
        # $0.02) which the mapper never prices and which otherwise overwrote the
        # io1/io2 rate ($0.10, RDS:PIOPS / RDS:IO2-PIOPS — same price). Verified
        # against the live us-east-1 Price List (2026-08).
        reject_contains={"usagetype": "GP3"},
        note="io1 and io2 share the $0.10 rate; gp3 baseline IOPS excluded.",
    ),
    SyncRule(
        status=RuleStatus.CONFIRMED,
        name="Load balancer hours",
        service_code="AWSELB",
        product_family="Load Balancer-Application",
        key_static={"productFamily": "Load Balancer-Application"},
        require_contains={"usagetype": "LoadBalancerUsage"},
        # 'TS-LoadBalancerUsage' ($0.005) collided with the real hourly rate
        # ('LoadBalancerUsage', $0.0225). Verified live us-east-1 (2026-08).
        reject_contains={"usagetype": "TS-"},
        note="Standard ALB hourly rate; the TS- variant is excluded. NLB/GWLB need sibling rules.",
    ),
    SyncRule(
        status=RuleStatus.CONFIRMED,
        name="ElastiCache node hours",
        service_code="AmazonElastiCache",
        product_family="Cache Instance",
        key_static={"productFamily": "Cache Instance"},
        key_from={"instanceType": "instanceType", "cacheEngine": "cacheEngine"},
        # Each (node type, engine) carries several NodeUsage rows: the standard
        # on-demand rate plus surcharge/variant rates (Extended Support for EOL
        # engines, Valkey Sync Durability, Outposts). Only the standard rate is
        # wanted; the rest differ in price and otherwise collide. Rejecting these
        # three clears all 153 collisions verified against us-east-1 (2026-08).
        reject_contains={"usagetype": ("ExtendedSupport", "SyncDurability", "Outpost")},
        note=(
            "Standard NodeUsage rate only; Extended Support, Sync Durability and "
            "Outposts excluded."
        ),
    ),
    SyncRule(
        status=RuleStatus.CONFIRMED,
        name="DynamoDB provisioned capacity",
        service_code="AmazonDynamoDB",
        product_family="Provisioned IOPS",
        key_static={"productFamily": "Provisioned IOPS"},
        key_from={"group": "group"},
        require_contains={"group": "DDB-"},
        note="Expects group values DDB-ReadUnits and DDB-WriteUnits.",
    ),
)


def rules_needing_verification(
    rules: Sequence[SyncRule] = SYNC_RULES,
) -> tuple[SyncRule, ...]:
    return tuple(r for r in rules if r.status is RuleStatus.NEEDS_VERIFICATION)


# -- sources --------------------------------------------------------------


class ProductSource(Protocol):
    """Yields raw Price List product entries."""

    def iter_products(
        self, service_code: str, region: str, product_family: str, group: str = ""
    ) -> Iterator[str]:
        ...


class Boto3ProductSource:
    """Reads from the AWS Price List Query API.

    The Price List endpoints live in a small number of regions, so this client
    is constructed against one of those regardless of which region's prices are
    being fetched. Region is a *filter*, not the endpoint.
    """

    def __init__(self, pricing_client, page_size: int = 100) -> None:
        self._client = pricing_client
        self._page_size = page_size

    def iter_products(
        self, service_code: str, region: str, product_family: str, group: str = ""
    ) -> Iterator[str]:
        # Most rules filter by productFamily to keep result sets small. Products
        # without a productFamily (public IPv4 addresses) are fetched by group.
        selector = (
            {"Type": "TERM_MATCH", "Field": "group", "Value": group}
            if group
            else {"Type": "TERM_MATCH", "Field": "productFamily", "Value": product_family}
        )
        filters = [
            {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
            selector,
        ]

        next_token: str | None = None
        while True:
            request = {
                "ServiceCode": service_code,
                "Filters": filters,
                "MaxResults": self._page_size,
            }
            if next_token:
                request["NextToken"] = next_token

            response = self._client.get_products(**request)
            yield from response.get("PriceList", [])

            next_token = response.get("NextToken")
            if not next_token:
                return


# -- orchestration --------------------------------------------------------

Sink = Callable[[PriceQuery, PriceRecord], None]


@dataclass
class SyncReport:
    """What a sync run did, and what it could not do."""

    products_seen: int = 0
    prices_written: int = 0
    unmatched: int = 0
    skipped: Counter = field(default_factory=Counter)
    rules_with_no_matches: list[str] = field(default_factory=list)
    collisions: list[str] = field(default_factory=list)
    version: str | None = None

    @property
    def healthy(self) -> bool:
        """A rule matching nothing means the cache is silently incomplete."""
        return (
            self.prices_written > 0
            and not self.rules_with_no_matches
            and not self.collisions
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "productsSeen": self.products_seen,
            "pricesWritten": self.prices_written,
            "unmatched": self.unmatched,
            "skipped": dict(self.skipped),
            "rulesWithNoMatches": self.rules_with_no_matches,
            "collisions": self.collisions[:20],
            "version": self.version,
            "healthy": self.healthy,
        }


class PriceSync:
    """Fetches, filters, and writes prices for one region."""

    def __init__(self, rules: Sequence[SyncRule] = SYNC_RULES) -> None:
        self._rules = tuple(rules)

    def run(self, source: ProductSource, region: str, sink: Sink) -> SyncReport:
        """Fetch every rule's products and write matching prices to ``sink``."""
        report = SyncReport()
        # key -> (sku, unit_price) of whatever was written there first.
        written: dict[str, tuple[str, Decimal]] = {}

        # Rules are grouped so each (service, fetch filter) pair is fetched once.
        # Fetching per rule would re-download the same products repeatedly.
        for (service_code, fetch_field, fetch_value), rules in self._grouped().items():
            matches_by_rule = {rule.name: 0 for rule in rules}
            product_family = fetch_value if fetch_field == "productFamily" else ""
            group = fetch_value if fetch_field == "group" else ""

            for raw in source.iter_products(service_code, region, product_family, group):
                report.products_seen += 1
                outcome = parse_product(raw)

                if isinstance(outcome, ParseSkip):
                    report.skipped[outcome.reason] += 1
                    continue

                if outcome.version and report.version is None:
                    report.version = outcome.version

                matched = False
                for rule in rules:
                    if not rule.matches(outcome):
                        continue

                    query = rule.build_query(outcome, region)
                    if query is None:
                        report.skipped[
                            f"{rule.name}: product missing a key attribute"
                        ] += 1
                        continue

                    matched = True
                    matches_by_rule[rule.name] += 1

                    # Two products claiming the same key at a DIFFERENT price
                    # means the filters are too loose and a real price is being
                    # overwritten. Same-price overwrites are harmless (the cached
                    # number is right either way), so only price differences are
                    # flagged — that is what "collision" is meant to catch.
                    previous = written.get(query.key)
                    if previous is not None and previous[1] != outcome.unit_price:
                        report.collisions.append(
                            f"{query.key} ({previous[0]} ${previous[1]} vs "
                            f"{outcome.sku} ${outcome.unit_price})"
                        )
                    written.setdefault(query.key, (outcome.sku, outcome.unit_price))

                    sink(
                        query,
                        PriceRecord(
                            unit_price=outcome.unit_price * rule.price_scale,
                            unit=outcome.unit,
                            price_list_version=outcome.version or "unknown",
                            sku=outcome.sku,
                        ),
                    )
                    report.prices_written += 1

                if not matched:
                    report.unmatched += 1

            report.rules_with_no_matches.extend(
                name for name, count in matches_by_rule.items() if count == 0
            )

        return report

    def _grouped(self) -> dict[tuple[str, str, str], list[SyncRule]]:
        grouped: dict[tuple[str, str, str], list[SyncRule]] = {}
        for rule in self._rules:
            field, value = rule.fetch_filter
            grouped.setdefault((rule.service_code, field, value), []).append(rule)
        return grouped


# -- verification ---------------------------------------------------------


class AttributeSource(Protocol):
    """Reads the attribute vocabulary a service actually publishes."""

    def attribute_names(self, service_code: str) -> frozenset[str]:
        ...

    def attribute_values(self, service_code: str, attribute: str) -> frozenset[str]:
        ...


class Boto3AttributeSource:
    """Reads service metadata from the Price List API."""

    def __init__(self, pricing_client) -> None:
        self._client = pricing_client

    def attribute_names(self, service_code: str) -> frozenset[str]:
        response = self._client.describe_services(ServiceCode=service_code)
        names: set[str] = set()
        for service in response.get("Services", []):
            names.update(service.get("AttributeNames", []))
        return frozenset(names)

    def attribute_values(self, service_code: str, attribute: str) -> frozenset[str]:
        values: set[str] = set()
        next_token: str | None = None
        while True:
            request = {"ServiceCode": service_code, "AttributeName": attribute}
            if next_token:
                request["NextToken"] = next_token
            response = self._client.get_attribute_values(**request)
            values.update(
                item["Value"]
                for item in response.get("AttributeValues", [])
                if "Value" in item
            )
            next_token = response.get("NextToken")
            if not next_token:
                return frozenset(values)


@dataclass
class RuleVerification:
    """Whether one rule's assumed attribute names actually exist."""

    rule: str
    service_code: str
    missing_attributes: tuple[str, ...] = ()
    unexpected_values: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.missing_attributes and not self.unexpected_values

    def to_dict(self) -> dict[str, object]:
        return {
            "rule": self.rule,
            "serviceCode": self.service_code,
            "ok": self.ok,
            "missingAttributes": list(self.missing_attributes),
            "unexpectedValues": self.unexpected_values,
        }


def verify_rules(
    source: AttributeSource, rules: Sequence[SyncRule] = SYNC_RULES
) -> list[RuleVerification]:
    """Check assumed attribute names and values against a live account.

    Turns the untestable guesses in :data:`SYNC_RULES` into a mechanical check.
    Run this before trusting any figure the sync produces.
    """
    results: list[RuleVerification] = []
    names_cache: dict[str, frozenset[str]] = {}
    values_cache: dict[tuple[str, str], frozenset[str]] = {}

    for rule in rules:
        if rule.service_code not in names_cache:
            names_cache[rule.service_code] = source.attribute_names(rule.service_code)
        published = names_cache[rule.service_code]

        missing = tuple(
            sorted(name for name in rule.aws_attributes_used if name not in published)
        )

        unexpected: dict[str, str] = {}
        for attribute, expected in rule.require.items():
            if attribute in missing:
                continue
            cache_key = (rule.service_code, attribute)
            if cache_key not in values_cache:
                values_cache[cache_key] = source.attribute_values(
                    rule.service_code, attribute
                )
            known = values_cache[cache_key]
            if known and expected not in known:
                unexpected[attribute] = (
                    f"{expected!r} not among published values "
                    f"(e.g. {sorted(known)[:3]})"
                )

        results.append(
            RuleVerification(
                rule=rule.name,
                service_code=rule.service_code,
                missing_attributes=missing,
                unexpected_values=unexpected,
            )
        )

    return results
