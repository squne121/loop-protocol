"""scripts/claude-gpt/tests/test_runtime_smoke_issue_to_impl_live.py

Issue #2340 AC7 (Runtime Verification Applicability: `decision: immediate`).

Claude-GPT live canary: measures `root_github_read` and
`controlled_github_read` actor-equivalent read-only probes, the AC1-fixed
`same_identity: true/false` (ambient `gh api user` vs the controlled
executor's sanitized-env `gh api user`), and Spark route status, against
the live runtime this process is actually running in.

Runtime prerequisite unavailable (no `gh` binary, not authenticated,
network/API unreachable) -> SKIP via `pytest.exit(..., returncode=77)`
(SKIP is NOT PASS). Spark `fallback_only`/`unavailable`, or AGY
unavailable, are legitimate DEGRADED live measurements recorded as
evidence -- they never get silently promoted into a claim that Spark/AGY
themselves are live-available (Runtime Verification Applicability
`fallback_policy.fallback_success_is_pass: false`).
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _TESTS_DIR.parent
_REPO_ROOT = _SCRIPTS_DIR.parent.parent
_GUARDS_DIR = _REPO_ROOT / "scripts" / "agent-guards"

for _p in (_SCRIPTS_DIR, _GUARDS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import workflow_capability_preflight as wcp  # noqa: E402
import controlled_skill_mutation_exec as _exec  # noqa: E402

_REPO = "squne121/loop-protocol"


def _write_artifact(payload: dict) -> Path:
    import os

    artifact_dir = Path(os.environ.get("RUNTIME_VERIFICATION_ARTIFACT_DIR", "artifacts"))
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact = artifact_dir / (
        "runtime-verification-AC7-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".log"
    )
    artifact.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return artifact


@pytest.mark.github_live
def test_actor_scoped_capability_and_credential_parity_live_canary():
    gh, gh_err = _exec._find_gh_bin()
    if gh is None:
        print(f"SKIP: gh CLI unavailable in trusted PATH ({gh_err})")
        pytest.exit("SKIP: runtime_smoke_issue_to_impl_live unavailable (gh not found)", returncode=77)

    # -- root_github_read: ambient-env `gh auth status` + `gh repo view` -----
    try:
        auth_proc = subprocess.run([gh, "auth", "status"], capture_output=True, text=True, timeout=15)
    except OSError as exc:
        print(f"SKIP: gh auth status could not be executed ({exc})")
        pytest.exit("SKIP: runtime_smoke_issue_to_impl_live unavailable (gh auth exec failed)", returncode=77)
    if auth_proc.returncode != 0:
        print("SKIP: gh auth status did not succeed (no live GitHub credential in this runtime)")
        pytest.exit("SKIP: runtime_smoke_issue_to_impl_live unavailable (gh auth unavailable)", returncode=77)

    root_repo_read_ok = wcp._github_repo_read_ok(_REPO)
    root_github_read = {
        "status": "ready" if root_repo_read_ok else "unavailable",
        "reason_code": None if root_repo_read_ok else "root_github_repo_read_failed",
        "probe_execution_class": "root_shell_gh_repo_view",
    }
    if not root_repo_read_ok:
        print("SKIP: root gh repo view read failed in this runtime (no live repo access)")
        pytest.exit("SKIP: runtime_smoke_issue_to_impl_live unavailable (root repo read failed)", returncode=77)

    # -- controlled_github_read: consumer-equivalent sanitized-env probe -----
    controlled_github_read = wcp._controlled_github_read_capability(_REPO)
    if controlled_github_read["status"] != "ready":
        # This IS the exact class of failure Issue #2340 exists to catch --
        # do not silently skip it. A genuinely broken runtime (rather than a
        # credential-parity regression) would already have failed the
        # root_github_read gate above.
        pytest.fail(
            "controlled_github_read probe failed live while root_github_read succeeded -- "
            f"this is the credential-context divergence Issue #2340 fixes: {controlled_github_read}"
        )

    # -- same_identity: ambient `gh api user` vs controlled sanitized env ----
    def _login(env):
        try:
            proc = subprocess.run(
                [gh, "api", "--hostname", "github.com", "user", "--jq", "{login: .login}"],
                capture_output=True,
                text=True,
                timeout=15,
                env=env,
            )
        except OSError:
            return None, "exec_failed"
        if proc.returncode != 0:
            return None, f"exit_{proc.returncode}"
        try:
            return json.loads(proc.stdout).get("login"), ""
        except (json.JSONDecodeError, ValueError):
            return None, "non_json_stdout"

    ambient_login, ambient_err = _login(None)
    controlled_login, controlled_err = _login(wcp._sanitized_controlled_env())
    if ambient_login is None or controlled_login is None:
        print(f"SKIP: could not resolve identity for same_identity comparison ({ambient_err}/{controlled_err})")
        pytest.exit("SKIP: runtime_smoke_issue_to_impl_live unavailable (identity probe failed)", returncode=77)

    same_identity = ambient_login == controlled_login

    # -- Spark route status (lazy, advisory measurement -- never promoted to
    # a claim that Spark itself is live-available; recorded as evidence
    # only, per Runtime Verification Applicability fallback_policy). -------
    env_only_result = wcp._run_env_only_preflight()
    spark_status = wcp._spark_capability("preferred", "allowed", env_only_result)

    artifact = _write_artifact(
        {
            "ac": "AC7",
            "command": "pytest -m github_live -k actor_scoped_capability_and_credential_parity_live_canary",
            "run_head_sha": _current_head_sha(),
            "launcher_identity": "test_runtime_smoke_issue_to_impl_live",
            "actor_capabilities": {
                "root_github_read": root_github_read,
                "controlled_github_read": controlled_github_read,
            },
            "same_identity": same_identity,
            "spark_route_status": spark_status,
            "cleanup_result": "read_only_no_mutation_performed",
        }
    )

    # same_identity is the core credential-parity claim of this Issue: the
    # AC1 fix means read (ambient) and the controlled write path's sanitized
    # env must resolve to the SAME authenticated GitHub identity.
    assert same_identity, (
        f"read/write identity mismatch: ambient={ambient_login!r} controlled={controlled_login!r} "
        f"(artifact: {artifact})"
    )
    # Spark fallback_only/unavailable is a legitimate live measurement, not a
    # test failure -- only assert the status is one of the known values so a
    # malformed/None result still fails closed.
    assert spark_status in (
        wcp.SPARK_NOT_REQUIRED,
        wcp.SPARK_ELIGIBLE,
        wcp.SPARK_FALLBACK_ONLY,
        wcp.SPARK_UNAVAILABLE,
    )


def _current_head_sha() -> str | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10, cwd=str(_REPO_ROOT)
        )
    except OSError:
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None
