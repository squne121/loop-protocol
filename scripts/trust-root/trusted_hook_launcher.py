#!/usr/bin/env python3
"""trusted_hook_launcher.py — external trust root dispatcher for the publish
lane authorization TCB (Issue #1454, Phase A).

This module is designed to be installed at a candidate-repository-EXTERNAL,
agent-write-denied absolute filesystem path (see ``install_trust_root.sh``).
It is invoked by a managed (admin-enforced) ``PreToolUse`` hook registration
and is responsible for deciding whether a publish-lane ``git push`` may
proceed, based ONLY on:

  1. an external trust manifest (``AUTHORIZATION_TCB_MANIFEST_V1``, see
     ``manifest_schema.py``) that is installed by a privileged operator and
     is NOT writable by the candidate-repository agent, and
  2. the exact tree/blob bytes of the commit (``local_oid``) that is about to
     be pushed, read via a trusted ``git`` invocation with a restricted,
     allowlisted environment — NEVER via ``lstat``/``open`` on the candidate
     working tree, and NEVER via any remote-tracking ref (``origin/main`` et
     al.) that lives inside the candidate repository.

Threat model addressed (see Issue #1454 contract for full detail):

  - The candidate repository (adapter / policy / analyzer / composite / any
    other file under the repository) is assumed to be FULLY attacker
    controlled. This module must never trust candidate-repository state to
    validate itself.
  - ``origin/main`` and other remote-tracking refs inside the candidate
    repository are candidate-controlled and are never consulted.
  - ``refs/replace/*`` object replacement is neutralized via
    ``--no-replace-objects``.
  - PATH-based binary resolution is never used for ``git``/``python3``; only
    a fixed allowlist of absolute paths is consulted.
  - The environment passed to any ``git`` subprocess is constructed via an
    ALLOWLIST (not a blacklist) of variables, so unknown-in-advance
    candidate-injected environment variables (e.g. ``GIT_DIR``,
    ``GIT_OBJECT_DIRECTORY``, ``GIT_CONFIG_COUNT``) can never leak through.
  - A working-tree-only edit to a critical component (without a matching
    commit) is NOT considered authoritative in either direction: the
    authorization decision is made exclusively from the tree of
    ``local_oid``. Uncommitted changes to a critical-path file are always
    treated as ambiguous and denied (``candidate_tree_ambiguous``), since an
    agent could otherwise race a ``git commit --amend`` between this check
    and the actual push (TOCTOU).

This module intentionally does NOT wire itself into ``.codex/hooks.json``
(project-local, candidate-controlled) — that production cutover is Issue
#1450 (Phase B), listed as an explicit Stop Condition / Out of Scope item
for this Issue.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from manifest_schema import (  # noqa: E402
    ManifestValidationResult,
    validate_manifest,
)

# ─── Fixed, non-PATH-based absolute binary resolution ────────────────────────

_GIT_BINARY_CANDIDATES: tuple[str, ...] = ("/usr/bin/git", "/usr/local/bin/git", "/bin/git")

# Default production trust root anchor. NEVER read from an environment
# variable in production; the only override path is the explicit
# ``trust_root_dir`` parameter, which production managed-hook registration
# fixes to this constant and which test harnesses pass explicitly as a
# same-process Python argument (never via inherited env / PATH / candidate
# working tree state).
DEFAULT_TRUST_ROOT_DIR = Path("/opt/loop-protocol/trust-root")

# ─── Reason codes (fail-closed; see Issue #1454 contract) ────────────────────

REASON_TRUST_ROOT_MISSING = "authorization_trust_root_missing"
REASON_MANIFEST_INVALID = "authorization_manifest_invalid"
REASON_COMPONENT_MISSING = "authorization_component_missing"
REASON_COMPONENT_TYPE_INVALID = "authorization_component_type_invalid"
REASON_COMPONENT_DIGEST_MISMATCH = "authorization_component_digest_mismatch"
REASON_REFERENCE_UNTRUSTED = "authorization_reference_untrusted"
REASON_RUNTIME_UNTRUSTED = "authorization_runtime_untrusted"
REASON_CANDIDATE_TREE_AMBIGUOUS = "candidate_tree_ambiguous"
REASON_CANDIDATE_COMMIT_COMPONENT_MISMATCH = "candidate_commit_component_mismatch"

_ALL_REASON_CODES = frozenset(
    {
        REASON_TRUST_ROOT_MISSING,
        REASON_MANIFEST_INVALID,
        REASON_COMPONENT_MISSING,
        REASON_COMPONENT_TYPE_INVALID,
        REASON_COMPONENT_DIGEST_MISMATCH,
        REASON_REFERENCE_UNTRUSTED,
        REASON_RUNTIME_UNTRUSTED,
        REASON_CANDIDATE_TREE_AMBIGUOUS,
        REASON_CANDIDATE_COMMIT_COMPONENT_MISMATCH,
    }
)


# ─── Data types ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PublishEvidence:
    """Publish evidence supplied by the trusted verifier (never inherited env).

    Production contract: this is delivered to the launcher as versioned
    JSON (stdin or an explicit FD), never read back from process environment
    variables — see Issue #1454 In Scope bullet on publish evidence.
    """

    session_id: str
    turn_id: str
    tool_use_id: str
    local_oid: str
    remote_oid: str
    ref: str
    issue_number: int
    nonce: str
    expiry: str
    candidate_repo_dir: str


@dataclass(frozen=True)
class AuthorizationDecision:
    decision: str  # "allow" | "deny"
    reason_code: str | None
    detail: str | None = None
    updated_command: tuple[str, ...] | None = None
    generation: int | None = None
    trusted_commit_oid: str | None = None


def deny(reason_code: str, detail: str | None = None) -> AuthorizationDecision:
    assert reason_code in _ALL_REASON_CODES, f"unknown reason_code: {reason_code}"
    return AuthorizationDecision(decision="deny", reason_code=reason_code, detail=detail)


# ─── Owner-only permission validation (AC1) ──────────────────────────────────


def _mode_is_owner_only(path: Path, *, allow_sticky_shared: bool = False) -> bool:
    """Return True iff ``path`` has no group/other write bits set.

    This is a necessary (not sufficient on its own) trust-root integrity
    signal: a trust root directory or manifest that is group/other-writable
    could be tampered with by a non-owner process, defeating the external
    trust anchor. Ownership (uid) matching is validated separately by the
    installer at bootstrap time (``install_trust_root.sh``); this launcher
    focuses on write-permission bits, which it can check without any
    privileged operator context.

    ``allow_sticky_shared`` permits the conventional shared-temp exception
    (e.g. ``/tmp`` at mode ``1777``): a world-writable directory is NOT a
    tamper vector for entries it does not own as long as the sticky bit
    (``S_ISVTX``) is set, since the sticky bit prevents non-owners from
    renaming/deleting/replacing another user's entries within it. This
    exception is only applied to ANCESTOR directories above the trust root
    itself (never to the trust root directory or its contents).
    """
    try:
        st = path.stat()
    except OSError:
        return False
    if (st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)) == 0:
        return True
    if allow_sticky_shared and (st.st_mode & stat.S_ISVTX):
        return True
    return False


def validate_trust_root_permissions(trust_root_dir: Path) -> AuthorizationDecision | None:
    """Validate owner-only write permissions on ``trust_root_dir`` itself and
    walk its ancestor chain, tolerating conventional sticky shared-temp
    ancestors (e.g. ``/tmp``) above it.

    Returns a deny decision if any component is missing or writable by a
    non-owner (without the sticky-bit exception), else ``None`` (meaning:
    permission check passed).
    """
    if not trust_root_dir.is_absolute():
        return deny(REASON_RUNTIME_UNTRUSTED, detail="trust root path must be absolute")

    if not trust_root_dir.exists():
        return deny(REASON_TRUST_ROOT_MISSING, detail=f"trust root path missing: {trust_root_dir}")
    if not _mode_is_owner_only(trust_root_dir, allow_sticky_shared=False):
        return deny(
            REASON_RUNTIME_UNTRUSTED,
            detail=f"trust root directory is group/other writable: {trust_root_dir}",
        )

    current = trust_root_dir.parent
    checked = 0
    while True:
        if not current.exists():
            return deny(REASON_TRUST_ROOT_MISSING, detail=f"trust root ancestor missing: {current}")
        if not _mode_is_owner_only(current, allow_sticky_shared=True):
            return deny(
                REASON_RUNTIME_UNTRUSTED,
                detail=f"trust root ancestor is group/other writable (no sticky bit): {current}",
            )
        checked += 1
        parent = current.parent
        if parent == current or checked >= 8:
            break
        current = parent
    return None


# ─── Restricted git environment (allowlist, not blacklist) ──────────────────


def _resolve_git_binary() -> str | None:
    for candidate in _GIT_BINARY_CANDIDATES:
        p = Path(candidate)
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


def _restricted_git_env(dedicated_home: str) -> dict[str, str]:
    """Build a fixed ALLOWLIST environment for trusted git subprocesses.

    Deliberately does NOT start from ``os.environ`` and strip a blacklist —
    it builds the environment from scratch so that candidate-injected
    variables (``GIT_DIR``, ``GIT_COMMON_DIR``, ``GIT_OBJECT_DIRECTORY``,
    ``GIT_ALTERNATE_OBJECT_DIRECTORIES``, ``GIT_CONFIG_COUNT``, a poisoned
    ``PATH``, etc.) can never leak through, regardless of what new variables
    a future attacker might invent.
    """
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": dedicated_home,
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "LC_ALL": "C",
    }


@dataclass(frozen=True)
class GitContext:
    git_binary: str
    git_dir: str
    env: dict[str, str]


def build_git_context(candidate_repo_dir: str, dedicated_home: str) -> GitContext | None:
    git_binary = _resolve_git_binary()
    if git_binary is None:
        return None
    git_dir = str(Path(candidate_repo_dir) / ".git")
    return GitContext(git_binary=git_binary, git_dir=git_dir, env=_restricted_git_env(dedicated_home))


def _run_git(ctx: GitContext, *args: str, binary_output: bool = False) -> subprocess.CompletedProcess:
    argv = [
        ctx.git_binary,
        "--no-replace-objects",
        f"--git-dir={ctx.git_dir}",
        *args,
    ]
    return subprocess.run(
        argv,
        env=ctx.env,
        capture_output=True,
        text=not binary_output,
        timeout=20,
    )


def resolve_trusted_commit(ctx: GitContext, oid: str) -> str | None:
    """Verify ``oid`` resolves to a real commit object (fail-closed).

    Uses ``rev-parse --verify <oid>^{commit} --end-of-options`` so that
    ``refs/replace`` substitution (neutralized via ``--no-replace-objects``)
    and non-commit objects (tags, blobs) cannot be smuggled in as a fake
    "local_oid".
    """
    result = _run_git(ctx, "rev-parse", "--verify", f"{oid}^{{commit}}", "--end-of-options")
    if result.returncode != 0:
        return None
    resolved = result.stdout.strip()
    return resolved or None


def ls_tree_entry(ctx: GitContext, oid: str, path: str) -> tuple[str, str] | None:
    """Return (mode, blob_sha) for ``path`` inside the tree of ``oid``.

    Returns ``None`` if the path does not exist in that tree.
    """
    result = _run_git(ctx, "ls-tree", oid, "--", path)
    if result.returncode != 0:
        return None
    line = result.stdout.strip()
    if not line:
        return None
    # Format: "<mode> <type> <sha>\t<path>"
    try:
        meta, entry_path = line.split("\t", 1)
    except ValueError:
        return None
    if entry_path != path:
        return None
    parts = meta.split()
    if len(parts) != 3:
        return None
    mode, obj_type, blob_sha = parts
    if obj_type != "blob":
        return None
    return mode, blob_sha


def read_blob_bytes(ctx: GitContext, blob_sha: str) -> bytes | None:
    result = _run_git(ctx, "cat-file", "-p", blob_sha, binary_output=True)
    if result.returncode != 0:
        return None
    return result.stdout


def working_tree_dirty_for_path(ctx: GitContext, candidate_repo_dir: str, path: str) -> bool:
    """Return True if ``path`` has uncommitted (working-tree or staged)
    changes relative to HEAD in the candidate repository.
    """
    argv = [
        ctx.git_binary,
        "--no-replace-objects",
        f"--git-dir={ctx.git_dir}",
        f"--work-tree={candidate_repo_dir}",
        "status",
        "--porcelain",
        "--",
        path,
    ]
    result = subprocess.run(argv, env=ctx.env, capture_output=True, text=True, timeout=20)
    if result.returncode != 0:
        # Fail-closed: an unparseable git status is treated as ambiguous, not clean.
        return True
    return bool(result.stdout.strip())


def _read_workdir_bytes(candidate_repo_dir: str, path: str) -> bytes | None:
    full_path = Path(candidate_repo_dir) / path
    try:
        if full_path.is_symlink() or not full_path.is_file():
            return None
        return full_path.read_bytes()
    except OSError:
        return None


# ─── Trust root / manifest loading ──────────────────────────────────────────


ManifestLoadResult = tuple[ManifestValidationResult | None, AuthorizationDecision | None]


def load_active_manifest(trust_root_dir: Path) -> ManifestLoadResult:
    """Load and validate the currently active manifest release.

    Layout (installed atomically by ``install_trust_root.sh``):
        <trust_root_dir>/active.json
            -> {"active_generation": N, "manifest_relpath": "releases/<gen>-<digest>/manifest.json"}
        <trust_root_dir>/releases/<gen>-<digest>/manifest.json
            -> AUTHORIZATION_TCB_MANIFEST_V1 JSON
    """
    permission_error = validate_trust_root_permissions(trust_root_dir)
    if permission_error is not None:
        return None, permission_error

    active_json_path = trust_root_dir / "active.json"
    if not active_json_path.is_file():
        return None, deny(REASON_TRUST_ROOT_MISSING, detail="active.json not found")

    try:
        active_raw = json.loads(active_json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, deny(REASON_TRUST_ROOT_MISSING, detail=f"active.json unreadable: {exc}")

    manifest_relpath = active_raw.get("manifest_relpath") if isinstance(active_raw, dict) else None
    if not isinstance(manifest_relpath, str) or not manifest_relpath:
        return None, deny(REASON_TRUST_ROOT_MISSING, detail="active.json missing manifest_relpath")

    normalized = os.path.normpath(manifest_relpath)
    if normalized.startswith("..") or normalized.startswith("/"):
        return None, deny(REASON_TRUST_ROOT_MISSING, detail="active.json manifest_relpath escapes trust root")

    manifest_path = trust_root_dir / normalized
    try:
        manifest_path.resolve().relative_to(trust_root_dir.resolve())
    except ValueError:
        return None, deny(REASON_TRUST_ROOT_MISSING, detail="manifest path escapes trust root")

    if not manifest_path.is_file() or manifest_path.is_symlink():
        return None, deny(REASON_TRUST_ROOT_MISSING, detail="manifest file missing or not a regular file")
    if not _mode_is_owner_only(manifest_path):
        return None, deny(REASON_RUNTIME_UNTRUSTED, detail="manifest file is group/other writable")

    try:
        manifest_raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, deny(REASON_MANIFEST_INVALID, detail=f"manifest unreadable: {exc}")

    result = validate_manifest(manifest_raw)
    if not result.ok:
        return None, deny(REASON_MANIFEST_INVALID, detail=result.detail)

    return result, None


# ─── Core authorization decision ─────────────────────────────────────────────


def evaluate_publish_authorization(
    evidence: PublishEvidence,
    trust_root_dir: Path,
    dedicated_home: str,
) -> AuthorizationDecision:
    manifest, load_error = load_active_manifest(trust_root_dir)
    if load_error is not None:
        return load_error
    assert manifest is not None

    ctx = build_git_context(evidence.candidate_repo_dir, dedicated_home)
    if ctx is None:
        return deny(REASON_RUNTIME_UNTRUSTED, detail="no trusted absolute git binary found")

    trusted_local_oid = resolve_trusted_commit(ctx, evidence.local_oid)
    if trusted_local_oid is None:
        return deny(REASON_REFERENCE_UNTRUSTED, detail="local_oid does not resolve to a commit object")

    ambiguous_paths: list[str] = []
    for component in manifest.components:
        entry = ls_tree_entry(ctx, trusted_local_oid, component.path)
        if entry is None:
            return deny(REASON_COMPONENT_MISSING, detail=f"component missing from tree: {component.path}")
        mode, blob_sha = entry
        if mode != "100644" and mode != "100755":
            return deny(
                REASON_COMPONENT_TYPE_INVALID,
                detail=f"component is not a regular file (mode={mode}): {component.path}",
            )
        blob_bytes = read_blob_bytes(ctx, blob_sha)
        if blob_bytes is None:
            return deny(REASON_COMPONENT_MISSING, detail=f"unable to read blob for: {component.path}")
        actual_digest = hashlib.sha256(blob_bytes).hexdigest()
        if actual_digest != component.sha256:
            workdir_bytes = _read_workdir_bytes(evidence.candidate_repo_dir, component.path)
            if workdir_bytes is not None and hashlib.sha256(workdir_bytes).hexdigest() == component.sha256:
                # The pushed commit's tree is malicious, but the on-disk working
                # copy currently matches the approved digest. This is exactly
                # the TOCTOU pattern this launcher must resist: a naive
                # working-tree-lstat based verifier would have allowed here.
                return deny(
                    REASON_CANDIDATE_COMMIT_COMPONENT_MISMATCH,
                    detail=f"committed tree digest mismatch (workdir looks clean) for: {component.path}",
                )
            return deny(
                REASON_COMPONENT_DIGEST_MISMATCH,
                detail=f"digest mismatch for: {component.path}",
            )
        if working_tree_dirty_for_path(ctx, evidence.candidate_repo_dir, component.path):
            ambiguous_paths.append(component.path)

    if ambiguous_paths:
        return deny(
            REASON_CANDIDATE_TREE_AMBIGUOUS,
            detail=f"uncommitted changes on critical path(s): {ambiguous_paths}",
        )

    updated_command = (
        ctx.git_binary,
        "push",
        f"--force-with-lease={evidence.ref}:{evidence.remote_oid}",
        "origin",
        f"{trusted_local_oid}:{evidence.ref}",
    )
    return AuthorizationDecision(
        decision="allow",
        reason_code=None,
        updated_command=updated_command,
        generation=manifest.generation,
        trusted_commit_oid=manifest.trusted_commit_oid,
    )


# ─── CLI entrypoint ───────────────────────────────────────────────────────────


def _evidence_from_payload(payload: dict) -> PublishEvidence:
    return PublishEvidence(
        session_id=str(payload.get("session_id", "")),
        turn_id=str(payload.get("turn_id", "")),
        tool_use_id=str(payload.get("tool_use_id", "")),
        local_oid=str(payload.get("local_oid", "")),
        remote_oid=str(payload.get("remote_oid", "")),
        ref=str(payload.get("ref", "")),
        issue_number=int(payload.get("issue_number", 0) or 0),
        nonce=str(payload.get("nonce", "")),
        expiry=str(payload.get("expiry", "")),
        candidate_repo_dir=str(payload.get("candidate_repo_dir", "")),
    )


def _decision_to_json(decision: AuthorizationDecision) -> dict:
    out: dict = {
        "schema": "TRUSTED_HOOK_LAUNCHER_DECISION_V1",
        "decision": decision.decision,
        "reason_code": decision.reason_code,
    }
    if decision.detail is not None:
        out["detail"] = decision.detail
    if decision.decision == "allow" and decision.updated_command is not None:
        out["updatedInput"] = {"command": list(decision.updated_command)}
        out["generation"] = decision.generation
        out["trusted_commit_oid"] = decision.trusted_commit_oid
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse
    import tempfile

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--trust-root-dir",
        default=None,
        help=(
            "Test-only dependency injection override for the trust root "
            "directory. Production managed-hook registration invokes this "
            "launcher WITHOUT this flag, always using the fixed default."
        ),
    )
    parser.add_argument("--evidence-file", default=None, help="Path to a JSON evidence file. Defaults to stdin.")
    args = parser.parse_args(argv)

    if args.evidence_file:
        payload = json.loads(Path(args.evidence_file).read_text(encoding="utf-8"))
    else:
        payload = json.loads(sys.stdin.read())

    evidence = _evidence_from_payload(payload)
    trust_root_dir = Path(args.trust_root_dir) if args.trust_root_dir else DEFAULT_TRUST_ROOT_DIR

    with tempfile.TemporaryDirectory(prefix="trusted-hook-launcher-home-") as dedicated_home:
        decision = evaluate_publish_authorization(evidence, trust_root_dir, dedicated_home)

    print(json.dumps(_decision_to_json(decision)))
    return 0 if decision.decision == "allow" else 1


if __name__ == "__main__":
    raise SystemExit(main())
