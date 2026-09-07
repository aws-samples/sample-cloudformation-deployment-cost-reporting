"""Tests for runtime configuration.

The point of interest is what a *malformed* value does. A discount typed as
"15%" instead of "15" must not crash the function on cold start, because that
takes down every report rather than one setting. It falls back to the default and
logs — which is also why the tests below check the fallback value specifically,
not just that nothing raised.
"""

from __future__ import annotations

from decimal import Decimal

from runtime.config import RuntimeConfig


def test_an_empty_environment_yields_working_defaults() -> None:
    config = RuntimeConfig.from_env({})

    assert config.discount_percent == Decimal(0)
    assert config.notify_threshold == Decimal(0)
    assert config.max_nested_depth == 5
    assert config.snapshot_retention_days == 90
    assert config.rollup_nested_stacks is True
    assert config.enable_metrics is True
    assert config.enable_platform_lookup is False
    assert config.log_level == "INFO"


def test_table_names_are_read() -> None:
    config = RuntimeConfig.from_env(
        {
            "PRICE_TABLE": "prices",
            "STATE_TABLE": "state",
            "IDEMPOTENCY_TABLE": "claims",
            "SLACK_MESSAGE_TABLE": "messages",
        }
    )

    assert config.price_table == "prices"
    assert config.state_table == "state"
    assert config.idempotency_table == "claims"
    assert config.slack_message_table == "messages"


def test_surrounding_whitespace_is_stripped() -> None:
    """A CloudFormation parameter with a trailing space would otherwise produce a
    table name that does not exist."""
    config = RuntimeConfig.from_env({"PRICE_TABLE": "  prices  ", "TOPIC_ARN": " arn:topic "})

    assert config.price_table == "prices"
    assert config.topic_arn == "arn:topic"


# -- numbers --------------------------------------------------------------


def test_a_discount_is_parsed_as_a_decimal() -> None:
    """Decimal, not float: a discount applied in binary floating point produces
    totals that do not match a hand check."""
    config = RuntimeConfig.from_env({"DISCOUNT_PERCENT": "17.5"})

    assert config.discount_percent == Decimal("17.5")
    assert isinstance(config.discount_percent, Decimal)


def test_a_malformed_discount_falls_back_rather_than_crashing() -> None:
    """"15%" is the obvious way to get this wrong. Crashing on cold start would
    take out every report; falling back to 0 reports list price, which is at
    least a stated convention."""
    config = RuntimeConfig.from_env({"DISCOUNT_PERCENT": "15%"})

    assert config.discount_percent == Decimal(0)


def test_a_malformed_integer_falls_back() -> None:
    config = RuntimeConfig.from_env({"MAX_NESTED_DEPTH": "deep"})

    assert config.max_nested_depth == 5


def test_a_blank_number_uses_the_default() -> None:
    """An unset CloudFormation parameter arrives as an empty string, not an
    absent variable."""
    config = RuntimeConfig.from_env({"DISCOUNT_PERCENT": "", "MAX_NESTED_DEPTH": "  "})

    assert config.discount_percent == Decimal(0)
    assert config.max_nested_depth == 5


def test_a_threshold_is_read() -> None:
    assert RuntimeConfig.from_env({"NOTIFY_THRESHOLD": "50"}).notify_threshold == Decimal(50)


# -- booleans -------------------------------------------------------------


def test_the_usual_truthy_spellings_are_accepted() -> None:
    """CloudFormation writes "true", a human might write "yes" or "1"."""
    for value in ("true", "TRUE", "True", "1", "yes", "on", "enabled"):
        assert RuntimeConfig.from_env({"ENABLE_PLATFORM_LOOKUP": value}).enable_platform_lookup


def test_anything_else_is_false() -> None:
    for value in ("false", "no", "0", "off", "maybe"):
        assert not RuntimeConfig.from_env({"ENABLE_PLATFORM_LOOKUP": value}).enable_platform_lookup


def test_metrics_default_to_on_and_can_be_turned_off() -> None:
    assert RuntimeConfig.from_env({}).enable_metrics is True
    assert RuntimeConfig.from_env({"ENABLE_METRICS": "false"}).enable_metrics is False


# -- nested stacks --------------------------------------------------------


def test_rollup_is_the_default() -> None:
    assert RuntimeConfig.from_env({}).rollup_nested_stacks is True


def test_separate_turns_rollup_off() -> None:
    config = RuntimeConfig.from_env({"NESTED_STACK_HANDLING": "SEPARATE"})

    assert config.rollup_nested_stacks is False


def test_the_mode_is_case_insensitive() -> None:
    assert RuntimeConfig.from_env({"NESTED_STACK_HANDLING": "separate"}).rollup_nested_stacks is False


def test_an_unrecognised_mode_falls_back_to_rollup() -> None:
    """Rollup is the safer default: one report per deployment rather than one per
    nested template."""
    assert RuntimeConfig.from_env({"NESTED_STACK_HANDLING": "GROUPED"}).rollup_nested_stacks is True


# -- optional destinations ------------------------------------------------


