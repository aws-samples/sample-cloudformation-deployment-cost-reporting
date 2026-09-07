"""Analyzer orchestration.

Turns one terminal CloudFormation event into one cost report:

    describe → resolve → price → diff → report → durable publish → commit state

Three rules are load-bearing:

**Never commit a snapshot built from incomplete data.** Transient, partial-page,
nested-child, state, and price-cache read failures propagate so SQS can retry.

**Publish before committing confirmed state.** A failed SNS hand-off must retry
against the original baseline, not an already-advanced snapshot. A suppressed
below-threshold change still commits because no notification is required.

**Filter the template against what actually exists.** After a rollback the template
still declares every resource while only some were created, so pricing the
template alone would bill for infrastructure that never existed.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from pricing import PlatformResolver, PriceCatalog, PricingBasis, PricingEngine
from resolver import PseudoContext, ResolvedResource, TemplateResolver
from state import (
    DeltaAction,
    StackDelta,
    StackSnapshot,
    StateStore,
    diff_deletion,
    diff_snapshots,
)

from .cfn import NESTED_STACK_TYPE, CloudFormationReader, StackDescription
from .changeset import ChangeSetEvent, ChangeSetNotReady
from .events import StackEvent
from .report import ReportPhase, build_report


@dataclass
class AnalyzerConfig:
    discount_percent: Decimal = Decimal(0)
    #: Aggregate nested stacks into one report at the root (S13). Without this a
    #: single logical deployment produces one message per child stack.
    rollup_nested_stacks: bool = True
    #: Suppress notification when the absolute net change is below this. State is
    #: still recorded.
    notify_threshold: Decimal = Decimal(0)
    #: Guard against pathological or cyclic nesting.
    max_nested_depth: int = 5


@dataclass
class AnalysisOutcome:
    """What the analyzer decided, and any state mutation to commit after delivery."""

    report: dict[str, Any] | None = None
    delta: StackDelta | None = None
    # Current-state reports carry the new snapshot here. Runtime delivery commits
    # it only after the canonical report is accepted by SNS; direct analyzer users
    # keep the historical immediate-commit default.
    pending_snapshot: StackSnapshot | None = None
    pending_retirement: str | None = None
    pending_retirement_time: str | None = None
    snapshot_saved: bool = False
    state_retired: bool = False
    skipped: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def reported(self) -> bool:
        return self.report is not None


class Analyzer:
    """Produces a cost report from a stack event."""

    @staticmethod
    def _is_stale_event(event_time: str | None, snapshot_time: str | None) -> bool:
        if not event_time or not snapshot_time:
            return False
        try:
            event_dt = datetime.fromisoformat(event_time.replace("Z", "+00:00"))
            snapshot_dt = datetime.fromisoformat(snapshot_time.replace("Z", "+00:00"))
        except ValueError:
            return False
        return event_dt < snapshot_dt

    def __init__(
        self,
        reader: CloudFormationReader,
        catalog: PriceCatalog,
        store: StateStore,
        config: AnalyzerConfig | None = None,
        platforms: PlatformResolver | None = None,
    ) -> None:
        self._reader = reader
        self._catalog = catalog
        self._store = store
        self._config = config or AnalyzerConfig()
        # Optional. Without it EC2 instances use the documented Linux assumption
        # and the plugin needs no EC2 permissions at all.
        self._platforms = platforms

    def _engine(self, region: str) -> PricingEngine:
        refresh = getattr(self._catalog, "refresh", None)
        if callable(refresh):
            refresh()
        return PricingEngine(
            self._catalog,
            region,
            discount_percent=self._config.discount_percent,
            platforms=self._platforms,
        )

    def commit(self, outcome: AnalysisOutcome) -> None:
        """Apply a confirmed state transition after durable report acceptance.

        Runtime analysis defers this call until SNS accepts the report. Direct
        analyzer callers retain immediate-commit behavior for backwards
        compatibility. The operation is idempotent on one outcome instance.
        """
        if outcome.pending_snapshot is not None and not outcome.snapshot_saved:
            self._store.save(outcome.pending_snapshot)
            outcome.snapshot_saved = True
        if outcome.pending_retirement is not None and not outcome.state_retired:
            self._store.delete(
                outcome.pending_retirement,
                event_time=outcome.pending_retirement_time,
            )
            outcome.state_retired = True

    # -- entry point ------------------------------------------------------

    def analyze(
        self, event: StackEvent, *, defer_state_commit: bool = False
    ) -> AnalysisOutcome:
        description = self._reader.describe_stack(event.stack_id)

        if (
            description is not None
            and description.is_nested
            and self._config.rollup_nested_stacks
        ):
            return AnalysisOutcome(
                skipped="Nested child stack; counted in the root stack's report"
            )

        if event.is_delete:
            return self._analyze_deletion(
                event, description, defer_state_commit=defer_state_commit
            )

        return self._analyze_current_state(
            event, description, defer_state_commit=defer_state_commit
        )

    # -- deletion ---------------------------------------------------------

    def _analyze_deletion(
        self,
        event: StackEvent,
        description: StackDescription | None,
        *,
        defer_state_commit: bool,
    ) -> AnalysisOutcome:
        """Report a teardown.

        The stored snapshot is the only possible source: once a stack is gone its
        resources cannot be inspected. Without history the deletion is still
        reported, but the prior cost is declared unknown rather than guessed.
        """
        before = self._store.load(event.stack_id)
        notes: list[str] = []

        if before is None:
            empty = StackSnapshot(
                stack_id=event.stack_id,
                stack_name=event.stack_name,
                account=event.account,
                region=event.region,
            )
            delta = diff_snapshots(
                empty,
                StackSnapshot.empty_like(empty, status=event.status),
                action=DeltaAction.DELETE,
            )
            notes.append(
                "No stored inventory for this stack, so the prior cost is unknown. "
                "The stack was created before the plugin was installed."
            )
            return AnalysisOutcome(
                report=self._report(
                    delta,
                    event,
                    description,
                    notes=notes,
                    pricing_basis=PricingBasis(price_list_version="unknown"),
                ),
                delta=delta,
                notes=notes,
            )

        if self._is_stale_event(event.event_time, before.event_time):
            return AnalysisOutcome(
                skipped=(
                    "Stale deletion event; a newer stack operation has already "
                    "advanced the confirmed snapshot"
                )
            )

        delta = diff_deletion(before)

        if delta.retained:
            notes.append(
                f"{len(delta.retained)} resource(s) were retained by deletion policy "
                f"and continue to bill, so they are excluded from the saving."
            )

        # Retired only after the report is accepted by the durable destination.
        # Otherwise an SNS failure would erase the inventory needed to replay the
        # deletion report on SQS retry.
        outcome = AnalysisOutcome(
            report=self._report(
                delta,
                event,
                description,
                notes=notes,
                pricing_basis=PricingBasis(
                    price_list_version=before.price_list_version or "unknown",
                    discount_percent=before.discount_percent,
                ),
            ),
            delta=delta,
            pending_retirement=event.stack_id,
            pending_retirement_time=event.event_time,
            notes=notes,
        )
        if not defer_state_commit:
            self.commit(outcome)
        return outcome

    # -- create, update, rollback ----------------------------------------

    def _analyze_current_state(
        self,
        event: StackEvent,
        description: StackDescription | None,
        *,
        defer_state_commit: bool,
    ) -> AnalysisOutcome:
        if description is None:
            return AnalysisOutcome(
                skipped="Could not describe the stack; nothing was persisted"
            )

        notes: list[str] = []
        collected = self._collect(
            event.stack_id, prefix="", depth=0, notes=notes, description=description
        )

        if collected is None:
            # Persisting here would make the next deployment report every
            # resource as deleted.
            return AnalysisOutcome(
                skipped="Could not fetch the stack template; nothing was persisted",
                notes=notes,
            )

        resolved, physical_ids = collected

        inventory = self._engine(event.region).price_inventory(resolved)

        after = StackSnapshot.from_inventory(
            inventory,
            stack_id=event.stack_id,
            stack_name=description.stack_name or event.stack_name,
            account=event.account,
            region=event.region,
            physical_ids=physical_ids,
            client_request_token=event.client_request_token,
            event_time=event.event_time,
            status=event.status,
        )

        before = self._store.load(event.stack_id)
        if before is not None and self._is_stale_event(event.event_time, before.event_time):
            return AnalysisOutcome(
                skipped=(
                    "Stale terminal event; a newer stack operation has already "
                    "advanced the confirmed snapshot"
                )
            )
        delta = diff_snapshots(before, after, action=event.action)

        if delta.is_baseline:
            notes.append(
                "First time this stack has been seen, so this is an inventory "
                "rather than a change."
            )
        if delta.retained:
            notes.append(
                f"{len(delta.retained)} resource(s) left the stack but are retained "
                f"by deletion policy and continue to bill."
            )
        if not delta.reconciles:
            # The invariant is arithmetic, so a failure means a bug rather than an
            # unusual stack. Surfaced instead of published silently.
            notes.append(
                "Internal check failed: the reported change does not reconcile "
                "with the stack totals."
            )

        outcome = AnalysisOutcome(
            delta=delta,
            pending_snapshot=after,
            notes=notes,
        )

        threshold = self._config.notify_threshold
        below_threshold = threshold > 0 and abs(delta.net_monthly) < threshold

        # Baselines start tracking, and reconciliation failures indicate that the
        # figures cannot be trusted; neither may be hidden by a dollar threshold.
        if below_threshold and not delta.is_baseline and delta.reconciles:
            outcome.skipped = (
                f"Net change ${abs(delta.net_monthly):.2f}/month is below the "
                f"${threshold:.2f} notification threshold"
            )
            if not defer_state_commit:
                self.commit(outcome)
            return outcome

        outcome.report = self._report(
            delta,
            event,
            description,
            notes=notes,
            pricing_basis=inventory.basis,
        )
        if not defer_state_commit:
            self.commit(outcome)
        return outcome

    # -- pre-deploy estimate (Path B) -------------------------------------

    def analyze_change_set(self, event: ChangeSetEvent) -> AnalysisOutcome:
        """Price a change set before it is executed.

        Raises:
            ChangeSetNotReady: The change set is still being computed. The caller
                should return the message to the queue rather than treat this as
                a failure.
        """
        change_set = self._reader.describe_change_set(event.change_set_id)
        if change_set is None:
            return AnalysisOutcome(skipped="Could not describe the change set")

        if change_set.is_pending:
            # SQS provides the wait, so the Lambda does not bill for sleeping.
            raise ChangeSetNotReady(
                f"Change set is {change_set.status}; will retry"
            )

        if not change_set.is_usable:
            # Usually CloudFormation reporting that the template produces no
            # changes, which is not an error and not worth a report.
            return AnalysisOutcome(
                skipped=(
                    f"Change set is {change_set.status}"
                    + (f": {change_set.status_reason}" if change_set.status_reason else "")
                )
            )

        description = self._reader.describe_stack(change_set.stack_id)

        if (
            description is not None
            and description.is_nested
            and self._config.rollup_nested_stacks
        ):
            return AnalysisOutcome(
                skipped="Nested child stack; counted in the root stack's estimate"
            )

        template = self._reader.get_change_set_template(
            change_set.stack_id, change_set.change_set_id
        )
        if template is None:
            return AnalysisOutcome(skipped="Could not fetch the change set template")

        notes: list[str] = []
        collected = self._collect(
            change_set.stack_id,
            prefix="",
            depth=0,
            notes=notes,
            description=description,
            template=template,
            parameters=change_set.parameters,
            # A change set proposes resources that do not exist yet, so filtering
            # against the current resource list would drop exactly the additions
            # the estimate exists to report.
            filter_by_existing=False,
        )
        if collected is None:
            return AnalysisOutcome(
                skipped="Could not read the stack for this change set", notes=notes
            )

        resolved, physical_ids = collected
        inventory = self._engine(event.region).price_inventory(resolved)

        proposed = StackSnapshot.from_inventory(
            inventory,
            stack_id=change_set.stack_id,
            stack_name=change_set.stack_name or description.stack_name if description else "",
            account=event.account,
            region=event.region,
            physical_ids=physical_ids,
            status=change_set.status,
        )
        for resource in proposed.resources:
            if resource.logical_id in change_set.replacements:
                resource.physical_id = (
                    f"proposed-replacement:{change_set.change_set_id}:{resource.logical_id}"
                )

        before = self._store.load(change_set.stack_id)

        # A stack in REVIEW_IN_PROGRESS has never been deployed, so its change set
        # genuinely creates everything. Without this the diff would fall back to a
        # baseline, which is useless as an estimate.
        never_deployed = (
            description is not None and description.status == "REVIEW_IN_PROGRESS"
        )
        action = (
            DeltaAction.CREATE
            if before is None and never_deployed
            else DeltaAction.UPDATE
        )

        delta = diff_snapshots(before, proposed, action=action)

        if delta.is_baseline:
            notes.append(
                "No stored history for this stack, so this estimate shows the "
                "proposed inventory rather than a change."
            )
        notes.append(
            "Estimated from the change set before deployment. Resources are not "
            "yet created and the figures are not confirmed."
        )

        # Deliberately not persisted. A change set is a proposal; storing it would
        # make the confirmed report diff against something never deployed.
        return AnalysisOutcome(
            report=self._report(
                delta,
                event,
                description,
                notes=notes,
                phase=ReportPhase.ESTIMATE,
                status=change_set.status,
                tags=change_set.tags,
                pricing_basis=inventory.basis,
            ),
            delta=delta,
            snapshot_saved=False,
            notes=notes,
        )

    # -- gathering --------------------------------------------------------

    def _collect(
        self,
        stack_id: str,
        prefix: str,
        depth: int,
        notes: list[str],
        description: StackDescription | None = None,
        template: dict[str, Any] | None = None,
        parameters: dict[str, str] | None = None,
        filter_by_existing: bool = True,
    ) -> tuple[list[ResolvedResource], dict[str, str]] | None:
        """Resolve one stack's resources, walking into nested stacks.

        Child resources are prefixed with the nesting path
        (``ChildLogicalId/ResourceLogicalId``) so they stay unique and traceable
        back to where they were declared.

        Returns None when the stack could not be read at all, which the caller
        must treat as a reason to persist nothing.
        """
        if description is None:
            description = self._reader.describe_stack(stack_id)
        if description is None:
            return None

        if template is None:
            template = self._reader.get_template(stack_id)
        if template is None:
            return None

        summaries = self._reader.list_resources(stack_id)
        existing = {s.logical_id for s in summaries if s.exists}
        physical_by_logical = {
            s.logical_id: s.physical_id for s in summaries if s.physical_id
        }

        resolver = TemplateResolver(
            template,
            parameters if parameters is not None else description.parameters,
            PseudoContext.from_stack_id(stack_id),
        )

        resolved: list[ResolvedResource] = []
        physical_ids: dict[str, str] = {}
        template_resources = resolver.resolve_resources()

        for resource in template_resources:
            # ListStackResources succeeded completely or raised. Its result is
            # authoritative even when empty (for example a complete rollback).
            if filter_by_existing and resource.logical_id not in existing:
                continue

            key = f"{prefix}{resource.logical_id}"
            resolved.append(replace(resource, logical_id=key))

            physical = physical_by_logical.get(resource.logical_id)
            if physical:
                physical_ids[key] = physical

        if not self._config.rollup_nested_stacks:
            return resolved, physical_ids

        if not filter_by_existing:
            summaries_by_logical = {s.logical_id: s for s in summaries}
            for resource in template_resources:
                if resource.resource_type != NESTED_STACK_TYPE:
                    continue
                summary = summaries_by_logical.get(resource.logical_id)
                logical_id = f"{prefix}{resource.logical_id}"
                if summary is None or not summary.physical_id:
                    # The proposed child template is not exposed through the
                    # parent change set. Represent the omitted contents as an
                    # explicit coverage gap rather than a confidently free stack.
                    resolved.append(
                        ResolvedResource(
                            logical_id=f"{logical_id}/<proposed-contents>",
                            resource_type="NestedStack::ProposedContents",
                        )
                    )
                    notes.append(
                        f"Proposed nested stack {logical_id} has no deployed child "
                        "template; its contents could not be estimated."
                    )
                else:
                    notes.append(
                        f"Nested stack {logical_id} estimate uses the currently "
                        "deployed child template; proposed child changes are not "
                        "available from the parent change set."
                    )

        if depth >= self._config.max_nested_depth:
            nested = [s for s in summaries if s.is_nested_stack and s.exists]
            if nested:
                notes.append(
                    f"Nested stack depth limit ({self._config.max_nested_depth}) "
                    f"reached; deeper stacks were not priced."
                )
                resolved.extend(
                    ResolvedResource(
                        logical_id=f"{prefix}{summary.logical_id}/<depth-limit>",
                        resource_type="NestedStack::DepthLimit",
                    )
                    for summary in nested
                )
            return resolved, physical_ids

        for summary in summaries:
            if not summary.is_nested_stack or not summary.exists:
                continue
            if not summary.physical_id:
                # A nested stack proposed by a change set has no ARN yet, so its
                # contents cannot be read and cannot be estimated.
                notes.append(
                    f"Nested stack {prefix}{summary.logical_id} has no physical ID "
                    f"and was not priced."
                )
                continue

            child = self._collect(
                summary.physical_id,
                prefix=f"{prefix}{summary.logical_id}/",
                depth=depth + 1,
                notes=notes,
                filter_by_existing=filter_by_existing,
            )
            if child is None:
                notes.append(
                    f"Could not read nested stack {prefix}{summary.logical_id}; "
                    f"its resources are not included."
                )
                resolved.append(
                    ResolvedResource(
                        logical_id=f"{prefix}{summary.logical_id}/<unreadable>",
                        resource_type="NestedStack::UnreadableContents",
                    )
                )
                continue

            child_resolved, child_physical = child
            resolved.extend(child_resolved)
            physical_ids.update(child_physical)

        return resolved, physical_ids

    # -- reporting --------------------------------------------------------

    def _report(
        self,
        delta: StackDelta,
        event: StackEvent | ChangeSetEvent,
        description: StackDescription | None,
        notes: list[str] | None = None,
        phase: ReportPhase = ReportPhase.CONFIRMED,
        status: str | None = None,
        tags: dict[str, str] | None = None,
        pricing_basis: PricingBasis | None = None,
    ) -> dict[str, Any]:
        return build_report(
            delta,
            phase=phase,
            status=status or getattr(event, "status", None),
            client_request_token=getattr(event, "client_request_token", None),
            tags=tags if tags is not None else (description.tags if description else {}),
            pricing_basis=pricing_basis or PricingBasis(price_list_version="unknown"),
            root_stack_id=description.root_id if description else None,
            is_nested_stack=bool(description.is_nested) if description else False,
            notes=notes,
            report_id=uuid5(NAMESPACE_URL, event.dedupe_key).hex,
        )
