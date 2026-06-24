#!/usr/bin/env python3
"""cleanup_contract_v3.py — one-shot POST_MERGE_CLEANUP_REQUEST_V3 (Issue #1137).

Hardened per the PR #1139 OWNER review. Single source of truth for:
  - V3 schema validation with a *three-valued* loader (ABSENT / VALID_V3 /
    PRESENT_BUT_INVALID) so a broken V3 never downgrades to legacy V2 (Blocker 2)
  - per-operation ``command_hash`` over the *actual* argv + nonce (Blocker 5 / High)
  - one-shot consume via atomic rename (replay protection, High)
  - ``git check-ref-format --branch`` based branch validation (Medium)
  - timezone-required, max-TTL-bounded expiry with ``issued_at`` (Medium)
  - durable + symlink-safe write/read via dir-fd traversal with ``O_NOFOLLOW``
    and ``fsync`` of file and parent dir (High)
  - ``SHARED_CLEANUP_REASON_CODES`` parity vocabulary (Claude / Codex)

Import-safe (no side effects).
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import secrets
import shutil
import stat
import subprocess
from datetime import datetime, timezone

SCHEMA_V3 = "POST_MERGE_CLEANUP_REQUEST_V3"
SAFE_SCRATCH_CONTRACT_PATH = "artifacts/agent-ops/cleanup_contract.json"
# Durable tombstone marking that V3 one-shot mode has been entered + a contract
# consumed. Its presence forbids any subsequent legacy V2 fallback (Blocker 3).
TOMBSTONE_REL_PATH = "artifacts/agent-ops/cleanup_contract.tombstone.json"
TOMBSTONE_SCHEMA = "POST_MERGE_CLEANUP_TOMBSTONE_V1"

OP_WORKTREE_REMOVE = "worktree_remove"
OP_BRANCH_DELETE = "branch_delete"
OPERATIONS = (OP_WORKTREE_REMOVE, OP_BRANCH_DELETE)

# Maximum allowed TTL between issued_at and expires_at (a few minutes).
MAX_TTL_SECONDS = 600
# Maximum tolerated clock skew (issued_at must not be far in the future).
MAX_CLOCK_SKEW_SECONDS = 120
# Maximum size of a contract file the reader will accept.
MAX_CONTRACT_BYTES = 64 * 1024

# Loader states (Blocker 2).
STATE_ABSENT = "ABSENT"
STATE_VALID_V3 = "VALID_V3"
STATE_PRESENT_BUT_INVALID = "PRESENT_BUT_INVALID"

# ── Shared cleanup reason codes (Claude / Codex parity) ───────────────────────
NO_CLEANUP_CONTRACT = "no_cleanup_contract"
CLEANUP_CONTRACT_PRESENT_BUT_INVALID = "cleanup_contract_present_but_invalid"
CLEANUP_CONTRACT_EXPIRED = "cleanup_contract_expired"
CLEANUP_CONTRACT_CONSUMED = "cleanup_contract_consumed"
CLEANUP_COMMAND_HASH_MISMATCH = "cleanup_command_hash_mismatch"
CLEANUP_OPERATION_MISMATCH = "cleanup_operation_mismatch"
WORKTREE_PATH_MISMATCH = "worktree_path_mismatch"
WORKTREE_NOT_IN_CATALOG = "worktree_not_in_catalog"
WORKTREE_DIRTY = "worktree_dirty"
BRANCH_FORCE_DELETE_DENIED = "branch_force_delete_denied"
ROOT_DRIFT_ACTIVE_WORKTREE_MISMATCH = "root_drift_active_worktree_mismatch"
PR_NOT_MERGED = "pr_not_merged"
GUARD_DEADLINE_EXCEEDED = "guard_deadline_exceeded"
# Blocker 3: a durable consume tombstone exists, so legacy V2 downgrade is denied.
CLEANUP_V2_DOWNGRADE_DENIED = "cleanup_v2_downgrade_denied"
# Blocker 9: the platform lacks the symlink-safe durable IO primitives required to
# evaluate a V3 contract safely (e.g. no O_NOFOLLOW / geteuid / dir_fd) → deny.
CLEANUP_IO_UNSUPPORTED_PLATFORM = "cleanup_io_unsupported_platform"

SHARED_CLEANUP_REASON_CODES = (
    NO_CLEANUP_CONTRACT,
    CLEANUP_CONTRACT_PRESENT_BUT_INVALID,
    CLEANUP_CONTRACT_EXPIRED,
    CLEANUP_CONTRACT_CONSUMED,
    CLEANUP_COMMAND_HASH_MISMATCH,
    CLEANUP_OPERATION_MISMATCH,
    WORKTREE_PATH_MISMATCH,
    WORKTREE_NOT_IN_CATALOG,
    WORKTREE_DIRTY,
    BRANCH_FORCE_DELETE_DENIED,
    ROOT_DRIFT_ACTIVE_WORKTREE_MISMATCH,
    PR_NOT_MERGED,
    GUARD_DEADLINE_EXCEEDED,
    CLEANUP_V2_DOWNGRADE_DENIED,
    CLEANUP_IO_UNSUPPORTED_PLATFORM,
)


# ── OS capability for durable symlink-safe IO (Blocker 9 portability) ──────────
def io_capabilities_ok() -> bool:
    """True iff the platform exposes the primitives the durable/symlink-safe IO
    path needs (``O_NOFOLLOW`` / ``O_DIRECTORY`` / ``geteuid`` / dir-fd rename).

    On platforms without these (notably Windows) a present V3 contract cannot be
    validated safely, so callers must deny with ``CLEANUP_IO_UNSUPPORTED_PLATFORM``
    rather than fall through to a less-safe path.
    """
    if not hasattr(os, "geteuid"):
        return False
    for attr in ("O_DIRECTORY", "O_NOFOLLOW"):
        if not hasattr(os, attr):
            return False
    if os.open not in os.supports_dir_fd:
        return False
    if os.rename not in os.supports_dir_fd:
        return False
    if os.unlink not in os.supports_dir_fd:
        return False
    return True


IO_CAPABLE = io_capabilities_ok()


# ── argv / hashing ────────────────────────────────────────────────────────────

def expected_argv(operation: str, worktree_path: str, branch_name: str) -> list[str]:
    """Canonical exact argv for an operation (worktree_path expected realpath-fixed)."""
    if operation == OP_WORKTREE_REMOVE:
        return ["git", "worktree", "remove", worktree_path]
    if operation == OP_BRANCH_DELETE:
        return ["git", "branch", "-d", branch_name]
    raise ValueError(f"unknown operation: {operation}")


def canonical_command_hash(argv: list[str], operation: str, project_root: str, nonce: str) -> str:
    """SHA-256 binding the *actual* argv to operation + project_root + nonce.

    Computed at materialize time from the realpath-fixed expected argv, and at
    runtime from the ``shlex.split()`` of the actual command — never reconstructed
    from contract fields. Including ``nonce`` makes each contract single-use.
    """
    payload = {
        "argv": [str(a) for a in argv],
        "operation": operation,
        "project_root": project_root,
        "nonce": nonce,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _is_hex_sha256(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return value == value.lower()


# ── time ──────────────────────────────────────────────────────────────────────

def parse_iso8601_tz(value: object) -> datetime | None:
    """Parse a *timezone-aware* ISO8601 timestamp to UTC. None if naive/invalid."""
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None  # timezone required (Medium)
    return dt.astimezone(timezone.utc)


def is_expired(contract: dict, now: datetime | None = None) -> bool:
    if not isinstance(contract, dict):
        return True
    exp = parse_iso8601_tz(contract.get("expires_at"))
    if exp is None:
        return True
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now >= exp


def _ttl_within_bounds(contract: dict, now: datetime | None = None) -> bool:
    issued = parse_iso8601_tz(contract.get("issued_at"))
    exp = parse_iso8601_tz(contract.get("expires_at"))
    if issued is None or exp is None:
        return False
    ttl = (exp - issued).total_seconds()
    if ttl <= 0 or ttl > MAX_TTL_SECONDS:
        return False
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    # issued_at must not be implausibly far in the future (clock skew bound).
    if (issued - now).total_seconds() > MAX_CLOCK_SKEW_SECONDS:
        return False
    return True


# ── branch validation ─────────────────────────────────────────────────────────

def is_valid_branch_ref(branch_name: object) -> bool:
    """Validate via ``git check-ref-format --branch`` (Medium: Git ref grammar)."""
    if not isinstance(branch_name, str) or not branch_name:
        return False
    if any(c in branch_name for c in " \t\n\r\0"):
        return False
    git = shutil.which("git")
    if not git:
        # Fail-closed conservative fallback: reject classic invalid ref chars.
        return not any(c in branch_name for c in "~^:?*[\\")
    try:
        out = subprocess.run(
            [git, "check-ref-format", "--branch", branch_name],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 0


# ── schema validation + three-valued loader ───────────────────────────────────

def validate_v3_contract(contract: object, now: datetime | None = None) -> tuple[bool, str | None]:
    """Validate a V3 contract's schema/fields (NOT expiry). Returns (ok, reason)."""
    if not isinstance(contract, dict):
        return False, "contract_not_object"
    if contract.get("schema") != SCHEMA_V3:
        return False, "schema_mismatch"

    if contract.get("operation") not in OPERATIONS:
        return False, "operation_invalid"

    wt_path = contract.get("worktree_path")
    if not isinstance(wt_path, str) or not os.path.isabs(wt_path):
        return False, "worktree_path_invalid"

    if not is_valid_branch_ref(contract.get("branch_name")):
        return False, "branch_name_invalid"

    if contract.get("require_clean") is not True:
        return False, "require_clean_not_true"

    if not _is_hex_sha256(contract.get("command_hash")):
        return False, "command_hash_invalid"

    nonce = contract.get("nonce")
    if not isinstance(nonce, str) or len(nonce) < 16:
        return False, "nonce_invalid"

    if parse_iso8601_tz(contract.get("issued_at")) is None:
        return False, "issued_at_invalid"
    if parse_iso8601_tz(contract.get("expires_at")) is None:
        return False, "expires_at_invalid"
    if not _ttl_within_bounds(contract, now=now):
        return False, "ttl_out_of_bounds"

    pr_number = contract.get("pr_number")
    if not isinstance(pr_number, int) or isinstance(pr_number, bool):
        return False, "pr_number_invalid"

    linked = contract.get("linked_issue_number")
    if linked is not None and (not isinstance(linked, int) or isinstance(linked, bool)):
        return False, "linked_issue_number_invalid"

    return True, None


