"""Tests for deduplication.

The load-bearing decision here is what happens when the store is *unavailable*.
Refusing to work would lose the report; proceeding risks a duplicate. A duplicate
is recoverable — someone sees two identical messages and shrugs. A missing report
is not, because nothing signals its absence. These tests pin that choice down so
it cannot be quietly reversed into the safer-looking but worse behaviour.
"""

from __future__ import annotations

from typing import Any

from runtime.idempotency import (
    ClaimResult,
    DynamoIdempotencyStore,
    InMemoryIdempotencyStore,
    _is_conditional_failure,
)


class ConditionalCheckFailedException(Exception):
    """Mirrors the botocore exception, including its error response shape."""

    def __init__(self) -> None:
        super().__init__("The conditional request failed")
        self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class ThrottlingException(Exception):
    def __init__(self) -> None:
        super().__init__("Rate exceeded")
        self.response = {"Error": {"Code": "ThrottlingException"}}


class FakeTable:
    """A DynamoDB table that honours the conditional put."""

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.fail_with = fail_with
        self.deleted: list[str] = []
        self.put_calls = 0

    def put_item(
        self,
        Item: dict[str, Any],
        ConditionExpression: str | None = None,
        ExpressionAttributeNames: dict[str, str] | None = None,
        ExpressionAttributeValues: dict[str, Any] | None = None,
    ) -> None:
        self.put_calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        key = Item["pk"]
        existing = self.items.get(key)
        if ConditionExpression and existing is not None:
            values = ExpressionAttributeValues or {}
            can_take_expired_lease = (
                existing.get("status") == "PROCESSING"
                and int(existing.get("leaseUntil") or 0) < int(values.get(":now") or 0)
            )
            if not can_take_expired_lease:
                raise ConditionalCheckFailedException()
        self.items[key] = Item

    def get_item(self, Key: dict[str, Any], ConsistentRead: bool = False) -> dict[str, Any]:
        if self.fail_with is not None:
            raise self.fail_with
        item = self.items.get(Key["pk"])
        return {"Item": item} if item else {}

    def update_item(self, Key: dict[str, Any], **kwargs: Any) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        item = self.items.setdefault(Key["pk"], {"pk": Key["pk"]})
        values = kwargs.get("ExpressionAttributeValues") or {}
        item["status"] = values.get(":completed", item.get("status"))
        item["completedAt"] = values.get(":completedAt")
        item["expiresAt"] = values.get(":expires", item.get("expiresAt"))
        item.pop("leaseUntil", None)

    def delete_item(self, Key: dict[str, Any]) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.deleted.append(Key["pk"])
        self.items.pop(Key["pk"], None)


# -- in-memory ------------------------------------------------------------


def test_first_claim_wins_and_the_second_loses() -> None:
    store = InMemoryIdempotencyStore()

    assert store.claim("token-1") is ClaimResult.ACQUIRED
    assert store.claim("token-1") is ClaimResult.BUSY


def test_release_lets_a_retry_take_the_claim() -> None:
    store = InMemoryIdempotencyStore()
    store.claim("token-1")

    store.release("token-1")

    assert store.claim("token-1") is ClaimResult.ACQUIRED


def test_releasing_an_unheld_key_is_harmless() -> None:
    InMemoryIdempotencyStore().release("never-claimed")


def test_an_empty_store_is_still_truthy() -> None:
    """`store or fallback` must not swap out a working store just because it
    happens to hold nothing yet."""
    assert bool(InMemoryIdempotencyStore()) is True


def test_different_keys_do_not_collide() -> None:
    store = InMemoryIdempotencyStore()

    assert store.claim("estimate#cs-1") is ClaimResult.ACQUIRED
    assert store.claim("estimate#cs-2") is ClaimResult.ACQUIRED


# -- dynamo ---------------------------------------------------------------


def test_claim_writes_a_conditional_item() -> None:
    table = FakeTable()

    assert DynamoIdempotencyStore(table).claim("token-1") is ClaimResult.ACQUIRED
    assert table.items["token-1"]["pk"] == "token-1"
    assert "claimedAt" in table.items["token-1"]


def test_claim_sets_a_ttl_so_claims_do_not_accumulate() -> None:
    table = FakeTable()
    DynamoIdempotencyStore(table, completion_ttl_hours=24).claim("token-1")

    expires_at = table.items["token-1"]["expiresAt"]

    assert isinstance(expires_at, int)
    # Roughly a day out. The exact value depends on wall clock, so only the
    # order of magnitude is asserted.
    assert 23 * 3600 < expires_at - int(__import__("time").time()) <= 25 * 3600


def test_a_duplicate_is_rejected() -> None:
    table = FakeTable()
    store = DynamoIdempotencyStore(table)

    assert store.claim("token-1") is ClaimResult.ACQUIRED
    store.complete("token-1")
    assert store.claim("token-1") is ClaimResult.COMPLETE


def test_store_unavailable_proceeds_rather_than_losing_the_report() -> None:
    """The decision this module exists to encode.

    On any error that is *not* a conditional failure, the work goes ahead. The
    alternative loses the report with nothing to indicate it is missing.
    """
    store = DynamoIdempotencyStore(FakeTable(fail_with=ThrottlingException()))

    assert store.claim("token-1") is ClaimResult.ACQUIRED


def test_an_unrecognised_error_also_proceeds() -> None:
    store = DynamoIdempotencyStore(FakeTable(fail_with=RuntimeError("network gone")))

    assert store.claim("token-1") is ClaimResult.ACQUIRED


def test_release_deletes_the_claim() -> None:
    table = FakeTable()
    store = DynamoIdempotencyStore(table)
    store.claim("token-1")

    store.release("token-1")

    assert table.deleted == ["token-1"]
    assert store.claim("token-1") is ClaimResult.ACQUIRED


def test_a_failed_release_does_not_raise() -> None:
    """The caller is already handling an exception. Raising here would replace
    the original error with a less useful one."""
    store = DynamoIdempotencyStore(FakeTable(fail_with=RuntimeError("gone")))

    store.release("token-1")


# -- error classification -------------------------------------------------


def test_conditional_failure_is_recognised_from_the_error_code() -> None:
    assert _is_conditional_failure(ConditionalCheckFailedException()) is True


def test_conditional_failure_is_recognised_from_the_class_name() -> None:
    """botocore builds exception classes dynamically, so the name is checked as
    well as the response body."""

    class ConditionalCheckFailedException(Exception):
        pass

    assert _is_conditional_failure(ConditionalCheckFailedException()) is True


def test_other_errors_are_not_treated_as_duplicates() -> None:
    """Misclassifying a throttle as a duplicate would silently skip the work."""
    assert _is_conditional_failure(ThrottlingException()) is False
    assert _is_conditional_failure(RuntimeError("boom")) is False


def test_an_exception_without_a_response_attribute_is_handled() -> None:
    assert _is_conditional_failure(ValueError("no response attribute")) is False
