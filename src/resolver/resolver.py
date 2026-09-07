"""Template resolution.

Turns a parsed CloudFormation template plus its stack parameters into concrete
resource properties that can be priced.

The guiding rule, from S4 in the spec: never guess. A value that cannot be
derived from the template is reported as UNRESOLVED with a reason. Producing a
plausible-looking wrong number is worse than admitting ignorance, because a cost
report that is quietly wrong destroys trust in every other figure it contains.
"""

from __future__ import annotations

import base64
import re
from typing import Any

from .conditions import ConditionEvaluator, is_condition_expression
from .models import (
    NO_VALUE,
    Inclusion,
    PseudoContext,
    Resolved,
    ResolvedResource,
    ResolvedVia,
    UnresolvedReason,
    stringify,
)
from .parameters import ParameterStore

__all__ = ["TemplateResolver", "stringify"]

_SUB_TOKEN = re.compile(r"\$\{([^{}]+)\}")

#: Path recorded when the resolved node is the property block itself.
_ROOT_PATH = "$"

#: Path recorded when a resource's ``Condition`` cannot be evaluated.
_CONDITION_PATH = "$condition"

# Functions whose value genuinely cannot come from a template.
_STRUCTURALLY_UNRESOLVABLE: dict[str, tuple[UnresolvedReason, str]] = {
    "Fn::GetAtt": (
        UnresolvedReason.RUNTIME_ATTRIBUTE,
        "Fn::GetAtt reads an attribute that only exists once the resource is created",
    ),
    "Fn::ImportValue": (
        UnresolvedReason.CROSS_STACK_IMPORT,
        "Fn::ImportValue depends on another stack's exports",
    ),
    "Fn::GetAZs": (
        UnresolvedReason.ACCOUNT_SPECIFIC,
        "Fn::GetAZs returns account- and region-specific availability zones",
    ),
}

# Language extensions and functions outside the pricing-relevant set. Marked
# unsupported rather than approximated. These almost never appear on a property
# that affects cost, so they cost nothing in practice.
_UNSUPPORTED = frozenset(
    {
        "Fn::Cidr",
        "Fn::ForEach",
        "Fn::Length",
        "Fn::ToJsonString",
        "Fn::Transform",
        "Fn::Contains",
        "Fn::EachMemberEquals",
        "Fn::EachMemberIn",
        "Fn::RefAll",
        "Fn::ValueOf",
        "Fn::ValueOfAll",
    }
)


