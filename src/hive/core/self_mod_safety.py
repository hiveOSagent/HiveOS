"""
self_mod_safety.py — pre-flight safety checks for self-modification proposals.

Pillar 4 hardening: even REVIEW-tier proposals reach /approvals/decide with
insufficient static checks today. This module runs a small set of cheap,
deterministic checks against the Edit *before* it goes to the gate:

  - check_python_syntax      (critical) — compile() must succeed
  - check_dangerous_patterns (warn)     — rm -rf, curl | sh, eval, exec, etc.
  - check_protected_paths    (critical) — reuses _touches_protected
  - check_test_coverage      (warn)     — did the edit remove existing tests?
  - check_file_count         (warn)     — touches > N files = suspicious

Each check is independent and composable. The tier policy in
`should_reject_for_tier` is table-driven so it stays easy to tune.

DAG: depends only on core.self_mod (for _touches_protected). No LLM, no IO.
"""
from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from hive.core.self_mod import _touches_protected

if TYPE_CHECKING:
    from hive.core.spec_search import Edit, RiskTier

log = logging.getLogger("hive.selfmod_safety")


# Severity levels — ordinal so they can be compared.
SEVERITY_INFO = "info"
SEVERITY_WARN = "warn"
SEVERITY_CRITICAL = "critical"

_SEVERITY_ORDER = {SEVERITY_INFO: 0, SEVERITY_WARN: 1, SEVERITY_CRITICAL: 2}


@dataclass(slots=True)
class SafetyCheckResult:
    """Outcome of one safety check.

    `passed=True` means the check found nothing wrong; `passed=False` means a
    problem was detected whose severity is `severity`. `check` is the check
    name (e.g. "python_syntax"); `reason` is a short human-readable string.
    `detail` carries extra structured context (e.g. offending line).
    """
    check: str
    passed: bool
    severity: str
    reason: str = ""
    detail: dict = field(default_factory=dict)

    def __bool__(self) -> bool:  # convenience: `if result: ...`
        return self.passed

    @classmethod
    def ok(cls, check: str) -> "SafetyCheckResult":
        return cls(check=check, passed=True, severity=SEVERITY_INFO, reason="")

    @classmethod
    def fail(cls, check: str, severity: str, reason: str,
             detail: dict | None = None) -> "SafetyCheckResult":
        return cls(check=check, passed=False, severity=severity, reason=reason,
                   detail=detail or {})


# --- individual checks --------------------------------------------------------

def check_python_syntax(code: str) -> SafetyCheckResult:
    """Parse `code` as Python. Fails (critical) if it raises SyntaxError.

    We use `ast.parse` rather than `compile` because the error message and
    location are more useful for the audit log. `mode="exec"` accepts module-
    level snippets (function bodies still parse because dedent happens before
    call sites). Empty/whitespace-only code is OK — caller decides whether
    to invoke us."""
    if not code or not code.strip():
        return SafetyCheckResult.ok("python_syntax")
    try:
        ast.parse(code, mode="exec")
    except SyntaxError as exc:
        return SafetyCheckResult.fail(
            "python_syntax", SEVERITY_CRITICAL,
            f"Python syntax error at line {exc.lineno}: {exc.msg}",
            detail={"line": exc.lineno, "offset": exc.offset,
                    "text": (exc.text or "").strip()[:200]},
        )
    return SafetyCheckResult.ok("python_syntax")


# Dangerous patterns — regex-only, line-by-line so we can report the offender.
# These are NOT a substitute for a real sandbox; they catch obvious attempts
# to ship destructive code into a self-mod edit.
_DANGEROUS_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\brm\s+-rf?\b", "rm -rf / destructive delete"),
    (r"\bdd\s+if=", "dd raw disk write"),
    (r"\bmkfs\b", "mkfs filesystem format"),
    (r"curl\s+[^|]*\|\s*sh\b", "curl | sh remote script exec"),
    (r"wget\s+[^|]*\|\s*sh\b", "wget | sh remote script exec"),
    (r"\beval\s*\(", "eval() dynamic execution"),
    (r"\bexec\s*\(", "exec() dynamic execution"),
    (r"\b__import__\s*\(", "dynamic __import__()"),
    (r"subprocess\.(Popen|call|run|check_output|check_call)\s*\(", "subprocess call"),
    (r"os\.system\s*\(", "os.system() shell call"),
    (r"shutil\.rmtree\s*\(", "shutil.rmtree recursive delete"),
    (r":\(\)\s*\{\s*:\|:\s*&\s*\}\s*;\s*:", "fork bomb pattern"),
)


