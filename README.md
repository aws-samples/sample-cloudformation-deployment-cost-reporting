# CloudFormation Cost Delta Plugin

> **Disclaimer**: This project is sample/educational code and is **not intended
> for production use** without additional security hardening and testing. See
> [SECURITY.md](SECURITY.md) for production-hardening recommendations.

Reports the effective monthly cost of resources that each CloudFormation deployment adds, removes, resizes, replaces, or retains. Reports can be delivered by plain SNS email, formatted SES email, Slack, and an optional central EventBridge bus.

The plugin cannot create, update, delete, or execute changes against monitored CloudFormation stacks or customer resources. It writes only its own cache/state tables, queues, topics, metrics, and delivery integrations.

## What it does

For each supported terminal CloudFormation status, the plugin:

1. Reads the processed template, stack parameters, tags, and actual physical-resource inventory.
2. Resolves values that are derivable from the template without guessing runtime attributes.
3. Prices deterministic resources from an atomically activated DynamoDB price generation.
4. Compares the result with the last confirmed snapshot for that exact stack ID.
5. Publishes one canonical report containing effective (discount-adjusted) monthly totals, itemized movements, retained resources, coverage gaps, and reconciliation status.
6. Fans that immutable report out to configured destinations.

The first observation of a pre-existing stack is a baseline, not a fabricated increase. The plugin’s own stack ARN is excluded exactly, so installing or updating the plugin does not generate a report about itself.

## Architecture

```text
CloudFormation terminal event ─► EventBridge (Path A) ─┐
                                                       ├─► SQS ─► Analyzer
CloudTrail CreateChangeSet ────► EventBridge (Path B) ─┘            │
                                                                    ▼
                                                         SNS report topic
                                                        ┌─────┼──────┐
                                                        ▼     ▼      ▼
                                                     email  Slack  optional
                                                                  central bus

CloudWatch alarms ─► separate SNS alarm topic ─► email / operator subscriptions

Install + EventBridge Scheduler ─► Price Sync ─► immutable DynamoDB generation
```

### Path A: confirmed reports

Path A reacts to supported stable stack statuses. SQS provides buffering, partial-batch retries, a processing lease, and an analyzer DLQ. The analyzer commits a new snapshot only after SNS accepts the report, so a publication retry recomputes against the original baseline rather than replacing the delta with zero.

### Path B: estimates

Path B reacts to CloudTrail `CreateChangeSet` events. Estimates are marked `ESTIMATE` and never persisted. A pending change set is retried without being counted as an analysis failure. Nested child templates proposed inside a parent change set are not exposed by CloudFormation; the report names that as an explicit coverage gap instead of silently treating the child as free.

### Price cache

Installation performs a synchronous bootstrap before event rules are enabled. Scheduled refreshes write an immutable generation and activate it only when every configured Region is healthy. Active and prior generations are retained so persistent metadata never points at expired prices. Warm analyzer containers check generation metadata and invalidate cached records and misses when activation changes.

## Repository layout

```text
template.yaml            SAM infrastructure, conditions, IAM, queues, alarms
pyproject.toml           Python and validation-tool configuration
src/analyzer/            CloudFormation orchestration and report rendering
src/pricing/             Price List sync, cache, classification, and engine
src/resolver/            CloudFormation template/intrinsic resolution
src/runtime/             Lambda handlers and SNS/SES/Slack delivery
src/state/               Snapshots, diff/reconciliation, persistence
scripts/                 Read-only pricing and account-coverage checks
tests/                   Unit and in-process integration tests
```

## Local validation

Python 3.12 or later is required locally; deployed functions use Python 3.13 on arm64.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

pytest
ruff check src tests scripts
mypy
cfn-lint template.yaml
sam validate --lint --template-file template.yaml
```

`sam validate --lint` is required in addition to cfn-lint because SAM event-transform errors are not necessarily detected by standalone cfn-lint.

## Deploy

```bash
sam build
sam deploy --guided
```

Important parameters:

| Parameter | Purpose |
|---|---|
| `NotificationEmail` | Recipient for reports and operational alarms. Blank disables email. |
| `SesSenderEmail` | Enables formatted SES HTML+text delivery when set with `NotificationEmail`. Blank uses plain SNS email. |
| `SesIdentityArn` | Optional verified SES email/domain identity ARN. Required when authorization comes from a verified domain rather than the exact sender address. |
| `DiscountPercent` | Flat effective discount applied consistently to resource rows, deltas, totals, thresholds, and metrics. |
| `EnablePlatformLookup` | Reads AMI platform details with `ec2:DescribeImages`; otherwise Linux is stated as an assumption. |
| `PriceSyncSchedule` / `SyncRegions` | Refresh cadence and target pricing Regions. Installation always performs the initial bootstrap. |
| `SlackSecretArn` / `SlackChannelId` | Both are required to enable Slack. |
| `NestedStackHandling` | `ROLLUP` aggregates current child stacks; `SEPARATE` reports each child independently. |
| `EnablePreDeployEstimate` | Enables CloudTrail-backed change-set estimates. |
| `DestinationType` / `CentralBusArn` | `CENTRAL_BUS` additionally forwards reports; the ARN is required by a template rule. |
| `EnableMetrics` | Enables detailed and alarm-compatible aggregate metrics. |

### Plain SNS email

Set `NotificationEmail` and leave `SesSenderEmail` blank. CloudFormation creates two SNS email subscriptions—one for reports and one for native operational alarms. Both confirmation messages must be accepted.

### Formatted SES email

Set `NotificationEmail` and `SesSenderEmail`. The plain report/alarm subscriptions are omitted; one Lambda consumes both separated topics and sends report-specific or alarm-specific HTML with a plain-text alternative.

SES prerequisites:

- Verify the sender identity in the deployment Region.
- In the SES sandbox, verify the recipient too.
- If a domain identity authorizes the sender, pass its ARN as `SesIdentityArn`.
- Use a domain whose DNS owners can configure DKIM/SPF/DMARC. Verifying one `@amazon.com` address in SES does not authorize an AWS account to authenticate the `amazon.com` domain; corporate gateways can therefore add an external/unverified-sender warning. Application HTML cannot remove that warning.

The IAM policy constrains the verified identity resource and exact From address. The recipient is read from the deployment configuration and used only by the email Lambda.

## Slack (optional)

Slack requires a bot token with `chat:write`, stored in Secrets Manager, and the channel ID. The bot must be a member of the destination channel.

```bash
aws secretsmanager create-secret \
  --name cfn-cost-delta/slack \
  --secret-string "$SLACK_BOT_TOKEN"

