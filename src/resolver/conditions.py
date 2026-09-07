"""Condition evaluation.

Conditions matter for pricing in two distinct ways:

1. ``Fn::If`` picks between two property values, so the branch taken decides
   what gets priced.
2. A resource carrying a top-level ``Condition`` is not created at all when that
   condition is false. Pricing it anyway would invent cost that never existed.

Values inside ``Fn::Equals`` can themselves be intrinsics, so evaluation needs a
way back into the main resolver. That is injected as a callable rather than
imported, which keeps the dependency one-directional.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .models import Resolved, ResolvedVia, UnresolvedReason, stringify

ValueResolver = Callable[[Any], Resolved]

_BOOLEAN_OPERATORS = frozenset({"Fn::And", "Fn::Or", "Fn::Not", "Fn::Equals"})


def _equal(left: Any, right: Any) -> bool:
    """Compare two resolved values the way ``Fn::Equals`` does.

    String comparison, so ``5`` matches ``"5"`` but ``"01"`` does not match
    ``"1"``. See :func:`~resolver.models.stringify` for why numeric coercion is
    avoided here.
    """
    return stringify(left) == stringify(right)


def is_condition_expression(node: Any) -> bool:
    """True when a node is a boolean operator rather than a value expression."""
    return (
        isinstance(node, dict)
        and len(node) == 1
        and next(iter(node)) in _BOOLEAN_OPERATORS
    )


class ConditionEvaluator:
    """Evaluates the template's ``Conditions`` block.

    Results are memoised, and cycles are detected rather than blowing the stack.
    """

    def __init__(
        self,
        conditions: dict[str, Any] | None,
        resolve_value: ValueResolver,
    ) -> None:
        self._conditions = conditions if isinstance(conditions, dict) else {}
        self._resolve_value = resolve_value
        self._cache: dict[str, Resolved] = {}
        self._in_progress: list[str] = []

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._conditions)

    def evaluate(self, name: str) -> Resolved:
        """Evaluate a named condition to a boolean."""
        if name in self._cache:
            return self._cache[name]

        if name not in self._conditions:
            return Resolved.fail(
                UnresolvedReason.UNKNOWN_CONDITION,
                raw={"Condition": name},
                ref=name,
                detail=f"Condition {name!r} is not defined in the template",
            )

        if name in self._in_progress:
            chain = " -> ".join([*self._in_progress, name])
            return Resolved.fail(
                UnresolvedReason.CIRCULAR_CONDITION,
                raw={"Condition": name},
                ref=name,
                detail=f"Circular condition reference: {chain}",
            )

        self._in_progress.append(name)
        try:
            result = self.evaluate_expression(self._conditions[name])
        finally:
            self._in_progress.pop()

        if result.is_resolved:
            result = Resolved.ok(
                bool(result.value),
                via=ResolvedVia.CONDITION,
                ref=name,
                raw=self._conditions[name],
            )
        self._cache[name] = result
        return result

    def evaluate_expression(self, node: Any) -> Resolved:
        """Evaluate a boolean expression node."""
        if isinstance(node, bool):
            return Resolved.ok(node, via=ResolvedVia.LITERAL, raw=node)

        if not isinstance(node, dict) or len(node) != 1:
            return Resolved.fail(
                UnresolvedReason.MALFORMED,
                raw=node,
                detail="Condition expression must be a single-key mapping",
            )

        key, args = next(iter(node.items()))

        if key == "Condition":
            if not isinstance(args, str):
                return Resolved.fail(
                    UnresolvedReason.MALFORMED,
                    raw=node,
                    detail="Condition reference must name a condition",
                )
            return self.evaluate(args)

        # A bare Ref to a condition name is accepted by CloudFormation inside
        # boolean operators, so handle it rather than failing.
        if key == "Ref" and isinstance(args, str) and args in self._conditions:
            return self.evaluate(args)

        handler = {
            "Fn::Equals": self._eval_equals,
            "Fn::Not": self._eval_not,
            "Fn::And": self._eval_and,
            "Fn::Or": self._eval_or,
        }.get(key)

        if handler is None:
            return Resolved.fail(
                UnresolvedReason.MALFORMED,
                raw=node,
                detail=f"{key} is not a boolean operator",
            )

        return handler(args, node)

    # -- operators --------------------------------------------------------

    def _eval_equals(self, args: Any, raw: Any) -> Resolved:
        if not isinstance(args, list) or len(args) != 2:
            return Resolved.fail(
                UnresolvedReason.MALFORMED,
                raw=raw,
                detail="Fn::Equals takes exactly two values",
            )

        left, right = (self._resolve_value(a) for a in args)
        for side in (left, right):
            if not side.is_resolved:
                return Resolved.fail(
                    UnresolvedReason.NESTED_UNRESOLVED,
                    raw=raw,
                    detail=f"Fn::Equals operand unresolved: {side.detail or side.reason}",
                )

        return Resolved.ok(
            _equal(left.value, right.value), via=ResolvedVia.FUNCTION, raw=raw
        )

    def _eval_not(self, args: Any, raw: Any) -> Resolved:
        if not isinstance(args, list) or len(args) != 1:
            return Resolved.fail(
                UnresolvedReason.MALFORMED,
                raw=raw,
                detail="Fn::Not takes exactly one condition",
            )

        inner = self.evaluate_expression(args[0])
        if not inner.is_resolved:
            return inner
        return Resolved.ok(not inner.value, via=ResolvedVia.FUNCTION, raw=raw)

    def _eval_and(self, args: Any, raw: Any) -> Resolved:
        return self._eval_junction(args, raw, "Fn::And")

    def _eval_or(self, args: Any, raw: Any) -> Resolved:
        return self._eval_junction(args, raw, "Fn::Or")

    def _eval_junction(self, args: Any, raw: Any, operator: str) -> Resolved:
        """Shared logic for And/Or, including short-circuiting.

        Short-circuiting is not just an optimisation here. ``Fn::Or`` with one
        true branch is true even if another branch is unresolvable, so
        evaluating greedily would report UNRESOLVED for a condition whose value
        is actually knowable.
        """
        if not isinstance(args, list) or not args:
            return Resolved.fail(
                UnresolvedReason.MALFORMED,
                raw=raw,
                detail=f"{operator} requires at least one condition",
            )

        decisive = operator == "Fn::Or"
        pending: Resolved | None = None

        for operand in args:
            result = self.evaluate_expression(operand)
            if not result.is_resolved:
                pending = pending or result
                continue
            if bool(result.value) is decisive:
                return Resolved.ok(decisive, via=ResolvedVia.FUNCTION, raw=raw)

        if pending is not None:
            return Resolved.fail(
                UnresolvedReason.NESTED_UNRESOLVED,
                raw=raw,
                detail=f"{operator} operand unresolved: {pending.detail or pending.reason}",
            )

        return Resolved.ok(not decisive, via=ResolvedVia.FUNCTION, raw=raw)
