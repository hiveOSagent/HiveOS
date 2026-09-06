"""
spec_search.py — risk-tiered self-improvement loop (M2 #si-1).

Adapts OpenJarvis's spec_search pattern (learning/spec_search/{models,plan/risk_tier,
orchestrator}.py) onto HiveOS's existing safety spine. The single most important
idea ported here: **the model cannot choose how risky its own change is.** A typed
edit is assigned a risk tier from a deterministic table; the tier — not the model —
decides the gate:

  AUTO   -> drive through SelfModifier.propose() (isolated worktree -> test -> PR);
            no human needed, but it still NEVER merges and NEVER touches PROTECTED files.
  REVIEW -> request human approval through the PROTECTED Core/approval_gate.py and stop;
            the edit is applied only after a human approves.
  MANUAL -> recorded for a human; never auto-applied.

Checkpoint/rollback is already provided by SelfModifier (snapshot last-good +
discard-worktree-on-failure), so no separate CheckpointStore is needed.

DAG: core leaf. Depends only on core.self_mod + core.approval (both core). The
diagnose step that proposes edits is INJECTED (a callable), so this module never
imports llm — same discipline as memory.keeper's injected Summarizer.
"""
from __future__ import annotations

import enum
import logging
import posixpath
import uuid
from dataclasses import dataclass, field, replace
from typing import Awaitable, Callable, Iterable, Protocol

from hive.core import approval
from hive.core.self_mod import ApplyFn, SelfModifier
from hive.core.self_mod_safety import (
    SafetyCheckResult,
    apply_tier_policy,
    highest_severity,
    run_all_checks,
)

log = logging.getLogger("hive.spec_search")


class EditOp(enum.Enum):
    """Kinds of self-modification Hive can propose. Every op MUST appear in
    _TIER_TABLE — a missing entry is a programming error (asserted at import)."""
    # Model routing / config — reversible, low blast radius
    SET_MODEL_FOR_TASK = "set_model_for_task"
    SET_MODEL_PARAM = "set_model_param"
    EDIT_TOOL_DESCRIPTION = "edit_tool_description"
    REMOVE_TOOL = "remove_tool"
    ADD_TEST = "add_test"
    EDIT_DOCS = "edit_docs"
    # Safe file creation (new test/doc files only; never overwrites existing code)
    CREATE_FILE = "create_file"
    # Behaviour-changing — needs human eyes
    PATCH_SYSTEM_PROMPT = "patch_system_prompt"
    PATCH_CODE = "patch_code"
    ADD_TOOL = "add_tool"
    # High blast radius — always human-only
    DEPENDENCY_CHANGE = "dependency_change"
    INFRA_DEPLOY = "infra_deploy"


class RiskTier(enum.Enum):
    AUTO = "auto"
    REVIEW = "review"
    MANUAL = "manual"


# The canonical op -> tier mapping. The teacher/model cannot override this; the
# planner overwrites whatever tier an edit arrives with (OpenJarvis spec §4.1).
_TIER_TABLE: dict[EditOp, RiskTier] = {
    EditOp.SET_MODEL_FOR_TASK: RiskTier.AUTO,
    EditOp.SET_MODEL_PARAM: RiskTier.AUTO,
    EditOp.EDIT_TOOL_DESCRIPTION: RiskTier.AUTO,
    EditOp.REMOVE_TOOL: RiskTier.AUTO,
    EditOp.ADD_TEST: RiskTier.AUTO,
    EditOp.EDIT_DOCS: RiskTier.AUTO,
    EditOp.CREATE_FILE: RiskTier.AUTO,   # safe: creates only; never overwrites existing code
    EditOp.PATCH_SYSTEM_PROMPT: RiskTier.REVIEW,
    EditOp.PATCH_CODE: RiskTier.REVIEW,
    EditOp.ADD_TOOL: RiskTier.REVIEW,
    EditOp.DEPENDENCY_CHANGE: RiskTier.MANUAL,
    EditOp.INFRA_DEPLOY: RiskTier.MANUAL,
}
# Fail loudly if a new EditOp is added without a tier — never default to AUTO.
assert set(_TIER_TABLE) == set(EditOp), "every EditOp needs an explicit risk tier"


_PATH_REVIEW_PREFIXES = (
    "src/hive/core",
    "src/hive/tools",
    "src/hive/gateway",
    "core",
    ".git",
    ".github",
)
_PATH_REVIEW_EXACT = {"pyproject.toml"}


