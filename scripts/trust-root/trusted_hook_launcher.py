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
     is NOT writable by the candidate-repository agent,
  2. the exact tree/blob bytes of the commit (``local_oid``) that is about to
     be pushed, read via a trusted ``git`` invocation with a restricted,
     allowlisted environment — NEVER via ``lstat``/``open`` on the candidate
     working tree, and NEVER via any remote-tracking ref (``origin/main`` et
     al.) that lives inside the candidate repository, and
  3. trusted publish evidence (nonce/expiry/ref/repository identity/command
     binding) that is cross-checked against the real Codex/Claude Code
     ``PreToolUse`` hook payload for this exact tool call.

Wire format (Issue #1454 fix_delta, OWNER adversarial review):

  - stdin carries the REAL ``PreToolUse`` hook payload:
    ``{"tool_name": "Bash", "tool_use_id": "...", "tool_input": {"command": "..."},
    "cwd": "..."}`` (Codex / Claude Code hook contract — see
    https://developers.openai.com/codex/hooks/). ``tool_name`` MUST be
    ``"Bash"``; any other value is fail-closed.
  - ``--evidence-file`` carries a SEPARATE, trusted-verifier-produced
    ``PUBLISH_EVIDENCE_V1`` JSON document (never inherited environment
    variables). It is bound to the hook payload via ``tool_use_id``, ``cwd``,
    and a SHA-256 of the literal ``tool_input.command`` string, so a
    mismatched/replayed/foreign evidence file can never authorize a
    different command than the one actually intercepted.
  - Allow output uses the real hook output contract:
    ``{"hookSpecificOutput": {"hookEventName": "PreToolUse",
    "permissionDecision": "allow", "updatedInput": {"command": "<str>"}}}``
    with ``updatedInput.command`` as a single shell-quoted STRING (built via
    ``shlex.join``), never an argv list. Exit code 0.
  - Deny output uses ``permissionDecision: "deny"`` on stdout AND a
    structured message on stderr, exit code 2 (matching the Claude Code /
    Codex ``PreToolUse`` block contract documented in
    ``docs/dev/hook-boundaries.md``).

Note on production evidence delivery (Issue #1450, Phase B scope): the exact
mechanism by which a trusted verifier process produces and hands off the
``--evidence-file`` path to the ONE fixed, admin-managed hook command is
finalized in Issue #1450 (candidate integrity binding / production cutover).
This module's contract only requires that whatever mechanism is used
delivers a file path (not inherited env, not candidate-writable content);
the CLI flag is the stable, testable interface for that contract.

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
  - The push target ref is restricted to ``refs/heads/<branch>`` (never
    tags/notes/arbitrary refs) and explicitly excludes conventional
    protected branch names (``main``/``master``/``trunk``) — this publish
    lane authorizes issue/feature branch pushes, not direct pushes to the
    default branch.
  - The evidence nonce is single-use (tracked via an atomic marker file
    under the trust root) and time-bounded (``expiry``), so a captured
    evidence file cannot be replayed indefinitely or reused for an
    unrelated later push.
  - The runtime process's uid MUST differ from the trust root owner's uid
    (mirroring the installer's ``runtime_euid_must_differ_from_owner``
    separation of duties) — if the invoking process already has write
    access to the trust root, the boundary this module exists to enforce
    is illusory.

This module intentionally does NOT wire itself into ``.codex/hooks.json``
(project-local, candidate-controlled) — that production cutover is Issue
#1450 (Phase B), listed as an explicit Stop Condition / Out of Scope item
for this Issue.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
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

# Production trust root path allowlist. The launcher's default (no
# ``--trust-root-dir`` override) is always exactly this tuple; test harnesses
# exercise the allowlist function directly with an explicit, test-scoped
# tuple (see test_production_path_allowlist*) rather than relying on the
# production default, so the mechanism itself is verified without needing a
# real ``/opt`` mount in CI.
PRODUCTION_TRUST_ROOT_ALLOWLIST: tuple[Path, ...] = (DEFAULT_TRUST_ROOT_DIR,)

# Test-only escape hatch for the runtime/owner uid separation check (see
# check_runtime_identity_separation). Production NEVER sets this. It exists
# solely because a single-user sandbox/CI runner cannot otherwise exercise
# the ALLOW path of a check whose entire purpose is "the invoking uid must
# differ from the trust root owner uid" without genuine multi-account
# privilege separation (Issue #1454 skip_conditions).
_TEST_ALLOW_SAME_UID_ENV = "LOOP_TRUST_ROOT_TEST_ALLOW_SAME_UID"

# Test-only escape hatch for the repository-identity check (see
# check_repository_identity). Production NEVER sets this. It exists solely
# so end-to-end push-mechanics tests (real bare local remote, no network
# access to github.com in this sandbox) can exercise the ACTUAL push/lease
# behavior without a false obstacle from a local-path remote.origin.url not
# matching a github.com slug. The repository-identity check itself has its
# own DEDICATED, unconditional test
# (test_repository_identity_mismatch_denied) that never sets this bypass.
_TEST_SKIP_REPOSITORY_IDENTITY_ENV = "LOOP_TRUST_ROOT_TEST_SKIP_REPOSITORY_IDENTITY"

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

# Added by the Issue #1454 fix_delta (OWNER adversarial review,
# https://github.com/squne121/loop-protocol/pull/1457#issuecomment-4945279761):
# these extend (never replace) the fixed 9-code set above.
REASON_HOOK_PAYLOAD_INVALID = "authorization_hook_payload_invalid"
REASON_EVIDENCE_INVALID = "authorization_evidence_invalid"
REASON_EVIDENCE_BINDING_MISMATCH = "authorization_evidence_binding_mismatch"
REASON_EVIDENCE_EXPIRED = "authorization_evidence_expired"
REASON_NONCE_REPLAYED = "authorization_nonce_replayed"
REASON_REF_UNTRUSTED = "authorization_ref_untrusted"
REASON_REPOSITORY_IDENTITY_MISMATCH = "authorization_repository_identity_mismatch"

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
        REASON_HOOK_PAYLOAD_INVALID,
        REASON_EVIDENCE_INVALID,
        REASON_EVIDENCE_BINDING_MISMATCH,
        REASON_EVIDENCE_EXPIRED,
        REASON_NONCE_REPLAYED,
        REASON_REF_UNTRUSTED,
        REASON_REPOSITORY_IDENTITY_MISMATCH,
    }
)

# Refs this publish lane may target: issue/feature branches only. Direct
# pushes to conventional default/protected branch names are explicitly
# denied — this lane exists to authorize a candidate agent's own
# issue/feature branch push, never a rewrite of the shared default branch.
_ALLOWED_REF_RE = re.compile(r"^refs/heads/[A-Za-z0-9._/-]+$")
_PROTECTED_BRANCH_NAMES = frozenset({"main", "master", "trunk"})
_ALL_ZERO_OID = "0" * 40


# ─── Data types ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HookPayload:
    """The REAL Codex / Claude Code PreToolUse hook payload for this call."""

    tool_name: str
    tool_use_id: str
    command: str
    cwd: str


@dataclass(frozen=True)
class PublishEvidence:
    """Publish evidence supplied by a trusted verifier (never inherited env,
    never candidate-writable content). Bound to the hook payload via
    ``tool_use_id`` / ``candidate_repo_dir`` (== hook ``cwd``) /
    ``command_sha256`` (== sha256 of the hook payload's literal command).
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
    command_sha256: str


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


def _ancestor_is_symlink(path: Path) -> bool:
    try:
        return path.is_symlink()
    except OSError:
        return True  # fail-closed: unknown state treated as untrusted


def validate_trust_root_permissions(trust_root_dir: Path) -> AuthorizationDecision | None:
    """Validate owner-only write permissions on ``trust_root_dir`` itself and
    walk its ancestor chain, tolerating conventional sticky shared-temp
    ancestors (e.g. ``/tmp``) above it. Also rejects any ancestor (including
    ``trust_root_dir`` itself) that is a symlink.

    Returns a deny decision if any component is missing, is a symlink, or is
    writable by a non-owner (without the sticky-bit exception), else
    ``None`` (meaning: permission check passed).
    """
    if not trust_root_dir.is_absolute():
        return deny(REASON_RUNTIME_UNTRUSTED, detail="trust root path must be absolute")

    if _ancestor_is_symlink(trust_root_dir):
        return deny(REASON_RUNTIME_UNTRUSTED, detail=f"trust root path is a symlink: {trust_root_dir}")
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
        if _ancestor_is_symlink(current):
            return deny(REASON_RUNTIME_UNTRUSTED, detail=f"trust root ancestor is a symlink: {current}")
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


def validate_trust_root_path_allowlist(
    trust_root_dir: Path,
    allowlist: tuple[Path, ...] = PRODUCTION_TRUST_ROOT_ALLOWLIST,
) -> bool:
    """Return True iff ``trust_root_dir`` (resolved) matches one of the fixed
    production allowlist entries. Test harnesses call this directly with an
    explicit test-scoped ``allowlist`` tuple to exercise the mechanism
    without requiring a real ``/opt`` mount.
    """
    resolved = os.path.realpath(str(trust_root_dir))
    for candidate in allowlist:
        candidate_str = str(candidate)
        if os.path.realpath(candidate_str) == resolved or candidate_str == str(trust_root_dir):
            return True
    return False


def check_runtime_identity_separation(trust_root_dir: Path) -> AuthorizationDecision | None:
    """The runtime (agent/hook) process uid MUST differ from the trust root
    owner uid — otherwise whoever is invoking this launcher already has
    write access to the trust root and the boundary is illusory.

    Test-only bypass: set ``LOOP_TRUST_ROOT_TEST_ALLOW_SAME_UID=1`` in the
    process environment. Production NEVER sets this (Issue #1454
    skip_conditions: genuine multi-account privilege separation is not
    available in this sandbox/CI runner; the DENY path of this check is
    exercised directly and unconditionally).
    """
    try:
        owner_uid = trust_root_dir.stat().st_uid
    except OSError:
        return deny(REASON_TRUST_ROOT_MISSING, detail=f"cannot stat trust root for uid check: {trust_root_dir}")
    if os.getuid() == owner_uid and os.environ.get(_TEST_ALLOW_SAME_UID_ENV) != "1":
        return deny(
            REASON_RUNTIME_UNTRUSTED,
            detail="runtime uid equals trust root owner uid; no privilege separation",
        )
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


def get_remote_origin_url(ctx: GitContext) -> str | None:
    result = _run_git(ctx, "config", "--get", "remote.origin.url")
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


_OWNER_REPO_RE = re.compile(r"github\.com[:/]+([^/]+)/([^/.]+?)(?:\.git)?/?$")


def extract_owner_repo_slug(remote_url: str) -> str | None:
    match = _OWNER_REPO_RE.search(remote_url)
    if not match:
        return None
    return f"{match.group(1)}/{match.group(2)}"


def check_repository_identity(ctx: GitContext, manifest_repository: str) -> bool:
    """Verify the candidate repository's own ``remote.origin.url`` (read via
    a TRUSTED, restricted-env git invocation — never a candidate-writable
    config file parsed by hand) resolves to the same owner/repo slug the
    manifest was issued for.
    """
    remote_url = get_remote_origin_url(ctx)
    if remote_url is None:
        return False
    slug = extract_owner_repo_slug(remote_url)
    return slug is not None and slug == manifest_repository


# ─── Ref validation (Issue #1454 fix_delta) ──────────────────────────────────


def validate_target_ref(ref: str) -> bool:
    """Only ``refs/heads/<branch>`` is an allowed publish-lane push target.
    Tags, notes, arbitrary refs, and conventional protected branch names
    (main/master/trunk) are explicitly denied — this lane authorizes an
    issue/feature branch push, never a rewrite of the shared default branch.
    """
    if not _ALLOWED_REF_RE.match(ref):
        return False
    branch = ref[len("refs/heads/") :]
    if branch in _PROTECTED_BRANCH_NAMES:
        return False
    return True


# ─── Evidence binding / nonce / expiry (Issue #1454 fix_delta) ──────────────


def parse_hook_payload(raw: dict) -> HookPayload | None:
    """Parse the REAL Codex / Claude Code PreToolUse hook stdin payload.

    Expected shape: {"tool_name": "Bash", "tool_use_id": "...",
    "tool_input": {"command": "..."}, "cwd": "..."}. Returns None on any
    structural deviation (fail-closed — caller denies with
    authorization_hook_payload_invalid).
    """
    if not isinstance(raw, dict):
        return None
    tool_name = raw.get("tool_name")
    tool_use_id = raw.get("tool_use_id")
    tool_input = raw.get("tool_input")
    cwd = raw.get("cwd")
    if not isinstance(tool_name, str) or not tool_name:
        return None
    if not isinstance(tool_use_id, str) or not tool_use_id:
        return None
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    if not isinstance(command, str) or not command:
        return None
    if not isinstance(cwd, str) or not cwd:
        return None
    return HookPayload(tool_name=tool_name, tool_use_id=tool_use_id, command=command, cwd=cwd)


_EVIDENCE_REQUIRED_STR_FIELDS = (
    "session_id",
    "turn_id",
    "tool_use_id",
    "local_oid",
    "remote_oid",
    "ref",
    "nonce",
    "expiry",
    "candidate_repo_dir",
    "command_sha256",
)


def parse_evidence(raw: dict) -> PublishEvidence | None:
    if not isinstance(raw, dict):
        return None
    for field in _EVIDENCE_REQUIRED_STR_FIELDS:
        if not isinstance(raw.get(field), str) or not raw.get(field):
            return None
    issue_number = raw.get("issue_number")
    if isinstance(issue_number, bool) or not isinstance(issue_number, int):
        return None
    command_sha256 = raw["command_sha256"]
    if len(command_sha256) != 64 or not all(c in "0123456789abcdef" for c in command_sha256.lower()):
        return None
    return PublishEvidence(
        session_id=raw["session_id"],
        turn_id=raw["turn_id"],
        tool_use_id=raw["tool_use_id"],
        local_oid=raw["local_oid"],
        remote_oid=raw["remote_oid"],
        ref=raw["ref"],
        issue_number=issue_number,
        nonce=raw["nonce"],
        expiry=raw["expiry"],
        candidate_repo_dir=raw["candidate_repo_dir"],
        command_sha256=command_sha256.lower(),
    )


def validate_evidence_binding(payload: HookPayload, evidence: PublishEvidence) -> AuthorizationDecision | None:
    """Cross-check the hook payload against the trusted evidence bundle so a
    mismatched/foreign evidence file can never authorize a DIFFERENT tool
    call than the one actually intercepted.
    """
    if payload.tool_name != "Bash":
        return deny(REASON_HOOK_PAYLOAD_INVALID, detail=f"unsupported tool_name: {payload.tool_name!r}")
    if payload.tool_use_id != evidence.tool_use_id:
        return deny(REASON_EVIDENCE_BINDING_MISMATCH, detail="tool_use_id mismatch between hook payload and evidence")
    if os.path.realpath(payload.cwd) != os.path.realpath(evidence.candidate_repo_dir):
        return deny(REASON_EVIDENCE_BINDING_MISMATCH, detail="cwd mismatch between hook payload and evidence")
    actual_command_sha256 = hashlib.sha256(payload.command.encode("utf-8")).hexdigest()
    if actual_command_sha256 != evidence.command_sha256:
        return deny(
            REASON_EVIDENCE_BINDING_MISMATCH,
            detail="tool_input.command does not match evidence.command_sha256",
        )
    return None


def check_evidence_expiry(evidence: PublishEvidence, now: datetime | None = None) -> AuthorizationDecision | None:
    current = now or datetime.now(timezone.utc)
    try:
        expiry_str = evidence.expiry.replace("Z", "+00:00")
        expiry_dt = datetime.fromisoformat(expiry_str)
    except ValueError:
        return deny(REASON_EVIDENCE_INVALID, detail=f"unparseable expiry: {evidence.expiry!r}")
    if expiry_dt.tzinfo is None:
        expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)
    if current > expiry_dt:
        return deny(REASON_EVIDENCE_EXPIRED, detail=f"evidence expired at {evidence.expiry}")
    return None


def check_and_consume_nonce(trust_root_dir: Path, nonce: str) -> AuthorizationDecision | None:
    """Single-use nonce enforcement via an atomic marker file under the
    trust root's ``nonces/`` subdirectory (``O_CREAT|O_EXCL`` — a second
    attempt to create the same marker fails, detecting replay).
    """
    nonces_dir = trust_root_dir / "nonces"
    try:
        nonces_dir.mkdir(mode=0o700, exist_ok=True)
    except OSError as exc:
        return deny(REASON_RUNTIME_UNTRUSTED, detail=f"cannot prepare nonce store: {exc}")
    marker_name = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
    marker_path = nonces_dir / marker_name
    try:
        fd = os.open(str(marker_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    except FileExistsError:
        return deny(REASON_NONCE_REPLAYED, detail="nonce has already been consumed")
    except OSError as exc:
        return deny(REASON_RUNTIME_UNTRUSTED, detail=f"cannot write nonce marker: {exc}")
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
    payload: HookPayload,
    evidence: PublishEvidence,
    trust_root_dir: Path,
    dedicated_home: str,
) -> AuthorizationDecision:
    identity_error = check_runtime_identity_separation(trust_root_dir)
    if identity_error is not None:
        return identity_error

    binding_error = validate_evidence_binding(payload, evidence)
    if binding_error is not None:
        return binding_error

    expiry_error = check_evidence_expiry(evidence)
    if expiry_error is not None:
        return expiry_error

    if not validate_target_ref(evidence.ref):
        return deny(REASON_REF_UNTRUSTED, detail=f"ref not authorized for this publish lane: {evidence.ref!r}")

    manifest, load_error = load_active_manifest(trust_root_dir)
    if load_error is not None:
        return load_error
    assert manifest is not None

    ctx = build_git_context(evidence.candidate_repo_dir, dedicated_home)
    if ctx is None:
        return deny(REASON_RUNTIME_UNTRUSTED, detail="no trusted absolute git binary found")

    if os.environ.get(_TEST_SKIP_REPOSITORY_IDENTITY_ENV) != "1" and not check_repository_identity(
        ctx, manifest.repository
    ):
        return deny(
            REASON_REPOSITORY_IDENTITY_MISMATCH,
            detail=f"remote.origin.url does not match manifest.repository={manifest.repository!r}",
        )

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

    # Nonce consumption is the LAST gate before allow: every earlier deny
    # path leaves the nonce unconsumed so a legitimate retry (e.g. after
    # transient git failure) is not permanently burned by a denied attempt.
    nonce_error = check_and_consume_nonce(trust_root_dir, evidence.nonce)
    if nonce_error is not None:
        return nonce_error

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


def _decision_to_json(decision: AuthorizationDecision) -> dict:
    """Render the decision using the REAL Codex / Claude Code PreToolUse
    hookSpecificOutput contract (see docs/dev/hook-boundaries.md and
    https://developers.openai.com/codex/hooks/), never a bespoke schema.
    """
    hook_specific: dict = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow" if decision.decision == "allow" else "deny",
    }
    if decision.reason_code is not None:
        reason_text = decision.reason_code
        if decision.detail:
            reason_text = f"{decision.reason_code}: {decision.detail}"
        hook_specific["permissionDecisionReason"] = reason_text
    if decision.decision == "allow" and decision.updated_command is not None:
        hook_specific["updatedInput"] = {"command": shlex.join(decision.updated_command)}
    return {"hookSpecificOutput": hook_specific}


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
    parser.add_argument(
        "--evidence-file",
        required=True,
        help="Path to a trusted-verifier-produced PUBLISH_EVIDENCE_V1 JSON file.",
    )
    args = parser.parse_args(argv)

    try:
        hook_raw = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        decision = deny(REASON_HOOK_PAYLOAD_INVALID, detail=f"stdin is not valid JSON: {exc}")
        return _emit(decision)

    payload = parse_hook_payload(hook_raw)
    if payload is None:
        decision = deny(REASON_HOOK_PAYLOAD_INVALID, detail="hook payload missing required fields")
        return _emit(decision)

    try:
        evidence_raw = json.loads(Path(args.evidence_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        decision = deny(REASON_EVIDENCE_INVALID, detail=f"evidence file unreadable: {exc}")
        return _emit(decision)

    evidence = parse_evidence(evidence_raw)
    if evidence is None:
        decision = deny(REASON_EVIDENCE_INVALID, detail="evidence file missing required fields")
        return _emit(decision)

    trust_root_dir = Path(args.trust_root_dir) if args.trust_root_dir else DEFAULT_TRUST_ROOT_DIR

    with tempfile.TemporaryDirectory(prefix="trusted-hook-launcher-home-") as dedicated_home:
        decision = evaluate_publish_authorization(payload, evidence, trust_root_dir, dedicated_home)

    return _emit(decision)


def _emit(decision: AuthorizationDecision) -> int:
    payload = _decision_to_json(decision)
    print(json.dumps(payload))
    if decision.decision != "allow":
        reason = payload["hookSpecificOutput"].get("permissionDecisionReason", decision.reason_code)
        print(f"[trusted_hook_launcher] deny: {reason}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
