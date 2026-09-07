#!/usr/bin/env python3
"""Report what this plugin would and would not price in a real account.

Answers the question "are our stacks EC2-era or container-era" with data instead
of recollection, and produces a prioritised backlog of the resource types worth
adding a mapper for next.

Walks the account's CloudFormation stacks, resolves each template, classifies
every resource, and reports:

* coverage, as the plugin would compute it on the next deployment
* which unsupported types appear most often, ranked
* which resources have a pricing-relevant property that would not resolve

Read-only. Uses ``cloudformation:ListStacks``, ``DescribeStacks``,
``GetTemplate``, and ``ListStackResources`` — the same set the plugin itself
needs. It creates nothing, changes nothing, and writes nothing.

Usage::

    python scripts/scan_account_coverage.py
    python scripts/scan_account_coverage.py --profile my-readonly --region eu-west-1
    python scripts/scan_account_coverage.py --max-stacks 200

Note on cost and throttling: this makes roughly three API calls per stack. The
default caps at 50 stacks; raise it deliberately.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from analyzer import Boto3CloudFormationReader
from pricing import PricingClass, classify
from resolver import PseudoContext, TemplateResolver

BOLD = "\033[1m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
DIM = "\033[2m"
RESET = "\033[0m"

#: Stack statuses worth scanning. Deleted and failed stacks say nothing useful
#: about what the account currently runs.
LIVE_STATUSES = [
    "CREATE_COMPLETE",
    "UPDATE_COMPLETE",
    "UPDATE_ROLLBACK_COMPLETE",
    "IMPORT_COMPLETE",
    "IMPORT_ROLLBACK_COMPLETE",
]


def list_stacks(client, max_stacks: int) -> list[str]:
    """Root stack names, newest first. Nested children are skipped."""
    names: list[str] = []
    next_token: str | None = None

    while len(names) < max_stacks:
        request = {"StackStatusFilter": LIVE_STATUSES}
        if next_token:
            request["NextToken"] = next_token
        response = client.list_stacks(**request)

        for summary in response.get("StackSummaries", []):
            # Children are counted through their root, so scanning them
            # separately would double-count every nested resource.
            if summary.get("ParentId"):
                continue
            names.append(summary["StackName"])
            if len(names) >= max_stacks:
                break

        next_token = response.get("NextToken")
        if not next_token:
            break

    return names


def scan(client, region: str, max_stacks: int) -> int:
    reader = Boto3CloudFormationReader(client)

    print(f"{DIM}Listing stacks...{RESET}")
    stacks = list_stacks(client, max_stacks)
    if not stacks:
        print(f"{YELLOW}No live stacks found in this account and region.{RESET}")
        return 0

    print(f"Scanning {len(stacks)} stack(s) in {region}\n")

    by_class: Counter[str] = Counter()
    unsupported: Counter[str] = Counter()
    usage_based: Counter[str] = Counter()
    priced: Counter[str] = Counter()
    unresolved: Counter[str] = Counter()
    stacks_scanned = 0
    stacks_failed: list[str] = []
    total_resources = 0

    for name in stacks:
        description = reader.describe_stack(name)
        template = reader.get_template(name)

        if description is None or template is None:
            stacks_failed.append(name)
            continue

        try:
            resolver = TemplateResolver(
                template,
                description.parameters,
                PseudoContext.from_stack_id(description.stack_id),
            )
            resources = resolver.resolve_resources()
        except Exception as exc:
            print(f"  {YELLOW}skipped {name}: {exc}{RESET}")
            stacks_failed.append(name)
            continue

        stacks_scanned += 1

        for resource in resources:
            if not resource.is_priceable:
                continue
            total_resources += 1

            classification = classify(resource, region)
            bucket = classification.pricing_class
            by_class[bucket.value] += 1

            if bucket is PricingClass.UNSUPPORTED:
                unsupported[resource.resource_type] += 1
            elif bucket is PricingClass.USAGE_BASED:
                usage_based[resource.resource_type] += 1
            elif bucket is PricingClass.UNRESOLVED:
                unresolved[resource.resource_type] += 1
            elif bucket is PricingClass.DETERMINISTIC:
                priced[resource.resource_type] += 1

        print(f"  {DIM}{name}: {len(resources)} resources{RESET}")

    return report(
        stacks_scanned,
        stacks_failed,
        total_resources,
        by_class,
        priced,
        usage_based,
        unsupported,
        unresolved,
    )


def report(
    stacks_scanned: int,
    stacks_failed: list[str],
    total_resources: int,
    by_class: Counter[str],
    priced: Counter[str],
    usage_based: Counter[str],
    unsupported: Counter[str],
    unresolved: Counter[str],
) -> int:
    free = by_class.get("FREE", 0)
    priced_count = by_class.get("DETERMINISTIC", 0)
    usage_count = by_class.get("USAGE_BASED", 0)
    unsupported_count = by_class.get("UNSUPPORTED", 0)
    unresolved_count = by_class.get("UNRESOLVED", 0)

    # Free resources are excluded from the denominator, exactly as the plugin
    # does, so this number matches what a real report would say.
    chargeable = priced_count + usage_count + unsupported_count + unresolved_count
    coverage = (priced_count / chargeable * 100) if chargeable else 100.0

    print(f"\n{BOLD}=== Coverage ==={RESET}\n")
    print(f"stacks scanned      {stacks_scanned}")
    if stacks_failed:
        print(f"stacks unreadable   {len(stacks_failed)}")
    print(f"resources           {total_resources}")
    print()
    print(f"{GREEN}priced{RESET}              {priced_count}")
    print(f"{YELLOW}usage-based{RESET}         {usage_count}   {DIM}(never estimated){RESET}")
    print(f"{RED}unsupported{RESET}         {unsupported_count}   {DIM}(no mapper yet){RESET}")
    print(
        f"{RED}unresolved{RESET}          {unresolved_count}   "
        f"{DIM}(property would not resolve){RESET}"
    )
    print(f"{DIM}free                {free}   (excluded from coverage){RESET}")

    colour = GREEN if coverage >= 75 else YELLOW if coverage >= 50 else RED
    print(f"\n{BOLD}coverage{RESET}            {colour}{coverage:.1f}%{RESET}")
    print(f"{DIM}{priced_count} of {chargeable} chargeable resources would be priced.{RESET}")

    if unsupported:
        print(f"\n{BOLD}=== Mapper backlog, by frequency ==={RESET}\n")
        print(f"{DIM}Each of these is currently reported as unpriced.{RESET}\n")
        for resource_type, count in unsupported.most_common(20):
            share = count / chargeable * 100 if chargeable else 0
            print(f"  {count:>5}  ({share:>4.1f}%)  {resource_type}")

    if unresolved:
        print(f"\n{BOLD}=== Types with unresolvable pricing properties ==={RESET}\n")
        for resource_type, count in unresolved.most_common(10):
            print(f"  {count:>5}  {resource_type}")

    if priced:
        print(f"\n{BOLD}=== Already priced ==={RESET}\n")
        for resource_type, count in priced.most_common(15):
            print(f"  {count:>5}  {resource_type}")

    print()
    if coverage >= 75:
        print(f"{GREEN}The mapped resource types fit this account well.{RESET}")
    elif unsupported:
        top = unsupported.most_common(3)
        names = ", ".join(t for t, _ in top)
        print(
            f"{YELLOW}Adding mappers for {names} would move coverage the most.{RESET}"
        )

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default="us-east-1", help="Region to scan")
    parser.add_argument("--profile", help="AWS profile to use")
    parser.add_argument(
        "--max-stacks",
        type=int,
        default=50,
        help="Maximum stacks to scan (default 50; ~3 API calls each)",
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
    client = session.client("cloudformation", region_name=args.region)

    try:
        return scan(client, args.region, args.max_stacks)
    except Exception as exc:
        print(f"\n{RED}Scan could not complete:{RESET} {exc}", file=sys.stderr)
        print(
            f"{DIM}Check credentials and that the profile can read "
            f"CloudFormation.{RESET}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