def check_dangerous_patterns(code: str) -> SafetyCheckResult:
    """Scan `code` for known dangerous shell/Python patterns.

    Severity is `warn` — matches are not always malicious (subprocess.Popen
    is normal in a deploy tool), but they warrant human review for an
    AUTO-tier edit."""
    if not code:
        return SafetyCheckResult.ok("dangerous_patterns")
    hits: list[dict] = []
    for i, line in enumerate(code.splitlines(), start=1):
        for pattern, label in _DANGEROUS_PATTERNS:
            if re.search(pattern, line):
                hits.append({"line": i, "label": label,
                             "snippet": line.strip()[:200]})
                break  # one hit per line is enough
    if hits:
        labels = sorted({h["label"] for h in hits})
        return SafetyCheckResult.fail(
            "dangerous_patterns", SEVERITY_WARN,
            f"dangerous pattern(s) detected: {', '.join(labels)}",
            detail={"hits": hits[:10], "total": len(hits)},
        )
    return SafetyCheckResult.ok("dangerous_patterns")


def check_protected_paths(files: list[str]) -> SafetyCheckResult:
    """Reuse SelfModifier's _touches_protected so we share one source of truth.

    Critical: PROTECTED files (Config/SOUL.md, Core/approval_gate.py) must
    NEVER be modified by the self-mod loop — they are the immutable spine."""
    if not files:
        return SafetyCheckResult.ok("protected_paths")
    offending = [f for f in files if _touches_protected([f])]
    if offending:
        return SafetyCheckResult.fail(
            "protected_paths", SEVERITY_CRITICAL,
            f"edit touches PROTECTED file(s): {', '.join(offending)}",
            detail={"files": offending},
        )
    return SafetyCheckResult.ok("protected_paths")


def check_test_coverage(before_files: list[str], after_files: list[str]) -> SafetyCheckResult:
    """Detect removal of tests.

    A self-mod edit that drops existing test files (without replacing them
    with equally broad coverage) is a coverage regression. We treat any
    `tests/` file present in `before` but absent in `after` as a removal.

    Severity `warn` — legitimate refactors sometimes consolidate tests.
    """
    before_tests = {f for f in before_files if "tests/" in f or "/test_" in f
                    or f.startswith("test_")}
    after_tests = {f for f in after_files if "tests/" in f or "/test_" in f
                   or f.startswith("test_")}
    removed = sorted(before_tests - after_tests)
    if removed:
        return SafetyCheckResult.fail(
            "test_coverage", SEVERITY_WARN,
            f"edit removes test file(s): {', '.join(removed[:5])}"
            + (" ..." if len(removed) > 5 else ""),
            detail={"removed": removed, "count": len(removed)},
        )
    return SafetyCheckResult.ok("test_coverage")


_DEFAULT_MAX_FILES = 20   # warn when an edit touches more than this many files


def check_file_count(files: list[str], *,
                     max_files: int = _DEFAULT_MAX_FILES) -> SafetyCheckResult:
    """Warn if an edit touches an unusually large number of files.

    Heuristic, not a hard rule. A sweeping refactor may legitimately touch
    many files; we just want a human to look before AUTO-applying it."""
    if len(files) > max_files:
        return SafetyCheckResult.fail(
            "file_count", SEVERITY_WARN,
            f"edit touches {len(files)} files (max recommended: {max_files})",
            detail={"count": len(files), "max": max_files},
        )
    return SafetyCheckResult.ok("file_count")


# --- composition --------------------------------------------------------------

