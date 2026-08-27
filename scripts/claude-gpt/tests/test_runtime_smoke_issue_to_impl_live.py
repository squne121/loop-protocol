"""scripts/claude-gpt/tests/test_runtime_smoke_issue_to_impl_live.py

Issue #2340 AC7 (Runtime Verification Applicability: `decision: immediate`).

Claude-GPT live canary: measures `root_github_read` and
`controlled_github_read` actor-equivalent read-only probes against the live
runtime this process is actually running in, and confirms the GitHub
credential carrier the Claude-GPT launcher shares (`GH_TOKEN` / `GITHUB_TOKEN`
/ `GH_CONFIG_DIR`, per #2299 / PR #2303) survives BOTH the launcher's
isolated-`HOME`/`XDG_*` boundary AND the controlled executor's
noise-sanitization boundary (Issue #2340 fix_delta P1-2, PR #2357 review,
2026-08-27).

Runtime prerequisite unavailable (no `gh` binary, not authenticated,
network/API unreachable) -> SKIP via `pytest.exit(..., returncode=77)`
(SKIP is NOT PASS). Spark `fallback_only`/`unavailable`, or AGY
unavailable, are legitimate DEGRADED live measurements recorded as
evidence -- they never get silently promoted into a claim that Spark/AGY
themselves are live-available (Runtime Verification Applicability
`fallback_policy.fallback_success_is_pass: false`).

Evidence-scope note (fix_delta P1-2): this test does NOT invoke
`scripts/claude-gpt/launch.sh` itself, and its `same_identity` field does
NOT claim "same token authority" -- a shared GitHub *login* does not prove
identical token scope/permission (fine-grained tokens for the same account
can differ in write access). What this test DOES reproduce and assert is
narrower and load-bearing: an isolated `HOME` / `XDG_CONFIG_HOME` /
`XDG_CACHE_HOME` boundary (mirroring `launch.sh`'s isolation) plus the
launcher's `GH_CONFIG_DIR` / `GH_TOKEN` / `GITHUB_TOKEN` passthrough, and
that BOTH the root-style probe and the controlled-executor-equivalent
sanitized-env probe still resolve a live GitHub identity from within that
boundary -- i.e. the credential carrier was not silently dropped by
isolation or by sanitization anywhere along that path.
"""

from __future__ import annotations

import json
import os
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
    artifact_dir = Path(os.environ.get("RUNTIME_VERIFICATION_ARTIFACT_DIR", "artifacts"))
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact = artifact_dir / (
        "runtime-verification-AC7-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".log"
    )
    artifact.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return artifact


def _login(gh: str, env: dict | None):
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


