"""
self_mod.py — safe self-modification engine (KEEP+ADAPT from Core/self_mod.py).

How Hive changes its OWN code without destroying itself. The flow from SOUL.md is
non-negotiable:
  1. snapshot last-known-good HEAD (instant rollback)
  2. isolated git worktree on a new branch (never live main)
  3. apply changes only inside the worktree
  4. run tests in the candidate
  5. fail -> discard worktree, stay on last-known-good, record
  6. pass -> commit + push branch + open PR (NEVER merge); a human merges
Changes touching SOUL.md / approval_gate.py are refused outright.

`dry_run=True` runs steps 1–4 and skips push/PR (the P8 verify). The shell runner
is injectable so the flow is unit-testable without real git. Depends on core only.
"""
from __future__ import annotations

import asyncio
import logging
import os
import posixpath
import subprocess
import time
from pathlib import Path
from typing import Awaitable, Callable

from hive.core.approval import PROTECTED_PATHS
from hive.core.events import EventBus, EventType

log = logging.getLogger("hive.selfmod")

# (cmd, cwd) -> (returncode, combined_output)
# cmd may be a list (exec, safe) or a plain string (shell, for trusted git sub-commands).
Runner = Callable[[str | list[str], str | None], Awaitable[tuple[int, str]]]
# (worktree_path) -> list of changed repo-relative paths
ApplyFn = Callable[[str], Awaitable[list[str]]]


async def _default_run(cmd: str | list[str], cwd: str | None = None) -> tuple[int, str]:
    child_env = {key: value for key, value in os.environ.items()
                 if key != "HIVE_APPROVER_KEY"}
    if isinstance(cmd, list):
        # Use exec (no shell interpretation) for commands with LLM-sourced arguments.
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=child_env)
    else:
        proc = await asyncio.create_subprocess_shell(
            cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=child_env)
    out, _ = await proc.communicate()
    if proc.returncode is None:
        raise RuntimeError("subprocess finished without a return code")
    return int(proc.returncode), out.decode()


# (branch, title, body) -> PR url (or None if opening failed). Injected so self_mod
# stays testable without the network; the runtime wires github_pr_opener().
PROpener = Callable[[str, str, str], Awaitable[str | None]]


def github_pr_opener(token: str, owner: str, repo: str, *, base: str = "main",
                     draft: bool = True) -> PROpener:
    """Real PR opener over the GitHub REST API using Hive's own token (#si-3).

    Hive opens a DRAFT PR from its pushed branch and NEVER merges — a human merges
    (SOUL.md hard rule). httpx is imported lazily so importing self_mod never
    requires it."""
    async def open_pr(branch: str, title: str, body: str) -> str | None:
        if not (token and owner and repo):
            return None
        import httpx
        url = f"https://api.github.com/repos/{owner}/{repo}/pulls"
        payload = {"title": title, "head": branch, "base": base,
                   "body": body, "draft": draft}
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(url, json=payload, headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                })
            if r.status_code in (200, 201):
                return r.json().get("html_url")
            log.warning("PR open failed (%s): %s", r.status_code, r.text[:300])
        except Exception as exc:  # noqa: BLE001 - PR opening is best-effort
            log.warning("PR open error: %s", exc)
        return None

    return open_pr


_PROTECTED_NAMES = {p.rsplit("/", 1)[-1].lower() for p in PROTECTED_PATHS}
_PROTECTED_PATHS_LOWER = {p.lower().replace("\\", "/") for p in PROTECTED_PATHS}


def _touches_protected(changed: list[str]) -> bool:
    """True if any changed path is a PROTECTED file."""
    for cp in changed:
        norm = cp.replace("\\", "/").lower()
        if any(norm == pp or norm.endswith("/" + pp) for pp in _PROTECTED_PATHS_LOWER):
            return True
        basename = norm.rsplit("/", 1)[-1]
        if basename in _PROTECTED_NAMES:
            return True
    return False


def _normalize_changed_path(path: str) -> str:
    """Return one normalized, repository-relative comparison form for a path."""
    normalized = posixpath.normpath(path.replace("\\", "/"))
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


async def _actual_changed_files(run: Runner, worktree: str) -> tuple[int, list[str], str]:
    """Read every tracked and untracked candidate change from Git.

    The callback's result is only an assertion. Git is the source of truth before
    tests, staging, committing, and pushing are allowed to proceed.
    """
    rc, diff_out = await run(["git", "diff", "--name-only", "--no-renames", "HEAD", "--"], worktree)
    if rc != 0:
        return rc, [], diff_out
    rc, untracked_out = await run(
        ["git", "ls-files", "--others", "--exclude-standard"], worktree,
    )
    if rc != 0:
        return rc, [], untracked_out
    changed = {
        _normalize_changed_path(line)
        for line in (diff_out + "\n" + untracked_out).splitlines()
        if line.strip()
    }
    return 0, sorted(path for path in changed if path), ""


