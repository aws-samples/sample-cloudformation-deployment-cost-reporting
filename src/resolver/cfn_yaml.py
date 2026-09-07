"""CloudFormation-aware template loader.

``GetTemplate`` returns the template in whatever format it was submitted, which
means YAML using short-form intrinsic tags (``!Ref``, ``!Sub``, ``!GetAtt``).
Standard ``yaml.safe_load`` rejects those outright, so templates have to be
loaded through a resolver that rewrites short form into the long form the rest
of the pipeline works with.

JSON is valid YAML, so a single entry point handles both formats.
"""

from __future__ import annotations

from typing import Any

import yaml

# Short-form tags that map onto an ``Fn::`` prefixed function.
_FN_TAGS = frozenset(
    {
        "And",
        "Base64",
        "Cidr",
        "Contains",
        "EachMemberEquals",
        "EachMemberIn",
        "Equals",
        "FindInMap",
        "ForEach",
        "GetAZs",
        "GetAtt",
        "If",
        "ImportValue",
        "Join",
        "Length",
        "Not",
        "Or",
        "RefAll",
        "Select",
        "Split",
        "Sub",
        "ToJsonString",
        "Transform",
        "ValueOf",
        "ValueOfAll",
    }
)

# Short-form tags that are *not* prefixed.
_BARE_TAGS = frozenset({"Ref", "Condition"})


class TemplateParseError(ValueError):
    """Raised when a template cannot be parsed at all."""


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader subclass so registered constructors stay scoped to it."""


def _split_getatt(value: str) -> list[str]:
    """``"Resource.Attr.Sub"`` becomes ``["Resource", "Attr.Sub"]``.

    Only the first dot separates the logical ID from the attribute path;
    attributes themselves may contain dots, as in
    ``!GetAtt Outputs.NestedStackOutput``.
    """
    logical_id, _, attribute = value.partition(".")
    return [logical_id, attribute] if attribute else [logical_id]


def _multi_constructor(loader: _CfnLoader, tag_suffix: str, node: yaml.Node) -> Any:
    """Rewrite a short-form tag into its long-form mapping."""
    if tag_suffix in _BARE_TAGS:
        key = tag_suffix
    elif tag_suffix in _FN_TAGS:
        key = f"Fn::{tag_suffix}"
    else:
        raise TemplateParseError(f"Unrecognised intrinsic tag: !{tag_suffix}")

    if isinstance(node, yaml.ScalarNode):
        value: Any = loader.construct_scalar(node)
        # !GetAtt takes a dotted scalar in short form but a list in long form.
        if key == "Fn::GetAtt" and isinstance(value, str):
            value = _split_getatt(value)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    elif isinstance(node, yaml.MappingNode):
        value = loader.construct_mapping(node, deep=True)
    else:  # pragma: no cover - PyYAML has no fourth node kind
        raise TemplateParseError(f"Unsupported node for !{tag_suffix}")

    return {key: value}


_CfnLoader.add_multi_constructor("!", _multi_constructor)


def _no_duplicate_keys(loader: _CfnLoader, node: yaml.MappingNode) -> dict[str, Any]:
    """Reject duplicate mapping keys instead of silently keeping the last.

    A duplicated ``Properties`` block would otherwise discard half a resource
    definition and quietly under-report its cost.
    """
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in mapping:
            raise TemplateParseError(f"Duplicate key in template: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=True)
    return mapping


_CfnLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicate_keys
)


def load_template(source: str) -> dict[str, Any]:
    """Parse a CloudFormation template from a YAML or JSON string.

    Args:
        source: Raw template body, as returned by ``GetTemplate``.

    Returns:
        The template as a plain dict, with all short-form intrinsics rewritten
        into long form.

    Raises:
        TemplateParseError: The template is unparseable or is not a mapping.
    """
    if not source or not source.strip():
        raise TemplateParseError("Template body is empty")

    try:
        # S506 flags yaml.load with a custom Loader, and rightly so in general.
        # It is safe here because _CfnLoader subclasses yaml.SafeLoader and adds
        # only two constructors, both of which return plain dicts:
        #
        #   - the "!" multi-constructor accepts a fixed allowlist of intrinsic
        #     tags and raises on anything else, so `!python/object/apply:...` is
        #     rejected rather than instantiated;
        #   - `!!python/...` resolves to a `tag:yaml.org,2002:` tag, which never
        #     reaches that constructor and has no SafeLoader handler, so PyYAML
        #     raises and it surfaces as TemplateParseError.
        #
        # Nothing here can construct an arbitrary object.
        parsed = yaml.load(source, Loader=_CfnLoader)  # noqa: S506
    except TemplateParseError:
        raise
    except yaml.YAMLError as exc:
        raise TemplateParseError(f"Template is not valid YAML or JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise TemplateParseError(
            f"Template must be a mapping at the top level, got {type(parsed).__name__}"
        )

    return parsed