sam deploy --parameter-overrides \
  SlackSecretArn=<secret-arn> SlackChannelId=<channel-id>
```

Estimates are edited in place when the confirmed report arrives. Retryable Slack failures (rate limits, service errors, network timeouts) are retried by Lambda; permanent configuration errors are logged. Exhausted Slack/SES invocations land in the delivery DLQ.

## Reliability model

- Analyzer messages use partial-batch SQS failure responses.
- Idempotency distinguishes `PROCESSING` leases from `COMPLETED` operations. An expired lease can be taken over after a hard Lambda termination; completed rows remain for 15 days.
- Confirmed snapshots are committed only after SNS accepts the canonical report.
- Report IDs are deterministic per operation, making retries identifiable.
- Transient or incomplete CloudFormation, snapshot, and price-cache reads fail the message and retry; partial inventories are never persisted.
- Older terminal events are ignored when a newer event timestamp has already advanced the snapshot.
- Price generation activation is atomic; unhealthy syncs leave the previous generation active.
- Operational alarms use a separate topic and native alarm rendering.

Two DLQs are provided: one for analyzer events and one for Slack/SES asynchronous delivery.

## Built-in alarms

Four alarms are created:

1. Aggregate analysis failures (when metrics are enabled).
2. Aggregate report reconciliation failures (when metrics are enabled).
3. Analyzer event DLQ depth.
4. Subscriber delivery DLQ depth.

Alarms publish to the separate alarm topic, never to the canonical report topic.

## Pricing scope

Deterministically mapped resources include EC2 instance compute, explicit EBS mappings/volumes, non-Aurora RDS instances and mapped storage, NAT Gateway hourly charges, load-balancer hourly charges, ElastiCache nodes, public IPv4 addresses, and DynamoDB provisioned table plus GSI capacity.

Usage-based resources such as S3, Lambda, API Gateway, SQS, SNS, CloudWatch Logs, SES, Kinesis, and EFS are named but not assigned invented monthly figures.

Known exclusions are shown in every report. Examples include:

- AMI-provided EC2 storage not explicitly declared in `BlockDeviceMappings`.
- NAT data processing and load-balancer capacity units.
- Tiered io2 IOPS.
- RDS gp3 IOPS/throughput where no validated sync mapping exists.
- Aurora cluster storage-mode-dependent pricing.
- Private CA certificates and advanced SSM parameters until mapped.
- Proposed nested-child contents that CloudFormation does not expose through the parent change set.

Free scaffolding is excluded from the coverage denominator. Unknown types and unresolved values remain visible in coverage and named gap sections.

## Important limitations

- Figures are marginal On-Demand estimates, not invoices.
- A flat discount cannot reproduce every Savings Plan, Reserved Instance, credit, or tiered agreement.
- Current-stack roll-up is authoritative only when every child can be read successfully.
- Classic Outlook can ignore cosmetic CSS such as shadows, rounded corners, or float placement; the table layout and text alternative preserve content.
- Sender-domain authentication is an SES/DNS prerequisite, not something this code can synthesize.

## Operational scripts

Both scripts are read-only and accept `--profile`:

```bash
python scripts/verify_pricing_rules.py --profile <aws-profile>
python scripts/scan_account_coverage.py --profile <aws-profile>
```

## Lifecycle

Price, snapshot, idempotency, Slack-correlation, queue, topic, and function resources are owned by the plugin stack. Snapshot rows for deleted monitored stacks remain until `SnapshotRetentionDays`; the StateTable itself is deleted when the plugin stack is deleted. Lambda-created log groups and externally supplied Secrets Manager, SES identity, DNS, and central-bus resources are external lifecycle concerns.