def load_contract_state(project_root: str, now: datetime | None = None) -> tuple[str, dict | None, str | None]:
    """Three-valued loader (Blocker 2).

    Returns ``(state, contract, reason)`` where state is ABSENT / VALID_V3 /
    PRESENT_BUT_INVALID. If the safe-scratch path ``lexists()`` (including a
    dangling symlink), ANY parse/schema/permission/I/O error yields
    PRESENT_BUT_INVALID (deny) — it never falls through to ABSENT. Only a truly
    absent path returns ABSENT (the sole case a legacy V2 fallback is allowed).
    """
    target = os.path.join(project_root, SAFE_SCRATCH_CONTRACT_PATH)
    if not os.path.lexists(target):
        return STATE_ABSENT, None, None
    # Blocker 9: a present contract on a platform without the symlink-safe IO
    # primitives cannot be evaluated safely → deny (never fall through to ABSENT).
    if not IO_CAPABLE:
        return STATE_PRESENT_BUT_INVALID, None, CLEANUP_IO_UNSUPPORTED_PLATFORM
    try:
        raw = read_regular_file_nofollow(project_root, SAFE_SCRATCH_CONTRACT_PATH)
    except (OSError, ValueError):
        return STATE_PRESENT_BUT_INVALID, None, "read_error"
    try:
        contract = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return STATE_PRESENT_BUT_INVALID, None, "json_error"
    ok, reason = validate_v3_contract(contract, now=now)
    if not ok:
        return STATE_PRESENT_BUT_INVALID, None, reason
    return STATE_VALID_V3, contract, None