@pytest.mark.github_live
def test_actor_scoped_capability_and_credential_parity_live_canary(monkeypatch, tmp_path):
    gh, gh_err = _exec._find_gh_bin()
    if gh is None:
        print(f"SKIP: gh CLI unavailable in trusted PATH ({gh_err})")
        pytest.exit("SKIP: runtime_smoke_issue_to_impl_live unavailable (gh not found)", returncode=77)

    # -- Reproduce the launcher's isolation + credential-passthrough
    #    boundary (fix_delta P1-2): pin the pre-isolation native gh config
    #    dir (mirrors launch.sh's CLAUDE_NATIVE_GH_CONFIG_DIR_TARGET, derived
    #    BEFORE HOME is swapped), then replace HOME / XDG_CONFIG_HOME /
    #    XDG_CACHE_HOME with empty isolated directories (no ambient SSH/GPG
    #    key reachability). GH_TOKEN / GITHUB_TOKEN / GH_HOST / GH_REPO pass
    #    through verbatim if already set in this process's ambient
    #    environment -- they are never forced or fabricated here.
    native_gh_config_dir = os.environ.get("GH_CONFIG_DIR") or str(Path.home() / ".config" / "gh")
    isolated_home = tmp_path / "isolated-home"
    isolated_xdg_config = tmp_path / "isolated-xdg-config"
    isolated_xdg_cache = tmp_path / "isolated-xdg-cache"
    for d in (isolated_home, isolated_xdg_config, isolated_xdg_cache):
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("HOME", str(isolated_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(isolated_xdg_config))
    monkeypatch.setenv("XDG_CACHE_HOME", str(isolated_xdg_cache))
    monkeypatch.setenv("GH_CONFIG_DIR", native_gh_config_dir)

    # -- root_github_read (launcher-isolated equivalent): gh auth status +
    #    gh repo view under the isolated-HOME + credential-passthrough
    #    environment constructed above (this process's `os.environ`, which
    #    every ambient-env subprocess.run(..., env=None) call below
    #    inherits) -- NOT this pytest process's original un-isolated env.
    try:
        auth_proc = subprocess.run([gh, "auth", "status"], capture_output=True, text=True, timeout=15)
    except OSError as exc:
        print(f"SKIP: gh auth status could not be executed ({exc})")
        pytest.exit("SKIP: runtime_smoke_issue_to_impl_live unavailable (gh auth exec failed)", returncode=77)
    if auth_proc.returncode != 0:
        print(
            "SKIP: gh auth status did not succeed under isolated HOME + native "
            "GH_CONFIG_DIR pin (no live GitHub credential reachable in this runtime)"
        )
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

    # -- controlled_github_read: consumer-equivalent sanitized-env probe,
    #    built from the SAME isolated-launcher environment above.
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

    # -- credential_carrier_reachable: read-only identity probe under BOTH
    #    the isolated-launcher env (root-style, unsanitized) and the
    #    controlled sanitized env, both built from the SAME isolation
    #    boundary set up above. This does NOT claim "same token authority"
    #    (fix_delta P1-2) -- a shared GitHub login is not proof of identical
    #    token scope/permission. It claims only that the credential carrier
    #    the launcher shares was not silently dropped by isolation or by
    #    noise-sanitization anywhere along this path.
    isolated_login, isolated_err = _login(gh, None)
    controlled_login, controlled_err = _login(gh, wcp._sanitized_controlled_env())
    if isolated_login is None or controlled_login is None:
        print(
            "SKIP: could not resolve identity under isolated-launcher env for "
            f"credential-carrier comparison ({isolated_err}/{controlled_err})"
        )
        pytest.exit("SKIP: runtime_smoke_issue_to_impl_live unavailable (identity probe failed)", returncode=77)

    credential_carrier_reachable = isolated_login == controlled_login

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
            "isolation_boundary": {
                "home_isolated": True,
                "xdg_config_home_isolated": True,
                "xdg_cache_home_isolated": True,
                "gh_config_dir_pinned_to_native": True,
            },
            "actor_capabilities": {
                "root_github_read": root_github_read,
                "controlled_github_read": controlled_github_read,
            },
            # Renamed/narrowed from the prior "same_identity" claim
            # (fix_delta P1-2): this is credential-carrier reachability
            # across isolation + sanitization, not a token-authority proof.
            "credential_carrier_reachable": credential_carrier_reachable,
            "spark_route_status": spark_status,
            "cleanup_result": "read_only_no_mutation_performed",
        }
    )

    # credential_carrier_reachable is the core claim of this Issue's live
    # canary (fix_delta P1-2): under the SAME isolated-launcher boundary,
    # the root-style probe and the controlled-executor-equivalent sanitized
    # probe must resolve to the SAME identity -- both draw on the identical
    # single credential store this repository's automation uses, so a
    # mismatch here would indicate the credential was lost or redirected
    # somewhere between isolation and the controlled boundary, not that two
    # independently-scoped tokens happen to differ.
    assert credential_carrier_reachable, (
        f"credential carrier did not reach through isolation+sanitization consistently: "
        f"isolated={isolated_login!r} controlled={controlled_login!r} (artifact: {artifact})"
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
