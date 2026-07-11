"""Real-subprocess integration tests for scripts/trust-root/trusted_hook_launcher.py.

These tests build a temporary candidate Git worktree, a temporary external
"trust root" directory (manifest + active.json fixtures), and invoke the
launcher as a real subprocess (not a mocked function call) so that the
tests exercise actual process spawning, actual `git` subprocess invocation,
and actual filesystem permission checks — matching the Issue #1454 Runtime
Verification Applicability contract (`decision: immediate`).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
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


def _init_candidate_repo(tmp_path: Path) -> Path:
    repo_dir = tmp_path / "candidate"
    repo_dir.mkdir(parents=True)
    _git(repo_dir, "init", "-q", "-b", "main")
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
) -> Path:
    """Manually construct a trust root fixture (bypassing install_trust_root.sh,
    which requires a euid separation this single-user test sandbox cannot
    provide — see test_installer_same_euid_rejected for that script's own
    dedicated coverage)."""
    trust_root_dir = tmp_path / "trust-root"
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
    active_path.chmod(0o400)
    (trust_root_dir / "releases").chmod(0o500)
    trust_root_dir.chmod(0o500)
    return trust_root_dir


def _evidence(
    candidate_repo_dir: Path,
    local_oid: str,
    ref: str = "refs/heads/main",
    remote_oid: str | None = None,
) -> dict:
    return {
        "session_id": "sess-1",
        "turn_id": "turn-1",
        "tool_use_id": "tool-1",
        "local_oid": local_oid,
        "remote_oid": remote_oid or ("0" * 40),
        "ref": ref,
        "issue_number": 1454,
        "nonce": "nonce-1",
        "expiry": "2999-01-01T00:00:00Z",
        "candidate_repo_dir": str(candidate_repo_dir),
    }


def _run_launcher(trust_root_dir: Path, evidence: dict, extra_env: dict[str, str] | None = None) -> tuple[int, dict]:
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        [sys.executable, str(_LAUNCHER_PATH), "--trust-root-dir", str(trust_root_dir)],
        input=json.dumps(evidence),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    return result.returncode, payload


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

    returncode, payload = _run_launcher(trust_root_dir, _evidence(repo_dir, tampered_oid))

    assert returncode != 0
    assert payload["decision"] == "deny"
    assert payload["reason_code"] == launcher.REASON_COMPONENT_DIGEST_MISMATCH


# ─── AC4: launcher denies independent of any candidate-side integrity check ─


def test_integrity_check_removed_still_denied(tmp_path: Path) -> None:
    """GIVEN a candidate repository that has entirely REMOVED any local
    integrity-check invocation file (simulating an attacker stripping out
    candidate-side verification calls), WHEN the external launcher runs,
    THEN it still denies a tampered component — proving the authorization
    decision never depends on candidate-side code execution
    (integrity_check_removed_still_denied)."""
    repo_dir = _init_candidate_repo(tmp_path)
    # Simulate a candidate-side "verifier" file that is deleted entirely.
    _write(repo_dir, "scripts/agent-guards/local_integrity_verifier.py", b"# calls hash check\n")
    good_oid = _commit_all(repo_dir, "add candidate-side verifier stub")

    trust_root_dir = _build_trust_root(
        tmp_path, good_oid, {COMPONENT_A: GOOD_CONTENT_A, COMPONENT_B: GOOD_CONTENT_B}
    )

    # Attacker deletes the candidate-side verifier AND tampers a component.
    (repo_dir / "scripts/agent-guards/local_integrity_verifier.py").unlink()
    _write(repo_dir, COMPONENT_B, b"# TAMPERED policy\nTRUSTED = False\n")
    tampered_oid = _commit_all(repo_dir, "remove verifier and tamper component B")

    returncode, payload = _run_launcher(trust_root_dir, _evidence(repo_dir, tampered_oid))

    assert returncode != 0
    assert payload["decision"] == "deny"
    assert payload["reason_code"] == launcher.REASON_COMPONENT_DIGEST_MISMATCH


# ─── AC5: on full match, trusted copy path is used, not candidate path ──────


def test_trusted_copy_executed_not_candidate(tmp_path: Path) -> None:
    """GIVEN all components matching the manifest, WHEN the launcher
    authorizes, THEN the resulting updatedInput.command references only the
    fixed absolute trusted git binary and Git objects (oid/ref) — never any
    path inside the candidate repository (trusted_copy_executed_not_candidate)."""
    repo_dir = _init_candidate_repo(tmp_path)
    good_oid = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    trust_root_dir = _build_trust_root(
        tmp_path, good_oid, {COMPONENT_A: GOOD_CONTENT_A, COMPONENT_B: GOOD_CONTENT_B}
    )

    returncode, payload = _run_launcher(trust_root_dir, _evidence(repo_dir, good_oid))

    assert returncode == 0
    assert payload["decision"] == "allow"
    command = payload["updatedInput"]["command"]
    assert Path(command[0]).is_absolute()
    assert str(repo_dir) not in " ".join(command)
    assert command[1] == "push"
    assert command[3] == "origin"


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

    returncode, payload = _run_launcher(trust_root_dir, _evidence(repo_dir, symlink_oid))
    assert returncode != 0
    assert payload["reason_code"] == launcher.REASON_COMPONENT_TYPE_INVALID

    # Missing case: component B removed entirely.
    repo_dir2 = _init_candidate_repo(tmp_path / "missing-case")
    good_oid2 = _git(repo_dir2, "rev-parse", "HEAD").stdout.strip()
    trust_root_dir2 = _build_trust_root(
        tmp_path / "missing-case", good_oid2, {COMPONENT_A: GOOD_CONTENT_A, COMPONENT_B: GOOD_CONTENT_B}
    )
    (repo_dir2 / COMPONENT_B).unlink()
    missing_oid = _commit_all(repo_dir2, "remove component B")

    returncode2, payload2 = _run_launcher(trust_root_dir2, _evidence(repo_dir2, missing_oid))
    assert returncode2 != 0
    assert payload2["reason_code"] == launcher.REASON_COMPONENT_MISSING

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
    returncode3, payload3 = _run_launcher(trust_root_dir3, _evidence(repo_dir3, good_oid3))
    assert returncode3 != 0
    assert payload3["reason_code"] == launcher.REASON_MANIFEST_INVALID


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

    # Reset the branch back to the good commit; local_oid passed explicitly
    # is the good commit regardless of origin/main state.
    returncode, payload = _run_launcher(trust_root_dir, _evidence(repo_dir, good_oid))

    assert returncode == 0
    assert payload["decision"] == "allow"


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

    returncode, payload = _run_launcher(trust_root_dir, _evidence(repo_dir, good_oid))

    assert returncode == 0
    assert payload["decision"] == "allow"


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
    returncode, payload = _run_launcher(
        trust_root_dir, _evidence(repo_dir, good_oid), extra_env={"PATH": poisoned_path}
    )

    assert returncode == 0
    assert payload["decision"] == "allow"


# ─── AC11: trust root missing is fail-closed, never generic allow ──────────


def test_trust_root_missing_denied(tmp_path: Path) -> None:
    """GIVEN a trust root directory that does not exist (feature not yet
    installed by a privileged operator), WHEN the launcher runs, THEN it
    denies with authorization_trust_root_missing rather than generically
    allowing (trust_root_missing_denied)."""
    repo_dir = _init_candidate_repo(tmp_path)
    good_oid = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    nonexistent_trust_root = tmp_path / "does-not-exist"

    returncode, payload = _run_launcher(nonexistent_trust_root, _evidence(repo_dir, good_oid))

    assert returncode != 0
    assert payload["decision"] == "deny"
    assert payload["reason_code"] == launcher.REASON_TRUST_ROOT_MISSING


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
    returncode, payload = _run_launcher(trust_root_dir2, _evidence(repo_dir, tampered_oid))

    assert returncode == 0
    assert payload["decision"] == "allow"


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

    returncode, payload = _run_launcher(trust_root_dir, _evidence(repo_dir, malicious_oid))

    assert returncode != 0
    assert payload["decision"] == "deny"
    assert payload["reason_code"] == launcher.REASON_CANDIDATE_COMMIT_COMPONENT_MISMATCH


# ─── AC15: trusted publisher command rewrite (allow path) ──────────────────


def test_trusted_publisher_command_rewrite(tmp_path: Path) -> None:
    """GIVEN all components match, WHEN the launcher authorizes, THEN it
    replaces the command with a FIXED, absolute-path, `--force-with-lease`
    pinned publisher argv — regardless of a poisoned PATH (fake `rtk`) or
    HEAD having moved to a different branch since evidence capture
    (trusted_publisher_command_rewrite)."""
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

    ref = "refs/heads/main"
    remote_oid = "1" * 40
    evidence = _evidence(repo_dir, good_oid, ref=ref, remote_oid=remote_oid)
    returncode, payload = _run_launcher(trust_root_dir, evidence, extra_env={"PATH": poisoned_path})

    assert returncode == 0
    assert payload["decision"] == "allow"
    command = payload["updatedInput"]["command"]
    assert command[0] not in (str(fake_rtk),)
    assert Path(command[0]).is_absolute()
    assert command[1] == "push"
    assert command[2] == f"--force-with-lease={ref}:{remote_oid}"
    assert command[3] == "origin"
    assert command[4] == f"{good_oid}:{ref}"


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

    returncode, payload = _run_launcher(trust_root_dir, _evidence(repo_dir, good_oid))

    assert returncode != 0
    assert payload["decision"] == "deny"
    assert payload["reason_code"] == launcher.REASON_CANDIDATE_TREE_AMBIGUOUS
