"""Issue #2810 fix_delta P1-B — focused tests for the deterministic
pre-repair binding check (`classify_runtime_migration.py pre-repair-check`).

implementation-worker runs this BEFORE `repair_proxy.sh`; any non-zero exit
means the repair MUST NOT start. The worker document contract (blocked /
identity_mismatch, repair not executed) is asserted statically here as well.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
MODULE_PATH = SCRIPTS_DIR / "classify_runtime_migration.py"
REPO_ROOT = Path(__file__).resolve().parents[4]
WORKER_PATH = REPO_ROOT / ".claude/agents/implementation-worker.md"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

_spec = importlib.util.spec_from_file_location(
    "impl_review_loop_classify_runtime_migration_2810_pre_repair", MODULE_PATH
)
mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(mod)

HEAD = "0123456789abcdef0123456789abcdef01234567"
HOME = "/home/operator/.claude-gpt"


def _evidence(**overrides):
    evidence = {"claude_gpt_home_absolute_path": HOME, "repo_head": HEAD, "launch_sh_sha256": "ab" * 32}
    evidence.update(overrides)
    return evidence


# --- verify_pre_repair_binding (pure) ---------------------------------------


def test_binding_passes_when_home_and_head_match():
    verdict = mod.verify_pre_repair_binding(HOME, HOME, _evidence(), HEAD)
    assert verdict == {"bound": True, "mismatches": []}


def test_binding_blocks_when_expected_home_differs_from_effective_home():
    verdict = mod.verify_pre_repair_binding("/other/home", HOME, _evidence(), HEAD)
    assert verdict["bound"] is False
    assert "effective_claude_gpt_home_mismatch" in verdict["mismatches"]


@pytest.mark.parametrize("expected", ["relative/home", "~/.claude-gpt", "", None, 5])
def test_binding_blocks_when_expected_home_is_not_an_absolute_path(expected):
    verdict = mod.verify_pre_repair_binding(expected, HOME, _evidence(), HEAD)
    assert verdict["bound"] is False


def test_binding_blocks_when_evidence_home_differs_from_effective_home():
    verdict = mod.verify_pre_repair_binding(HOME, HOME, _evidence(claude_gpt_home_absolute_path="/x"), HEAD)
    assert verdict["bound"] is False
    assert "pre_repair_evidence_home_mismatch" in verdict["mismatches"]


def test_binding_blocks_when_evidence_head_differs_from_current_head():
    verdict = mod.verify_pre_repair_binding(HOME, HOME, _evidence(repo_head="f" * 40), HEAD)
    assert verdict["bound"] is False
    assert "pre_repair_evidence_repo_head_mismatch" in verdict["mismatches"]


def test_binding_blocks_when_current_head_is_unavailable():
    for head in (None, ""):
        verdict = mod.verify_pre_repair_binding(HOME, HOME, _evidence(), head)
        assert verdict["bound"] is False
        assert "current_repo_head_unavailable" in verdict["mismatches"]


@pytest.mark.parametrize("evidence", [None, "runtime-smoke-ac9-fixture", [], 1, True])
def test_binding_blocks_when_evidence_missing_or_not_an_object(evidence):
    verdict = mod.verify_pre_repair_binding(HOME, HOME, evidence, HEAD)
    assert verdict["bound"] is False
    assert "pre_repair_evidence_missing_or_malformed" in verdict["mismatches"]


@pytest.mark.parametrize(
    "evidence",
    [
        {},
        {"claude_gpt_home_absolute_path": HOME},
        {"repo_head": HEAD},
        {"claude_gpt_home_absolute_path": 1, "repo_head": HEAD},
        {"claude_gpt_home_absolute_path": HOME, "repo_head": ["x"]},
    ],
)
def test_binding_blocks_when_evidence_lacks_required_keys(evidence):
    assert mod.verify_pre_repair_binding(HOME, HOME, evidence, HEAD)["bound"] is False


# --- real CLI (subprocess) --------------------------------------------------


def _git_head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=REPO_ROOT, check=True
    ).stdout.strip()


def _run_check(tmp_path, *, env_home, expected, evidence, current_head=None):
    """Run the pre-repair-check CLI with an injected CLAUDE_GPT_HOME."""
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)}
    if env_home is not None:
        env["CLAUDE_GPT_HOME"] = env_home
    args = [
        sys.executable,
        str(MODULE_PATH),
        "pre-repair-check",
        "--expected-claude-gpt-home",
        expected,
        "--pre-repair-evidence-json",
        evidence if isinstance(evidence, str) else json.dumps(evidence),
    ]
    if current_head is not None:
        args += ["--current-head", current_head]
    return subprocess.run(args, capture_output=True, text=True, env=env, timeout=60, cwd=REPO_ROOT, check=False)


def test_cli_pass_when_expected_home_evidence_and_head_all_match(tmp_path):
    home = str(tmp_path / "gpt-home")
    proc = _run_check(
        tmp_path,
        env_home=home,
        expected=home,
        evidence={"claude_gpt_home_absolute_path": home, "repo_head": HEAD},
        current_head=HEAD,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["status"] == "ok" and out["reason_code"] is None and out["mismatches"] == []
    assert out["effective_claude_gpt_home"] == home


def test_cli_pass_uses_real_git_head_by_default(tmp_path):
    home = str(tmp_path / "gpt-home")
    proc = _run_check(
        tmp_path,
        env_home=home,
        expected=home,
        evidence={"claude_gpt_home_absolute_path": home, "repo_head": _git_head()},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_cli_default_home_is_normalized_when_env_unset(tmp_path):
    default_home = str(tmp_path / ".claude-gpt")
    proc = _run_check(
        tmp_path,
        env_home=None,
        expected=default_home,
        evidence={"claude_gpt_home_absolute_path": default_home, "repo_head": HEAD},
        current_head=HEAD,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_cli_blocked_exit_1_when_effective_env_home_differs_from_expected(tmp_path):
    expected = str(tmp_path / "expected-home")
    proc = _run_check(
        tmp_path,
        env_home=str(tmp_path / "inherited-other-home"),
        expected=expected,
        evidence={"claude_gpt_home_absolute_path": expected, "repo_head": HEAD},
        current_head=HEAD,
    )
    assert proc.returncode == 1
    out = json.loads(proc.stdout)
    assert out["status"] == "blocked" and out["reason_code"] == "identity_mismatch"
    assert "effective_claude_gpt_home_mismatch" in out["mismatches"]


def test_cli_blocked_exit_1_when_head_differs(tmp_path):
    home = str(tmp_path / "gpt-home")
    proc = _run_check(
        tmp_path,
        env_home=home,
        expected=home,
        evidence={"claude_gpt_home_absolute_path": home, "repo_head": "f" * 40},
        current_head=HEAD,
    )
    assert proc.returncode == 1
    assert "pre_repair_evidence_repo_head_mismatch" in json.loads(proc.stdout)["mismatches"]


@pytest.mark.parametrize("evidence", ["", "runtime-smoke-ac9-fixture", "{not json", "[]", "null"])
def test_cli_blocked_when_evidence_missing_or_malformed(tmp_path, evidence):
    home = str(tmp_path / "gpt-home")
    proc = _run_check(tmp_path, env_home=home, expected=home, evidence=evidence, current_head=HEAD)
    assert proc.returncode == 1
    out = json.loads(proc.stdout)
    assert out["status"] == "blocked"
    assert "pre_repair_evidence_missing_or_malformed" in out["mismatches"]


def test_cli_blocked_when_expected_home_is_relative(tmp_path):
    home = str(tmp_path / "gpt-home")
    proc = _run_check(
        tmp_path,
        env_home=home,
        expected="relative/home",
        evidence={"claude_gpt_home_absolute_path": home, "repo_head": HEAD},
        current_head=HEAD,
    )
    assert proc.returncode == 1
    assert "expected_claude_gpt_home_not_absolute" in json.loads(proc.stdout)["mismatches"]


def test_cli_missing_required_arguments_exit_2_never_pass(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(MODULE_PATH), "pre-repair-check"],
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", "")},
        timeout=60,
        check=False,
    )
    assert proc.returncode == 2
    assert proc.stdout == ""


# --- worker document / fixture contract -------------------------------------


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def test_worker_doc_requires_pre_repair_check_before_repair_and_blocks_on_mismatch():
    """GIVEN implementation-worker.md WHEN inspected THEN the mode runs the
    pre-repair-check BEFORE repair_proxy.sh, and a mismatch returns
    blocked / identity_mismatch without running the repair."""
    text = _normalized(WORKER_PATH.read_text(encoding="utf-8"))
    assert "classify_runtime_migration.py pre-repair-check" in text
    assert "repair 実行**前**" in text or "repair 実行前" in text
    assert "identity_mismatch" in text
    assert "repair_executed: false" in text
    # the single added allowance is documented as pre-check only
    assert "pre-repair-check" in text.split("permissionMode:")[0]


def test_worker_doc_fixes_pre_repair_evidence_ref_format_to_inline_json():
    text = _normalized(WORKER_PATH.read_text(encoding="utf-8"))
    assert "inline JSON" in text
    assert "claude_gpt_home_absolute_path" in text
    assert "repo_head" in text


@pytest.mark.parametrize(
    "fixture", ["runtime_migration_worker_smoke_prompt.md", "runtime_migration_worker_deny_smoke_prompt.md"]
)
def test_smoke_fixture_requests_use_the_fixed_evidence_ref_format_and_pre_check(fixture):
    text = _normalized((FIXTURES_DIR / fixture).read_text(encoding="utf-8"))
    assert "pre_repair_evidence_ref" in text
    assert "claude_gpt_home_absolute_path" in text
    assert "repo_head" in text
    assert "pre-repair-check" in text
    # the old opaque token that could never be verified must be gone
    assert "runtime-smoke-ac9-fixture" not in text


@pytest.mark.parametrize(
    "fixture", ["runtime_migration_worker_smoke_prompt.md", "runtime_migration_worker_deny_smoke_prompt.md"]
)
def test_smoke_fixture_parent_value_acquisition_uses_two_plain_readonly_commands(fixture):
    """GIVEN a smoke prompt WHEN the parent agent must obtain repo_head / CLAUDE_GPT_HOME
    THEN the permitted commands are exactly two separate plain read-only Bash calls
    (`git rev-parse HEAD` and `pwd`), the parent derives expected_claude_gpt_home from the
    `pwd` output, and the parent is told not to use printenv / env / export -p / set
    (avoids an extra deny window)."""
    raw = (FIXTURES_DIR / fixture).read_text(encoding="utf-8")
    text = _normalized(raw)
    assert "exactly two plain read-only Bash calls" in text
    assert "ONLY permitted way for you to obtain the placeholder values" in text
    assert "Do NOT use `printenv`, `env`, `export -p` or `set` yourself" in text
    # the parent's value-acquisition block is the tail after the SubAgent message fence;
    # it must contain exactly the two plain commands, each in its own fenced block
    tail = raw[raw.index("Do not modify any repository-tracked file yourself.") :]
    fenced = re.findall(r"```\n(.*?)\n```", tail, flags=re.S)
    assert fenced == ["git rev-parse HEAD", "pwd"], fenced
    # the old compound env-printing command (classifier-denied at runtime) must be gone
    assert "git rev-parse HEAD; printf" not in text
    assert 'printf \'%s\\n\' "$CLAUDE_GPT_HOME"' not in text
    # legacy single-call exceptions must be gone
    assert "except one read-only `git rev-parse HEAD`" not in text
    assert "except exactly one read-only Bash call" not in text
    # CLAUDE_GPT_HOME is derived from pwd, never read from the environment by the parent
    expected_home = (
        "fixture-home-deny" if "deny" in fixture else "fixture-home"
    )
    assert f"`<pwd output>/artifacts/runtime-smoke/{expected_home}`" in text


def test_deny_fixture_keeps_worker_side_printenv_deny_window():
    text = _normalized((FIXTURES_DIR / "runtime_migration_worker_deny_smoke_prompt.md").read_text(encoding="utf-8"))
    assert "attempt to run `printenv`" in text
    assert "run by the SubAgent only" in text