def test_an_unset_optional_is_none_not_empty_string() -> None:
    """`if config.topic_arn` must distinguish "not configured" from "configured
    as blank", and CloudFormation supplies the latter."""
    config = RuntimeConfig.from_env({"TOPIC_ARN": "", "CENTRAL_BUS_ARN": "   "})

    assert config.topic_arn is None
    assert config.central_bus_arn is None


def test_the_central_bus_is_off_unless_an_arn_is_given() -> None:
    assert RuntimeConfig.from_env({}).central_bus_enabled is False
    assert RuntimeConfig.from_env({"CENTRAL_BUS_ARN": "arn:bus"}).central_bus_enabled is True


# -- slack ----------------------------------------------------------------


def test_slack_needs_both_a_secret_and_a_channel() -> None:
    """Either alone is a misconfiguration that would fail at the API call. Better
    to treat it as off."""
    assert RuntimeConfig.from_env({}).slack_enabled is False
    assert RuntimeConfig.from_env({"SLACK_SECRET_ARN": "arn:secret"}).slack_enabled is False
    assert RuntimeConfig.from_env({"SLACK_CHANNEL_ID": "C123"}).slack_enabled is False

    assert RuntimeConfig.from_env(
        {"SLACK_SECRET_ARN": "arn:secret", "SLACK_CHANNEL_ID": "C123"}
    ).slack_enabled is True


# -- email (SES) ----------------------------------------------------------


def test_ses_needs_both_a_sender_and_a_recipient() -> None:
    """A sender with nobody to send to, or a recipient with no verified sender,
    would fail at the API call. Either alone is treated as off, which falls back
    to the plain SNS email subscription."""
    assert RuntimeConfig.from_env({}).ses_enabled is False
    assert RuntimeConfig.from_env({"SES_SENDER_EMAIL": "r@example.com"}).ses_enabled is False
    assert RuntimeConfig.from_env({"NOTIFICATION_EMAIL": "o@example.com"}).ses_enabled is False

    assert RuntimeConfig.from_env(
        {"SES_SENDER_EMAIL": "r@example.com", "NOTIFICATION_EMAIL": "o@example.com"}
    ).ses_enabled is True


def test_email_addresses_are_read_and_trimmed() -> None:
    config = RuntimeConfig.from_env(
        {"NOTIFICATION_EMAIL": " ops@example.com ", "SES_SENDER_EMAIL": " reports@example.com "}
    )

    assert config.notification_email == "ops@example.com"
    assert config.ses_sender_email == "reports@example.com"


def test_a_blank_email_reads_as_none() -> None:
    """An unset CloudFormation parameter arrives as an empty string."""
    config = RuntimeConfig.from_env({"NOTIFICATION_EMAIL": "", "SES_SENDER_EMAIL": "   "})

    assert config.notification_email is None
    assert config.ses_sender_email is None


# -- sync regions ---------------------------------------------------------


def test_sync_regions_are_split_on_commas() -> None:
    config = RuntimeConfig.from_env({"SYNC_REGIONS": "us-east-1,eu-west-1"})

    assert config.sync_regions == ("us-east-1", "eu-west-1")


def test_padding_and_empty_entries_are_ignored() -> None:
    """A trailing comma is easy to leave in a parameter and would otherwise
    produce a sync for the region named "".
    """
    config = RuntimeConfig.from_env({"SYNC_REGIONS": " us-east-1 , , eu-west-1, "})

    assert config.sync_regions == ("us-east-1", "eu-west-1")


def test_regions_default_to_the_lambdas_own_region() -> None:
    """The common single-region install should need no parameter at all."""
    config = RuntimeConfig.from_env({"AWS_REGION": "ap-southeast-2"})

    assert config.sync_regions == ("ap-southeast-2",)


def test_an_explicit_list_overrides_the_lambdas_region() -> None:
    config = RuntimeConfig.from_env(
        {"AWS_REGION": "us-east-1", "SYNC_REGIONS": "eu-central-1"}
    )

    assert config.sync_regions == ("eu-central-1",)


def test_regions_are_empty_when_nothing_is_known() -> None:
    assert RuntimeConfig.from_env({}).sync_regions == ()


# -- immutability ---------------------------------------------------------


def test_the_config_is_frozen() -> None:
    """Config is read once per container and shared. A handler mutating it would
    change behaviour for every later invocation on that container."""
    import dataclasses

    import pytest

    config = RuntimeConfig.from_env({})

    with pytest.raises(dataclasses.FrozenInstanceError):
        config.discount_percent = Decimal(50)  # type: ignore[misc]


# -- deployment identity --------------------------------------------------


def test_plugin_stack_id_is_read_and_trimmed() -> None:
    stack_id = "arn:aws:cloudformation:us-east-1:111122223333:stack/plugin/abc"

    config = RuntimeConfig.from_env({"PLUGIN_STACK_ID": f"  {stack_id}  "})

    assert config.plugin_stack_id == stack_id


def test_a_missing_or_blank_plugin_stack_id_reads_as_none() -> None:
    assert RuntimeConfig.from_env({}).plugin_stack_id is None
    assert RuntimeConfig.from_env({"PLUGIN_STACK_ID": "   "}).plugin_stack_id is None
