# Security Policy

## Reporting a vulnerability

Do not disclose security vulnerabilities through a public GitHub issue.

Report potential vulnerabilities through the [AWS vulnerability reporting page](https://aws.amazon.com/security/vulnerability-reporting/). Include the affected commit or version, impact, reproduction steps, and any suggested mitigation. Do not include live credentials, customer data, or account identifiers.

## Supported versions

This repository is sample and educational code. Security updates apply to the latest commit on `main`; older commits and forks are not supported.

## Security model

The plugin observes AWS CloudFormation deployments and reports estimated cost changes. Its analyzer can read CloudFormation stack data but has no CloudFormation mutation actions. Permissions are split by function and scoped to the plugin's own state, queues, topics, metrics, delivery destinations, and configured secrets/identities.

The solution uses:

- AWS CloudFormation and AWS SAM
- Amazon EventBridge and Amazon SQS
- AWS Lambda
- Amazon DynamoDB
- Amazon SNS and optional Amazon SES
- Amazon CloudWatch
- AWS Secrets Manager for the optional Slack token
- AWS CloudTrail for optional pre-deployment estimates
- AWS Price List API

No credentials are stored in source code. The Slack token is retrieved from Secrets Manager and is not logged. User-controlled YAML is parsed with a restricted `SafeLoader` subclass.

## Accepted security debt for this sample

These choices are intentional for derived, reconstructible cost metadata and are not approval for every production environment:

1. DynamoDB and SQS use AWS-managed encryption rather than a customer-managed KMS key.
2. DynamoDB point-in-time recovery is not enabled. Losing state causes re-baselining; the price cache is rebuilt.
3. Read-only APIs that do not support resource-level permissions retain `Resource: "*"`, including `pricing:GetProducts`, `ec2:DescribeImages`, `cloudtrail:DescribeTrails`, `cloudtrail:GetEventSelectors`, and `cloudwatch:PutMetricData`. `PutMetricData` is restricted by namespace.
4. Lambda-generated CloudWatch log groups have an external lifecycle unless explicit log-group resources are added by the deployer.

`ReportTopic` uses the AWS-managed SNS KMS key. `AlarmTopic` remains unencrypted in this sample because CloudWatch alarms cannot use the immutable default `alias/aws/sns` key policy. CloudFormation reads are scoped to stack ARNs in the deployment account and Region.

## Production-hardening recommendations

Before production deployment:

- Perform an independent threat model and security review for your environment.
- Replace AWS-managed encryption with a customer-managed KMS key when key rotation, cross-account controls, or key-use auditing are required. Grant the relevant AWS services access to that key.
- Encrypt `AlarmTopic` with a customer-managed symmetric KMS key whose policy grants `cloudwatch.amazonaws.com` `kms:Decrypt` and `kms:GenerateDataKey`. Do not use `alias/aws/sns` for this topic; its AWS-managed policy cannot be edited for CloudWatch alarms.
- Consider DynamoDB point-in-time recovery for `StateTable` if continuous report history matters.
- Define explicit CloudWatch log groups with required retention and encryption.
- Verify the optional Slack secret uses an approved KMS key and restrict secret administration.
- Use a domain whose DNS owners can configure SES DKIM, SPF, and DMARC. Verifying an individual email address does not grant authority over its parent domain.
- Validate CloudTrail selectors when enabling pre-deployment estimates.
- Review the central EventBridge bus policy for cross-account deployments.
- Configure termination protection, backup, retention, and incident-response controls according to your organization’s requirements.
- Run dependency, secret, static-analysis, infrastructure-as-code, and dynamic deployment scans in CI.
- Re-run the full test and validation suite after changing IAM, encryption, services, dependencies, or delivery behavior.

## Data classification

Reports contain AWS account/Region identifiers, stack names, logical resource IDs, resource types, tags, and estimated cost data. They are operational metadata, not secrets, but can reveal architecture and business context. Restrict SNS, SES, Slack, EventBridge, CloudWatch Logs, and report recipients accordingly.
