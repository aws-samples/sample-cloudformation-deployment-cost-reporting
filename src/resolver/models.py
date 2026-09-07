"""Core types for CloudFormation template resolution.

Every resolved value carries provenance. When a cost figure looks wrong, the
`Resolved` record is what lets you trace it back to the expression and the
source that produced it, rather than guessing.

Nothing here ever invents a value. A value that cannot be derived from the
template is marked UNRESOLVED with a reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ResolutionStatus(str, Enum):
    RESOLVED = "RESOLVED"
    UNRESOLVED = "UNRESOLVED"


class ResolvedVia(str, Enum):
    """Where a resolved value came from."""

    LITERAL = "LITERAL"
    PARAMETER = "PARAMETER"
    PARAMETER_DEFAULT = "PARAMETER_DEFAULT"
    PSEUDO = "PSEUDO"
    MAPPING = "MAPPING"
    CONDITION = "CONDITION"
    FUNCTION = "FUNCTION"


class UnresolvedReason(str, Enum):
    """Why a value could not be derived from the template alone.

    These are reported, never guessed around. See S4 in the spec.
    """

    # Structurally impossible from a template
    RUNTIME_ATTRIBUTE = "RUNTIME_ATTRIBUTE"  # Fn::GetAtt
    CROSS_STACK_IMPORT = "CROSS_STACK_IMPORT"  # Fn::ImportValue
    RESOURCE_REFERENCE = "RESOURCE_REFERENCE"  # Ref to a resource, not a parameter
    ACCOUNT_SPECIFIC = "ACCOUNT_SPECIFIC"  # Fn::GetAZs

    # Missing or malformed inputs
    MISSING_PARAMETER = "MISSING_PARAMETER"
    MISSING_MAPPING = "MISSING_MAPPING"
    UNKNOWN_CONDITION = "UNKNOWN_CONDITION"
    CIRCULAR_CONDITION = "CIRCULAR_CONDITION"
    MALFORMED = "MALFORMED"
    UNSUPPORTED_FUNCTION = "UNSUPPORTED_FUNCTION"

    # A nested part of the expression failed
    NESTED_UNRESOLVED = "NESTED_UNRESOLVED"


class _NoValue:
    """Sentinel for ``Ref: AWS::NoValue``.

    CloudFormation drops the enclosing property entirely when a value resolves
    to this. Very common in ``Fn::If`` patterns like::

        MultiAZ: !If [IsProd, true, !Ref "AWS::NoValue"]

    Treating it as ``None`` would be wrong: ``None`` means "present but null",
    NoValue means "not present at all". For pricing that distinction matters,
    since an absent property falls back to the AWS service default.
    """

    _instance: _NoValue | None = None

    def __new__(cls) -> _NoValue:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<AWS::NoValue>"

    def __bool__(self) -> bool:
        return False


NO_VALUE = _NoValue()


def stringify(value: Any) -> str:
    """Render a value the way CloudFormation does in string contexts.

    Booleans become lowercase ``true``/``false`` rather than Python's
    ``True``/``False``, and lists join on commas.

    Deliberately string-based rather than numeric. Comparing ``"01"`` against
    ``"1"`` numerically would call them equal, and coercing a 12-digit account ID
    through a float would lose precision. CloudFormation compares as strings, so
    this does too.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ",".join(stringify(item) for item in value)
    if value is None:
        return ""
    return str(value)


@dataclass
class Resolved:
    """The outcome of resolving a single expression."""

    status: ResolutionStatus
    value: Any = None
    via: ResolvedVia | None = None
    ref: str | None = None
    raw: Any = None
    reason: UnresolvedReason | None = None
    detail: str | None = None

    @property
    def is_resolved(self) -> bool:
        return self.status is ResolutionStatus.RESOLVED

    @property
    def is_no_value(self) -> bool:
        return self.is_resolved and self.value is NO_VALUE

    # -- constructors -----------------------------------------------------

    @classmethod
    def ok(
        cls,
        value: Any,
        via: ResolvedVia = ResolvedVia.LITERAL,
        ref: str | None = None,
        raw: Any = None,
    ) -> Resolved:
        return cls(
            status=ResolutionStatus.RESOLVED, value=value, via=via, ref=ref, raw=raw
        )

    @classmethod
    def fail(
        cls,
        reason: UnresolvedReason,
        raw: Any = None,
        detail: str | None = None,
        ref: str | None = None,
    ) -> Resolved:
        return cls(
            status=ResolutionStatus.UNRESOLVED,
            reason=reason,
            raw=raw,
            detail=detail,
            ref=ref,
        )

    def to_dict(self) -> dict[str, Any]:
        """Shape written into the report's ``resolution`` block."""
        out: dict[str, Any] = {"status": self.status.value}
        if self.via is not None:
            out["via"] = self.via.value
        if self.ref is not None:
            out["ref"] = self.ref
        if self.reason is not None:
            out["reason"] = self.reason.value
        if self.detail is not None:
            out["detail"] = self.detail
        if self.raw is not None:
            out["raw"] = self.raw
        return out


