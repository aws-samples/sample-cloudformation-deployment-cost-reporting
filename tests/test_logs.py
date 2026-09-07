"""Tests for structured logging.

Two properties actually matter here. Every line must be valid JSON, because
CloudWatch Logs Insights is the only way to query them and a single malformed
line breaks a query rather than skipping. And context must not leak between
invocations, because a warm container handles many deployments and a stack ID
attached to the wrong report is worse than no stack ID at all.
"""

from __future__ import annotations

import json
import logging

import pytest

from runtime.logs import (
    JsonFormatter,
    configure_logging,
    current_context,
    get_logger,
    log_context,
)


def render(record: logging.LogRecord) -> dict:
    return json.loads(JsonFormatter().format(record))


def make_record(
    message: str = "hello", level: int = logging.INFO, **extra: object
) -> logging.LogRecord:
    record = logging.LogRecord(
        name="test", level=level, pathname=__file__, lineno=1, msg=message, args=(), exc_info=None
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


# -- formatting -----------------------------------------------------------


def test_output_is_one_json_object() -> None:
    payload = render(make_record("something happened"))

    assert payload["message"] == "something happened"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test"


def test_extra_fields_are_promoted_to_top_level() -> None:
    """Nesting them under a key would mean every Insights query needs a path."""
    payload = render(make_record(stackId="arn:aws:...:stack/x/1", netMonthly=42.5))

    assert payload["stackId"] == "arn:aws:...:stack/x/1"
    assert payload["netMonthly"] == 42.5


def test_standard_record_attributes_are_not_emitted() -> None:
    """Otherwise every line carries pathname, threadName, and relativeCreated,
    tripling its size for no diagnostic value."""
    payload = render(make_record())

    for noise in ("pathname", "threadName", "relativeCreated", "msg", "args"):
        assert noise not in payload


def test_message_interpolation_is_applied() -> None:
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="priced %d of %d",
        args=(6, 8),
        exc_info=None,
    )
    assert render(record)["message"] == "priced 6 of 8"


def test_unserialisable_values_do_not_break_the_line() -> None:
    """A Decimal or a boto3 object in `extra` must not turn one log line into a
    formatter exception, which in Lambda surfaces as a lost line."""
    from decimal import Decimal

    payload = render(make_record(amount=Decimal("12.34"), client=object()))

    assert payload["amount"] == "12.34"
    assert isinstance(payload["client"], str)


def test_exceptions_are_included() -> None:
    try:
        raise ValueError("pricing failed")
    except ValueError:
        import sys

        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="boom",
            args=(),
            exc_info=sys.exc_info(),
        )

    payload = render(record)
    assert "ValueError: pricing failed" in payload["exception"]


# -- context --------------------------------------------------------------


def test_context_is_attached_to_every_line() -> None:
    with log_context(stackId="stack-1", dedupeKey="token-1"):
        payload = render(make_record())

    assert payload["stackId"] == "stack-1"
    assert payload["dedupeKey"] == "token-1"


def test_context_is_removed_on_exit() -> None:
    with log_context(stackId="stack-1"):
        pass

    assert "stackId" not in render(make_record())


def test_context_is_removed_even_when_the_block_raises() -> None:
    """A failed analysis must not leave its stack ID attached to whatever the
    container handles next."""
    with pytest.raises(RuntimeError), log_context(stackId="stack-1"):
        raise RuntimeError("analysis failed")

    assert current_context() == {}


def test_nested_context_merges_and_unwinds() -> None:
    with log_context(stackId="stack-1"):
        with log_context(reportId="report-9"):
            both = current_context()
        after_inner = current_context()

    assert both == {"stackId": "stack-1", "reportId": "report-9"}
    assert after_inner == {"stackId": "stack-1"}


def test_inner_context_wins_on_conflict() -> None:
    with log_context(phase="ESTIMATE"), log_context(phase="CONFIRMED"):
        assert current_context()["phase"] == "CONFIRMED"


def test_none_values_are_dropped_rather_than_logged_as_null() -> None:
    """Report fields are frequently absent. `"changeSetId": null` on every stack
    event is noise that makes a real null indistinguishable."""
    with log_context(stackId="stack-1", changeSetId=None):
        context = current_context()

    assert context == {"stackId": "stack-1"}


def test_explicit_extra_overrides_context() -> None:
    with log_context(stackId="from-context"):
        payload = render(make_record(stackId="from-extra"))

    assert payload["stackId"] == "from-extra"


# -- configuration --------------------------------------------------------


def test_configure_logging_replaces_the_formatter_rather_than_adding_a_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lambda pre-installs a root handler. Adding a second one emits every line
    twice, which doubles the log bill and breaks any count-based query."""
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [logging.StreamHandler()])

    configure_logging("INFO")
    configure_logging("INFO")

    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, JsonFormatter)


def test_configure_logging_adds_a_handler_when_there_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])

    configure_logging("INFO")

    assert len(root.handlers) == 1


def test_level_comes_from_the_environment_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "warning")
    monkeypatch.setattr(logging.getLogger(), "handlers", [])

    configure_logging()

    assert logging.getLogger().level == logging.WARNING


def test_boto3_stays_quiet_at_debug(monkeypatch: pytest.MonkeyPatch) -> None:
    """botocore at DEBUG logs every request and response. Left alone it buries
    the plugin's own output in wire traffic."""
    monkeypatch.setattr(logging.getLogger(), "handlers", [])

    configure_logging("DEBUG")

    assert logging.getLogger("botocore").level >= logging.WARNING
    assert logging.getLogger().level == logging.DEBUG


def test_get_logger_returns_a_standard_logger() -> None:
    assert isinstance(get_logger("x.y"), logging.Logger)