# ── durable + symlink-safe IO (dir-fd traversal, O_NOFOLLOW, fsync) ────────────
# openat2(RESOLVE_NO_SYMLINKS|RESOLVE_BENEATH) would be ideal on Linux but is not
# exposed by the Python stdlib; per-component O_NOFOLLOW dir-fd traversal gives
# equivalent symlink safety portably (Stop Condition fallback).

def _open_dir_nofollow(name: str, parent_fd: int) -> int:
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)


def _walk_to_dir(project_root: str, rel_components: list[str], *, create: bool) -> int:
    """Return an fd for the directory holding the final component.

    Each intermediate component is opened with ``O_DIRECTORY|O_NOFOLLOW``; a
    symlink component raises ELOOP (fail-closed). With ``create=True`` missing
    intermediate dirs are created with mode 0700.
    """
    root_real = os.path.realpath(project_root)
    cur = os.open(root_real, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for comp in rel_components[:-1]:
            try:
                nxt = _open_dir_nofollow(comp, cur)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(comp, mode=0o700, dir_fd=cur)
                nxt = _open_dir_nofollow(comp, cur)
            os.close(cur)
            cur = nxt
        return cur
    except BaseException:
        os.close(cur)
        raise


def write_json_durably(project_root: str, rel_path: str, data: dict) -> None:
    """Atomically + durably write JSON to project_root/rel_path, symlink-safe.

    temp (O_CREAT|O_EXCL|O_NOFOLLOW, 0600) -> write -> fchmod 0600 -> fsync(file)
    -> rename within dir -> fsync(parent dir). Any symlink component fails closed.
    """
    rel_components = [c for c in rel_path.split("/") if c]
    if not rel_components:
        raise ValueError("empty rel_path")
    final_name = rel_components[-1]
    tmp_name = f".{final_name}.tmp.{os.getpid()}"
    blob = (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")

    dir_fd = _walk_to_dir(project_root, rel_components, create=True)
    try:
        # Remove a stale temp if present (could be a symlink → unlink, don't follow).
        try:
            os.unlink(tmp_name, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        fd = os.open(
            tmp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=dir_fd,
        )
        try:
            os.fchmod(fd, 0o600)
            written = 0
            while written < len(blob):
                written += os.write(fd, blob[written:])
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(tmp_name, final_name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def read_regular_file_nofollow(project_root: str, rel_path: str) -> str:
    """Read project_root/rel_path with per-component O_NOFOLLOW + fstat checks.

    Rejects symlink components, non-regular files, group/other-accessible modes,
    foreign owners, and oversized files. Returns decoded UTF-8 text.
    """
    rel_components = [c for c in rel_path.split("/") if c]
    if not rel_components:
        raise ValueError("empty rel_path")
    final_name = rel_components[-1]
    dir_fd = _walk_to_dir(project_root, rel_components, create=False)
    try:
        fd = os.open(final_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise ValueError("symlink_component") from e
        raise
    finally:
        os.close(dir_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError("not_regular_file")
        if st.st_mode & 0o077:
            raise ValueError("insecure_mode")
        if st.st_uid != os.geteuid():
            raise ValueError("foreign_owner")
        if st.st_size > MAX_CONTRACT_BYTES:
            raise ValueError("too_large")
        return os.read(fd, st.st_size + 1).decode("utf-8")
    finally:
        os.close(fd)


# ── claim-first one-shot consume (Blocker 2) ──────────────────────────────────
# The previous "validate then consume" flow was fail-open: two concurrent guard
# invocations could both validate the same contract and both allow, because the
# consume return value was ignored. The hardened flow CLAIMS the contract FIRST
# via an atomic rename — only the single winner of the rename may then validate
# the claimed copy and allow. Everyone else (already-claimed / lost-race / absent)
# is denied with ``cleanup_contract_consumed``.

def _contract_dir_components() -> list[str]:
    return [c for c in SAFE_SCRATCH_CONTRACT_PATH.split("/") if c]


def claim_contract(project_root: str) -> str | None:
    """Atomically claim the safe-scratch contract; return the claimed basename.

    Renames ``cleanup_contract.json`` to a unique ``.cleanup_contract.json.claimed.<pid>.<token>``
    via dir-fd + ``O_NOFOLLOW`` rename. Exactly one concurrent caller wins the
    rename; everyone else gets ``FileNotFoundError`` and receives ``None``. The
    winner alone holds the right to validate + allow the cleanup. Returns ``None``
    when nothing could be claimed (absent / already consumed / lost race / IO error
    / unsupported platform).
    """
    if not IO_CAPABLE:
        return None
    rel_components = _contract_dir_components()
    final_name = rel_components[-1]
    token = secrets.token_hex(8)
    claimed_name = f".{final_name}.claimed.{os.getpid()}.{token}"
    try:
        dir_fd = _walk_to_dir(project_root, rel_components, create=False)
    except OSError:
        return None
    try:
        try:
            os.rename(final_name, claimed_name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except OSError:
            return None
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        return claimed_name
    finally:
        os.close(dir_fd)


def read_claimed_contract(
    project_root: str, claimed_name: str, now: datetime | None = None
) -> tuple[bool, dict | None, str | None]:
    """Read + validate a previously claimed contract file. Returns (ok, contract, reason).

    The claimed file is read with the same per-component ``O_NOFOLLOW`` + ``fstat``
    checks as the live contract, so a swapped symlink or foreign-owned claim fails
    closed. A claim that fails read/parse/schema/expiry validation yields
    ``(False, None, reason)`` and the caller must deny.
    """
    if not IO_CAPABLE:
        return False, None, CLEANUP_IO_UNSUPPORTED_PLATFORM
    rel_components = _contract_dir_components()
    rel_dir = "/".join(rel_components[:-1])
    rel_path = f"{rel_dir}/{claimed_name}" if rel_dir else claimed_name
    try:
        raw = read_regular_file_nofollow(project_root, rel_path)
    except (OSError, ValueError):
        return False, None, "read_error"
    try:
        contract = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return False, None, "json_error"
    ok, reason = validate_v3_contract(contract, now=now)
    if not ok:
        return False, None, reason
    if is_expired(contract, now=now):
        return False, None, CLEANUP_CONTRACT_EXPIRED
    return True, contract, None


def write_consume_tombstone(project_root: str, contract: dict | None) -> None:
    """Write a durable tombstone recording the consumed contract (Blocker 3).

    Its presence forbids subsequent legacy V2 fallback (see ``v2_fallback_forbidden``)
    so a single V3 consume cannot be followed by a V2-authorized second operation.
    """
    record = {
        "schema": TOMBSTONE_SCHEMA,
        "consumed_operation": (contract or {}).get("operation"),
        "consumed_nonce": (contract or {}).get("nonce"),
        "consumed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    try:
        write_json_durably(project_root, TOMBSTONE_REL_PATH, record)
    except (OSError, ValueError):
        # Tombstone is best-effort durability; the claim rename already prevents
        # replay of THIS contract. A missing tombstone only relaxes the V2-downgrade
        # ban, which still requires a separately-present (and now claimed-away) file.
        pass


def discard_claimed(project_root: str, claimed_name: str) -> None:
    """Remove a claimed contract file (used after consume or on validation failure)."""
    if not IO_CAPABLE:
        return
    rel_components = _contract_dir_components()
    try:
        dir_fd = _walk_to_dir(project_root, rel_components, create=False)
    except OSError:
        return
    try:
        try:
            os.unlink(claimed_name, dir_fd=dir_fd)
        except OSError:
            pass
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
    finally:
        os.close(dir_fd)


def v2_fallback_forbidden(project_root: str) -> bool:
    """True iff a durable consume tombstone exists; forbids legacy V2 downgrade (Blocker 3)."""
    return os.path.lexists(os.path.join(project_root, TOMBSTONE_REL_PATH))
