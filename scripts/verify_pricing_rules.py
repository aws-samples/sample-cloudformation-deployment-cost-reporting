#!/usr/bin/env python3
"""Check the Price List sync rules against a real AWS account.

Every rule in ``SYNC_RULES`` is currently marked NEEDS_VERIFICATION, because
AWS's per-service product attribute names are discoverable only at runtime and
are not published as a fixed list. Until this script has been run, the pricing
layer is built on assumptions.

Two checks, in order of severity:

1. **Attribute names.** Do the attributes each rule filters and keys on actually
   exist for that service? A missing name means the rule can never match.
2. **Live match counts.** Fetch real products and report how many each rule
   matched, plus any key collisions. A rule matching zero products means a silent
   gap in the cache; a collision means the filters are too loose and one price is
   overwriting another.

Read-only. Uses ``pricing:DescribeServices``, ``pricing:GetAttributeValues``, and
``pricing:GetProducts``. It creates nothing, changes nothing, and writes nothing.

Usage::

    python scripts/verify_pricing_rules.py
    python scripts/verify_pricing_rules.py --region eu-west-1 --profile my-readonly
    python scripts/verify_pricing_rules.py --names-only

Note the two different regions. ``--endpoint-region`` is where the Price List API
itself is reachable; ``--region`` is the region whose prices you want. They are
independent.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pricing import (
    SYNC_RULES,
    Boto3AttributeSource,
    Boto3ProductSource,
    PriceSync,
    StaticPriceCatalog,
    verify_rules,
)

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
DIM = "\033[2m"
RESET = "\033[0m"


def mark(ok: bool) -> str:
    return f"{GREEN}ok{RESET}" if ok else f"{RED}FAIL{RESET}"


def check_attribute_names(client) -> int:
    print("\n=== 1. Attribute names ===\n")
    results = verify_rules(Boto3AttributeSource(client))
    failures = 0

    for result in results:
        print(f"[{mark(result.ok)}] {result.rule}  {DIM}({result.service_code}){RESET}")
        if result.missing_attributes:
            failures += 1
            print(
                f"        {RED}attributes not published:{RESET} "
                f"{', '.join(result.missing_attributes)}"
            )
        for attribute, detail in result.unexpected_values.items():
            failures += 1
            print(f"        {YELLOW}{attribute}:{RESET} {detail}")

    print(f"\n{len(results) - failures}/{len(results)} rules have valid attributes.")
    return failures


def check_live_matches(client, region: str) -> int:
    print(f"\n=== 2. Live match counts ({region}) ===\n")
    print(f"{DIM}Fetching products. This makes several paginated calls.{RESET}\n")

    catalog = StaticPriceCatalog(version="verification")
    report = PriceSync().run(
        Boto3ProductSource(client),
        region,
        lambda query, record: catalog.put(
            query, record.unit_price, record.unit, record.sku
        ),
    )

    print(f"products seen    {report.products_seen}")
    print(f"prices written   {report.prices_written}")
    print(f"unmatched        {report.unmatched}")
    print(f"price list ver.  {report.version}")

    problems = 0

    if report.rules_with_no_matches:
        problems += len(report.rules_with_no_matches)
        print(f"\n{RED}Rules that matched nothing (silent gaps in the cache):{RESET}")
        for name in report.rules_with_no_matches:
            print(f"  - {name}")

    if report.collisions:
        problems += 1
        print(
            f"\n{YELLOW}Key collisions (filters too loose; one price overwrote "
            f"another):{RESET}"
        )
        for collision in report.collisions[:10]:
            print(f"  - {collision}")

    if report.skipped:
        print(f"\n{DIM}Skipped products by reason:{RESET}")
        for reason, count in report.skipped.most_common(8):
            print(f"  {count:>6}  {reason}")

    print(f"\nhealthy: {mark(report.healthy)}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--region",
        default="us-east-1",
        help="Region whose prices to verify (default: us-east-1)",
    )
    parser.add_argument(
        "--endpoint-region",
        default="us-east-1",
        help="Region where the Price List API is reachable (default: us-east-1)",
    )
    parser.add_argument("--profile", help="AWS profile to use")
    parser.add_argument(
        "--names-only",
        action="store_true",
        help="Skip the live product fetch and only check attribute names",
    )
    args = parser.parse_args()

    try:
        import boto3
    except ImportError:
        print("boto3 is not installed. Try: pip install boto3", file=sys.stderr)
        return 2

    session = (
        boto3.Session(profile_name=args.profile) if args.profile else boto3.Session()
    )
    client = session.client("pricing", region_name=args.endpoint_region)

    print(f"Verifying {len(SYNC_RULES)} sync rules")
    print(f"{DIM}endpoint {args.endpoint_region} · prices for {args.region}{RESET}")

    try:
        problems = check_attribute_names(client)
        if not args.names_only:
            problems += check_live_matches(client, args.region)
    except Exception as exc:
        print(f"\n{RED}Verification could not complete:{RESET} {exc}", file=sys.stderr)
        print(
            f"{DIM}Check credentials and that the profile can call the Price List "
            f"API.{RESET}",
            file=sys.stderr,
        )
        return 2

    if problems:
        print(
            f"\n{RED}{problems} problem(s) found.{RESET} Each one is a rule in "
            f"src/pricing/sync.py that needs its attribute names corrected."
        )
        return 1

    print(
        f"\n{GREEN}All rules verified.{RESET} They can be marked "
        f"RuleStatus.CONFIRMED in src/pricing/sync.py."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
