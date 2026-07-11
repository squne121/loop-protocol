"""Real-subprocess integration tests for scripts/trust-root/trusted_hook_launcher.py.

These tests build a temporary candidate Git worktree, a temporary external
"trust root" directory (manifest + active.json fixtures), and invoke the
launcher as a real subprocess (not a mocked function call) so that the
tests exercise actual process spawning, actual `git` subprocess invocation,
and actual filesystem permission checks — matching the Issue #1454 Runtime
Verification Applicability contract (`decision: immediate`).

Wire format (Issue #1454 fix_delta, OWNER adversarial review — see
https://github.com/squne121/loop-protocol/pull/1457#issuecomment-4945279761):
stdin carries the REAL Codex/Claude Code PreToolUse hook payload
(tool_name/tool_use_id/tool_input.command/cwd); ``--evidence-file`` carries
the separate trusted-verifier evidence bundle; allow/deny output uses the
real ``hookSpecificOutput.permissionDecision`` contract.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_TRUST_ROOT_DIR = _TESTS_DIR.parent
_LAUNCHER_PATH = _TRUST_ROOT_DIR / "trusted_hook_launcher.py"
_INSTALLER_PATH = _TRUST_ROOT_DIR / "install_trust_root.sh"

if str(_TRUST_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_TRUST_ROOT_DIR))

import trusted_hook_launcher as launcher  # noqa: E402

COMPONENT_A = "scripts/agent-guards/codex-hook-adapter.mjs"
COMPONENT_B = "scripts/agent-guards/git_mutation_command_policy.py"
GOOD_CONTENT_A = b"// trusted adapter v1\nexport const TRUSTED = true;\n"
GOOD_CONTENT_B = b"# trusted policy v1\nTRUSTED = True\n"
DEFAULT_TEST_REF = "refs/heads/worktree-issue-1454-test"
FAR_FUTURE_EXPIRY = "2999-01-01T00:00:00Z"

_GIT_TEST_ENV_EXTRA = {
    "GIT_AUTHOR_NAME": "trust-root-tests",
    "GIT_AUTHOR_EMAIL": "trust-root-tests@example.invalid",
    "GIT_COMMITTER_NAME": "trust-root-tests",
    "GIT_COMMITTER_EMAIL": "trust-root-tests@example.invalid",
}


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(_GIT_TEST_ENV_EXTRA)
    return env


def _git(repo_dir: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        env=env if env is not None else _git_env(),
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result


def _write(repo_dir: Path, rel_path: str, content: bytes) -> None:
    full_path = repo_dir / rel_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_bytes(content)


def _commit_all(repo_dir: Path, message: str) -> str:
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "-c", "commit.gpgsign=false", "commit", "-m", message)
    return _git(repo_dir, "rev-parse", "HEAD").stdout.strip()


def _init_candidate_repo(tmp_path: Path, repository_slug: str = "squne121/loop-protocol") -> Path:
    repo_dir = tmp_path / "candidate"
    repo_dir.mkdir(parents=True)
    _git(repo_dir, "init", "-q", "-b", "main")
    _git(repo_dir, "remote", "add", "origin", f"https://github.com/{repository_slug}.git")
    _write(repo_dir, COMPONENT_A, GOOD_CONTENT_A)
    _write(repo_dir, COMPONENT_B, GOOD_CONTENT_B)
    _write(repo_dir, ".codex/hooks.json", b'{"hooks": {}}\n')
    _commit_all(repo_dir, "initial trusted components")
    return repo_dir


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _build_trust_root(
    tmp_path: Path,
    trusted_commit_oid: str,
    components: dict[str, bytes],
    generation: int = 1,
    repository: str = "squne121/loop-protocol",
    raw_manifest_override: dict | None = None,
    name: str = "trust-root",
) -> Path:
    """Manually construct a trust root fixture (bypassing install_trust_root.sh,
    which requires a euid separation this single-user test sandbox cannot
    provide — see test_installer_same_euid_rejected for that script's own
    dedicated coverage)."""
    trust_root_dir = tmp_path / name
    trust_root_dir.mkdir(mode=0o700, parents=True)

    if raw_manifest_override is not None:
        manifest = raw_manifest_override
    else:
        manifest = {
            "manifest_version": "AUTHORIZATION_TCB_MANIFEST_V1",
            "repository": repository,
            "trusted_commit_oid": trusted_commit_oid,
            "components": [{"path": path, "sha256": _sha256_hex(content)} for path, content in components.items()],
            "issued_by": "test-operator@bastion",
            "generation": generation,
        }
    manifest_json = json.dumps(manifest, sort_keys=True)
    digest = _sha256_hex(manifest_json.encode("utf-8"))

    # Build the full directory/file layout FIRST (with owner-write enabled so
    # nested mkdir/write calls succeed), then lock permissions down to
    # owner-only (read/execute for dirs, read-only for files) as the final
    # step — mirroring install_trust_root.sh's own create-then-finalize order.
    release_dir = trust_root_dir / "releases" / f"{generation}-{digest}"
    release_dir.mkdir(parents=True, mode=0o700)
    manifest_path = release_dir / "manifest.json"
    manifest_path.write_text(manifest_json, encoding="utf-8")

    active_path = trust_root_dir / "active.json"
    active_payload = {
        "active_generation": generation,
        "manifest_relpath": f"releases/{generation}-{digest}/manifest.json",
    }
    active_path.write_text(json.dumps(active_payload, sort_keys=True), encoding="utf-8")

    manifest_path.chmod(0o400)
    release_dir.chmod(0o500)
    (trust_root_dir / "releases").chmod(0o500)
    # The trust root's TOP-LEVEL directory keeps owner write (0700, not 0500):
    # the launcher (running as the trust root owner) still needs to create the
    # nonces/ single-use marker directory at runtime. Immutable release
    # artifacts (manifest.json, releases/) remain owner-read-only above.
    # Non-owner (group/other) access remains fully denied either way.
    trust_root_dir.chmod(0o700)
    return trust_root_dir


def _command_string(ref: str = DEFAULT_TEST_REF) -> str:
    return f"git push origin HEAD:{ref}"


def _hook_payload(
    repo_dir: Path,
    tool_use_id: str = "tool-1",
    tool_name: str = "Bash",
    command: str | None = None,
) -> dict:
    return {
        "tool_name": tool_name,
        "tool_use_id": tool_use_id,
        "tool_input": {"command": command if command is not None else _command_string()},
        "cwd": str(repo_dir),
    }


def _evidence(
    candidate_repo_dir: Path,
    local_oid: str,
    ref: str = DEFAULT_TEST_REF,
    remote_oid: str | None = None,
    tool_use_id: str = "tool-1",
    nonce: str = "nonce-1",
    expiry: str = FAR_FUTURE_EXPIRY,
    command: str | None = None,
) -> dict:
    command_str = command if command is not None else _command_string(ref)
    return {
        "session_id": "sess-1",
        "turn_id": "turn-1",
        "tool_use_id": tool_use_id,
        "local_oid": local_oid,
        "remote_oid": remote_oid or ("0" * 40),
        "ref": ref,
        "issue_number": 1454,
        "nonce": nonce,
        "expiry": expiry,
        "candidate_repo_dir": str(candidate_repo_dir),
        "command_sha256": hashlib.sha256(command_str.encode("utf-8")).hexdigest(),
    }


def _matching_hook_and_evidence(
    repo_dir: Path,
    local_oid: str,
    ref: str = DEFAULT_TEST_REF,
    remote_oid: str | None = None,
    nonce: str = "nonce-1",
) -> tuple[dict, dict]:
    """Build a hook payload + evidence bundle whose command / tool_use_id /
    cwd are all mutually consistent (the "happy path" binding)."""
    command = _command_string(ref)
    hook = _hook_payload(repo_dir, command=command)
    evidence = _evidence(repo_dir, local_oid, ref=ref, remote_oid=remote_oid, nonce=nonce, command=command)
    return hook, evidence


def _run_launcher(
    trust_root_dir: Path,
    hook_payload: dict,
    evidence: dict,
    extra_env: dict[str, str] | None = None,
    allow_same_uid: bool = True,
) -> tuple[int, dict]:
    env = dict(os.environ)
    if allow_same_uid:
        env["LOOP_TRUST_ROOT_TEST_ALLOW_SAME_UID"] = "1"
    if extra_env:
        env.update(extra_env)
    evidence_path = trust_root_dir.parent / f"evidence-{os.getpid()}-{id(evidence)}.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(_LAUNCHER_PATH),
            "--trust-root-dir",
            str(trust_root_dir),
            "--evidence-file",
            str(evidence_path),
        ],
        input=json.dumps(hook_payload),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    return result.returncode, payload


def _decision(payload: dict) -> str:
    return payload["hookSpecificOutput"]["permissionDecision"]


def _reason_code(payload: dict) -> str:
    reason = payload["hookSpecificOutput"].get("permissionDecisionReason", "")
    return reason.split(":", 1)[0]


def _updated_command_str(payload: dict) -> str:
    return payload["hookSpecificOutput"]["updatedInput"]["command"]


# ─── AC1: external absolute path + owner-only permission validation ─────────


def test_external_absolute_path_permission(tmp_path: Path) -> None:
    """GIVEN the launcher module, WHEN its own file path is resolved, THEN it
    is an absolute path (external_absolute_path_permission). AND GIVEN a
    trust root directory with group-writable permissions, WHEN permission is
    validated, THEN it is denied; GIVEN owner-only permissions, THEN it is
    accepted."""
    assert Path(launcher.__file__).is_absolute()

    writable_dir = tmp_path / "group-writable-root"
    writable_dir.mkdir()
    writable_dir.chmod(0o770)
    denial = launcher.validate_trust_root_permissions(writable_dir)
    assert denial is not None
    assert denial.reason_code == launcher.REASON_RUNTIME_UNTRUSTED

    owner_only_dir = tmp_path / "owner-only-root"
    owner_only_dir.mkdir()
    owner_only_dir.chmod(0o500)
    ok = launcher.validate_trust_root_permissions(owner_only_dir)
    assert ok is None


def test_production_path_allowlist(tmp_path: Path) -> None:
    """GIVEN the fixed production trust root allowlist mechanism, WHEN a path
    matches one of the allowlist entries, THEN it is accepted; WHEN it does
    not, THEN it is rejected."""
    allowed_dir = tmp_path / "allowed"
    allowed_dir.mkdir()
    other_dir = tmp_path / "other"
    other_dir.mkdir()

    assert launcher.validate_trust_root_path_allowlist(allowed_dir, allowlist=(allowed_dir,)) is True
    assert launcher.validate_trust_root_path_allowlist(other_dir, allowlist=(allowed_dir,)) is False
    # Production default: the fixed constant is a 1-element allowlist of
    # DEFAULT_TRUST_ROOT_DIR itself.
    assert launcher.validate_trust_root_path_allowlist(launcher.DEFAULT_TRUST_ROOT_DIR) is True


# ─── AC3: tamper on a committed component is denied ─────────────────────────


def test_tamper_component_denied(tmp_path: Path) -> None:
    """GIVEN a manifest bound to a trusted commit's good component digests,
    WHEN a NEW commit changes a critical component's content, THEN the
    launcher denies with authorization_component_digest_mismatch
    (tamper_component_denied) for the new (tampered) commit."""
    repo_dir = _init_candidate_repo(tmp_path)
    good_oid = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    trust_root_dir = _build_trust_root(
        tmp_path, good_oid, {COMPONENT_A: GOOD_CONTENT_A, COMPONENT_B: GOOD_CONTENT_B}
    )

    _write(repo_dir, COMPONENT_A, b"// TAMPERED adapter\nexport const TRUSTED = false;\n")
    tampered_oid = _commit_all(repo_dir, "tamper component A")

    hook, evidence = _matching_hook_and_evidence(repo_dir, tampered_oid)
    returncode, payload = _run_launcher(trust_root_dir, hook, evidence)

    assert returncode != 0
    assert _decision(payload) == "deny"
    assert _reason_code(payload) == launcher.REASON_COMPONENT_DIGEST_MISMATCH


# ─── AC4: launcher denies independent of any candidate-side integrity check ─


def test_integrity_check_removed_still_denied(tmp_path: Path) -> None:
    """GIVEN a candidate repository that has entirely REMOVED any local
    integrity-check invocation file (simulating an attacker stripping out
    candidate-side verification calls), WHEN the external launcher runs,
    THEN it still denies a tampered component — proving the authorization
    decision never depends on candidate-side code execution
    (integrity_check_removed_still_denied)."""
    repo_dir = _init_candidate_repo(tmp_path)
    _write(repo_dir, "scripts/agent-guards/local_integrity_verifier.py", b"# calls hash check\n")
    good_oid = _commit_all(repo_dir, "add candidate-side verifier stub")

    trust_root_dir = _build_trust_root(
        tmp_path, good_oid, {COMPONENT_A: GOOD_CONTENT_A, COMPONENT_B: GOOD_CONTENT_B}
    )

    (repo_dir / "scripts/agent-guards/local_integrity_verifier.py").unlink()
    _write(repo_dir, COMPONENT_B, b"# TAMPERED policy\nTRUSTED = False\n")
    tampered_oid = _commit_all(repo_dir, "remove verifier and tamper component B")

    hook, evidence = _matching_hook_and_evidence(repo_dir, tampered_oid)
    returncode, payload = _run_launcher(trust_root_dir, hook, evidence)

    assert returncode != 0
    assert _decision(payload) == "deny"
    assert _reason_code(payload) == launcher.REASON_COMPONENT_DIGEST_MISMATCH


# ─── AC5: on full match, trusted copy path is used, not candidate path ──────


def test_trusted_copy_executed_not_candidate(tmp_path: Path) -> None:
    """GIVEN all components matching the manifest, WHEN the launcher
    authorizes, THEN the resulting updatedInput.command (a single shell
    string) references only the fixed absolute trusted git binary and Git
    objects (oid/ref) — never any path inside the candidate repository
    (trusted_copy_executed_not_candidate)."""
    repo_dir = _init_candidate_repo(tmp_path)
    good_oid = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    trust_root_dir = _build_trust_root(
        tmp_path, good_oid, {COMPONENT_A: GOOD_CONTENT_A, COMPONENT_B: GOOD_CONTENT_B}
    )

    hook, evidence = _matching_hook_and_evidence(repo_dir, good_oid)
    returncode, payload = _run_launcher(trust_root_dir, hook, evidence)

    assert returncode == 0
    assert _decision(payload) == "allow"
    command = _updated_command_str(payload)
    assert isinstance(command, str)
    import shlex

    argv = shlex.split(command)
    assert Path(argv[0]).is_absolute()
    assert str(repo_dir) not in command
    assert argv[1] == "push"
    assert argv[3] == "origin"


# ─── AC6: symlink / missing / duplicate-in-manifest all fail closed ─────────


def test_symlink_and_missing_denied(tmp_path: Path) -> None:
    """GIVEN a critical component that is symlinked in the candidate tree,
    WHEN the launcher runs, THEN it denies with
    authorization_component_type_invalid. GIVEN a critical component that is
    entirely absent from the tree, THEN it denies with
    authorization_component_missing. GIVEN a manifest containing a duplicate
    component path, THEN it denies with authorization_manifest_invalid
    (symlink_and_missing_denied)."""
    # Symlink case.
    repo_dir = _init_candidate_repo(tmp_path)
    good_oid = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    trust_root_dir = _build_trust_root(
        tmp_path, good_oid, {COMPONENT_A: GOOD_CONTENT_A, COMPONENT_B: GOOD_CONTENT_B}
    )
    (repo_dir / COMPONENT_A).unlink()
    os.symlink("/etc/passwd", repo_dir / COMPONENT_A)
    symlink_oid = _commit_all(repo_dir, "symlink-ify component A")

    hook, evidence = _matching_hook_and_evidence(repo_dir, symlink_oid, nonce="nonce-symlink")
    returncode, payload = _run_launcher(trust_root_dir, hook, evidence)
    assert returncode != 0
    assert _reason_code(payload) == launcher.REASON_COMPONENT_TYPE_INVALID

    # Missing case: component B removed entirely.
    repo_dir2 = _init_candidate_repo(tmp_path / "missing-case")
    good_oid2 = _git(repo_dir2, "rev-parse", "HEAD").stdout.strip()
    trust_root_dir2 = _build_trust_root(
        tmp_path / "missing-case", good_oid2, {COMPONENT_A: GOOD_CONTENT_A, COMPONENT_B: GOOD_CONTENT_B}
    )
    (repo_dir2 / COMPONENT_B).unlink()
    missing_oid = _commit_all(repo_dir2, "remove component B")

    hook2, evidence2 = _matching_hook_and_evidence(repo_dir2, missing_oid, nonce="nonce-missing")
    returncode2, payload2 = _run_launcher(trust_root_dir2, hook2, evidence2)
    assert returncode2 != 0
    assert _reason_code(payload2) == launcher.REASON_COMPONENT_MISSING

    # Duplicate-in-manifest case.
    repo_dir3 = _init_candidate_repo(tmp_path / "dup-case")
    good_oid3 = _git(repo_dir3, "rev-parse", "HEAD").stdout.strip()
    duplicate_manifest = {
        "manifest_version": "AUTHORIZATION_TCB_MANIFEST_V1",
        "repository": "squne121/loop-protocol",
        "trusted_commit_oid": good_oid3,
        "components": [
            {"path": COMPONENT_A, "sha256": _sha256_hex(GOOD_CONTENT_A)},
            {"path": COMPONENT_A, "sha256": _sha256_hex(GOOD_CONTENT_A)},
        ],
        "issued_by": "test-operator@bastion",
        "generation": 1,
    }
    trust_root_dir3 = _build_trust_root(
        tmp_path / "dup-case", good_oid3, {}, raw_manifest_override=duplicate_manifest
    )
    hook3, evidence3 = _matching_hook_and_evidence(repo_dir3, good_oid3, nonce="nonce-dup")
    returncode3, payload3 = _run_launcher(trust_root_dir3, hook3, evidence3)
    assert returncode3 != 0
    assert _reason_code(payload3) == launcher.REASON_MANIFEST_INVALID


# ─── AC7: origin/main rewrite inside the candidate repo is ignored ──────────


def test_origin_main_rewrite_ignored(tmp_path: Path) -> None:
    """GIVEN a candidate repository whose `refs/remotes/origin/main` has been
    rewritten to point at a malicious commit, WHEN the launcher evaluates
    authorization using an explicit good local_oid, THEN the decision is
    unaffected by origin/main and correctly allows
    (origin_main_rewrite_ignored)."""
    repo_dir = _init_candidate_repo(tmp_path)
    good_oid = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    trust_root_dir = _build_trust_root(
        tmp_path, good_oid, {COMPONENT_A: GOOD_CONTENT_A, COMPONENT_B: GOOD_CONTENT_B}
    )

    _write(repo_dir, COMPONENT_A, b"// malicious origin/main content\n")
    malicious_oid = _commit_all(repo_dir, "malicious commit for origin/main rewrite")
    _git(repo_dir, "update-ref", "refs/remotes/origin/main", malicious_oid)

    hook, evidence = _matching_hook_and_evidence(repo_dir, good_oid)
    returncode, payload = _run_launcher(trust_root_dir, hook, evidence)

    assert returncode == 0
    assert _decision(payload) == "allow"


# ─── AC8: refs/replace substitution is ignored ──────────────────────────────


def test_refs_replace_ignored(tmp_path: Path) -> None:
    """GIVEN a `refs/replace/<local_oid>` object substituting a different
    (malicious) commit in place of the trusted local_oid, WHEN the launcher
    resolves local_oid with `--no-replace-objects`, THEN the substitution has
    no effect and the good commit's tree is still what gets verified
    (refs_replace_ignored)."""
    repo_dir = _init_candidate_repo(tmp_path)
    good_oid = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    trust_root_dir = _build_trust_root(
        tmp_path, good_oid, {COMPONENT_A: GOOD_CONTENT_A, COMPONENT_B: GOOD_CONTENT_B}
    )

    _write(repo_dir, COMPONENT_A, b"// malicious replacement content\n")
    malicious_oid = _commit_all(repo_dir, "malicious replacement commit")
    _git(repo_dir, "replace", good_oid, malicious_oid)

    hook, evidence = _matching_hook_and_evidence(repo_dir, good_oid)
    returncode, payload = _run_launcher(trust_root_dir, hook, evidence)

    assert returncode == 0
    assert _decision(payload) == "allow"


# ─── AC9: fake binaries at the front of PATH have no effect ─────────────────


def test_fake_path_binaries_ignored(tmp_path: Path) -> None:
    """GIVEN a PATH whose first entry contains fake `python3`/`git`/`node`/`rtk`
    executables (each of which would corrupt behavior if consulted), WHEN
    the launcher subprocess runs with that PATH inherited, THEN the launcher
    still resolves its OWN trusted absolute git binary (never consulting
    PATH) and produces the correct decision (fake_path_binaries_ignored)."""
    repo_dir = _init_candidate_repo(tmp_path)
    good_oid = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    trust_root_dir = _build_trust_root(
        tmp_path, good_oid, {COMPONENT_A: GOOD_CONTENT_A, COMPONENT_B: GOOD_CONTENT_B}
    )

    fake_bin_dir = tmp_path / "fake-bin"
    fake_bin_dir.mkdir()
    for fake_name in ("python3", "git", "node", "rtk"):
        fake_script = fake_bin_dir / fake_name
        fake_script.write_text("#!/bin/sh\necho FAKE_BINARY_INVOKED >&2\nexit 99\n")
        fake_script.chmod(0o755)

    poisoned_path = f"{fake_bin_dir}:{os.environ.get('PATH', '')}"
    hook, evidence = _matching_hook_and_evidence(repo_dir, good_oid)
    returncode, payload = _run_launcher(trust_root_dir, hook, evidence, extra_env={"PATH": poisoned_path})

    assert returncode == 0
    assert _decision(payload) == "allow"


# ─── AC11: trust root missing is fail-closed, never generic allow ──────────


def test_trust_root_missing_denied(tmp_path: Path) -> None:
    """GIVEN a trust root directory that does not exist (feature not yet
    installed by a privileged operator), WHEN the launcher runs, THEN it
    denies with authorization_trust_root_missing rather than generically
    allowing (trust_root_missing_denied)."""
    repo_dir = _init_candidate_repo(tmp_path)
    good_oid = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    nonexistent_trust_root = tmp_path / "does-not-exist"

    hook, evidence = _matching_hook_and_evidence(repo_dir, good_oid)
    returncode, payload = _run_launcher(nonexistent_trust_root, hook, evidence)

    assert returncode != 0
    assert _decision(payload) == "deny"
    assert _reason_code(payload) == launcher.REASON_TRUST_ROOT_MISSING


# ─── AC13: managed registration survives local hook removal ───────────────


@pytest.mark.parametrize(
    "hooks_json_state",
    ["deleted", "emptied", "changed"],
)
def test_managed_registration_survives_local_hook_removal(tmp_path: Path, hooks_json_state: str) -> None:
    """GIVEN a candidate repository's project-local `.codex/hooks.json` is
    deleted, emptied, or changed to a different command, WHEN the external
    launcher runs (simulating a managed-hook-registration invocation that
    does not depend on that project-local file at all), THEN the
    authorization decision is unaffected — proving the launcher's operation
    does not read or depend on `.codex/hooks.json`
    (managed_registration_survives_local_hook_removal)."""
    repo_dir = _init_candidate_repo(tmp_path)
    _git(repo_dir, "rev-parse", "HEAD")  # sanity: repo is committed and readable

    hooks_path = repo_dir / ".codex" / "hooks.json"
    if hooks_json_state == "deleted":
        hooks_path.unlink()
    elif hooks_json_state == "emptied":
        hooks_path.write_text("", encoding="utf-8")
    elif hooks_json_state == "changed":
        hooks_path.write_text('{"hooks": {"PreToolUse": []}}\n', encoding="utf-8")
    tampered_oid = _commit_all(repo_dir, f"mutate .codex/hooks.json: {hooks_json_state}")

    # component contents did not change, so this should still allow using the
    # new commit as local_oid — the launcher never even looks at hooks.json.
    trust_root_dir2 = _build_trust_root(
        tmp_path / f"tr-{hooks_json_state}", tampered_oid, {COMPONENT_A: GOOD_CONTENT_A, COMPONENT_B: GOOD_CONTENT_B}
    )
    hook, evidence = _matching_hook_and_evidence(repo_dir, tampered_oid, nonce=f"nonce-{hooks_json_state}")
    returncode, payload = _run_launcher(trust_root_dir2, hook, evidence)

    assert returncode == 0
    assert _decision(payload) == "allow"


# ─── AC14: committed malicious content wins over a "cleaned" working tree ───


def test_committed_malicious_working_tree_clean_denied(tmp_path: Path) -> None:
    """GIVEN HEAD contains a malicious component commit, and the WORKING TREE
    has been reverted (without a new commit) to the approved bytes, WHEN the
    launcher evaluates local_oid=HEAD, THEN it denies based on the
    COMMITTED tree/blob content (not the working tree bytes) — a naive
    lstat-based verifier would have incorrectly allowed here
    (committed_malicious_working_tree_clean_denied)."""
    repo_dir = _init_candidate_repo(tmp_path)
    good_oid = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    trust_root_dir = _build_trust_root(
        tmp_path, good_oid, {COMPONENT_A: GOOD_CONTENT_A, COMPONENT_B: GOOD_CONTENT_B}
    )

    _write(repo_dir, COMPONENT_A, b"// MALICIOUS committed content\n")
    malicious_oid = _commit_all(repo_dir, "commit malicious component A")

    # Revert the WORKING TREE ONLY back to approved bytes, without committing.
    _write(repo_dir, COMPONENT_A, GOOD_CONTENT_A)

    hook, evidence = _matching_hook_and_evidence(repo_dir, malicious_oid)
    returncode, payload = _run_launcher(trust_root_dir, hook, evidence)

    assert returncode != 0
    assert _decision(payload) == "deny"
    assert _reason_code(payload) == launcher.REASON_CANDIDATE_COMMIT_COMPONENT_MISMATCH


# ─── AC15: trusted publisher command rewrite (allow path) ──────────────────


def test_trusted_publisher_command_rewrite(tmp_path: Path) -> None:
    """GIVEN all components match, WHEN the launcher authorizes, THEN it
    replaces the command with a FIXED, absolute-path, `--force-with-lease`
    pinned publisher argv (rendered as a single shell string) — regardless
    of a poisoned PATH (fake `rtk`) or HEAD having moved to a different
    branch since evidence capture (trusted_publisher_command_rewrite)."""
    repo_dir = _init_candidate_repo(tmp_path)
    good_oid = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    trust_root_dir = _build_trust_root(
        tmp_path, good_oid, {COMPONENT_A: GOOD_CONTENT_A, COMPONENT_B: GOOD_CONTENT_B}
    )

    fake_bin_dir = tmp_path / "fake-bin-2"
    fake_bin_dir.mkdir()
    fake_rtk = fake_bin_dir / "rtk"
    fake_rtk.write_text("#!/bin/sh\necho FAKE_RTK >&2\nexit 1\n")
    fake_rtk.chmod(0o755)
    poisoned_path = f"{fake_bin_dir}:{os.environ.get('PATH', '')}"

    # Move HEAD to a different branch AFTER the evidence's local_oid was
    # determined, simulating a TOCTOU attempt between capture and execution.
    _git(repo_dir, "checkout", "-q", "-b", "attacker-branch")
    _write(repo_dir, COMPONENT_A, b"// unrelated attacker-branch change\n")
    _commit_all(repo_dir, "attacker branch unrelated change")

    ref = DEFAULT_TEST_REF
    remote_oid = "1" * 40
    hook, evidence = _matching_hook_and_evidence(repo_dir, good_oid, ref=ref, remote_oid=remote_oid)
    returncode, payload = _run_launcher(trust_root_dir, hook, evidence, extra_env={"PATH": poisoned_path})

    assert returncode == 0
    assert _decision(payload) == "allow"
    command = _updated_command_str(payload)
    assert isinstance(command, str)
    import shlex

    argv = shlex.split(command)
    assert argv[0] not in (str(fake_rtk),)
    assert Path(argv[0]).is_absolute()
    assert argv[1] == "push"
    assert argv[2] == f"--force-with-lease={ref}:{remote_oid}"
    assert argv[3] == "origin"
    assert argv[4] == f"{good_oid}:{ref}"


# ─── AC16: installer refuses same-euid execution ────────────────────────────


def test_installer_same_euid_rejected(tmp_path: Path) -> None:
    """GIVEN the installer is invoked by the SAME account that owns the
    target trust_root_dir (as is always true in this single-user test
    sandbox), WHEN install_trust_root.sh runs, THEN it refuses installation
    with runtime_euid_must_differ_from_owner
    (installer_same_euid_rejected)."""
    repo_dir = _init_candidate_repo(tmp_path)
    good_oid = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()

    trust_root_dir = tmp_path / "installer-target"
    trust_root_dir.mkdir()

    result = subprocess.run(
        [
            "bash",
            str(_INSTALLER_PATH),
            str(trust_root_dir),
            str(repo_dir),
            good_oid,
            "squne121/loop-protocol",
            COMPONENT_A,
            COMPONENT_B,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode != 0
    assert "runtime_euid_must_differ_from_owner" in result.stderr


@pytest.mark.skipif(
    os.getuid() == 0,
    reason=(
        "genuine multi-account privilege separation is required to exercise the "
        "installer's SUCCESS path (a real second uid owning trust_root_dir, distinct "
        "from the invoking account); unavailable in this single-user sandbox/CI runner "
        "(Issue #1454 skip_conditions). Reproduction: run as a non-root operator account "
        "against a trust_root_dir chowned to a DIFFERENT dedicated service account, e.g. "
        "`sudo -u trust-root-owner install -d -m 0700 /opt/loop-protocol/trust-root` then "
        "`install_trust_root.sh /opt/loop-protocol/trust-root <repo> <oid> <slug> <paths...>` "
        "as the privileged operator account (NOT trust-root-owner)."
    ),
)
def test_installer_success_path_requires_privileged_multi_uid() -> None:  # pragma: no cover
    """Documents (Issue #1454 fix_delta P1-6) that a genuine installer SUCCESS
    path (distinct trust-root-owner vs. invoking-operator uid) cannot be
    exercised without real multi-account privilege separation, which this
    sandbox does not provide. This test intentionally always skips here; it
    exists so CI output makes the gap and its reproduction steps explicit
    rather than silently omitting install-success coverage."""
    pytest.skip("requires real multi-uid privilege separation; see skip reason")


# ─── Additional tamper coverage: uncommitted ambiguity on a critical path ───


def test_candidate_tree_ambiguous_denied(tmp_path: Path) -> None:
    """GIVEN local_oid's tree fully matches the manifest, but the candidate
    working tree ALSO has an uncommitted (dirty) modification to a critical
    component path, WHEN the launcher evaluates authorization, THEN it
    denies with candidate_tree_ambiguous rather than allowing — an
    uncommitted critical-path edit creates a TOCTOU race between this check
    and the eventual push."""
    repo_dir = _init_candidate_repo(tmp_path)
    good_oid = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    trust_root_dir = _build_trust_root(
        tmp_path, good_oid, {COMPONENT_A: GOOD_CONTENT_A, COMPONENT_B: GOOD_CONTENT_B}
    )

    # Dirty, uncommitted edit to a critical path (different from both the
    # committed AND the manifest-approved bytes).
    _write(repo_dir, COMPONENT_B, GOOD_CONTENT_B + b"# uncommitted trailer\n")

    hook, evidence = _matching_hook_and_evidence(repo_dir, good_oid)
    returncode, payload = _run_launcher(trust_root_dir, hook, evidence)

    assert returncode != 0
    assert _decision(payload) == "deny"
    assert _reason_code(payload) == launcher.REASON_CANDIDATE_TREE_AMBIGUOUS


# ─── Issue #1454 fix_delta: real PreToolUse wire format contract tests ──────


def test_hook_payload_wrong_tool_name_denied(tmp_path: Path) -> None:
    """GIVEN a PreToolUse hook payload whose tool_name is not "Bash", WHEN the
    launcher runs, THEN it denies with authorization_hook_payload_invalid —
    this publish lane only ever intercepts Bash tool calls."""
    repo_dir = _init_candidate_repo(tmp_path)
    good_oid = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    trust_root_dir = _build_trust_root(
        tmp_path, good_oid, {COMPONENT_A: GOOD_CONTENT_A, COMPONENT_B: GOOD_CONTENT_B}
    )
    command = _command_string()
    hook = _hook_payload(repo_dir, tool_name="Write", command=command)
    evidence = _evidence(repo_dir, good_oid, command=command)

    returncode, payload = _run_launcher(trust_root_dir, hook, evidence)

    assert returncode != 0
    assert _decision(payload) == "deny"
    assert _reason_code(payload) == launcher.REASON_HOOK_PAYLOAD_INVALID


def test_hook_payload_malformed_stdin_denied(tmp_path: Path) -> None:
    """GIVEN stdin that is not valid JSON at all, WHEN the launcher runs,
    THEN it denies with authorization_hook_payload_invalid rather than
    crashing or generically allowing."""
    repo_dir = _init_candidate_repo(tmp_path)
    good_oid = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    trust_root_dir = _build_trust_root(
        tmp_path, good_oid, {COMPONENT_A: GOOD_CONTENT_A, COMPONENT_B: GOOD_CONTENT_B}
    )
    evidence = _evidence(repo_dir, good_oid)
    evidence_path = tmp_path / "evidence-malformed.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    env = dict(os.environ)
    env["LOOP_TRUST_ROOT_TEST_ALLOW_SAME_UID"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            str(_LAUNCHER_PATH),
            "--trust-root-dir",
            str(trust_root_dir),
            "--evidence-file",
            str(evidence_path),
        ],
        input="{not valid json",
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    payload = json.loads(result.stdout.strip().splitlines()[-1])

    assert result.returncode != 0
    assert _decision(payload) == "deny"
    assert _reason_code(payload) == launcher.REASON_HOOK_PAYLOAD_INVALID


@pytest.mark.parametrize(
    "mutate",
    ["tool_use_id", "cwd", "command"],
)
def test_evidence_binding_mismatch_denied(tmp_path: Path, mutate: str) -> None:
    """GIVEN an evidence bundle whose tool_use_id, candidate_repo_dir, or
    command_sha256 does NOT match the actual hook payload for this call,
    WHEN the launcher runs, THEN it denies with
    authorization_evidence_binding_mismatch — a foreign/stale evidence file
    can never authorize a different tool call than the one intercepted."""
    repo_dir = _init_candidate_repo(tmp_path)
    good_oid = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    trust_root_dir = _build_trust_root(
        tmp_path, good_oid, {COMPONENT_A: GOOD_CONTENT_A, COMPONENT_B: GOOD_CONTENT_B}
    )
    command = _command_string()
    hook = _hook_payload(repo_dir, tool_use_id="tool-real", command=command)
    evidence = _evidence(repo_dir, good_oid, tool_use_id="tool-real", command=command)

    if mutate == "tool_use_id":
        evidence["tool_use_id"] = "tool-DIFFERENT"
    elif mutate == "cwd":
        evidence["candidate_repo_dir"] = str(tmp_path / "somewhere-else")
    elif mutate == "command":
        evidence["command_sha256"] = hashlib.sha256(b"git push origin HEAD:refs/heads/other").hexdigest()

    returncode, payload = _run_launcher(trust_root_dir, hook, evidence)

    assert returncode != 0
    assert _decision(payload) == "deny"
    assert _reason_code(payload) == launcher.REASON_EVIDENCE_BINDING_MISMATCH


def test_evidence_expired_denied(tmp_path: Path) -> None:
    """GIVEN evidence whose expiry is in the past, WHEN the launcher runs,
    THEN it denies with authorization_evidence_expired."""
    repo_dir = _init_candidate_repo(tmp_path)
    good_oid = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    trust_root_dir = _build_trust_root(
        tmp_path, good_oid, {COMPONENT_A: GOOD_CONTENT_A, COMPONENT_B: GOOD_CONTENT_B}
    )
    command = _command_string()
    hook = _hook_payload(repo_dir, command=command)
    past_expiry = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    evidence = _evidence(repo_dir, good_oid, command=command, expiry=past_expiry)

    returncode, payload = _run_launcher(trust_root_dir, hook, evidence)

    assert returncode != 0
    assert _decision(payload) == "deny"
    assert _reason_code(payload) == launcher.REASON_EVIDENCE_EXPIRED


def test_nonce_replay_denied(tmp_path: Path) -> None:
    """GIVEN a nonce that has already been consumed by a prior ALLOW
    decision, WHEN the SAME nonce is presented again (even with an
    otherwise-valid evidence bundle), THEN the launcher denies with
    authorization_nonce_replayed."""
    repo_dir = _init_candidate_repo(tmp_path)
    good_oid = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    trust_root_dir = _build_trust_root(
        tmp_path, good_oid, {COMPONENT_A: GOOD_CONTENT_A, COMPONENT_B: GOOD_CONTENT_B}
    )
    hook, evidence = _matching_hook_and_evidence(repo_dir, good_oid, nonce="nonce-replay-test")

    first_rc, first_payload = _run_launcher(trust_root_dir, hook, evidence)
    assert first_rc == 0
    assert _decision(first_payload) == "allow"

    second_rc, second_payload = _run_launcher(trust_root_dir, hook, evidence)
    assert second_rc != 0
    assert _decision(second_payload) == "deny"
    assert _reason_code(second_payload) == launcher.REASON_NONCE_REPLAYED


@pytest.mark.parametrize(
    "bad_ref",
    [
        "refs/heads/main",
        "refs/heads/master",
        "refs/tags/v1.0.0",
        "refs/notes/commits",
        "main",
    ],
)
def test_ref_untrusted_denied(tmp_path: Path, bad_ref: str) -> None:
    """GIVEN a target ref that is not an allowed `refs/heads/<branch>` form,
    OR is a conventional protected branch name, WHEN the launcher runs,
    THEN it denies with authorization_ref_untrusted — this publish lane
    only authorizes issue/feature branch pushes."""
    repo_dir = _init_candidate_repo(tmp_path)
    good_oid = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    trust_root_dir = _build_trust_root(
        tmp_path, good_oid, {COMPONENT_A: GOOD_CONTENT_A, COMPONENT_B: GOOD_CONTENT_B}
    )
    command = _command_string(bad_ref)
    hook = _hook_payload(repo_dir, command=command)
    evidence = _evidence(repo_dir, good_oid, ref=bad_ref, command=command)

    returncode, payload = _run_launcher(trust_root_dir, hook, evidence)

    assert returncode != 0
    assert _decision(payload) == "deny"
    assert _reason_code(payload) == launcher.REASON_REF_UNTRUSTED


def test_repository_identity_mismatch_denied(tmp_path: Path) -> None:
    """GIVEN a candidate repository whose remote.origin.url points at a
    DIFFERENT repository than manifest.repository declares, WHEN the
    launcher runs, THEN it denies with
    authorization_repository_identity_mismatch."""
    repo_dir = _init_candidate_repo(tmp_path, repository_slug="attacker/unrelated-repo")
    good_oid = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    trust_root_dir = _build_trust_root(
        tmp_path, good_oid, {COMPONENT_A: GOOD_CONTENT_A, COMPONENT_B: GOOD_CONTENT_B},
        repository="squne121/loop-protocol",
    )

    hook, evidence = _matching_hook_and_evidence(repo_dir, good_oid)
    returncode, payload = _run_launcher(trust_root_dir, hook, evidence)

    assert returncode != 0
    assert _decision(payload) == "deny"
    assert _reason_code(payload) == launcher.REASON_REPOSITORY_IDENTITY_MISMATCH


def test_runtime_identity_separation_denied_by_default(tmp_path: Path) -> None:
    """GIVEN the launcher runs WITHOUT the test-only same-uid bypass, WHEN
    the runtime process uid equals the trust root owner uid (always true in
    this single-user sandbox), THEN it denies with
    authorization_runtime_untrusted — proving the check is a real fail-closed
    gate and not a no-op (the genuine multi-uid ALLOW path requires real
    privilege separation unavailable here; see
    test_installer_success_path_requires_privileged_multi_uid)."""
    repo_dir = _init_candidate_repo(tmp_path)
    good_oid = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    trust_root_dir = _build_trust_root(
        tmp_path, good_oid, {COMPONENT_A: GOOD_CONTENT_A, COMPONENT_B: GOOD_CONTENT_B}
    )
    hook, evidence = _matching_hook_and_evidence(repo_dir, good_oid)

    returncode, payload = _run_launcher(trust_root_dir, hook, evidence, allow_same_uid=False)

    assert returncode != 0
    assert _decision(payload) == "deny"
    assert _reason_code(payload) == launcher.REASON_RUNTIME_UNTRUSTED


def test_deny_never_includes_updated_input(tmp_path: Path) -> None:
    """GIVEN any deny decision, WHEN the output is inspected, THEN
    hookSpecificOutput never contains an updatedInput key at all — a
    correctly-behaving hook harness that only acts on updatedInput when
    permissionDecision == allow can never accidentally execute the original
    (unvalidated) command on a deny path."""
    repo_dir = _init_candidate_repo(tmp_path)
    good_oid = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    trust_root_dir = _build_trust_root(
        tmp_path, good_oid, {COMPONENT_A: GOOD_CONTENT_A, COMPONENT_B: GOOD_CONTENT_B}
    )
    _write(repo_dir, COMPONENT_A, b"// TAMPERED for deny-shape check\n")
    tampered_oid = _commit_all(repo_dir, "tamper for deny-shape check")

    hook, evidence = _matching_hook_and_evidence(repo_dir, tampered_oid)
    returncode, payload = _run_launcher(trust_root_dir, hook, evidence)

    assert returncode != 0
    assert _decision(payload) == "deny"
    assert "updatedInput" not in payload["hookSpecificOutput"]


def test_allow_pushes_to_real_bare_remote(tmp_path: Path) -> None:
    """GIVEN a real temporary bare Git remote, WHEN the launcher's allow
    decision command is actually executed as a subprocess, THEN the ref is
    genuinely updated on that remote (end-to-end, not a mocked assertion on
    argv shape alone)."""
    repo_dir = _init_candidate_repo(tmp_path)
    good_oid = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    trust_root_dir = _build_trust_root(
        tmp_path, good_oid, {COMPONENT_A: GOOD_CONTENT_A, COMPONENT_B: GOOD_CONTENT_B}
    )

    bare_remote_dir = tmp_path / "bare-remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare_remote_dir)], check=True, timeout=20)
    _git(repo_dir, "remote", "set-url", "origin", str(bare_remote_dir))

    ref = DEFAULT_TEST_REF
    hook, evidence = _matching_hook_and_evidence(repo_dir, good_oid, ref=ref)
    # This test verifies real push/lease mechanics against a local bare
    # remote (no network access to github.com in this sandbox); the
    # repository-identity check itself is independently and unconditionally
    # covered by test_repository_identity_mismatch_denied.
    returncode, payload = _run_launcher(
        trust_root_dir, hook, evidence, extra_env={"LOOP_TRUST_ROOT_TEST_SKIP_REPOSITORY_IDENTITY": "1"}
    )
    assert returncode == 0
    assert _decision(payload) == "allow"

    command = _updated_command_str(payload)
    import shlex

    push_result = subprocess.run(shlex.split(command), cwd=repo_dir, capture_output=True, text=True, timeout=20)
    assert push_result.returncode == 0, push_result.stderr

    remote_oid_after = subprocess.run(
        ["git", "--git-dir", str(bare_remote_dir), "rev-parse", ref],
        capture_output=True,
        text=True,
        timeout=20,
    ).stdout.strip()
    assert remote_oid_after == good_oid


def test_allow_command_rejected_on_stale_lease(tmp_path: Path) -> None:
    """GIVEN a real temporary bare Git remote whose ref has ALREADY moved
    past the `remote_oid` the evidence declared (a race), WHEN the
    launcher's allow decision command is actually executed, THEN the real
    `--force-with-lease` push is rejected by git itself (not merely by our
    own logic) — proving the lease pinning is genuinely enforced at push
    time, not just asserted in argv."""
    repo_dir = _init_candidate_repo(tmp_path)
    good_oid = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    trust_root_dir = _build_trust_root(
        tmp_path, good_oid, {COMPONENT_A: GOOD_CONTENT_A, COMPONENT_B: GOOD_CONTENT_B}
    )

    bare_remote_dir = tmp_path / "bare-remote-race.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare_remote_dir)], check=True, timeout=20)
    _git(repo_dir, "remote", "set-url", "origin", str(bare_remote_dir))

    ref = DEFAULT_TEST_REF
    # Push the INITIAL (good_oid) commit to the remote first, then add a NEW
    # local commit (still manifest-clean) that has not been pushed yet. The
    # evidence declares a stale_remote_oid that matches neither the real
    # current remote oid (good_oid) nor the about-to-be-pushed local oid —
    # so --force-with-lease must reject due to a genuine remote-state race,
    # not merely a no-op "everything up to date".
    _git(repo_dir, "push", str(bare_remote_dir), f"HEAD:{ref}")
    real_current_remote_oid = subprocess.run(
        ["git", "--git-dir", str(bare_remote_dir), "rev-parse", ref],
        capture_output=True,
        text=True,
        timeout=20,
    ).stdout.strip()
    assert real_current_remote_oid == good_oid

    _write(repo_dir, "docs/unrelated-followup.md", b"# unrelated follow-up change\n")
    new_local_oid = _commit_all(repo_dir, "unrelated follow-up change, not yet pushed")

    stale_remote_oid = "2" * 40
    assert stale_remote_oid != real_current_remote_oid

    hook, evidence = _matching_hook_and_evidence(repo_dir, new_local_oid, ref=ref, remote_oid=stale_remote_oid)
    returncode, payload = _run_launcher(
        trust_root_dir, hook, evidence, extra_env={"LOOP_TRUST_ROOT_TEST_SKIP_REPOSITORY_IDENTITY": "1"}
    )
    assert returncode == 0
    assert _decision(payload) == "allow"

    command = _updated_command_str(payload)
    import shlex

    push_result = subprocess.run(shlex.split(command), cwd=repo_dir, capture_output=True, text=True, timeout=20)
    assert push_result.returncode != 0
    assert "stale info" in push_result.stderr or "rejected" in push_result.stderr
