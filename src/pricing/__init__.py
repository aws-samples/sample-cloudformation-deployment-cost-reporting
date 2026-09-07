"""Pricing core.

Step 2 of the build order. Turns resolved resources into monthly cost figures.

Typical use::

    from pricing import PricingEngine, StaticPriceCatalog

    engine = PricingEngine(catalog, region="us-east-1", discount_percent=0)
    inventory = engine.price_inventory(resolver.resolve_resources())

    print(inventory.monthly_cost, inventory.coverage.priced_percent)

Design rules enforced here, not left to callers:

* Never invent a number. A missing price yields ``Confidence.UNAVAILABLE`` and
  is excluded from totals rather than guessed at.
* Never silently drop a resource. Everything comes back classified, including
  free and unsupported types.
* Coverage is judged per pricing dimension, not per property. A NAT Gateway with
  no resolvable properties is still fully priceable.
"""

from .catalog import PriceCatalog, StaticPriceCatalog
from .dimensions import Classification, ComponentSpec, classify, supported_types
from .dynamo_catalog import (
    METADATA_KEY,
    DynamoPriceCatalog,
    DynamoPriceWriter,
    PriceCatalogError,
)
from .engine import PricedInventory, PricingBasis, PricingEngine
from .models import (
    CURRENCY,
    DEFAULT_OPERATING_SYSTEM,
    DEFAULT_TENANCY,
    HOURS_PER_MONTH,
    RATE_TYPE,
    Confidence,
    CostComponent,
    Coverage,
    PricedResource,
    PriceQuery,
    PriceRecord,
    PricingClass,
    apply_discount,
    coverage_of,
    money,
    weakest,
)
from .platform import (
    ASSUMED_LINUX,
    PLATFORM_DETAILS,
    AssumedPlatformResolver,
    Ec2PlatformResolver,
    Platform,
    PlatformResolver,
    platform_from_details,
)
from .pricelist import ParsedProduct, ParseSkip, parse_product
from .sync import (
    SYNC_RULES,
    AttributeSource,
    Boto3AttributeSource,
    Boto3ProductSource,
    PriceSync,
    ProductSource,
    RuleStatus,
    RuleVerification,
    SyncReport,
    SyncRule,
    rules_needing_verification,
    verify_rules,
)

__all__ = [
    "ASSUMED_LINUX",
    "CURRENCY",
    "DEFAULT_OPERATING_SYSTEM",
    "DEFAULT_TENANCY",
    "HOURS_PER_MONTH",
    "METADATA_KEY",
    "PLATFORM_DETAILS",
    "RATE_TYPE",
    "SYNC_RULES",
    "AssumedPlatformResolver",
    "AttributeSource",
    "Boto3AttributeSource",
    "Boto3ProductSource",
    "Classification",
    "ComponentSpec",
    "Confidence",
    "CostComponent",
    "Coverage",
    "DynamoPriceCatalog",
    "DynamoPriceWriter",
    "Ec2PlatformResolver",
    "ParseSkip",
    "ParsedProduct",
    "Platform",
    "PlatformResolver",
    "PriceCatalog",
    "PriceCatalogError",
    "PriceQuery",
    "PriceRecord",
    "PriceSync",
    "PricedInventory",
    "PricedResource",
    "PricingBasis",
    "PricingClass",
    "PricingEngine",
    "ProductSource",
    "RuleStatus",
    "RuleVerification",
    "StaticPriceCatalog",
    "SyncReport",
    "SyncRule",
    "apply_discount",
    "classify",
    "coverage_of",
    "money",
    "parse_product",
    "platform_from_details",
    "rules_needing_verification",
    "supported_types",
    "verify_rules",
    "weakest",
]
