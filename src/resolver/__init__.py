"""CloudFormation template resolution.

Step 1 of the build order. Turns a template plus its stack parameters into
concrete resource properties the pricing engine can consume.

Typical use::

    from resolver import load_template, PseudoContext, TemplateResolver

    template = load_template(get_template_response["TemplateBody"])
    context = PseudoContext.from_stack_id(stack_id)
    resolver = TemplateResolver(template, parameters, context)

    for resource in resolver.resolve_resources():
        if not resource.is_priceable:
            continue  # Condition was false, so it was never created
        ...
"""

from .cfn_yaml import TemplateParseError, load_template
from .conditions import ConditionEvaluator
from .models import (
    NO_VALUE,
    Inclusion,
    PseudoContext,
    ResolutionStatus,
    Resolved,
    ResolvedResource,
    ResolvedVia,
    UnresolvedReason,
)
from .parameters import ParameterStore
from .resolver import TemplateResolver, stringify

__all__ = [
    "NO_VALUE",
    "ConditionEvaluator",
    "Inclusion",
    "ParameterStore",
    "PseudoContext",
    "ResolutionStatus",
    "Resolved",
    "ResolvedResource",
    "ResolvedVia",
    "TemplateParseError",
    "TemplateResolver",
    "UnresolvedReason",
    "load_template",
    "stringify",
]
