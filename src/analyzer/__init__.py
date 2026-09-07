"""Analyzer.

Step 5 of the build order. One terminal CloudFormation event in, one cost report
out.

Typical use::

    from analyzer import Analyzer, Boto3CloudFormationReader, parse_stack_event

    analyzer = Analyzer(
        reader=Boto3CloudFormationReader(boto3.client("cloudformation")),
        catalog=DynamoPriceCatalog(price_table),
        store=DynamoStateStore(state_table),
        config=AnalyzerConfig(discount_percent=Decimal(0)),
    )

    outcome = analyzer.analyze(parse_stack_event(event))
    if outcome.reported:
        publish(outcome.report)

The analyzer is read-only with respect to infrastructure. It writes only to its
own state table.
"""

from .analyzer import AnalysisOutcome, Analyzer, AnalyzerConfig
from .cfn import (
    ABSENT_STATUSES,
    NESTED_STACK_TYPE,
    PENDING_CHANGE_SET_STATUSES,
    Boto3CloudFormationReader,
    ChangeSetDescription,
    CloudFormationReader,
    CloudFormationReadError,
    StackDescription,
    StackResourceSummary,
)
from .changeset import (
    CHANGE_SET_EVENT_NAMES,
    CLOUDTRAIL_DETAIL_TYPE,
    ChangeSetEvent,
    ChangeSetNotReady,
    parse_change_set_event,
)
from .events import (
    STACK_STATUS_CHANGE,
    TERMINAL_STATUSES,
    NotActionable,
    StackEvent,
    parse_stack_event,
    stack_name_from_id,
)
from .report import (
    SCHEMA_VERSION,
    ReportPhase,
    build_report,
    console_url,
    message_attributes,
    render_email_body,
    render_email_html,
    subject_line,
)

__all__ = [
    "ABSENT_STATUSES",
    "CHANGE_SET_EVENT_NAMES",
    "CLOUDTRAIL_DETAIL_TYPE",
    "NESTED_STACK_TYPE",
    "PENDING_CHANGE_SET_STATUSES",
    "SCHEMA_VERSION",
    "STACK_STATUS_CHANGE",
    "TERMINAL_STATUSES",
    "AnalysisOutcome",
    "Analyzer",
    "AnalyzerConfig",
    "Boto3CloudFormationReader",
    "ChangeSetDescription",
    "ChangeSetEvent",
    "ChangeSetNotReady",
    "CloudFormationReadError",
    "CloudFormationReader",
    "NotActionable",
    "ReportPhase",
    "StackDescription",
    "StackEvent",
    "StackResourceSummary",
    "build_report",
    "console_url",
    "message_attributes",
    "parse_change_set_event",
    "parse_stack_event",
    "render_email_body",
    "render_email_html",
    "stack_name_from_id",
    "subject_line",
]