class TemplateResolver:
    """Resolves intrinsic functions in a CloudFormation template.

    Args:
        template: Parsed template, with short-form tags already rewritten by
            :func:`~resolver.cfn_yaml.load_template`.
        parameters: Parameter values from ``DescribeStacks``. Defaults from the
            template fill any gaps.
        context: Pseudo-parameter values for the stack.
    """

    def __init__(
        self,
        template: dict[str, Any],
        parameters: dict[str, Any] | None = None,
        context: PseudoContext | None = None,
    ) -> None:
        self._template = template or {}
        self._parameters = ParameterStore.build(self._template, parameters)
        self._context = context
        self._pseudo = context.as_map() if context else {}

        mappings = self._template.get("Mappings")
        self._mappings = mappings if isinstance(mappings, dict) else {}

        resources = self._template.get("Resources")
        self._resources = resources if isinstance(resources, dict) else {}

        self._conditions = ConditionEvaluator(
            self._template.get("Conditions"), self._resolve_arg
        )

    # -- public API -------------------------------------------------------

    @property
    def parameters(self) -> ParameterStore:
        return self._parameters

    @property
    def conditions(self) -> ConditionEvaluator:
        return self._conditions

    def resolve_resources(self) -> list[ResolvedResource]:
        """Resolve every resource in the template.

        Resources whose ``Condition`` is false are returned with
        ``inclusion=EXCLUDED`` rather than omitted, so the caller can report
        them without pricing them.
        """
        return [
            self.resolve_resource(logical_id, body)
            for logical_id, body in self._resources.items()
        ]

    def resolve_resource(self, logical_id: str, body: Any) -> ResolvedResource:
        """Resolve a single resource definition."""
        if not isinstance(body, dict):
            return ResolvedResource(
                logical_id=logical_id,
                resource_type="",
                resolution={
                    _ROOT_PATH: Resolved.fail(
                        UnresolvedReason.MALFORMED,
                        raw=body,
                        detail="Resource definition is not a mapping",
                    )
                },
            )

        sink: dict[str, Resolved] = {}
        condition_name = body.get("Condition")
        inclusion = self._determine_inclusion(condition_name, sink)

        resolved = self._resolve(body.get("Properties") or {}, "", sink)
        properties = (
            resolved.value
            if resolved.is_resolved and isinstance(resolved.value, dict)
            else {}
        )

        deletion_policy = body.get("DeletionPolicy")
        update_replace_policy = body.get("UpdateReplacePolicy")

        return ResolvedResource(
            logical_id=logical_id,
            resource_type=str(body.get("Type") or ""),
            properties=properties,
            resolution=sink,
            inclusion=inclusion,
            condition_name=condition_name if isinstance(condition_name, str) else None,
            deletion_policy=(
                str(deletion_policy) if deletion_policy is not None else None
            ),
            update_replace_policy=(
                str(update_replace_policy)
                if update_replace_policy is not None
                else None
            ),
        )

    def resolve_value(self, node: Any) -> Resolved:
        """Resolve a single expression. Useful for tests and one-off lookups."""
        return self._resolve_arg(node)

    # -- inclusion --------------------------------------------------------

    def _determine_inclusion(
        self, condition_name: Any, sink: dict[str, Resolved]
    ) -> Inclusion:
        if condition_name is None:
            return Inclusion.INCLUDED

        if not isinstance(condition_name, str):
            sink[_CONDITION_PATH] = Resolved.fail(
                UnresolvedReason.MALFORMED,
                raw=condition_name,
                detail="Resource Condition must be a condition name",
            )
            return Inclusion.UNKNOWN

        outcome = self._conditions.evaluate(condition_name)
        sink[_CONDITION_PATH] = outcome

        if not outcome.is_resolved:
            # Include but flag. Excluding on an unknown condition would
            # under-report cost, which is the more dangerous direction.
            return Inclusion.UNKNOWN

        return Inclusion.INCLUDED if outcome.value else Inclusion.EXCLUDED

    # -- traversal --------------------------------------------------------

    def _resolve_arg(self, node: Any) -> Resolved:
        """Resolve an intrinsic's argument, strictly.

        Strict means a single unresolvable member fails the whole argument.
        Within one expression a partial result is not merely incomplete, it is
        wrong: dropping a member of ``Fn::Join`` silently omits a segment, and
        dropping one from ``Fn::Select`` shifts every subsequent index so the
        function returns a different element than the template asked for. Both
        produce a confident wrong answer, which is the failure mode the engine
        exists to avoid.

        Nested resolutions are also discarded here so a resource's
        ``resolution`` map records one entry per property rather than one per
        sub-expression. The outer entry keeps the whole expression in ``raw``.
        """
        return self._resolve(node, "", {}, strict=True)

    def _resolve(
        self,
        node: Any,
        path: str,
        sink: dict[str, Resolved],
        strict: bool = False,
    ) -> Resolved:
        if isinstance(node, dict):
            if len(node) == 1:
                key = next(iter(node))
                if key == "Ref" or key.startswith("Fn::"):
                    result = self._dispatch(key, node[key], node)
                    sink[path or _ROOT_PATH] = result
                    return result
            return self._resolve_mapping(node, path, sink, strict)

        if isinstance(node, list):
            return self._resolve_sequence(node, path, sink, strict)

        return Resolved.ok(node, via=ResolvedVia.LITERAL, raw=node)

    def _resolve_mapping(
        self,
        node: dict[str, Any],
        path: str,
        sink: dict[str, Resolved],
        strict: bool,
    ) -> Resolved:
        out: dict[str, Any] = {}
        for key, value in node.items():
            child_path = f"{path}.{key}" if path else str(key)
            child = self._resolve(value, child_path, sink, strict)

            if not child.is_resolved:
                if strict:
                    return Resolved.fail(
                        UnresolvedReason.NESTED_UNRESOLVED,
                        raw=node,
                        detail=f"key {key!r} unresolved: {child.detail or child.reason}",
                    )
                # Independent properties: omit this one and carry on. The reason
                # is already recorded in the sink under child_path.
                continue

            # NoValue drops the key, exactly as CloudFormation does.
            if child.value is not NO_VALUE:
                out[key] = child.value

        return Resolved.ok(out, via=ResolvedVia.LITERAL, raw=node)

    def _resolve_sequence(
        self,
        node: list[Any],
        path: str,
        sink: dict[str, Resolved],
        strict: bool,
    ) -> Resolved:
        out: list[Any] = []
        for index, item in enumerate(node):
            child = self._resolve(item, f"{path}[{index}]", sink, strict)

            if not child.is_resolved:
                if strict:
                    return Resolved.fail(
                        UnresolvedReason.NESTED_UNRESOLVED,
                        raw=node,
                        detail=(
                            f"item {index} unresolved: {child.detail or child.reason}"
                        ),
                    )
                continue

            if child.value is not NO_VALUE:
                out.append(child.value)

        return Resolved.ok(out, via=ResolvedVia.LITERAL, raw=node)

    # -- dispatch ---------------------------------------------------------

    def _dispatch(self, key: str, args: Any, raw: Any) -> Resolved:
        if key == "Ref":
            return self._ref(args, raw)

        if key in _STRUCTURALLY_UNRESOLVABLE:
            reason, detail = _STRUCTURALLY_UNRESOLVABLE[key]
            return Resolved.fail(reason, raw=raw, detail=detail)

        if key in _UNSUPPORTED:
            return Resolved.fail(
                UnresolvedReason.UNSUPPORTED_FUNCTION,
                raw=raw,
                detail=f"{key} is not resolved by this engine",
            )

        if is_condition_expression(raw):
            return self._conditions.evaluate_expression(raw)

        handler = {
            "Fn::FindInMap": self._find_in_map,
            "Fn::If": self._if,
            "Fn::Sub": self._sub,
            "Fn::Select": self._select,
            "Fn::Join": self._join,
            "Fn::Split": self._split,
            "Fn::Base64": self._base64,
        }.get(key)

        if handler is None:
            return Resolved.fail(
                UnresolvedReason.UNSUPPORTED_FUNCTION,
                raw=raw,
                detail=f"Unrecognised intrinsic function {key}",
            )

        return handler(args, raw)

    # -- Ref --------------------------------------------------------------

    def _ref(self, name: Any, raw: Any) -> Resolved:
        if not isinstance(name, str):
            return Resolved.fail(
                UnresolvedReason.MALFORMED, raw=raw, detail="Ref must name a string"
            )

        if name == "AWS::NoValue":
            return Resolved.ok(NO_VALUE, via=ResolvedVia.PSEUDO, ref=name, raw=raw)

        if name == "AWS::NotificationARNs":
            return Resolved.fail(
                UnresolvedReason.ACCOUNT_SPECIFIC,
                raw=raw,
                ref=name,
                detail="Notification ARNs are supplied at stack operation time",
            )

        if name in self._pseudo:
            return Resolved.ok(
                self._pseudo[name], via=ResolvedVia.PSEUDO, ref=name, raw=raw
            )

        if name.startswith("AWS::"):
            return Resolved.fail(
                UnresolvedReason.MISSING_PARAMETER,
                raw=raw,
                ref=name,
                detail=f"No value supplied for pseudo-parameter {name}",
            )

        if name in self._parameters:
            return self._parameters.get(name)

        # A Ref to a resource yields its physical ID, which does not exist until
        # the resource does. Distinguished from a missing parameter because the
        # cause and the fix are entirely different.
        if name in self._resources:
            return Resolved.fail(
                UnresolvedReason.RESOURCE_REFERENCE,
                raw=raw,
                ref=name,
                detail=f"Ref to resource {name!r} resolves to a physical ID at deploy time",
            )

        return self._parameters.get(name)

    # -- Fn::FindInMap ----------------------------------------------------

    def _find_in_map(self, args: Any, raw: Any) -> Resolved:
        if not isinstance(args, list) or len(args) not in (3, 4):
            return Resolved.fail(
                UnresolvedReason.MALFORMED,
                raw=raw,
                detail="Fn::FindInMap takes [MapName, TopLevelKey, SecondLevelKey]",
            )

        keys: list[str] = []
        for position, argument in enumerate(args[:3]):
            resolved = self._resolve_arg(argument)
            if not resolved.is_resolved:
                return Resolved.fail(
                    UnresolvedReason.NESTED_UNRESOLVED,
                    raw=raw,
                    detail=(
                        f"Fn::FindInMap key at position {position} unresolved: "
                        f"{resolved.detail or resolved.reason}"
                    ),
                )
            keys.append(stringify(resolved.value))

        # CloudFormation's language extension allows a default when the lookup
        # misses. Honour it before failing.
        default: Resolved | None = None
        if len(args) == 4 and isinstance(args[3], dict) and "DefaultValue" in args[3]:
            default = self._resolve_arg(args[3]["DefaultValue"])

        map_name, top_key, second_key = keys
        cursor: Any = self._mappings
        levels = (
            ("map", map_name),
            ("top-level key", top_key),
            ("second-level key", second_key),
        )
        for label, key in levels:
            if not isinstance(cursor, dict) or key not in cursor:
                if default is not None and default.is_resolved:
                    return Resolved.ok(
                        default.value, via=ResolvedVia.MAPPING, ref=map_name, raw=raw
                    )
                return Resolved.fail(
                    UnresolvedReason.MISSING_MAPPING,
                    raw=raw,
                    ref=map_name,
                    detail=f"Mappings lookup failed: no {label} {key!r}",
                )
            cursor = cursor[key]

        return Resolved.ok(cursor, via=ResolvedVia.MAPPING, ref=map_name, raw=raw)

    # -- Fn::If -----------------------------------------------------------

    def _if(self, args: Any, raw: Any) -> Resolved:
        if not isinstance(args, list) or len(args) != 3:
            return Resolved.fail(
                UnresolvedReason.MALFORMED,
                raw=raw,
                detail="Fn::If takes [ConditionName, ValueIfTrue, ValueIfFalse]",
            )

        condition_name, if_true, if_false = args
        if not isinstance(condition_name, str):
            return Resolved.fail(
                UnresolvedReason.MALFORMED,
                raw=raw,
                detail="Fn::If's first argument must be a condition name",
            )

        outcome = self._conditions.evaluate(condition_name)
        if not outcome.is_resolved:
            return Resolved.fail(
                UnresolvedReason.NESTED_UNRESOLVED,
                raw=raw,
                ref=condition_name,
                detail=(
                    f"Condition {condition_name!r} unresolved: "
                    f"{outcome.detail or outcome.reason}"
                ),
            )

        branch = self._resolve_arg(if_true if outcome.value else if_false)
        if not branch.is_resolved:
            return Resolved.fail(
                branch.reason or UnresolvedReason.NESTED_UNRESOLVED,
                raw=raw,
                ref=condition_name,
                detail=(
                    f"Fn::If branch "
                    f"{'true' if outcome.value else 'false'} unresolved: "
                    f"{branch.detail or branch.reason}"
                ),
            )

        return Resolved.ok(
            branch.value, via=ResolvedVia.CONDITION, ref=condition_name, raw=raw
        )

    # -- Fn::Sub ----------------------------------------------------------

    def _sub(self, args: Any, raw: Any) -> Resolved:
        if isinstance(args, str):
            body, local_source = args, {}
        elif isinstance(args, list) and len(args) == 2 and isinstance(args[0], str):
            body = args[0]
            local_source = args[1] if isinstance(args[1], dict) else {}
        else:
            return Resolved.fail(
                UnresolvedReason.MALFORMED,
                raw=raw,
                detail="Fn::Sub takes a string, or [string, {variables}]",
            )

        locals_resolved: dict[str, Any] = {}
        for name, value in local_source.items():
            resolved = self._resolve_arg(value)
            if not resolved.is_resolved:
                return Resolved.fail(
                    UnresolvedReason.NESTED_UNRESOLVED,
                    raw=raw,
                    detail=(
                        f"Fn::Sub variable {name!r} unresolved: "
                        f"{resolved.detail or resolved.reason}"
                    ),
                )
            locals_resolved[name] = resolved.value

        failures: list[str] = []

        def substitute(match: re.Match[str]) -> str:
            token = match.group(1)

            # ${!Literal} escapes to a literal ${Literal}
            if token.startswith("!"):
                return "${" + token[1:] + "}"

            if token in locals_resolved:
                return stringify(locals_resolved[token])

            # A dot means an attribute lookup, which is GetAtt in disguise.
            # Pseudo-parameters use "::" so they aren't caught here.
            if "." in token:
                failures.append(f"{token} (runtime attribute)")
                return match.group(0)

            resolved = self._ref(token, {"Ref": token})
            if resolved.is_resolved and resolved.value is not NO_VALUE:
                return stringify(resolved.value)

            failures.append(f"{token} ({resolved.detail or resolved.reason})")
            return match.group(0)

        substituted = _SUB_TOKEN.sub(substitute, body)

        if failures:
            return Resolved.fail(
                UnresolvedReason.NESTED_UNRESOLVED,
                raw=raw,
                detail="Fn::Sub could not resolve: " + "; ".join(failures),
            )

        return Resolved.ok(substituted, via=ResolvedVia.FUNCTION, raw=raw)

    # -- list functions ---------------------------------------------------

    def _select(self, args: Any, raw: Any) -> Resolved:
        if not isinstance(args, list) or len(args) != 2:
            return Resolved.fail(
                UnresolvedReason.MALFORMED,
                raw=raw,
                detail="Fn::Select takes [index, list]",
            )

        index_resolved = self._resolve_arg(args[0])
        list_resolved = self._resolve_arg(args[1])

        for label, resolved in (("index", index_resolved), ("list", list_resolved)):
            if not resolved.is_resolved:
                return Resolved.fail(
                    UnresolvedReason.NESTED_UNRESOLVED,
                    raw=raw,
                    detail=(
                        f"Fn::Select {label} unresolved: "
                        f"{resolved.detail or resolved.reason}"
                    ),
                )

        try:
            index = int(index_resolved.value)
        except (TypeError, ValueError):
            return Resolved.fail(
                UnresolvedReason.MALFORMED,
                raw=raw,
                detail=f"Fn::Select index is not an integer: {index_resolved.value!r}",
            )

        items = list_resolved.value
        if not isinstance(items, list):
            return Resolved.fail(
                UnresolvedReason.MALFORMED,
                raw=raw,
                detail="Fn::Select's second argument did not resolve to a list",
            )

        if not -len(items) <= index < len(items):
            return Resolved.fail(
                UnresolvedReason.MALFORMED,
                raw=raw,
                detail=f"Fn::Select index {index} is out of range for {len(items)} items",
            )

        return Resolved.ok(items[index], via=ResolvedVia.FUNCTION, raw=raw)

    def _join(self, args: Any, raw: Any) -> Resolved:
        if not isinstance(args, list) or len(args) != 2:
            return Resolved.fail(
                UnresolvedReason.MALFORMED,
                raw=raw,
                detail="Fn::Join takes [delimiter, list]",
            )

        delimiter = args[0] if isinstance(args[0], str) else ""
        list_resolved = self._resolve_arg(args[1])

        if not list_resolved.is_resolved:
            return Resolved.fail(
                UnresolvedReason.NESTED_UNRESOLVED,
                raw=raw,
                detail=f"Fn::Join list unresolved: {list_resolved.detail or list_resolved.reason}",
            )

        items = list_resolved.value
        if not isinstance(items, list):
            return Resolved.fail(
                UnresolvedReason.MALFORMED,
                raw=raw,
                detail="Fn::Join's second argument did not resolve to a list",
            )

        return Resolved.ok(
            delimiter.join(stringify(item) for item in items),
            via=ResolvedVia.FUNCTION,
            raw=raw,
        )

    def _split(self, args: Any, raw: Any) -> Resolved:
        if not isinstance(args, list) or len(args) != 2 or not isinstance(args[0], str):
            return Resolved.fail(
                UnresolvedReason.MALFORMED,
                raw=raw,
                detail="Fn::Split takes [delimiter, string]",
            )

        source = self._resolve_arg(args[1])
        if not source.is_resolved:
            return Resolved.fail(
                UnresolvedReason.NESTED_UNRESOLVED,
                raw=raw,
                detail=f"Fn::Split source unresolved: {source.detail or source.reason}",
            )

        return Resolved.ok(
            stringify(source.value).split(args[0]), via=ResolvedVia.FUNCTION, raw=raw
        )

    def _base64(self, args: Any, raw: Any) -> Resolved:
        inner = self._resolve_arg(args)
        if not inner.is_resolved:
            return Resolved.fail(
                UnresolvedReason.NESTED_UNRESOLVED,
                raw=raw,
                detail=f"Fn::Base64 input unresolved: {inner.detail or inner.reason}",
            )

        encoded = base64.b64encode(stringify(inner.value).encode("utf-8")).decode("ascii")
        return Resolved.ok(encoded, via=ResolvedVia.FUNCTION, raw=raw)