class Inclusion(str, Enum):
    """Whether a resource actually exists in the deployed stack.

    A resource carrying a ``Condition`` that evaluates false is never created,
    so it must not be priced. When the condition itself cannot be evaluated the
    answer is UNKNOWN — we include the resource but flag it, because silently
    dropping it would under-report cost, which is the more dangerous error.
    """

    INCLUDED = "INCLUDED"
    EXCLUDED = "EXCLUDED"
    UNKNOWN = "UNKNOWN"


@dataclass
class ResolvedResource:
    """A single template resource with its properties resolved."""

    logical_id: str
    resource_type: str
    properties: dict[str, Any] = field(default_factory=dict)
    resolution: dict[str, Resolved] = field(default_factory=dict)
    inclusion: Inclusion = Inclusion.INCLUDED
    condition_name: str | None = None
    deletion_policy: str | None = None
    update_replace_policy: str | None = None

    @property
    def unresolved_paths(self) -> list[str]:
        return sorted(
            path for path, res in self.resolution.items() if not res.is_resolved
        )

    @property
    def fully_resolved(self) -> bool:
        return not self.unresolved_paths

    @property
    def is_priceable(self) -> bool:
        """Excluded resources don't exist, so they're never priced."""
        return self.inclusion is not Inclusion.EXCLUDED

    def to_dict(self) -> dict[str, Any]:
        return {
            "logicalId": self.logical_id,
            "resourceType": self.resource_type,
            "properties": self.properties,
            "inclusion": self.inclusion.value,
            "conditionName": self.condition_name,
            "deletionPolicy": self.deletion_policy,
            "updateReplacePolicy": self.update_replace_policy,
            "resolution": {p: r.to_dict() for p, r in self.resolution.items()},
            "unresolvedPaths": self.unresolved_paths,
        }


@dataclass
class PseudoContext:
    """Values for CloudFormation pseudo-parameters.

    Populated from the EventBridge event and ``DescribeStacks``, all of which
    the analyzer already has before resolution begins.
    """

    region: str
    account_id: str
    stack_name: str
    stack_id: str
    partition: str = "aws"
    url_suffix: str = "amazonaws.com"

    def as_map(self) -> dict[str, Any]:
        return {
            "AWS::Region": self.region,
            "AWS::AccountId": self.account_id,
            "AWS::StackName": self.stack_name,
            "AWS::StackId": self.stack_id,
            "AWS::Partition": self.partition,
            "AWS::URLSuffix": self.url_suffix,
        }

    @classmethod
    def from_stack_id(cls, stack_id: str, stack_name: str | None = None) -> PseudoContext:
        """Derive context from a stack ARN.

        ``arn:aws:cloudformation:us-east-1:111122223333:stack/my-stack/abc-123``
        """
        parts = stack_id.split(":")
        if len(parts) < 6 or parts[0] != "arn":
            raise ValueError(f"Not a stack ARN: {stack_id!r}")

        partition, region, account = parts[1], parts[3], parts[4]
        derived_name = stack_name
        if derived_name is None:
            resource = parts[5]
            segments = resource.split("/")
            derived_name = segments[1] if len(segments) > 1 else resource

        url_suffix = "amazonaws.com.cn" if partition == "aws-cn" else "amazonaws.com"

        return cls(
            region=region,
            account_id=account,
            stack_name=derived_name,
            stack_id=stack_id,
            partition=partition,
            url_suffix=url_suffix,
        )
