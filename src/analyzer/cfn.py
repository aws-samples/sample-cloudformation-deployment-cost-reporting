"""CloudFormation read access.

A narrow, read-only seam over the three calls the analyzer needs, so the
orchestration can be tested without AWS credentials and so the IAM surface stays
obviously minimal (S28).

Read failures are explicit. Only an authoritative "stack does not exist"
response becomes ``None``; throttles, access failures, malformed templates, and
partial pagination raise :class:`CloudFormationReadError` so SQS retries instead
of acknowledging or persisting incomplete state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from resolver import TemplateParseError, load_template

#: Resource statuses meaning the resource does not exist.
#:
#: This matters after a rollback: the template still declares every resource, but
#: the failed ones were removed. Pricing the template alone would report cost for
#: infrastructure that was never successfully created.
#:
#: ``DELETE_SKIPPED`` is deliberately absent. It means CloudFormation left the
#: resource alone because of a ``Retain`` deletion policy, so it still exists and
#: still bills.
ABSENT_STATUSES = frozenset(
    {
        "DELETE_COMPLETE",
        "DELETE_IN_PROGRESS",
        "CREATE_FAILED",
    }
)

NESTED_STACK_TYPE = "AWS::CloudFormation::Stack"


class CloudFormationReadError(RuntimeError):
    """A CloudFormation read was incomplete or failed and should be retried."""


def _is_stack_not_found(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    error = response.get("Error") or {}
    message = str(error.get("Message") or "").lower()
    return error.get("Code") == "ValidationError" and "does not exist" in message


@dataclass(frozen=True)
class StackResourceSummary:
    logical_id: str
    physical_id: str | None
    resource_type: str
    status: str

    @property
    def exists(self) -> bool:
        return self.status not in ABSENT_STATUSES

    @property
    def is_nested_stack(self) -> bool:
        return self.resource_type == NESTED_STACK_TYPE


@dataclass(frozen=True)
class StackDescription:
    stack_id: str
    stack_name: str
    status: str
    parameters: dict[str, str] = field(default_factory=dict)
    tags: dict[str, str] = field(default_factory=dict)
    root_id: str | None = None
    parent_id: str | None = None

    @property
    def is_nested(self) -> bool:
        return bool(self.parent_id)


#: Change set statuses meaning the change set is still being computed.
#:
#: ``CreateChangeSet`` returns before the change set exists, so the CloudTrail
#: event routinely arrives too early. These statuses mean "come back later"
#: rather than "something is wrong".
PENDING_CHANGE_SET_STATUSES = frozenset({"CREATE_PENDING", "CREATE_IN_PROGRESS"})


@dataclass(frozen=True)
class ChangeSetDescription:
    change_set_id: str
    stack_id: str
    stack_name: str
    status: str
    execution_status: str
    status_reason: str = ""
    parameters: dict[str, str] = field(default_factory=dict)
    tags: dict[str, str] = field(default_factory=dict)
    replacements: frozenset[str] = frozenset()

    @property
    def is_pending(self) -> bool:
        return self.status in PENDING_CHANGE_SET_STATUSES

    @property
    def is_usable(self) -> bool:
        """Ready to price.

        A ``FAILED`` change set is usually CloudFormation reporting that the
        template produces no changes, which is not an error and not worth a
        report.
        """
        return self.status == "CREATE_COMPLETE"


class CloudFormationReader(Protocol):
    """The read-only surface the analyzer depends on."""

    def describe_stack(self, stack_id: str) -> StackDescription | None:
        ...

    def get_template(self, stack_id: str) -> dict[str, Any] | None:
        ...

    def list_resources(self, stack_id: str) -> list[StackResourceSummary]:
        ...

    def describe_change_set(self, change_set_id: str) -> ChangeSetDescription | None:
        ...

    def get_change_set_template(
        self, stack_id: str, change_set_id: str
    ) -> dict[str, Any] | None:
        ...


class Boto3CloudFormationReader:
    """Reads through a boto3 CloudFormation client."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def describe_stack(self, stack_id: str) -> StackDescription | None:
        try:
            response = self._client.describe_stacks(StackName=stack_id)
        except Exception as exc:
            if _is_stack_not_found(exc):
                return None
            raise CloudFormationReadError(
                f"Could not describe stack {stack_id}"
            ) from exc

        stacks = response.get("Stacks") or []
        if not stacks:
            return None
        stack = stacks[0]

        return StackDescription(
            stack_id=str(stack.get("StackId") or stack_id),
            stack_name=str(stack.get("StackName") or ""),
            status=str(stack.get("StackStatus") or ""),
            parameters={
                p["ParameterKey"]: p.get("ResolvedValue", p.get("ParameterValue"))
                for p in stack.get("Parameters") or []
                if "ParameterKey" in p
            },
            tags={
                t["Key"]: t.get("Value", "")
                for t in stack.get("Tags") or []
                if "Key" in t
            },
            root_id=stack.get("RootId"),
            parent_id=stack.get("ParentId"),
        )

    def get_template(self, stack_id: str) -> dict[str, Any] | None:
        """Fetch the processed template.

        ``TemplateStage="Processed"`` is mandatory (S2). Without it, SAM and
        macro-based templates come back unexpanded and most resources are
        invisible, which silently under-reports cost.
        """
        return self._fetch_template(StackName=stack_id, TemplateStage="Processed")

    def get_change_set_template(
        self, stack_id: str, change_set_id: str
    ) -> dict[str, Any] | None:
        """Fetch the template a change set would apply.

        This is what makes a pre-deploy estimate possible: the proposed template
        is readable before anything is provisioned, so it can be resolved, priced,
        and diffed against the stored snapshot exactly like a real deployment.
        """
        return self._fetch_template(
            StackName=stack_id,
            ChangeSetName=change_set_id,
            TemplateStage="Processed",
        )

    def _fetch_template(self, **request: Any) -> dict[str, Any] | None:
        try:
            response = self._client.get_template(**request)
        except Exception as exc:
            raise CloudFormationReadError("Could not fetch processed template") from exc

        body = response.get("TemplateBody")

        # boto3 decodes JSON templates into a dict but hands back YAML as a
        # string, so both shapes have to be accepted.
        if isinstance(body, dict):
            return body
        if isinstance(body, str):
            try:
                return load_template(body)
            except TemplateParseError as exc:
                raise CloudFormationReadError("Processed template could not be parsed") from exc
        raise CloudFormationReadError("CloudFormation returned no processed template body")

    def describe_change_set(self, change_set_id: str) -> ChangeSetDescription | None:
        try:
            response = self._client.describe_change_set(ChangeSetName=change_set_id)
        except Exception as exc:
            raise CloudFormationReadError(
                f"Could not describe change set {change_set_id}"
            ) from exc

        first_response = response
        replacements: set[str] = set()
        while True:
            for change in response.get("Changes") or []:
                resource_change = change.get("ResourceChange") or {}
                if resource_change.get("Replacement") in ("True", "Conditional"):
                    logical_id = resource_change.get("LogicalResourceId")
                    if logical_id:
                        replacements.add(str(logical_id))
            next_token = response.get("NextToken")
            if not next_token:
                break
            try:
                response = self._client.describe_change_set(
                    ChangeSetName=change_set_id, NextToken=next_token
                )
            except Exception as exc:
                raise CloudFormationReadError(
                    f"Could not completely describe change set {change_set_id}"
                ) from exc

        response = first_response
        stack_id = str(response.get("StackId") or "")
        if not stack_id:
            raise CloudFormationReadError(
                f"Change set {change_set_id} response contained no stack ID"
            )

        return ChangeSetDescription(
            change_set_id=str(response.get("ChangeSetId") or change_set_id),
            stack_id=stack_id,
            stack_name=str(response.get("StackName") or ""),
            status=str(response.get("Status") or ""),
            execution_status=str(response.get("ExecutionStatus") or ""),
            status_reason=str(response.get("StatusReason") or ""),
            parameters={
                p["ParameterKey"]: p.get("ResolvedValue", p.get("ParameterValue"))
                for p in response.get("Parameters") or []
                if "ParameterKey" in p
            },
            tags={
                t["Key"]: t.get("Value", "")
                for t in response.get("Tags") or []
                if "Key" in t
            },
            replacements=frozenset(replacements),
        )

    def list_resources(self, stack_id: str) -> list[StackResourceSummary]:
        summaries: list[StackResourceSummary] = []
        next_token: str | None = None

        while True:
            request: dict[str, Any] = {"StackName": stack_id}
            if next_token:
                request["NextToken"] = next_token
            try:
                response = self._client.list_stack_resources(**request)
            except Exception as exc:
                raise CloudFormationReadError(
                    f"Could not completely list resources for stack {stack_id}"
                ) from exc

            for item in response.get("StackResourceSummaries") or []:
                summaries.append(
                    StackResourceSummary(
                        logical_id=str(item.get("LogicalResourceId") or ""),
                        physical_id=item.get("PhysicalResourceId"),
                        resource_type=str(item.get("ResourceType") or ""),
                        status=str(item.get("ResourceStatus") or ""),
                    )
                )

            next_token = response.get("NextToken")
            if not next_token:
                return summaries
