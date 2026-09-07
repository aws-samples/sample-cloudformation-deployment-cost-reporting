"""Stack parameter handling.

``DescribeStacks`` returns every parameter value as a string, including numbers
and lists. Coercing them back to their declared types matters for pricing: a
``VolumeSize`` of ``"100"`` needs to be the integer 100 before it can be
multiplied by a per-GB rate.

Parameters absent from the stack fall back to the template's ``Default``. Only
when there is neither a supplied value nor a default is the parameter treated as
unresolvable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import Resolved, ResolvedVia, UnresolvedReason

_LIST_TYPES = frozenset({"CommaDelimitedList", "List<Number>"})


def _coerce_number(raw: str) -> int | float | str:
    """Turn a numeric string into int or float, leaving it alone if it isn't one."""
    text = raw.strip()
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        # Declared Number but not numeric. Preserve rather than crash; the
        # pricing layer will reject it and report low confidence.
        return raw


def _coerce(raw: Any, declared_type: str) -> Any:
    """Apply a parameter's declared type to a raw value."""
    if declared_type == "Number":
        return _coerce_number(raw) if isinstance(raw, str) else raw

    if declared_type in _LIST_TYPES:
        if isinstance(raw, list):
            items: list[Any] = raw
        elif isinstance(raw, str):
            items = [part.strip() for part in raw.split(",")] if raw else []
        else:
            return raw
        if declared_type == "List<Number>":
            return [_coerce_number(i) if isinstance(i, str) else i for i in items]
        return items

    # AWS-specific list types (List<AWS::EC2::Subnet::Id> and friends).
    if declared_type.startswith("List<"):
        if isinstance(raw, str):
            return [part.strip() for part in raw.split(",")] if raw else []
        return raw

    return raw


@dataclass
class ParameterStore:
    """Resolved parameter values, with the source of each recorded."""

    values: dict[str, Any] = field(default_factory=dict)
    sources: dict[str, ResolvedVia] = field(default_factory=dict)
    declared: frozenset[str] = frozenset()

    @classmethod
    def build(
        cls,
        template: dict[str, Any],
        supplied: dict[str, Any] | None = None,
    ) -> ParameterStore:
        """Merge supplied values over template defaults.

        Args:
            template: The parsed template.
            supplied: Values from ``DescribeStacks``, keyed by parameter name.
        """
        declarations = template.get("Parameters") or {}
        if not isinstance(declarations, dict):
            declarations = {}
        supplied = supplied or {}

        values: dict[str, Any] = {}
        sources: dict[str, ResolvedVia] = {}

        for name, spec in declarations.items():
            spec = spec if isinstance(spec, dict) else {}
            declared_type = str(spec.get("Type", "String"))

            if name in supplied:
                values[name] = _coerce(supplied[name], declared_type)
                sources[name] = ResolvedVia.PARAMETER
            elif "Default" in spec:
                values[name] = _coerce(spec["Default"], declared_type)
                sources[name] = ResolvedVia.PARAMETER_DEFAULT

        # Values supplied for parameters the template doesn't declare are kept.
        # Harmless, and it keeps hand-built test fixtures from silently failing.
        for name, value in supplied.items():
            if name not in values:
                values[name] = value
                sources[name] = ResolvedVia.PARAMETER

        return cls(
            values=values,
            sources=sources,
            declared=frozenset(declarations.keys()),
        )

    def get(self, name: str) -> Resolved:
        """Look up a parameter, reporting rather than guessing when absent."""
        if name in self.values:
            return Resolved.ok(
                self.values[name],
                via=self.sources.get(name, ResolvedVia.PARAMETER),
                ref=name,
                raw={"Ref": name},
            )

        if name in self.declared:
            return Resolved.fail(
                UnresolvedReason.MISSING_PARAMETER,
                raw={"Ref": name},
                ref=name,
                detail=f"Parameter {name!r} has no supplied value and no default",
            )

        return Resolved.fail(
            UnresolvedReason.MISSING_PARAMETER,
            raw={"Ref": name},
            ref=name,
            detail=f"{name!r} is not a declared parameter",
        )

    def __contains__(self, name: object) -> bool:
        return name in self.values