def _review_required_paths(paths: list[str]) -> list[str]:
    """Return actual paths that cannot remain on the autonomous AUTO path."""
    # This import must stay lazy: spec_search imports SelfModifier.
    from hive.core.spec_search import path_requires_review

    return [path for path in paths if path_requires_review(path)]


async def _verify_candidate_changes(
    run: Runner, worktree: str, reported_changed: list[str], *, approved_review: bool,
) -> dict:
    """Fail closed unless Git matches the callback and policy permits the paths."""
    if not isinstance(reported_changed, list) or any(
        not isinstance(path, str) for path in reported_changed
    ):
        return {
            "ok": False,
            "stage": "changed_files",
            "msg": "apply_fn must report a list of repository-relative paths",
        }
    rc, actual_changed, error = await _actual_changed_files(run, worktree)
    if rc != 0:
        return {
            "ok": False,
            "stage": "changed_files",
            "msg": "unable to verify candidate worktree changes",
            "log": error[-1000:],
        }
    reported_set = {_normalize_changed_path(path) for path in reported_changed}
    actual_set = set(actual_changed)
    if _touches_protected(actual_changed):
        return {
            "ok": False,
            "stage": "protected",
            "msg": "actual change touches SOUL.md or approval gate — human-only",
        }
    if reported_set != actual_set:
        log.warning(
            "self_mod BLOCKED: callback paths differ from Git paths; reported=%s actual=%s",
            sorted(reported_set), actual_changed,
        )
        return {
            "ok": False,
            "stage": "changed_files",
            "msg": "apply_fn file list does not match actual Git changes",
            "reported": sorted(reported_set),
            "actual": actual_changed,
        }
    review_paths = _review_required_paths(actual_changed)
    if review_paths and not approved_review:
        return {
            "ok": False,
            "stage": "review_required",
            "msg": "actual change requires REVIEW tier before commit",
            "review_paths": review_paths,
            "changed": actual_changed,
        }
    return {"changed": actual_changed}


_MAX_HISTORY = 50   # keep at most this many proposal records in memory


def _parse_worktree_list(porcelain: str) -> list[tuple[str, str]]:
    """Parse `git worktree list --porcelain` output into (path, branch) pairs.

    Detached worktrees (no ``branch`` line) are skipped — SelfModifier always
    creates a named branch (``hive/auto-<ts>``), so a detached entry can never
    be one of ours.
    """
    out: list[tuple[str, str]] = []
    path: str | None = None
    branch: str | None = None
    for line in porcelain.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):].strip()
            branch = None
        elif line.startswith("branch "):
            ref = line[len("branch "):].strip()
            branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
        elif line == "":
            if path and branch:
                out.append((path, branch))
            path = None
            branch = None
    if path and branch:
        out.append((path, branch))
    return out