def _normalize_repo_path(path: str) -> str:
    """Normalize a model-supplied repository path for policy comparisons."""
    normalized = str(path).replace("\\", "/").strip()
    normalized = posixpath.normpath(normalized)
    return normalized.lower()


def path_requires_review(path: str) -> bool:
    """Return whether a path has a minimum REVIEW risk floor.

    This is intentionally path-based: the model-selected operation is not
    trusted to describe the blast radius of the file content it targets.
    """
    normalized = _normalize_repo_path(path)
    return normalized in _PATH_REVIEW_EXACT or any(
        normalized == prefix or normalized.startswith(prefix + "/")
        for prefix in _PATH_REVIEW_PREFIXES
    )


def validate_edit_target(op: EditOp, path: str) -> str | None:
    """Validate the operation/path pairing supplied by the diagnoser.

    Empty paths remain valid for programmatic edits that provide their own
    apply callback. JSON edits, however, must provide a path for EDIT_DOCS so
    that the operation cannot silently become a generic source-file patch.
    """
    if op is EditOp.EDIT_DOCS:
        if not path:
            return "EDIT_DOCS requires a target path"
        normalized = _normalize_repo_path(path)
        document = (
            normalized.startswith("docs/")
            or normalized.startswith("doc/")
            or normalized.startswith("readme")
            or normalized.startswith("changelog")
            or normalized.endswith((".md", ".mdx", ".rst", ".adoc", ".txt"))
        )
        if not document:
            return f"EDIT_DOCS target is not documentation: {path}"
    return None


def assign_tier(op: EditOp, target_files: Iterable[str] | None = None) -> RiskTier:
    """Return the canonical op tier, raised to REVIEW for sensitive paths."""
    tier = _TIER_TABLE[op]
    if tier is RiskTier.AUTO and any(path_requires_review(path) for path in (target_files or ())):
        return RiskTier.REVIEW
    return tier


@dataclass(slots=True)
class Edit:
    """One proposed self-modification.

    `apply` is the worktree mutator handed to SelfModifier.propose (it edits files
    inside the candidate worktree and returns the changed repo-relative paths).
    `risk_tier` is advisory on input — it is always overwritten deterministically.

    `target_files` is the OPTIONAL pre-flight hint: paths the diagnoser knows this
    edit will touch. When provided, the safety checks can fire BEFORE the worktree
    is built (file_count, protected_paths). When the diagnoser doesn't know yet,
    `target_files=[]` and run_all_checks returns only the file_count "ok" stub —
    SelfModifier's own _touches_protected is the final defence.

    `code` is the OPTIONAL textual payload (Python body / patch text). When
    provided, dangerous_patterns checks fire. Python syntax is checked only
    when `code_is_complete_file` is true; replacement fragments are not whole
    Python files and must not be parsed as such.
    """
    op: EditOp
    summary: str
    apply: ApplyFn
    rationale: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    risk_tier: RiskTier = RiskTier.MANUAL
    target_files: list[str] = field(default_factory=list)
    code: str | None = None
    code_is_complete_file: bool = True


@dataclass(slots=True)
class EditOutcome:
    edit_id: str
    op: EditOp
    tier: RiskTier
    status: str           # applied | pending_approval | manual | failed | blocked_protected
                          #          | blocked_safety | escalated_safety
    detail: str = ""
    branch: str | None = None
    approval_id: str | None = None
    safety_findings: list[dict] = field(default_factory=list)  # per-check summary


# A diagnose step proposes edits from a context blob (recent failures, gaps, goals).
# Injected so this module never imports the llm layer (DAG leaf).
Diagnoser = Callable[[str], Awaitable[list[Edit]]]


class _GateLike(Protocol):
    def request(self, name: str, args: dict, reason: str) -> object: ...


class _MemoryLike(Protocol):
    def learn(self, kind: str, topic: str, content: str, source: str = "") -> None: ...


def tiered(edits: list[Edit]) -> list[Edit]:
    """Return copies with risk_tier overwritten from the canonical table.

    A model-supplied tier that disagrees is logged and discarded — the model can
    never escalate its own change to a lower-friction tier."""
    out: list[Edit] = []
    for e in edits:
        correct = assign_tier(e.op, e.target_files)
        if e.risk_tier is not correct:
            log.info("edit %s: tier %s -> %s (op=%s)", e.id, e.risk_tier.value,
                     correct.value, e.op.value)
        out.append(replace(e, risk_tier=correct))
    return out


