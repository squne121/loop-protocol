#!/usr/bin/env python3
"""Controlled stage/commit executor (Issue #1611).

Owns the entire literal-pathspec stage -> rename-aware audit -> commit ->
post-commit re-audit transaction as a single trusted boundary. Raw
`git add` / `git commit` (and the legacy `rtk git add` / `rtk git commit`
shapes previously allowed by `git_mutation_command_policy.py`) are denied
for agent lanes outside this executor (AC9) -- this module is the only
authorized path that mutates the index/commit graph for agent-driven
changes.

Design notes (documented per Issue #1611 "In Scope" -- private index vs.
plain index tradeoff):
  A private `GIT_INDEX_FILE` + `git update-ref` compare-and-swap primitive
  would eliminate the residual "another process mutates the shared index or
  moves HEAD between our pre-check and our commit" race window entirely.
  This implementation instead uses the repository's normal index and closes
  the race window with three narrower, composable checks that are
  sufficient for the actual agent-lane threat model (a single controlled
  executor process, invoked from one worktree, with no concurrent writers
  expected in the same worktree):
    1. `expected_head` is re-verified via a live `git rev-parse HEAD`
       immediately before staging AND immediately before commit (AC8).
    2. The staged set is re-read from the index via `git diff --cached
       --name-status -M -z` (never trusted from the argv we requested) and
       compared against the requested set before commit proceeds (AC7).
    3. A post-commit re-audit re-reads the committed diff and rolls the
       commit back (`git reset --mixed <prior_head>`) if it disagrees with
       what was staged, rather than leaving an unverified commit in place.
  Callers that need true compare-and-swap semantics (e.g. multiple
  concurrent controlled-executor invocations against the same worktree)
  are out of scope for this Issue -- the residual race is documented in
  `docs/dev/agent-runtime-ops.md`.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from changed_file_matcher import (  # noqa: E402
    AllowedPathsMatcher,
    ChangedFileRecord,
    parse_git_diff_name_status_z,
)
from protected_paths_policy import (  # noqa: E402
    PROTECTED_PATHS_POLICY_VERSION,
    is_protected_path,
)

SCOPE_SNAPSHOT_SCHEMA = "ISSUE_SCOPE_SNAPSHOT_V1"

# ─── Authority version state machine (AC12) ──────────────────────────────────

AUTHORITY_STATE_OLD_ONLY = "old_only"
AUTHORITY_STATE_MIGRATION_VALIDATION = "migration_validation"
AUTHORITY_STATE_NEW_ONLY = "new_only"
AUTHORITY_STATE_ROLLBACK_TO_OLD = "rollback_to_old"
AUTHORITY_STATES = frozenset(
    {
        AUTHORITY_STATE_OLD_ONLY,
        AUTHORITY_STATE_MIGRATION_VALIDATION,
        AUTHORITY_STATE_NEW_ONLY,
        AUTHORITY_STATE_ROLLBACK_TO_OLD,
    }
)

# Env var an operator sets to force the legacy (pre-#1611) env-only lane back
# on, e.g. during an incident. Independent of CODEX_ALLOWED_PATHS itself.
ROLLBACK_ENV_VAR = "LOOP_CONTROLLED_GIT_CHANGE_EXEC_ROLLBACK_TO_OLD"


def resolve_authority_version(
    *, legacy_env_present: bool, snapshot_present: bool, rollback_requested: bool = False
) -> tuple[str, bool]:
    """Return `(authority_state, snapshot_is_authority)`.

    Exactly one of "legacy env" / "new snapshot" is authoritative at any
    decision point -- `snapshot_is_authority` is a single boolean, never a
    pair of independently-true flags (AC12). `migration_validation` means
    both exist (parallel-run period) but only the snapshot governs the
    actual staging/commit decision; the legacy env is validated against it
    for drift detection only, never independently authoritative.
    """
    if rollback_requested:
        return AUTHORITY_STATE_ROLLBACK_TO_OLD, False
    if snapshot_present and legacy_env_present:
        return AUTHORITY_STATE_MIGRATION_VALIDATION, True
    if snapshot_present:
        return AUTHORITY_STATE_NEW_ONLY, True
    return AUTHORITY_STATE_OLD_ONLY, False


# ─── Scope snapshot (AC1) ─────────────────────────────────────────────────────


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compute_allowed_paths_normalized_sha256(allowed_paths: Sequence[str]) -> str:
    """Deterministic sha256 over the *normalized* Allowed Paths pattern set
    (sorted, so declaration order in the Issue body does not change the
    fingerprint)."""
    normalized: List[str] = []
    for pattern in allowed_paths:
        norm = AllowedPathsMatcher.normalize_allowed_pattern(pattern)
        if norm is not None:
            normalized.append(norm)
    canonical = json.dumps(sorted(set(normalized)), separators=(",", ":"))
    return sha256_hex(canonical.encode("utf-8"))


def build_scope_snapshot(
    *,
    issue_number: int,
    contract_body_sha256: str,
    allowed_paths: Sequence[str],
    base_ref: str,
    base_sha: str,
    worktree_path: str,
    generated_at: str,
    protected_paths_policy_version: str = PROTECTED_PATHS_POLICY_VERSION,
    legacy_env_present: bool = False,
    rollback_requested: bool = False,
) -> Dict[str, Any]:
    """Build an `ISSUE_SCOPE_SNAPSHOT_V1` dict binding the fields required by
    AC1: Issue body_sha256, Allowed Paths normalized sha256, base branch/sha,
    worktree realpath, and protected_paths_policy_version."""
    worktree_realpath = os.path.realpath(worktree_path)
    authority_state, snapshot_is_authority = resolve_authority_version(
        legacy_env_present=legacy_env_present,
        snapshot_present=True,
        rollback_requested=rollback_requested,
    )
    return {
        "schema": SCOPE_SNAPSHOT_SCHEMA,
        "issue_number": issue_number,
        "body_sha256": contract_body_sha256,
        "allowed_paths_normalized_sha256": compute_allowed_paths_normalized_sha256(allowed_paths),
        "allowed_paths": list(allowed_paths),
        "base_ref": base_ref,
        "base_sha": base_sha,
        "worktree_realpath": worktree_realpath,
        "protected_paths_policy_version": protected_paths_policy_version,
        "authority_version": authority_state,
        "snapshot_is_authority": snapshot_is_authority,
        "generated_at": generated_at,
    }


SCOPE_SNAPSHOT_REQUIRED_FIELDS: tuple[str, ...] = (
    "schema",
    "issue_number",
    "body_sha256",
    "allowed_paths_normalized_sha256",
    "base_ref",
    "base_sha",
    "worktree_realpath",
    "protected_paths_policy_version",
)


def validate_scope_snapshot_shape(snapshot: Dict[str, Any]) -> List[str]:
    """Return a list of missing/invalid required fields (empty == valid)."""
    problems: List[str] = []
    for field_name in SCOPE_SNAPSHOT_REQUIRED_FIELDS:
        if field_name not in snapshot or snapshot[field_name] in (None, ""):
            problems.append(field_name)
    return problems


# ─── Pathspec classification (AC5, AC6) ──────────────────────────────────────

_PATHSPEC_MAGIC_PREFIXES = (":(", ":!", ":^")


@dataclass(frozen=True)
class PathspecRejection:
    pathspec: str
    reason_code: str


def classify_pathspecs(pathspecs: Sequence[str], cwd: str) -> tuple[List[str], List[PathspecRejection]]:
    """Split `pathspecs` into `(literal_paths, rejections)`.

    Rejects (fail-closed, never literal-izes a guess):
      - broad roots: ".", "..", ":/", ""
      - pathspec magic: ":(...)", ":!", ":^"
      - glob characters: * ? [ ]
      - directory pathspecs (resolves to an existing directory) -- the
        executor only stages individually-named files, never a directory
        (which would silently pull in files the caller did not enumerate)
      - absolute paths / paths escaping the repository root
    """
    literal_paths: List[str] = []
    rejections: List[PathspecRejection] = []
    for pathspec in pathspecs:
        if pathspec in {"", ".", "..", ":/"}:
            rejections.append(PathspecRejection(pathspec, "broad_pathspec_root"))
            continue
        if any(pathspec.startswith(prefix) for prefix in _PATHSPEC_MAGIC_PREFIXES):
            rejections.append(PathspecRejection(pathspec, "pathspec_magic_rejected"))
            continue
        if any(ch in pathspec for ch in "*?[]"):
            rejections.append(PathspecRejection(pathspec, "pathspec_glob_rejected"))
            continue
        if pathspec.startswith("/") or "\x00" in pathspec:
            rejections.append(PathspecRejection(pathspec, "pathspec_invalid"))
            continue
        normalized = AllowedPathsMatcher.normalize_path(pathspec)
        if normalized is None:
            rejections.append(PathspecRejection(pathspec, "pathspec_invalid"))
            continue
        resolved = os.path.realpath(os.path.join(cwd, pathspec))
        if os.path.isdir(resolved):
            rejections.append(PathspecRejection(pathspec, "directory_pathspec_rejected"))
            continue
        literal_paths.append(normalized)
    return literal_paths, rejections


# ─── Change-type classification (AC4) ────────────────────────────────────────

SUBMODULE_MODE = "160000"


def _cached_raw_records(cwd: str, timeout: int = 10) -> List[Dict[str, str]]:
    """Return parsed `git diff --cached --raw -z` records:
    `{old_mode, new_mode, status, path, previous_path}`."""
    result = subprocess.run(
        ["git", "diff", "--cached", "--raw", "-M", "-z"],
        cwd=cwd,
        capture_output=True,
        text=False,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git diff --cached --raw failed: {result.stderr!r}")
    tokens = [tok for tok in result.stdout.split(b"\x00") if tok != b""]
    records: List[Dict[str, str]] = []
    i = 0
    while i < len(tokens):
        header = tokens[i].decode("utf-8", errors="replace")
        i += 1
        if not header.startswith(":"):
            raise ValueError(f"malformed raw diff header: {header!r}")
        parts = header[1:].split(" ")
        if len(parts) < 5:
            raise ValueError(f"malformed raw diff header fields: {header!r}")
        old_mode, new_mode, _old_sha, _new_sha, status_field = parts[0], parts[1], parts[2], parts[3], parts[4]
        letter = status_field[0]
        if letter in ("R", "C"):
            if i + 1 >= len(tokens):
                raise ValueError("malformed raw diff rename/copy record: missing old/new path")
            old_path = tokens[i].decode("utf-8", errors="replace")
            new_path = tokens[i + 1].decode("utf-8", errors="replace")
            i += 2
            records.append(
                {
                    "old_mode": old_mode,
                    "new_mode": new_mode,
                    "status": letter,
                    "path": new_path,
                    "previous_path": old_path,
                }
            )
        else:
            if i >= len(tokens):
                raise ValueError("malformed raw diff record: missing path")
            path = tokens[i].decode("utf-8", errors="replace")
            i += 1
            records.append(
                {
                    "old_mode": old_mode,
                    "new_mode": new_mode,
                    "status": letter,
                    "path": path,
                    "previous_path": None,
                }
            )
    return records


def classify_change(record: ChangedFileRecord, raw_mode_info: Optional[Dict[str, str]]) -> str:
    """Classify a single changed-file record into an exclusive change-type
    vocabulary. Submodule (gitlink) changes are detected via mode 160000 and
    take priority over the name-status letter classification, because git
    reports submodule add/remove/update using the ordinary A/D/M letters."""
    if raw_mode_info is not None:
        if raw_mode_info.get("old_mode") == SUBMODULE_MODE or raw_mode_info.get("new_mode") == SUBMODULE_MODE:
            return "submodule_change"
    if record.status == "type_changed":
        return "type_changed"
    if record.status == "removed":
        # Translate the shared `changed_file_matcher` vocabulary ("removed",
        # kept for parity with the pre-existing `allowed_paths_review_gate.py`
        # consumer) into this executor's more explicit AC4 classification
        # term ("deleted").
        return "deleted"
    return record.status


# ─── Result shape ─────────────────────────────────────────────────────────────


@dataclass
class ControlledChangeResult:
    status: str  # "ok" | "deny"
    reason_code: str
    commit_sha: Optional[str] = None
    staged_paths: List[str] = field(default_factory=list)
    classifications: Dict[str, str] = field(default_factory=dict)
    rejections: List[Dict[str, str]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "reason_code": self.reason_code,
            "commit_sha": self.commit_sha,
            "staged_paths": self.staged_paths,
            "classifications": self.classifications,
            "rejections": self.rejections,
            "errors": self.errors,
        }


def _deny(reason_code: str, **kwargs: Any) -> ControlledChangeResult:
    return ControlledChangeResult(status="deny", reason_code=reason_code, **kwargs)


def _git_rev_parse_head(cwd: str) -> Optional[str]:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        return None
    head = result.stdout.strip()
    return head or None


def _rollback_commit(cwd: str, prior_head: str) -> bool:
    """Best-effort rollback: reset HEAD (and the index) back to `prior_head`
    while preserving the working tree changes (`git reset --mixed`), so an
    unverified commit is never left in place. Returns True on success."""
    result = subprocess.run(
        ["git", "reset", "--mixed", prior_head],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return result.returncode == 0


def execute_controlled_stage_commit(
    *,
    cwd: str,
    snapshot: Dict[str, Any],
    requested_paths: Sequence[str],
    commit_message: str,
    expected_head: str,
    current_body_sha256: str,
    current_allowed_paths: Sequence[str],
) -> ControlledChangeResult:
    """Execute the single stage -> audit -> commit -> re-audit transaction
    (AC2, AC3, AC4, AC5, AC6, AC7, AC8)."""
    problems = validate_scope_snapshot_shape(snapshot)
    if problems:
        return _deny("scope_snapshot_incomplete", errors=problems)

    # AC8: stale snapshot detection -- recompute against freshly supplied
    # contract state and compare to what the snapshot bound.
    if current_body_sha256 != snapshot["body_sha256"]:
        return _deny("stale_snapshot_body_sha256_drift")
    current_allowed_paths_sha = compute_allowed_paths_normalized_sha256(current_allowed_paths)
    if current_allowed_paths_sha != snapshot["allowed_paths_normalized_sha256"]:
        return _deny("stale_snapshot_allowed_paths_drift")

    # AC8: worktree binding.
    if os.path.realpath(cwd) != snapshot["worktree_realpath"]:
        return _deny("worktree_mismatch")

    # AC8: HEAD/branch race -- verify HEAD has not moved before we touch the
    # index at all.
    head_before = _git_rev_parse_head(cwd)
    if head_before is None or head_before != expected_head:
        return _deny("head_race_detected_before_stage")

    # AC6: literal-ize / reject pathspecs.
    literal_paths, rejections = classify_pathspecs(requested_paths, cwd)
    if rejections:
        return _deny(
            "pathspec_rejected",
            rejections=[{"pathspec": r.pathspec, "reason_code": r.reason_code} for r in rejections],
        )
    if not literal_paths:
        return _deny("no_pathspecs_supplied")

    # AC10: protected paths deny regardless of Allowed Paths.
    protected_hits = [p for p in literal_paths if is_protected_path(p)]
    if protected_hits:
        return _deny(
            "protected_path_denied",
            rejections=[{"pathspec": p, "reason_code": "protected_path"} for p in protected_hits],
        )

    # Pre-stage Allowed Paths check on the requested (pre-rename-detection)
    # identities.
    allowed_paths = snapshot["allowed_paths"]
    outside = [p for p in literal_paths if not AllowedPathsMatcher.is_file_allowed(p, allowed_paths)]
    if outside:
        return _deny(
            "pathspec_outside_allowed_paths",
            rejections=[{"pathspec": p, "reason_code": "outside_allowed_paths"} for p in outside],
        )

    # AC5: stage via NUL-delimited pathspec-from-file so unicode / newline /
    # quote / leading-dash paths are handled safely without shell quoting.
    stdin_payload = "\x00".join(literal_paths) + "\x00"
    add_result = subprocess.run(
        ["git", "add", "--pathspec-from-file=-", "--pathspec-file-nul", "--"],
        cwd=cwd,
        input=stdin_payload,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if add_result.returncode != 0:
        return _deny("git_add_failed", errors=[add_result.stderr.strip()])

    # AC3: re-read the index via the rename-aware, NUL-delimited source of
    # truth -- never trust the argv we requested as the staged set.
    try:
        diff_result = subprocess.run(
            ["git", "diff", "--cached", "--name-status", "-M", "-z"],
            cwd=cwd,
            capture_output=True,
            text=False,
            timeout=30,
            check=False,
        )
        if diff_result.returncode != 0:
            return _deny("git_diff_cached_failed", errors=[diff_result.stderr.decode("utf-8", "replace")])
        records = parse_git_diff_name_status_z(
            diff_result.stdout.decode("utf-8", errors="replace"),
            source="git_diff_cached_name_status_m_z",
        )
        raw_records = _cached_raw_records(cwd)
    except (ValueError, RuntimeError) as exc:
        subprocess.run(["git", "reset", "--mixed", head_before], cwd=cwd, capture_output=True, timeout=10, check=False)
        return _deny("staged_index_audit_parse_error", errors=[str(exc)])

    raw_by_path: Dict[str, Dict[str, str]] = {r["path"]: r for r in raw_records}

    # AC3/AC4: check BOTH old and new path (renames) plus deletion/type
    # change/submodule against Allowed Paths + protected paths.
    touched_identities: set[str] = set()
    classifications: Dict[str, str] = {}
    policy_violations: List[Dict[str, str]] = []
    for record in records:
        touched_identities.add(record.path)
        candidates = [record.path]
        if record.previous_path:
            touched_identities.add(record.previous_path)
            candidates.append(record.previous_path)
        for candidate in candidates:
            if is_protected_path(candidate):
                policy_violations.append({"pathspec": candidate, "reason_code": "protected_path"})
            elif not AllowedPathsMatcher.is_file_allowed(candidate, allowed_paths):
                policy_violations.append({"pathspec": candidate, "reason_code": "outside_allowed_paths"})
        classifications[record.path] = classify_change(record, raw_by_path.get(record.path))

    if policy_violations:
        subprocess.run(["git", "reset", "--mixed", head_before], cwd=cwd, capture_output=True, timeout=10, check=False)
        return _deny("staged_change_outside_policy", rejections=policy_violations)

    # AC7: staged set must equal requested set exactly (no silent extra
    # files, no silently-dropped files).
    if touched_identities != set(literal_paths):
        subprocess.run(["git", "reset", "--mixed", head_before], cwd=cwd, capture_output=True, timeout=10, check=False)
        return _deny(
            "staged_requested_mismatch",
            errors=[
                f"requested={sorted(literal_paths)}",
                f"staged={sorted(touched_identities)}",
            ],
        )

    # AC8: re-confirm HEAD has not moved immediately before commit.
    head_before_commit = _git_rev_parse_head(cwd)
    if head_before_commit != expected_head:
        subprocess.run(["git", "reset", "--mixed", head_before], cwd=cwd, capture_output=True, timeout=10, check=False)
        return _deny("head_race_detected_before_commit")

    commit_result = subprocess.run(
        ["git", "commit", "-m", commit_message],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if commit_result.returncode != 0:
        return _deny("git_commit_failed", errors=[commit_result.stderr.strip()])

    new_head = _git_rev_parse_head(cwd)
    if new_head is None:
        return _deny("post_commit_head_unavailable")

    # Post-commit re-audit: the committed diff (prior_head..new_head) must
    # touch exactly the identities we staged.
    show_result = subprocess.run(
        ["git", "diff", "--name-status", "-M", "-z", head_before, new_head],
        cwd=cwd,
        capture_output=True,
        text=False,
        timeout=30,
        check=False,
    )
    committed_identities: set[str] = set()
    if show_result.returncode == 0:
        try:
            committed_records = parse_git_diff_name_status_z(
                show_result.stdout.decode("utf-8", errors="replace"),
                source="post_commit_reaudit",
            )
            for rec in committed_records:
                committed_identities.add(rec.path)
                if rec.previous_path:
                    committed_identities.add(rec.previous_path)
        except ValueError:
            committed_identities = set()

    if committed_identities != touched_identities:
        _rollback_commit(cwd, head_before)
        return _deny(
            "post_commit_audit_mismatch_rolled_back",
            errors=[f"committed={sorted(committed_identities)}", f"staged={sorted(touched_identities)}"],
        )

    return ControlledChangeResult(
        status="ok",
        reason_code="controlled_stage_commit_ok",
        commit_sha=new_head,
        staged_paths=sorted(touched_identities),
        classifications=classifications,
    )


# ─── Raw / rtk `git add` / `git commit` outside-executor detector (AC9) ──────


def is_raw_or_rtk_git_add_or_commit_command(command: str) -> bool:
    """Return True if `command` is a raw `git add` / `git commit` shape, OR
    the legacy `rtk git add` / `rtk git commit` shape -- both of which MUST
    be denied outside this controlled executor for agent lanes (AC9). This
    is a pure string classifier (no execution); the actual deny decision at
    the hook layer is `git_mutation_command_policy.classify_rtk_git_mutation`
    (the pre-existing Issue #1241 lane) plus this classifier for the raw
    (non-`rtk`-prefixed) shape and as the additional fail-closed authority a
    hook-layer caller consults before ever reaching that lane."""
    import shlex

    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return True  # fail-closed: unparseable git-add/commit-looking input
    if not tokens:
        return False
    idx = 0
    if tokens[idx] == "rtk":
        idx += 1
    if idx >= len(tokens) or tokens[idx] != "git":
        return False
    idx += 1
    if idx >= len(tokens):
        return False
    return tokens[idx] in ("add", "commit")


def main(argv: Optional[Sequence[str]] = None) -> int:  # pragma: no cover - thin CLI wrapper
    import argparse

    parser = argparse.ArgumentParser(description="Controlled stage/commit executor (Issue #1611)")
    parser.add_argument("--snapshot-file", required=True)
    parser.add_argument("--path", action="append", dest="paths", default=[])
    parser.add_argument("--commit-message", required=True)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--current-body-sha256", required=True)
    parser.add_argument("--cwd", default=os.getcwd())
    args = parser.parse_args(argv)

    with open(args.snapshot_file, encoding="utf-8") as fh:
        snapshot = json.load(fh)

    result = execute_controlled_stage_commit(
        cwd=args.cwd,
        snapshot=snapshot,
        requested_paths=args.paths,
        commit_message=args.commit_message,
        expected_head=args.expected_head,
        current_body_sha256=args.current_body_sha256,
        current_allowed_paths=snapshot.get("allowed_paths", []),
    )
    print(json.dumps(result.to_dict(), sort_keys=True))
    return 0 if result.status == "ok" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