class SelfModifier:
    def __init__(self, *, repo_root: str = ".", run: Runner | None = None,
                 test_cmd: str = "python -m pytest -q",
                 open_pr: PROpener | None = None,
                 bus: EventBus | None = None) -> None:
        self._root = repo_root
        self._run = run or _default_run
        self._test_cmd = test_cmd
        self._open_pr = open_pr
        self._bus = bus
        self._history: list[dict] = []   # recent proposal outcomes (capped at _MAX_HISTORY)

    def _emit(self, event_type: EventType, data: dict) -> None:
        if self._bus is not None:
            try:
                self._bus.publish(event_type, data)
            except Exception:  # noqa: BLE001 - observability must not break self-mod
                pass

    def history(self, limit: int = 20) -> list[dict]:
        """Return the most recent proposal outcomes (newest first), capped to `limit`."""
        return list(reversed(self._history[-_MAX_HISTORY:]))[:limit]

    @property
    def last_result(self) -> dict | None:
        """The outcome dict from the most recent propose() call, or None."""
        return self._history[-1] if self._history else None

    def recent_branches(self, n: int = 5) -> list[str]:
        """Return up to n branch names from the most recent successful proposals (newest first)."""
        branches = []
        for record in reversed(self._history[-_MAX_HISTORY:]):
            if record.get("ok") and record.get("branch"):
                branches.append(record["branch"])
                if len(branches) >= n:
                    break
        return branches

    def clear_history(self) -> int:
        """Discard all recorded proposal history. Returns the count cleared."""
        count = len(self._history)
        self._history = []
        return count

    def proposal_count(self) -> int:
        """Return the total number of proposals recorded in history (capped at _MAX_HISTORY)."""
        return len(self._history)

    def success_rate(self) -> float:
        """Fraction of proposals that succeeded (ok=True). Returns 0.0 if no history."""
        if not self._history:
            return 0.0
        ok = sum(1 for r in self._history if r.get("ok"))
        return round(ok / len(self._history), 4)

    def failed_proposals(self, limit: int = 10) -> list[dict]:
        """Return the most recent failed proposals (ok=False), newest first."""
        failed = [r for r in reversed(self._history[-_MAX_HISTORY:]) if not r.get("ok")]
        return failed[:max(1, limit)]

    def proposals_by_stage(self) -> dict[str, int]:
        """Return a count of proposals grouped by their terminal stage.

        Useful for spotting patterns: if 'test' dominates, the tests are too brittle;
        if 'protected' dominates, the diagnoser keeps targeting locked files."""
        counts: dict[str, int] = {}
        for r in self._history:
            stage = str(r.get("stage") or "unknown")
            counts[stage] = counts.get(stage, 0) + 1
        return counts

    async def sweep_orphaned_worktrees(self) -> dict:
        """Startup crash-recovery: reclaim any self-mod worktree/branch left behind
        by a process that was killed mid-``propose()`` — between ``git worktree
        add`` and the ``finally`` cleanup in ``_propose_inner`` (which only runs
        on a normal return/exception, never on SIGKILL/OOM/container restart).

        Mirrors ``TaskBoard.requeue_running()`` for the self-mod side of
        autonomy: it does NOT try to resume the half-finished edit (the
        `apply_fn` closure that produced it is gone with the dead process
        anyway) — it only reclaims disk/git state. Per ADR 005's fail-forward
        philosophy, the heartbeat re-detects the original symptom and
        re-proposes a fresh edit on its own; this just stops orphaned
        ``.worktrees/hive-auto-*`` directories and branches from accumulating
        forever.

        Safe to call on a clean start: with nothing orphaned, ``git worktree
        list`` has no ``hive/auto-*`` entries and this is a no-op.
        """
        removed: list[str] = []
        errors: list[str] = []
        rc, out = await self._run(["git", "worktree", "list", "--porcelain"], self._root)
        if rc != 0:
            return {"removed": removed, "errors": [out[:300]]}
        for path, branch in _parse_worktree_list(out):
            # Defense in depth: require BOTH the branch name and the worktree
            # path to match what _propose_inner actually creates (path derives
            # from branch via `.replace("/", "-")`, see the `wt =` line above).
            # A branch-name-only check would delete a human's worktree if they
            # ever happened to name a branch `hive/auto-<anything>` by hand;
            # this way that would need the exact matching directory too.
            if not branch.startswith("hive/auto-"):
                continue
            expected_dir = Path(self._root) / ".worktrees" / branch.replace("/", "-")
            if Path(path).resolve() != expected_dir.resolve():
                continue
            rc2, out2 = await self._run(["git", "worktree", "remove", "--force", path],
                                        self._root)
            if rc2 != 0:
                errors.append(f"{path}: {out2[:200]}")
                continue
            removed.append(path)
            rc3, out3 = await self._run(["git", "branch", "-D", branch], self._root)
            if rc3 != 0:
                log.warning("self_mod: orphaned branch cleanup failed for %s: %s",
                           branch, out3[:200])
        # Clear stale metadata for any worktree whose directory is already gone
        # (e.g. the container's ephemeral disk was wiped but .git/worktrees
        # bookkeeping survived on a persistent volume).
        await self._run(["git", "worktree", "prune"], self._root)
        if removed:
            log.info("self_mod: swept %d orphaned worktree(s) from a prior crashed run: %s",
                     len(removed), removed)
        return {"removed": removed, "errors": errors}

    async def propose(self, title: str, description: str, apply_fn: ApplyFn,
                      *, dry_run: bool = False, approved_review: bool = False) -> dict:
        self._emit(EventType.SELFMOD_START, {"title": title, "dry_run": dry_run})
        result = await self._propose_inner(
            title, description, apply_fn, dry_run=dry_run, approved_review=approved_review,
        )
        self._emit(EventType.SELFMOD_END, {
            "title": title, "ok": result.get("ok"), "stage": result.get("stage"),
            "branch": result.get("branch"), "dry_run": dry_run,
        })
        # Record in history (trim to _MAX_HISTORY).
        record = {"title": title, "dry_run": dry_run, "ts": time.time(),
                  "ok": result.get("ok"), "stage": result.get("stage"),
                  "branch": result.get("branch")}
        self._history.append(record)
        if len(self._history) > _MAX_HISTORY:
            self._history = self._history[-_MAX_HISTORY:]
        return result

    async def propose_approved(self, title: str, description: str, apply_fn: ApplyFn,
                               *, dry_run: bool = False) -> dict:
        """Run a human-approved REVIEW edit through the isolated modifier flow."""
        return await self.propose(
            title, description, apply_fn, dry_run=dry_run, approved_review=True,
        )

    async def _propose_inner(self, title: str, description: str, apply_fn: ApplyFn,
                             *, dry_run: bool = False, approved_review: bool = False) -> dict:
        branch = f"hive/auto-{int(time.time())}"
        wt = str(Path(self._root) / ".worktrees" / branch.replace("/", "-"))

        _, head = await self._run("git rev-parse HEAD", self._root)
        last_good = head.strip()

        rc, out = await self._run(f"git worktree add -b {branch} {wt}", self._root)
        if rc != 0:
            return {"ok": False, "stage": "worktree", "log": out}
        try:
            reported_changed = await apply_fn(wt)
            if isinstance(reported_changed, list) and _touches_protected(reported_changed):
                log.warning("self_mod BLOCKED: proposed edit touches protected files: %s",
                            [p for p in reported_changed if _touches_protected([p])])
                return {"ok": False, "stage": "protected",
                        "msg": "change touches SOUL.md or approval gate — human-only"}

            verified = await _verify_candidate_changes(
                self._run, wt, reported_changed, approved_review=approved_review,
            )
            if verified.get("ok") is False:
                return verified
            changed = verified["changed"]

            rc, test_out = await self._run(self._test_cmd, wt)
            if rc != 0:
                return {"ok": False, "stage": "test", "last_good": last_good,
                        "log": test_out[-2000:], "recorded": True}

            # Tests/callbacks must not add or alter paths after the initial check
            # and before `git add -A` below.
            verified = await _verify_candidate_changes(
                self._run, wt, reported_changed, approved_review=approved_review,
            )
            if verified.get("ok") is False:
                return verified
            changed = verified["changed"]

            if dry_run:
                return {"ok": True, "stage": "dry_run", "branch": branch,
                        "last_good": last_good, "changed": changed}

            await self._run("git add -A", wt)
            # Abort early if apply_fn made no actual changes (avoids empty-commit error).
            _, status_out = await self._run("git status --porcelain", wt)
            if not status_out.strip():
                return {"ok": False, "stage": "no_changes",
                        "msg": "apply_fn produced no file changes"}
            # Use list form (exec, not shell) so LLM-sourced title cannot inject shell.
            title = title.replace("\n", " ").replace("\r", " ")[:120]
            await self._run(["git", "commit", "-m", title], wt)
            rc, push_out = await self._run(f"git push -u origin {branch}", wt)
            if rc != 0:
                # Push failed (auth/network) — surface it instead of falsely reporting ok.
                return {"ok": False, "stage": "push", "branch": branch,
                        "last_good": last_good, "log": push_out[-500:]}

            result = {"ok": True, "stage": "pushed", "branch": branch,
                      "last_good": last_good, "push": push_out[-500:]}
            # #si-3: open a DRAFT PR via the GitHub REST API; never merge (human merges).
            if self._open_pr is not None:
                pr_body = (
                    f"## Summary\n\n{description or title}\n\n"
                    f"## Changed files\n\n"
                    + "".join(f"- `{f}`\n" for f in changed)
                    + "\n## Safety\n\n"
                    "- Proposed by Hive's self-improvement loop\n"
                    "- Tests passed in isolated git worktree before this PR was opened\n"
                    "- **Hive never merges — a human reviews and merges**\n"
                    f"\nBranch: `{branch}` | Base commit: `{last_good[:8]}`"
                )
                pr_url = await self._open_pr(branch, title, pr_body)
                result["pr_url"] = pr_url
                result["note"] = ("draft PR opened by Hive; a human merges"
                                  if pr_url else "branch pushed; PR open failed (see logs)")
            else:
                result["note"] = "branch pushed; open a PR to review (Hive never merges)"
            return result
        finally:
            rc, out = await self._run(f"git worktree remove --force {wt}", self._root)
            if rc != 0:
                log.warning("self_mod: worktree cleanup failed for %s: %s", wt, out[:200])
            if not dry_run:
                # branch is pushed (or never created on failure); local branch is disposable
                rc, out = await self._run(f"git branch -D {branch}", self._root)
                if rc != 0:
                    log.warning("self_mod: branch cleanup failed for %s: %s", branch, out[:200])