class SelfImprovement:
    """Drives proposed edits through the risk gate. Reuses SelfModifier (worktree +
    test + PR + PROTECTED-file refusal) and the canonical approval gate.

    Pillar 4: optional pre-flight safety checks run BEFORE the gate. AUTO-tier
    edits with `warn`-level findings are escalated to REVIEW; AUTO edits with
    `critical` findings are also escalated to REVIEW (the deep protected-path
    check inside SelfModifier is the final defence, but this catches obvious
    bad edits earlier — at the proposal stage, not after a worktree was built).
    """

    def __init__(self, modifier: SelfModifier, *, gate: _GateLike | None = None,
                 pending_store: dict[str, Edit] | None = None,
                 safety_enabled: bool = True,
                 safety_max_files: int = 20,
                 safety_check_fn: Callable[..., list[SafetyCheckResult]] | None = None,
                 audit: Callable[[dict], None] | None = None,
                 memory_provider: _MemoryLike | None = None) -> None:
        self._mod = modifier
        self._gate: _GateLike = gate or approval.gate
        self._pending_store: dict[str, Edit] = pending_store if pending_store is not None else {}
        self._safety_enabled = safety_enabled
        self._safety_max_files = safety_max_files
        self._safety_check_fn = safety_check_fn or run_all_checks
        self._audit = audit  # optional callable to record findings (e.g. AuditLog.record)
        self._memory = memory_provider  # optional: learn() outcomes for the learning loop

    def _record_outcome(self, outcome: EditOutcome) -> None:
        """Mirror an AUTO or human-approved outcome into memory (Pillar 1).

        Best-effort: a down/raising memory layer must never break the self-mod
        loop or revert an already-applied/approved edit."""
        if self._memory is None:
            return
        try:
            if outcome.status == "applied":
                self._memory.learn(
                    "self_mod", f"success:{outcome.op.value}",
                    f"self-mod succeeded: {outcome.detail[:120]} → {outcome.branch}",
                    source="self_mod",
                )
            elif outcome.status == "failed":
                stage = outcome.detail.split(":", 1)[0].strip() or "unknown"
                self._memory.learn(
                    "self_mod", f"failure:{stage}",
                    f"self-mod failed ({stage}): {outcome.detail[:120]}",
                    source="self_mod",
                )
            elif outcome.status == "blocked_protected":
                self._memory.learn(
                    "self_mod", "failure:protected",
                    f"self-mod blocked: {outcome.detail[:120]}",
                    source="self_mod",
                )
        except Exception as exc:  # noqa: BLE001 - memory recording must never break self-mod
            log.warning("self_mod memory recording failed: %s", exc)

    def _safety_run(self, edit: Edit) -> list[SafetyCheckResult]:
        """Run pre-flight safety checks against an edit. Returns [] if disabled.

        Wires Edit.target_files -> run_all_checks(after_files=...) so file-level
        checks (protected_paths, file_count) fire in production. Edit.code (when
        present) is forwarded as the textual payload for python_syntax +
        dangerous_patterns checks.
        """
        if not self._safety_enabled:
            return []
        return self._safety_check_fn(
            edit,
            after_files=list(edit.target_files) if edit.target_files else None,
            code=edit.code,
            max_files=self._safety_max_files,
        )

    def _safety_audit_log(self, edit: Edit, results: list[SafetyCheckResult],
                          effective_tier: RiskTier, escalated: bool) -> None:
        """Best-effort audit log emission. Never raises."""
        if self._audit is None:
            return
        try:
            self._audit({
                "tool": "self_mod_safety",
                "status": "ok" if not escalated else "escalated",
                "approved": False,
                "error": None,
                "args": {
                    "edit_id": edit.id,
                    "op": edit.op.value,
                    "effective_tier": effective_tier.value,
                    "checks": [
                        {"check": r.check, "passed": r.passed,
                         "severity": r.severity, "reason": r.reason}
                        for r in results
                    ],
                },
            })
        except Exception as exc:  # noqa: BLE001 - audit must not break self-mod
            log.warning("safety audit log failed: %s", exc)

    async def run(self, edits: list[Edit], *, dry_run: bool = False) -> list[EditOutcome]:
        return [await self._apply_one(e, dry_run=dry_run) for e in tiered(edits)]

    async def _apply_one(self, edit: Edit, *, dry_run: bool) -> EditOutcome:
        if edit.risk_tier is RiskTier.MANUAL:
            detail = ("manual tier — human-only, not auto-applied"
                      + (" (dry-run preview)" if dry_run else ""))
            return EditOutcome(
                edit_id=edit.id, op=edit.op, tier=edit.risk_tier, status="manual",
                detail=detail,
            )

        # Pre-flight safety (Pillar 4). Cheap, deterministic, no IO.
        results = self._safety_run(edit)
        new_tier, failing = apply_tier_policy(edit.risk_tier, results)
        escalated = new_tier is not edit.risk_tier
        if results:
            self._safety_audit_log(edit, results, new_tier, escalated)
            for r in results:
                if not r.passed:
                    log.info("self_mod_safety %s edit=%s severity=%s: %s",
                             r.check, edit.id, r.severity, r.reason)
        # CRITICAL + REVIEW -> MANUAL: short-circuit, never apply.
        if new_tier is RiskTier.MANUAL and edit.risk_tier is not RiskTier.MANUAL:
            return EditOutcome(
                edit_id=edit.id, op=edit.op, tier=new_tier,
                status="blocked_safety",
                detail="; ".join(f"{r.check}:{r.reason}" for r in failing)[:500],
                safety_findings=[{
                    "check": r.check, "severity": r.severity, "reason": r.reason,
                } for r in failing],
            )

        # Use the new (possibly escalated) tier for the rest of the flow.
        edit = replace(edit, risk_tier=new_tier) if escalated else edit

        if edit.risk_tier is RiskTier.REVIEW:
            # Route to the PROTECTED gate; apply only after a human approves (the
            # gateway /approvals flow resolves it, then apply_approved runs the edit).
            # Honor the global kill-switch: refuse new REVIEW-tier requests when on.
            try:
                from hive.core.approval_enhancements import enhance as _enhance
                if _enhance.is_request_blocked():
                    return EditOutcome(
                        edit_id=edit.id, op=edit.op, tier=edit.risk_tier,
                        status="blocked_kill_switch",
                        detail="kill-switch engaged; no new self-mod requests",
                    )
            except Exception:  # noqa: BLE001
                pass
            approval_id = str(self._gate.request(
                f"self_mod:{edit.op.value}", {"summary": edit.summary}, edit.rationale))
            try:
                from hive.core.approval_enhancements import enhance as _enhance
                _enhance.audit_request(approval_id)
            except Exception:  # noqa: BLE001
                pass
            self._pending_store[approval_id] = edit  # retrieved by gateway on approval
            status = "pending_approval" if not escalated else "escalated_safety"
            detail = "awaiting human approval"
            if escalated and failing:
                detail = ("escalated from AUTO by safety: "
                          + "; ".join(r.reason for r in failing)[:300])
            return EditOutcome(
                edit_id=edit.id, op=edit.op, tier=edit.risk_tier,
                status=status, approval_id=approval_id, detail=detail,
                safety_findings=[{
                    "check": r.check, "severity": r.severity, "reason": r.reason,
                } for r in failing],
            )

        # AUTO: still isolated, still tested, still never merged, still PROTECTED-safe.
        result = await self._mod.propose(edit.summary, edit.rationale, edit.apply,
                                         dry_run=dry_run)
        if not result.get("ok"):
            stage = result.get("stage")
            if stage == "protected":
                outcome = EditOutcome(
                    edit_id=edit.id, op=edit.op, tier=edit.risk_tier,
                    status="blocked_protected",
                    detail=str(result.get("msg", "touches a PROTECTED file")),
                )
            else:
                outcome = EditOutcome(
                    edit_id=edit.id, op=edit.op, tier=edit.risk_tier, status="failed",
                    detail=f"{stage}: {str(result.get('log', ''))[:200]}",
                )
            self._record_outcome(outcome)
            return outcome
        outcome = EditOutcome(
            edit_id=edit.id, op=edit.op, tier=edit.risk_tier, status="applied",
            branch=str(result.get("branch")) if result.get("branch") else None,
            detail=str(result.get("stage", "")),
            safety_findings=[{
                "check": r.check, "severity": r.severity, "reason": r.reason,
            } for r in failing],
        )
        self._record_outcome(outcome)
        return outcome

    def get_pending(self, approval_id: str) -> "Edit | None":
        """Return the pending REVIEW edit for an approval_id, or None if not found."""
        return self._pending_store.get(approval_id)

    def cancel_review(self, approval_id: str) -> bool:
        """Remove a REVIEW-tier edit from the pending store. Returns False if not found."""
        if approval_id in self._pending_store:
            self._pending_store.pop(approval_id)
            return True
        return False

    def pending_count(self) -> int:
        """Number of REVIEW-tier edits awaiting human approval."""
        return len(self._pending_store)

    def get_all_pending(self) -> dict[str, "Edit"]:
        """Return a copy of all pending REVIEW-tier edits keyed by approval_id."""
        return dict(self._pending_store)

    def cancel_all_pending(self) -> int:
        """Cancel all pending REVIEW-tier edits. Returns the count cancelled."""
        count = len(self._pending_store)
        self._pending_store.clear()
        return count

    def describe_pending(self) -> list[dict]:
        """Return a list of metadata dicts for all pending REVIEW-tier edits."""
        result = []
        for approval_id, edit in self._pending_store.items():
            result.append({
                "approval_id": approval_id,
                "edit_id": edit.id,
                "op": edit.op.value,
                "summary": edit.summary,
                "rationale": edit.rationale,
                "risk_tier": edit.risk_tier.value,
            })
        return result

    def oldest_pending_id(self) -> "str | None":
        """Return the approval_id of the oldest pending REVIEW edit (first inserted), or None."""
        return next(iter(self._pending_store), None)

    def tier_summary(self) -> dict:
        """Return a summary of pending edits by op category.

        Returns counts of pending REVIEW edits grouped by EditOp value."""
        counts: dict[str, int] = {}
        for edit in self._pending_store.values():
            key = edit.op.value
            counts[key] = counts.get(key, 0) + 1
        return {"pending_review": len(self._pending_store), "by_op": counts}

    async def apply_approved(self, edit: Edit, *, dry_run: bool = False) -> EditOutcome:
        """Run a REVIEW-tier edit after a human approved it (called by the gateway
        approvals flow). Goes through the same SelfModifier safety path as AUTO.

        Pillar 4: we re-run safety checks here as a final guard — the edit may
        have been queued before safety was enabled, or a human may be approving
        a finding-laden edit. We honour the human's approval: critical safety
        findings are LOGGED as a WARNING and AUDITED, but the edit is still
        attempted (fall-through to SelfModifier.propose). SelfModifier's own
        _touches_protected + test-failure machinery are the real final defences.
        A silent downgrade to blocked_safety here would erase the human's
        approval without trace — that's a worse failure mode than running the
        edit through the existing defences and recording the advisory."""
        results = self._safety_run(edit)
        _, failing = apply_tier_policy(edit.risk_tier, results)
        if results:
            self._safety_audit_log(edit, results, edit.risk_tier, False)
        if highest_severity(results) == "critical" and failing:
            log.warning(
                "self_mod_safety CRITICAL on approved edit %s (op=%s) — honouring "
                "human approval and falling through to SelfModifier; findings: %s",
                edit.id, edit.op.value,
                "; ".join(f"{r.check}:{r.reason}" for r in failing)[:500],
            )

        result = await self._mod.propose(edit.summary, edit.rationale, edit.apply,
                                         dry_run=dry_run)
        if not result.get("ok"):
            stage = result.get("stage")
            status = "blocked_protected" if stage == "protected" else "failed"
            # Match the AUTO-path detail format: "<stage>: <log[:200]>" so callers
            # (memory recording, dashboards) get the same context either way.
            detail = str(result.get("msg") or f"{stage}: {str(result.get('log', ''))[:200]}")
            outcome = EditOutcome(
                edit_id=edit.id, op=edit.op, tier=edit.risk_tier, status=status,
                detail=detail,
                safety_findings=[{
                    "check": r.check, "severity": r.severity, "reason": r.reason,
                } for r in failing],
            )
            self._record_outcome(outcome)
            return outcome
        outcome = EditOutcome(
            edit_id=edit.id, op=edit.op, tier=edit.risk_tier, status="applied",
            branch=str(result.get("branch")) if result.get("branch") else None,
            detail=str(result.get("stage", "")),
            safety_findings=[{
                "check": r.check, "severity": r.severity, "reason": r.reason,
            } for r in failing],
        )
        self._record_outcome(outcome)
        return outcome


async def diagnose_and_run(diagnoser: Diagnoser, context: str,
                           improver: SelfImprovement, *, dry_run: bool = False,
                           ) -> list[EditOutcome]:
    """Full loop: diagnose (injected LLM) -> typed edits -> tier -> gate -> apply.
    Returns one outcome per proposed edit. A diagnoser that returns [] is a no-op."""
    edits = await diagnoser(context)
    if not edits:
        return []
    return await improver.run(edits, dry_run=dry_run)
