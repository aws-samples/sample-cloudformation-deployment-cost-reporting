"""Tests that the SAM template and the code agree.

This is the drift nobody catches until a deploy. Three specific failures are
invisible to both the unit tests and cfn-lint:

* A handler path typo. The template deploys, the function is created, and the
  first invocation fails with an import error.
* An environment variable named one way in the template and another in
  ``config.py``. Nothing errors — the config silently uses its default, so a
  discount of 40% quietly becomes 0% and every report is wrong by that much.
* An IAM action the code calls but the policy omits. Works in tests against
  fakes, fails in production on the one path that needs it.

All three are cheap to assert here and expensive to find in an account.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import Any

import pytest

from resolver.cfn_yaml import load_template

TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "template.yaml"
CONFIG_PATH = Path(__file__).resolve().parent.parent / "src" / "runtime" / "config.py"

#: Injected by the Lambda runtime, never set in the template.
RUNTIME_INJECTED = frozenset({"AWS_REGION"})

#: Set for the runtime's benefit rather than read by our own config.
NOT_READ_BY_CONFIG = frozenset({"POWERTOOLS_SERVICE_NAME"})


@pytest.fixture(scope="module")
def template() -> dict[str, Any]:
    return load_template(TEMPLATE_PATH.read_text())


@pytest.fixture(scope="module")
def functions(template: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        name: body
        for name, body in template["Resources"].items()
        if body.get("Type") == "AWS::Serverless::Function"
    }


def test_template_parses(template: dict[str, Any]) -> None:
    assert template["Transform"] == "AWS::Serverless-2016-10-31"
    assert template["Resources"]


def test_every_handler_resolves_to_a_callable(
    functions: dict[str, dict[str, Any]],
) -> None:
    """A typo here deploys cleanly and fails on first invocation."""
    assert functions, "Template declares no Lambda functions"

    for name, body in functions.items():
        handler = body["Properties"]["Handler"]
        module_path, _, attribute = handler.rpartition(".")

        module = importlib.import_module(module_path)
        target = getattr(module, attribute, None)

        assert callable(target), f"{name}: {handler} is not a callable"


def _config_env_names() -> set[str]:
    """Environment variable names ``RuntimeConfig.from_env`` reads."""
    source = CONFIG_PATH.read_text()
    return set(re.findall(r'env\.get\(\s*"([A-Z_0-9]+)"', source)) | set(
        re.findall(r'_(?:decimal|int|bool|optional)\(\s*env,\s*"([A-Z_0-9]+)"', source)
    )


def _template_env_names(template: dict[str, Any]) -> set[str]:
    names: set[str] = set()

    globals_env = (
        template.get("Globals", {})
        .get("Function", {})
        .get("Environment", {})
        .get("Variables")
        or {}
    )
    names |= set(globals_env)

    for body in template["Resources"].values():
        if body.get("Type") != "AWS::Serverless::Function":
            continue
        variables = (body["Properties"].get("Environment") or {}).get("Variables") or {}
        names |= set(variables)

    return names


def test_config_reads_something(template: dict[str, Any]) -> None:
    """Guard the regexes above: a silent zero match would make the next two
    tests pass vacuously."""
    assert len(_config_env_names()) > 10
    assert len(_template_env_names(template)) > 10


def test_no_setting_is_read_but_never_set(template: dict[str, Any]) -> None:
    """Every variable the config reads is set by some function.

    A miss means that setting is permanently stuck at its default, with no error
    to reveal it.
    """
    missing = _config_env_names() - _template_env_names(template) - RUNTIME_INJECTED
    assert not missing, f"Read by config.py but never set in template: {sorted(missing)}"


def test_no_setting_is_set_but_never_read(template: dict[str, Any]) -> None:
    """The reverse: a variable in the template that nothing reads is either a
    typo or dead weight, and a typo looks exactly like the previous test's
    failure from the other side."""
    unused = _template_env_names(template) - _config_env_names() - NOT_READ_BY_CONFIG
    assert not unused, f"Set in template but never read: {sorted(unused)}"


# -- IAM ------------------------------------------------------------------


def _actions_for(function: dict[str, Any]) -> set[str]:
    """Every IAM action granted to a function, flattened."""
    actions: set[str] = set()
    for policy in function["Properties"].get("Policies") or []:
        if not isinstance(policy, dict):
            continue
        for statement in policy.get("Statement") or []:
            if not isinstance(statement, dict):
                continue
            action = statement.get("Action")
            if isinstance(action, str):
                actions.add(action)
            elif isinstance(action, list):
                actions.update(a for a in action if isinstance(a, str))
    return actions


def test_analyzer_can_perform_every_dynamo_call_it_makes(
    functions: dict[str, dict[str, Any]],
) -> None:
    """The analyzer deletes items on two paths, and both are easy to miss.

    A retired snapshot and a released idempotency claim are both DeleteItem. If
    the policy omits it, the release fails, the claim is held, and the SQS retry
    is skipped — turning one transient error into a permanently missing report.
    """
    actions = _actions_for(functions["AnalyzerFunction"])
    for required in (
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
    ):
        assert required in actions, f"AnalyzerFunction is missing {required}"


def test_price_sync_can_batch_write(functions: dict[str, dict[str, Any]]) -> None:
    """The sync writes through a batch writer, which is BatchWriteItem, not
    PutItem. Granting only PutItem fails every sync."""
    actions = _actions_for(functions["PriceSyncFunction"])
    assert "dynamodb:BatchWriteItem" in actions
    assert "pricing:GetProducts" in actions


def test_plugin_holds_no_infrastructure_write_permission(
    functions: dict[str, dict[str, Any]],
) -> None:
    """The core safety claim: read-only by design.

    Asserted mechanically so it cannot be quietly broken by someone adding a
    convenience permission later.
    """
    forbidden = {
        "cloudformation:CreateStack",
        "cloudformation:UpdateStack",
        "cloudformation:DeleteStack",
        "cloudformation:ExecuteChangeSet",
        "cloudformation:CreateChangeSet",
        "cloudformation:SetStackPolicy",
        "iam:PassRole",
        "iam:CreateRole",
        "iam:AttachRolePolicy",
    }

    for name, body in functions.items():
        granted = _actions_for(body)
        overlap = granted & forbidden
        assert not overlap, f"{name} grants infrastructure write permission: {sorted(overlap)}"

        # A bare wildcard would grant everything above without naming it.
        assert "*" not in granted, f"{name} grants Action: '*'"
        for action in granted:
            _, _, verb = action.partition(":")
            assert verb != "*", f"{name} grants {action}, a whole-service wildcard"


def test_analyzer_reads_the_price_cache_without_writing_it(
    functions: dict[str, dict[str, Any]],
) -> None:
    """Only the scheduled sync writes prices.

    If the analyzer could write the cache, a request-path fallback could be added
    later without anyone noticing that reports had stopped being reproducible.
    """
    statements = [
        statement
        for policy in functions["AnalyzerFunction"]["Properties"]["Policies"]
        if isinstance(policy, dict)
        for statement in policy.get("Statement") or []
        if isinstance(statement, dict) and statement.get("Sid") == "ReadPriceCache"
    ]
    assert len(statements) == 1, "Expected exactly one ReadPriceCache statement"

    action = statements[0]["Action"]
    actions = {action} if isinstance(action, str) else set(action)
    assert actions == {"dynamodb:GetItem"}


# -- triggers -------------------------------------------------------------


def _rules(template: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        name: body
        for name, body in template["Resources"].items()
        if body.get("Type") == "AWS::Events::Rule"
    }


def test_stack_rule_matches_only_terminal_statuses(template: dict[str, Any]) -> None:
    """Reading a stack mid-deployment returns a half-applied resource list, so a
    non-terminal status must never reach the Lambda."""
    from analyzer.events import TERMINAL_STATUSES

    pattern = _rules(template)["StackEventRule"]["Properties"]["EventPattern"]
    matched = set(pattern["detail"]["status-details"]["status"])

    assert matched == set(TERMINAL_STATUSES), (
        "The rule and the parser disagree about which statuses are terminal"
    )
    assert not any("IN_PROGRESS" in status for status in matched)


def test_change_set_rule_matches_only_what_the_parser_accepts(
    template: dict[str, Any],
) -> None:
    """Matching an event the parser discards spends an invocation to reach a
    ``NotActionable``."""
    from analyzer.changeset import CHANGE_SET_EVENT_NAMES, CLOUDTRAIL_DETAIL_TYPE

    pattern = _rules(template)["ChangeSetRule"]["Properties"]["EventPattern"]

    assert set(pattern["detail"]["eventName"]) == set(CHANGE_SET_EVENT_NAMES)
    assert pattern["detail-type"] == [CLOUDTRAIL_DETAIL_TYPE]


def test_both_rules_target_the_queue(template: dict[str, Any]) -> None:
    """A rule with no target deploys cleanly and drops every event."""
    for name, body in _rules(template).items():
        targets = body["Properties"].get("Targets") or []
        assert targets, f"{name} has no target"


def test_queue_visibility_timeout_covers_the_function_timeout(
    template: dict[str, Any], functions: dict[str, dict[str, Any]]
) -> None:
    """Lambda rejects an event source mapping whose visibility timeout is below
    the function timeout, so this fails at deploy rather than at runtime."""
    visibility = template["Resources"]["EventQueue"]["Properties"]["VisibilityTimeout"]
    timeout = functions["AnalyzerFunction"]["Properties"].get("Timeout") or template[
        "Globals"
    ]["Function"]["Timeout"]

    assert visibility >= timeout


def test_analyzer_reports_partial_batch_failures(
    functions: dict[str, dict[str, Any]],
) -> None:
    """The handler returns ``batchItemFailures``, which SQS ignores unless the
    event source mapping opts in. Without this, one bad message sends the whole
    batch of ten to the dead-letter queue."""
    events = functions["AnalyzerFunction"]["Properties"]["Events"]
    queue = events["Queue"]["Properties"]

    assert queue["FunctionResponseTypes"] == ["ReportBatchItemFailures"]


def test_dead_letter_queue_is_wired(template: dict[str, Any]) -> None:
    """Without a redrive policy a permanently failing message retries until it
    expires, silently."""
    redrive = template["Resources"]["EventQueue"]["Properties"]["RedrivePolicy"]
    assert redrive["maxReceiveCount"] >= 1


# -- storage --------------------------------------------------------------


def test_every_ttl_table_uses_the_attribute_the_code_writes(
    template: dict[str, Any],
) -> None:
    """The code writes ``expiresAt``. A TTL configured on any other attribute
    expires nothing, and the table grows without limit."""
    for name in ("StateTable", "IdempotencyTable", "SlackMessageTable"):
        properties = template["Resources"][name]["Properties"]
        ttl = properties.get("TimeToLiveSpecification")
        assert ttl, f"{name} has no TTL"
        assert ttl["AttributeName"] == "expiresAt", f"{name} TTL is on the wrong attribute"
        assert ttl["Enabled"] is True


def test_active_price_generations_do_not_expire_under_metadata(
    template: dict[str, Any],
) -> None:
    """Persistent metadata must never point at TTL-expired active prices."""
    properties = template["Resources"]["PriceTable"]["Properties"]
    assert "TimeToLiveSpecification" not in properties


def test_every_table_is_keyed_the_way_the_code_reads_it(
    template: dict[str, Any],
) -> None:
    """All four stores do point reads on a single partition key named ``pk``."""
    for name in ("PriceTable", "StateTable", "IdempotencyTable", "SlackMessageTable"):
        properties = template["Resources"][name]["Properties"]
        assert properties["KeySchema"] == [{"AttributeName": "pk", "KeyType": "HASH"}]


def test_nothing_survives_an_uninstall(template: dict[str, Any]) -> None:
    """The documented uninstall is "delete the stack, nothing left behind".

    A Retain policy anywhere makes that untrue, and an orphaned table keeps
    costing money after someone believes they have removed the plugin.
    """
    for name, body in template["Resources"].items():
        assert body.get("DeletionPolicy") != "Retain", (
            f"{name} is retained on delete, which contradicts the documented uninstall"
        )


def test_analyzer_receives_the_exact_plugin_stack_id(
    functions: dict[str, dict[str, Any]],
) -> None:
    """CloudFormation supplies the full stack ARN; no user-entered name or
    prefix can accidentally suppress an unrelated stack."""
    variables = functions["AnalyzerFunction"]["Properties"]["Environment"]["Variables"]

    assert variables["PLUGIN_STACK_ID"] == {"Ref": "AWS::StackId"}