def run_all_checks(edit: "Edit", *, before_files: list[str] | None = None,
                   after_files: list[str] | None = None,
                   code: str | None = None,
                   max_files: int = _DEFAULT_MAX_FILES,
                   ) -> list[SafetyCheckResult]:
    """Run every check against `edit` and return a list of SafetyCheckResults.

    `code` is the textual payload of the edit (Python body, patch, script).
    If absent, syntax/dangerous-pattern checks are skipped — the caller may
    not have the code in hand yet. Dangerous-pattern checks run for every
    payload; syntax runs only for a complete file because replacement fragments
    are not valid standalone modules. `before_files`/`after_files` are optional
    file-list snapshots for test-coverage regression detection.

    `check_protected_paths` always runs against the edit's planned target
    files (encoded in the apply fn's last return value when available, else
    skipped — the deep path runs in SelfModifier too).
    """
    results: list[SafetyCheckResult] = []

    # Dangerous patterns apply to every payload. Syntax is scoped to complete
    # files, avoiding false critical findings for valid replacement fragments.
    if code is not None:
        if getattr(edit, "code_is_complete_file", True):
            results.append(check_python_syntax(code))
        results.append(check_dangerous_patterns(code))

    # file-level checks
    if after_files:
        results.append(check_protected_paths(after_files))
        results.append(check_file_count(after_files, max_files=max_files))
    if before_files is not None and after_files is not None:
        results.append(check_test_coverage(before_files, after_files))

    return results


# --- tier policy --------------------------------------------------------------

# Tier policy table. For each (tier, severity) pair, decide whether to reject.
# "Reject" for AUTO means: escalate to REVIEW (still applied after approval).
# "Reject" for REVIEW means: escalate to MANUAL (record only, no PR).
# "Reject" for MANUAL means: already human-only — log but don't escalate further.
#
# critical + AUTO  -> escalate to REVIEW
# critical + REVIEW -> escalate to MANUAL
# critical + MANUAL -> keep MANUAL (log only, already human-only)
# warn     + AUTO  -> escalate to REVIEW
# warn     + REVIEW -> keep REVIEW (log only)
# warn     + MANUAL -> keep MANUAL
# info     + ANY    -> never reject (log only)
#
# Returns True when the proposed tier should be elevated.
def should_reject_for_tier(tier: "RiskTier",
                           results: list[SafetyCheckResult]) -> bool:
    """Return True if the highest-severity failing check requires escalation
    for `tier`. False means the edit may proceed at its current tier.

    Escalation ladder:
      AUTO   + critical -> REVIEW
      AUTO   + warn     -> REVIEW
      REVIEW + critical -> MANUAL
      REVIEW + warn     -> False   (already human-gated)
      MANUAL + critical -> False   (already human-only)
      MANUAL + warn     -> False
      any    + info     -> False
    """
    from hive.core.spec_search import RiskTier  # local import: DAG leaf
    max_sev = SEVERITY_INFO
    for r in results:
        if r.passed:
            continue
        if _SEVERITY_ORDER[r.severity] > _SEVERITY_ORDER[max_sev]:
            max_sev = r.severity

    if max_sev == SEVERITY_INFO:
        return False

    if tier is RiskTier.AUTO:
        return True   # both warn and critical escalate AUTO -> REVIEW
    if tier is RiskTier.REVIEW:
        return max_sev == SEVERITY_CRITICAL  # critical -> MANUAL; warn stays
    # MANUAL
    return False


def apply_tier_policy(tier: "RiskTier",
                      results: list[SafetyCheckResult]
                      ) -> tuple["RiskTier", list[SafetyCheckResult]]:
    """Return the effective tier after applying the policy, plus the list of
    failing checks (used for logging).

    NOTE: when `should_reject_for_tier` returns True, we escalate one step
    (AUTO -> REVIEW, REVIEW -> MANUAL). For MANUAL we leave it alone (already
    terminal) and log.
    """
    from hive.core.spec_search import RiskTier  # DAG leaf
    failing = [r for r in results if not r.passed]
    if not should_reject_for_tier(tier, results):
        return tier, failing

    if tier is RiskTier.AUTO:
        return RiskTier.REVIEW, failing
    if tier is RiskTier.REVIEW:
        return RiskTier.MANUAL, failing
    return RiskTier.MANUAL, failing   # terminal; same tier but logged


def highest_severity(results: list[SafetyCheckResult]) -> str:
    """Return the highest severity among `results`. SEVERITY_INFO if all pass."""
    max_sev = SEVERITY_INFO
    for r in results:
        if r.passed:
            continue
        if _SEVERITY_ORDER[r.severity] > _SEVERITY_ORDER[max_sev]:
            max_sev = r.severity
    return max_sev
