"""Issue #2839: runtime VC approval carrier の hermetic 契約 test.

この test は fake ``claude`` と一時 git repository の stand-in fixture だけで動く。
#2810 の実 fixture (``fake_proxy_installer.sh``) や実 Auto mode classifier には依存しない。
carrier の実 classifier に対する効果は、この test では検証しない (#2810 の AC9/AC10 で確認する)。
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
AGENT_OPS = REPO_ROOT / "scripts" / "agent-ops"
RUNNER = AGENT_OPS / "run_worktree_agent_runtime_smoke.py"
CONTRACT = AGENT_OPS / "runtime_vc_approval_contract.py"
POLICY_DOC = REPO_ROOT / "docs" / "dev" / "runtime-verification-policy.md"
PROFILE_ID = "repair_proxy_hermetic_fixture"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def contract():
    return _load("runtime_vc_approval_contract_under_test_2839", CONTRACT)


@pytest.fixture(scope="module")
def runner():
    return _load("runner_under_test_2839", RUNNER)


# ---------------------------------------------------------------------------
# helpers: 一時 git repository と stand-in fixture
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, env=env)


def _standin_repo(tmp_path: Path, contract, *, with_fixture: bool = True) -> tuple[Path, Path]:
    """一時 repository と worktree を作り、registry の fixture path に stand-in を commit する。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("remote", "add", "origin", "https://github.com/squne121/loop-protocol.git", cwd=repo)
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-m", "seed", cwd=repo)
    worktree = repo / ".claude" / "worktrees" / "issue-0000-fixture"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git("branch", "worktree-fixture", cwd=repo)
    _git("worktree", "add", str(worktree), "worktree-fixture", cwd=repo)
    if with_fixture:
        rel = contract.get_approval_profile(PROFILE_ID).fixture_relpath
        path = worktree / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\necho stand-in fixture\n", encoding="utf-8")
        _git("add", rel, cwd=worktree)
        _git("commit", "-m", "standin fixture", cwd=worktree)
    return repo, worktree


def _good_env(contract, worktree: Path, *, home_name: str = "fixture-home") -> dict[str, str]:
    wt = os.path.realpath(worktree)
    rel = contract.get_approval_profile(PROFILE_ID).fixture_relpath
    return {
        "CLAUDE_GPT_HOME": os.path.join(wt, "artifacts", "runtime-smoke", home_name),
        "CLAUDE_GPT_REPAIR_INSTALLER_URL": "file://" + os.path.join(wt, rel),
        "PATH": os.environ.get("PATH", ""),
    }


def _verify(contract, worktree: Path, env: dict[str, str], **overrides):
    kwargs = dict(worktree=str(worktree), env=env, claude_adapter="native", mode="structured",
                  incompatible_flags={})
    kwargs.update(overrides)
    return contract.verify_approval_carrier_preconditions(PROFILE_ID, **kwargs)


# ---------------------------------------------------------------------------
# AC1 / AC7: docs
# ---------------------------------------------------------------------------


def _policy_section_14() -> str:
    text = POLICY_DOC.read_text(encoding="utf-8")
    start = text.index("## 14. 承認が必要な runtime VC の approval carrier")
    end = text.index("## 関連ドキュメント", start)
    return text[start:end]


def test_ownership_decision_recorded():
    section = _policy_section_14()
    assert "closed enum" in section and "`--approval-profile`" in section
    assert "runtime_vc_approval_contract.py" in section
    for rejected in ("consumer wrapper", "caller 指定の `--settings`", "恒久変更"):
        assert rejected in section, rejected
    assert "**採用**" in section and section.count("**不採用") >= 3


REQUIRED_NO_INHERITANCE_SENTENCE = (
    "親 transcript および親 session の承認は、独立した子 `claude -p` session に継承されない。"
)
FORBIDDEN_INHERITANCE_PHRASES = (
    "親 transcript は子 session に継承される",
    "親 session の承認は子 session にも有効",
    "親の承認がそのまま届く",
    "承認は自動的に子 session に伝わる",
    "対話 session で承認済みなので子でも通る",
)


def test_no_parent_transcript_inheritance_assumption():
    text = POLICY_DOC.read_text(encoding="utf-8")
    assert REQUIRED_NO_INHERITANCE_SENTENCE in text
    for phrase in FORBIDDEN_INHERITANCE_PHRASES:
        assert phrase not in text, phrase


# ---------------------------------------------------------------------------
# AC2: overlay
# ---------------------------------------------------------------------------


def _base_overlay(runner) -> str:
    return runner._CLAUDE_SPAWN_HOOK_OBSERVABILITY_SETTINGS_JSON


def test_overlay_bounded_allow_single_rule(contract, runner):
    overlay = json.loads(contract.build_approval_overlay_json(PROFILE_ID, _base_overlay(runner)))
    assert set(overlay["autoMode"]) == {"allow"}
    allow = overlay["autoMode"]["allow"]
    assert len(allow) == 2 and allow[0] == "$defaults"
    rule = allow[1]
    profile = contract.get_approval_profile(PROFILE_ID)
    assert profile.repair_command in rule
    assert profile.fixture_relpath in rule
    assert "file://" in rule and "artifacts/runtime-smoke/" in rule
    assert "no network installer" in rule
    # 書き込み先の説明は実装に即する: 永続 install 先だけが fixture home 配下で、処理中の一時
    # file / directory の作成と削除を含む。実際の user 領域の claude-gpt home は変更しない。
    assert "persistent install destination" in rule
    assert "temporary files and directories" in rule
    assert "is never modified" in rule
    assert "writes only under" not in rule


def test_overlay_defaults_preserved(contract, runner):
    base = json.loads(_base_overlay(runner))
    overlay = json.loads(contract.build_approval_overlay_json(PROFILE_ID, _base_overlay(runner)))
    assert overlay["autoMode"]["allow"][0] == "$defaults"
    for key, value in base.items():
        assert overlay[key] == value, key


def test_overlay_no_broad_allow(contract, runner):
    overlay = json.loads(contract.build_approval_overlay_json(PROFILE_ID, _base_overlay(runner)))
    for key in ("soft_deny", "hard_deny", "environment"):
        assert key not in overlay["autoMode"]
    assert "allow" not in overlay.get("permissions", {})
    rule = overlay["autoMode"]["allow"][1]
    assert "*" not in rule and "Bash(" not in rule
    # 追加される allow は 1 件だけで、2 つ目以降の要素は無い。
    assert len(overlay["autoMode"]["allow"]) == 2


def test_negative_control_not_weakened(contract, runner):
    base = json.loads(_base_overlay(runner))
    overlay = json.loads(contract.build_approval_overlay_json(PROFILE_ID, _base_overlay(runner)))
    # carrier は permissions.deny と hooks を base のまま保持する (deny を緩める key を持たない)。
    assert overlay["permissions"] == base["permissions"]
    assert overlay["hooks"] == base["hooks"]
    # repository の deny list (契約外の環境変数一覧表示 command の拒否) は carrier の対象外のまま。
    project_settings = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    deny = project_settings["permissions"]["deny"]
    assert any(rule.startswith("Bash(printenv") for rule in deny)
    # carrier の allow rule は、契約外 command (環境変数の一覧表示) を許可する文言を持たない。
    assert "printenv" not in overlay["autoMode"]["allow"][1]


# ---------------------------------------------------------------------------
# AC3: generic passthrough 不在
# ---------------------------------------------------------------------------


def test_no_generic_passthrough_argparse_has_no_settings_flag(runner):
    options = [opt for action in runner.build_parser()._actions for opt in action.option_strings]
    for opt in options:
        lowered = opt.lower()
        assert "settings" not in lowered and "automode" not in lowered and "auto-mode" not in lowered, opt


def test_no_generic_passthrough_approval_profile_choices_equal_registry_keys(contract, runner):
    action = next(a for a in runner.build_parser()._actions if "--approval-profile" in a.option_strings)
    assert sorted(action.choices) == sorted(contract._APPROVAL_PROFILE_REGISTRY)
    assert tuple(action.choices) == contract.approval_profile_ids()


def test_no_generic_passthrough_overlay_builder_accepts_only_profile_id(contract, runner):
    params = list(inspect.signature(contract.build_approval_overlay_json).parameters)
    assert params == ["profile_id", "base_settings_json"]
    for bad in ({"allow": ["Bash(*)"]}, "not-a-profile", None, 3):
        with pytest.raises((ValueError, TypeError)):
            contract.build_approval_overlay_json(bad, _base_overlay(runner))
    with pytest.raises(TypeError):
        contract.build_approval_overlay_json(PROFILE_ID, {"autoMode": {"allow": ["Bash(*)"]}})


def test_no_generic_passthrough_unknown_profile_exits_2(tmp_path):
    result = subprocess.run(
        [sys.executable, str(RUNNER), "--runtime", "claude", "--mode", "structured",
         "--worktree", str(tmp_path), "--prompt-file", str(tmp_path / "p.md"),
         "--output-dir", str(tmp_path / "o"), "--approval-profile", "bogus"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 2
    assert "invalid choice" in result.stderr


# ---------------------------------------------------------------------------
# AC4: precondition (unit)
# ---------------------------------------------------------------------------


def test_precondition_fail_closed_positive_path_standin(contract, tmp_path):
    _, worktree = _standin_repo(tmp_path, contract)
    verified = _verify(contract, worktree, _good_env(contract, worktree))
    assert verified["ok"] is True, verified
    assert len(verified["fixture_git_blob_hash"]) == 40 and len(verified["repo_head"]) == 40
    assert verified["claude_gpt_home_relpath"] == "artifacts/runtime-smoke/fixture-home"


def test_precondition_fail_closed_real_profile_without_fixture(contract, tmp_path):
    _, worktree = _standin_repo(tmp_path, contract, with_fixture=False)
    verified = _verify(contract, worktree, _good_env(contract, worktree))
    assert verified == {"ok": False, "reason_code": "fixture_missing"}


@pytest.mark.parametrize("case", [
    "adapter_not_native", "mode_not_structured",
    "fixture_is_symlink", "fixture_outside_worktree", "fixture_not_tracked", "fixture_modified",
    "installer_url_mismatch", "installer_url_dotdot", "installer_url_percent_encoded",
    "claude_gpt_home_missing", "claude_gpt_home_escape", "claude_gpt_home_outside_allowed",
    "claude_gpt_home_symlink", "claude_gpt_home_parent_symlink",
    "override_env_present:CLAUDE_CODE_PROXY_INSTALL_DIR", "override_env_present:CLAUDE_CODE_PROXY_VERSION",
])
def test_precondition_fail_closed_cases(contract, tmp_path, case):
    _, worktree = _standin_repo(tmp_path, contract)
    wt = Path(os.path.realpath(worktree))
    rel = contract.get_approval_profile(PROFILE_ID).fixture_relpath
    fixture = wt / rel
    env = _good_env(contract, worktree)
    kwargs: dict = {}
    reason = case
    if case == "adapter_not_native":
        kwargs["claude_adapter"] = "claude-gpt"
    elif case == "mode_not_structured":
        kwargs["mode"] = "interactive"
    elif case == "fixture_is_symlink":
        real = tmp_path / "elsewhere.sh"
        real.write_text("#!/bin/sh\n", encoding="utf-8")
        fixture.unlink()
        fixture.symlink_to(real)
    elif case == "fixture_outside_worktree":
        outside = tmp_path / "outside_dir"
        shutil_target = fixture.parent
        outside.mkdir()
        (outside / fixture.name).write_text("#!/bin/sh\n", encoding="utf-8")
        for child in shutil_target.iterdir():
            child.unlink()
        shutil_target.rmdir()
        shutil_target.symlink_to(outside)
    elif case == "fixture_not_tracked":
        _git("rm", "--cached", "--", rel, cwd=wt)
    elif case == "fixture_modified":
        fixture.write_text("#!/bin/sh\necho tampered\n", encoding="utf-8")
    elif case == "installer_url_mismatch":
        env["CLAUDE_GPT_REPAIR_INSTALLER_URL"] = "https://example.invalid/install.sh"
        reason = "installer_url_mismatch"
    elif case == "installer_url_dotdot":
        env["CLAUDE_GPT_REPAIR_INSTALLER_URL"] = "file://" + str(wt / "x" / ".." / rel)
        reason = "installer_url_mismatch"
    elif case == "installer_url_percent_encoded":
        env["CLAUDE_GPT_REPAIR_INSTALLER_URL"] = "file://" + str(wt / rel).replace("/", "%2F")
        reason = "installer_url_mismatch"
    elif case == "claude_gpt_home_missing":
        del env["CLAUDE_GPT_HOME"]
    elif case == "claude_gpt_home_escape":
        env["CLAUDE_GPT_HOME"] = str(wt / "artifacts" / "runtime-smoke" / ".." / ".." / "escape")
    elif case == "claude_gpt_home_outside_allowed":
        env["CLAUDE_GPT_HOME"] = str(wt / "somewhere-else")
    elif case == "claude_gpt_home_symlink":
        parent = wt / "artifacts" / "runtime-smoke"
        parent.mkdir(parents=True)
        target = tmp_path / "real-home"
        target.mkdir()
        (parent / "linked-home").symlink_to(target)
        env["CLAUDE_GPT_HOME"] = str(parent / "linked-home")
    elif case == "claude_gpt_home_parent_symlink":
        (wt / "artifacts").mkdir()
        target = tmp_path / "real-smoke"
        target.mkdir()
        (wt / "artifacts" / "runtime-smoke").symlink_to(target)
    elif case.startswith("override_env_present:"):
        env[case.split(":", 1)[1]] = "x"
    verified = _verify(contract, worktree, env, **kwargs)
    assert verified["ok"] is False, (case, verified)
    assert verified["reason_code"] == reason, (case, verified)


@pytest.mark.parametrize("flag", ["expect_skill_command", "hermetic_agent_definition"])
def test_precondition_fail_closed_incompatible_flag(contract, tmp_path, flag):
    _, worktree = _standin_repo(tmp_path, contract)
    verified = _verify(contract, worktree, _good_env(contract, worktree), incompatible_flags={flag: True})
    assert verified == {"ok": False, "reason_code": f"incompatible_flag:{flag}"}


def test_precondition_fail_closed_incompatible_flag_list_is_closed(contract):
    # --require-hook-chain-evidence は PR #2844 の OWNER review で併用可へ訂正した (固定 overlay の
    # 同一 object に autoMode を足す)。overlay の派生 variant / hermetic settings は fail-closed のまま。
    assert contract.INCOMPATIBLE_RUNNER_FLAGS == ("expect_skill_command", "hermetic_agent_definition")


def test_precondition_fail_closed_hook_chain_evidence_flag_is_compatible(contract, tmp_path):
    _, worktree = _standin_repo(tmp_path, contract)
    verified = _verify(contract, worktree, _good_env(contract, worktree),
                       incompatible_flags={"require_hook_chain_evidence": True})
    assert verified["ok"] is True, verified


def test_precondition_fail_closed_child_env_exact(contract, tmp_path):
    _, worktree = _standin_repo(tmp_path, contract)
    env = _good_env(contract, worktree)
    verified = _verify(contract, worktree, env)
    child = contract.build_approval_child_env({**env, "KEEP_ME": "1"}, verified)
    assert child["CLAUDE_GPT_HOME"] == env["CLAUDE_GPT_HOME"]
    assert child["CLAUDE_GPT_REPAIR_INSTALLER_URL"] == env["CLAUDE_GPT_REPAIR_INSTALLER_URL"]
    assert child["KEEP_ME"] == "1"
    for var in contract.OVERRIDE_ENV_VARS:
        assert var not in child
    with pytest.raises(ValueError):
        contract.build_approval_child_env(env, {"ok": False})


# ---------------------------------------------------------------------------
# AC4 / AC5: runner 全体 (fake claude)
# ---------------------------------------------------------------------------


def _write_fake_claude(path: Path, argv_marker: Path, env_marker: Path, *, tamper: Path | None = None) -> None:
    tamper_line = f'echo tampered >> "{tamper}"' if tamper else ""
    path.write_text(
        f"""#!/usr/bin/env bash
if [ "$1" = "--version" ]; then echo "1.2.3 (Claude Code)"; exit 0; fi
cat > /dev/null
printf '%s\\n' "$@" > "{argv_marker}"
printf 'HOME=%s\\nURL=%s\\nINSTALL_DIR=%s\\nVERSION=%s\\n' \\
  "${{CLAUDE_GPT_HOME-__UNSET__}}" "${{CLAUDE_GPT_REPAIR_INSTALLER_URL-__UNSET__}}" \\
  "${{CLAUDE_CODE_PROXY_INSTALL_DIR-__UNSET__}}" "${{CLAUDE_CODE_PROXY_VERSION-__UNSET__}}" > "{env_marker}"
{tamper_line}
echo '{{"type":"result","subtype":"success"}}'
exit 0
""",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run_runner(repo: Path, worktree: Path, tmp_path: Path, env: dict[str, str], *extra: str,
                claude_bin: Path, evidence: Path | None = None) -> subprocess.CompletedProcess[str]:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("hello\n", encoding="utf-8")
    out_dir = tmp_path / "out"
    args = [
        sys.executable, str(RUNNER), "--repo-root", str(repo), "--worktree", str(worktree),
        "--runtime", "claude", "--mode", "structured", "--prompt-file", str(prompt),
        "--output-dir", str(out_dir), "--claude-bin", str(claude_bin),
        "--timeout-seconds", "30", "--max-turns", "2", *extra,
    ]
    if evidence is not None:
        args += ["--evidence-json", str(evidence)]
    return subprocess.run(args, cwd=str(repo), capture_output=True, text=True, check=False, env=env)


def _recorded_settings(argv_marker: Path) -> str:
    lines = argv_marker.read_text(encoding="utf-8").splitlines()
    return lines[lines.index("--settings") + 1]


def test_precondition_fail_closed_runner_does_not_launch_child(contract, tmp_path):
    repo, worktree = _standin_repo(tmp_path, contract, with_fixture=False)
    argv_marker, env_marker = tmp_path / "argv", tmp_path / "env"
    fake = tmp_path / "claude"
    _write_fake_claude(fake, argv_marker, env_marker)
    env = {**os.environ, **_good_env(contract, worktree)}
    result = _run_runner(repo, worktree, tmp_path, env, "--approval-profile", PROFILE_ID, claude_bin=fake)
    assert result.returncode == 2, result.stderr
    assert "fixture_missing" in result.stderr
    assert not argv_marker.exists(), "child claude must not be launched when a precondition fails"


def test_audit_evidence_records_approval_carrier(contract, runner, tmp_path):
    repo, worktree = _standin_repo(tmp_path, contract)
    argv_marker, env_marker = tmp_path / "argv", tmp_path / "env"
    fake = tmp_path / "claude"
    _write_fake_claude(fake, argv_marker, env_marker)
    env = {**os.environ, **_good_env(contract, worktree)}
    evidence = tmp_path / "evidence.json"
    result = _run_runner(repo, worktree, tmp_path, env, "--approval-profile", PROFILE_ID,
                         claude_bin=fake, evidence=evidence)
    assert result.returncode == 0, result.stderr + result.stdout
    data = json.loads(evidence.read_text(encoding="utf-8"))
    carrier = data["approval_carrier"]
    settings_json = _recorded_settings(argv_marker)
    head = _git("rev-parse", "HEAD", cwd=worktree).stdout.strip()
    rel = contract.get_approval_profile(PROFILE_ID).fixture_relpath
    blob = _git("rev-parse", f"HEAD:{rel}", cwd=worktree).stdout.strip()
    assert carrier["profile_id"] == PROFILE_ID
    assert carrier["repo_head"] == head
    assert carrier["overlay_sha256"] == hashlib.sha256(settings_json.encode("utf-8")).hexdigest()
    assert carrier["fixture_git_blob_hash"] == blob
    assert carrier["fixture_unchanged"] is True
    assert carrier["preconditions"]["fixture_relpath"] == rel
    # raw の環境値 (tmp の絶対 path) を evidence に含めない。
    assert str(tmp_path) not in json.dumps(carrier)
    # 子 env は検証済みの値そのままで、override 変数は無い。
    env_lines = dict(line.split("=", 1) for line in env_marker.read_text(encoding="utf-8").splitlines())
    assert env_lines["HOME"] == env["CLAUDE_GPT_HOME"]
    assert env_lines["URL"] == env["CLAUDE_GPT_REPAIR_INSTALLER_URL"]
    assert env_lines["INSTALL_DIR"] == "__UNSET__" and env_lines["VERSION"] == "__UNSET__"


def test_audit_evidence_fixture_changed_after_run_not_pass(contract, tmp_path):
    repo, worktree = _standin_repo(tmp_path, contract)
    rel = contract.get_approval_profile(PROFILE_ID).fixture_relpath
    argv_marker, env_marker = tmp_path / "argv", tmp_path / "env"
    fake = tmp_path / "claude"
    _write_fake_claude(fake, argv_marker, env_marker, tamper=Path(os.path.realpath(worktree)) / rel)
    env = {**os.environ, **_good_env(contract, worktree)}
    evidence = tmp_path / "evidence.json"
    result = _run_runner(repo, worktree, tmp_path, env, "--approval-profile", PROFILE_ID,
                         claude_bin=fake, evidence=evidence)
    assert result.returncode != 0
    data = json.loads(evidence.read_text(encoding="utf-8"))
    assert data["approval_carrier"]["fixture_unchanged"] is False
    assert any("fixture installer content changed" in e for e in data["errors"])


def test_argv_unchanged_without_profile(contract, runner, tmp_path):
    repo, worktree = _standin_repo(tmp_path, contract)
    env = {**os.environ, **_good_env(contract, worktree)}

    def run(name: str, *extra: str):
        sub = tmp_path / name
        sub.mkdir()
        argv_marker, env_marker = sub / "argv", sub / "env"
        fake = sub / "claude"
        _write_fake_claude(fake, argv_marker, env_marker)
        evidence = sub / "evidence.json"
        result = _run_runner(repo, worktree, sub, env, *extra, claude_bin=fake, evidence=evidence)
        assert result.returncode == 0, result.stderr + result.stdout
        return (
            argv_marker.read_text(encoding="utf-8").splitlines(),
            json.loads(evidence.read_text(encoding="utf-8")),
        )

    plain_argv, plain_evidence = run("plain")
    carrier_argv, carrier_evidence = run("carrier", "--approval-profile", PROFILE_ID)
    # flag 未指定: 基底 overlay 定数そのもの (byte-identical) で、evidence に approval_carrier が無い。
    assert plain_argv[plain_argv.index("--settings") + 1] == runner._CLAUDE_SPAWN_HOOK_OBSERVABILITY_SETTINGS_JSON
    assert "approval_carrier" not in plain_evidence
    # carrier 使用時の argv は、--settings の値以外は flag 未指定と同一で、overlay は autoMode の追加だけ。
    assert len(carrier_argv) == len(plain_argv)
    plain_settings = json.loads(plain_argv[plain_argv.index("--settings") + 1])
    carrier_settings = json.loads(carrier_argv[carrier_argv.index("--settings") + 1])
    assert "autoMode" in carrier_settings
    carrier_settings.pop("autoMode")
    assert carrier_settings == plain_settings
    without_settings = lambda argv: [a for i, a in enumerate(argv) if argv[i - 1] != "--settings" or i == 0]  # noqa: E731
    assert without_settings(carrier_argv) == without_settings(plain_argv)
    assert "approval_carrier" in carrier_evidence


# ---------------------------------------------------------------------------
# PR #2844 OWNER fix_delta (P1): --require-hook-chain-evidence との合成
# ---------------------------------------------------------------------------

# #2810 の現行 AC10 と同じ flag 構成 (consumer command shape)。
AC10_SHAPE_FLAGS = (
    "--agent-type", "implementation-worker",
    "--require-subagent-causal-evidence", "--require-min-subagents", "1",
    "--require-hook-chain-evidence",
    "--require-observed-runtime-field", "effective_permission_profile",
    "--require-clean-postcondition",
    "--approval-profile", PROFILE_ID,
)


def test_hook_chain_evidence_composition_ac10_shape_reaches_child_with_one_settings(contract, runner, tmp_path):
    repo, worktree = _standin_repo(tmp_path, contract)
    argv_marker, env_marker = tmp_path / "argv", tmp_path / "env"
    fake = tmp_path / "claude"
    _write_fake_claude(fake, argv_marker, env_marker)
    env = {**os.environ, **_good_env(contract, worktree)}
    evidence = tmp_path / "evidence.json"
    result = _run_runner(repo, worktree, tmp_path, env, *AC10_SHAPE_FLAGS, claude_bin=fake, evidence=evidence)
    # precondition は通る (parser.error の exit 2 ではない)。fake claude は causal evidence を
    # 出さないので run 自体の判定は PASS にならなくてよい。ここで見るのは child が受け取る argv。
    assert result.returncode != 2, result.stderr
    assert "incompatible_flag" not in result.stderr
    assert argv_marker.exists(), "child claude must be launched when the AC10-shaped precondition passes"
    argv = argv_marker.read_text(encoding="utf-8").splitlines()
    assert argv.count("--settings") == 1
    assert argv.count("--setting-sources") == 1
    assert argv[argv.index("--setting-sources") + 1] == "project"
    settings = json.loads(argv[argv.index("--settings") + 1])
    # 観測 hooks と carrier の autoMode が同一 settings に共存する。
    hooks = settings["hooks"]
    assert set(hooks) == {"SubagentStart", "SubagentStop", "PreToolUse", "Stop"}
    assert hooks["PreToolUse"][0]["matcher"] == runner._HOOK_CHAIN_EVIDENCE_TOOL
    assert hooks["PreToolUse"][0]["hooks"] == [{"type": "command", "command": "cat"}]
    assert hooks["Stop"] == [{"hooks": [{"type": "command", "command": "cat"}]}]
    rule = contract.get_approval_profile(PROFILE_ID).allow_rule
    assert settings["autoMode"] == {"allow": ["$defaults", rule]}
    # 固定値は保持される。
    assert settings["permissions"] == {"deny": ["SendMessage", "ListAgents"]}
    assert settings["crossSessionInbound"] == "refuse"
    # 任意の caller settings は混入しない (許可される key は固定 overlay のものだけ)。
    assert set(settings) == {"crossSessionInbound", "permissions", "hooks", "autoMode"}
    for forbidden in ("soft_deny", "hard_deny", "environment"):
        assert forbidden not in settings["autoMode"]
    # runner が選択した overlay に autoMode を足しただけである。
    selected = json.loads(runner.select_native_observation_settings_json(include_hook_chain_evidence_hooks=True))
    settings.pop("autoMode")
    assert settings == selected
    # audit の hash は child が受け取った settings と一致する。
    carrier = json.loads(evidence.read_text(encoding="utf-8"))["approval_carrier"]
    assert carrier["overlay_sha256"] == hashlib.sha256(
        argv[argv.index("--settings") + 1].encode("utf-8")
    ).hexdigest()


def test_overlay_hook_chain_evidence_selected_overlay_default_is_byte_identical(runner):
    assert runner.select_native_observation_settings_json() == runner._CLAUDE_SPAWN_HOOK_OBSERVABILITY_SETTINGS_JSON


def test_overlay_hook_chain_evidence_composition_rejects_mismatched_overlay(contract, runner, tmp_path):
    """no_generic_passthrough: run_structured_claude は、選択された overlay + autoMode 以外の
    approval overlay (観測 hooks の欠落・任意 key の混入) を子の起動前に拒否する。"""
    base_only = contract.build_approval_overlay_json(PROFILE_ID, _base_overlay(runner))
    with pytest.raises(ValueError, match="selected observation overlay"):
        # hook-chain lane を要求しているのに、hook-chain hooks を持たない overlay (置換で hooks を失う)。
        runner.run_structured_claude(
            str(tmp_path), "p", 5, 1, approval_settings_json=base_only,
            include_hook_chain_evidence_hooks=True,
        )
    selected = runner.select_native_observation_settings_json(include_hook_chain_evidence_hooks=True)
    injected = json.loads(contract.build_approval_overlay_json(PROFILE_ID, selected))
    injected["permissions"]["allow"] = ["Bash(*)"]
    with pytest.raises(ValueError, match="selected observation overlay"):
        runner.run_structured_claude(
            str(tmp_path), "p", 5, 1, approval_settings_json=json.dumps(injected),
            include_hook_chain_evidence_hooks=True,
        )
    with pytest.raises(ValueError, match="selected observation overlay"):
        runner.run_structured_claude(  # autoMode を持たない overlay は carrier ではない
            str(tmp_path), "p", 5, 1, approval_settings_json=selected,
            include_hook_chain_evidence_hooks=True,
        )


@pytest.mark.parametrize("extra", [("--expect-skill-command", "x"), ("--hermetic-agent-definition",)])
def test_precondition_fail_closed_runner_rejects_other_composition_flags(contract, tmp_path, extra):
    """generic な overlay 合成には広げない: --expect-skill-command / --hermetic-agent-definition は
    引き続き carrier と併用できず、child を起動しない。"""
    repo, worktree = _standin_repo(tmp_path, contract)
    argv_marker, env_marker = tmp_path / "argv", tmp_path / "env"
    fake = tmp_path / "claude"
    _write_fake_claude(fake, argv_marker, env_marker)
    env = {**os.environ, **_good_env(contract, worktree)}
    result = _run_runner(repo, worktree, tmp_path, env, "--approval-profile", PROFILE_ID, *extra, claude_bin=fake)
    assert result.returncode == 2, result.stderr
    assert not argv_marker.exists()


# ---------------------------------------------------------------------------
# AC6: checker (判定表の行と 1 対 1)
# ---------------------------------------------------------------------------

def _good_vc(contract, *, profile: str = PROFILE_ID, ac: str | None = "AC9", extra: str = "",
             home: str = "$PWD/artifacts/runtime-smoke/fixture-home", installer: str | None = "default") -> str:
    rel = contract.get_approval_profile(PROFILE_ID).fixture_relpath
    prefix = f"CLAUDE_GPT_HOME={home} "
    if installer == "default":
        prefix += f"CLAUDE_GPT_REPAIR_INSTALLER_URL=file://$PWD/{rel} "
    elif installer:
        prefix += f"CLAUDE_GPT_REPAIR_INSTALLER_URL={installer} "
    cmd = (
        f"{prefix}uv run --locked python3 scripts/agent-ops/run_worktree_agent_runtime_smoke.py "
        f'--runtime claude --mode structured --worktree "$PWD" --prompt-file p.md --output-dir o '
        f"--approval-profile {profile} {extra}"
    ).strip()
    head = f"# {ac}\n" if ac else ""
    return f"{head}$ {cmd}"


def _body(decl: str | None, vc: str) -> str:
    rva = "## Runtime Verification Applicability\n\n- decision: immediate\n"
    if decl is not None:
        rva += decl + "\n"
    return (
        "## Outcome\n\nx\n\n" + rva
        + "\n## Verification Commands\n\n```bash\n" + vc + "\n```\n\n## Allowed Paths\n\n- a\n"
    )


DECL = f"approval_required_actions: [{PROFILE_ID}]"


def _cases(contract):
    good = _good_vc(contract)
    plain_runner = (
        "# AC9\n$ uv run --locked python3 scripts/agent-ops/run_worktree_agent_runtime_smoke.py "
        '--runtime claude --mode structured --worktree "$PWD" --prompt-file p.md --output-dir o'
    )
    return {
        "row0": (_body(DECL, good + " ; echo hi"), "row0_unparseable_runner_line"),
        "row0_bash_c": (
            # 承認に関係する (carrier flag を含む) invocation が bash -c に包まれている場合は fail-closed。
            _body(DECL, "# AC9\n$ bash -c 'uv run python3 scripts/agent-ops/run_worktree_agent_runtime_smoke.py "
                        f"--approval-profile {PROFILE_ID}'"),
            "row0_unparseable_runner_line",
        ),
        "row2_flag": (_body(None, good), "row2_flag_or_signature_without_declaration"),
        "row2_signature": (_body(None, "# AC9\n$ bash scripts/claude-gpt/repair_proxy.sh"),
                           "row2_flag_or_signature_without_declaration"),
        "row3_empty": (_body("approval_required_actions: []", good), "row3_declaration_invalid"),
        "row3_duplicate": (_body(f"approval_required_actions: [{PROFILE_ID}, {PROFILE_ID}]", good),
                           "row3_declaration_invalid"),
        "row3_unknown": (_body("approval_required_actions: [nope]", good), "row3_declaration_invalid"),
        "row3_malformed": (_body("approval_required_actions: repair", good), "row3_declaration_invalid"),
        "row4": (_body(DECL, plain_runner), "row4_declared_without_runner_line"),
        "row5": (_body(DECL, _good_vc(contract, ac=None)), "row5_flag_line_not_attached_to_ac"),
        "row6": (_body(DECL, _good_vc(contract, extra="--claude-adapter claude-gpt --claude-bin x")),
                 "row6_flag_with_claude_gpt_adapter"),
        "row7_missing_installer": (_body(DECL, _good_vc(contract, installer=None)),
                                   "row7_env_prefix_missing_or_mismatch"),
        "row7_home_mismatch": (_body(DECL, _good_vc(contract, home="$PWD/other/place")),
                               "row7_env_prefix_missing_or_mismatch"),
        "row7a": (_body(DECL, good + "\n\n" + _good_vc(contract, profile="typo", ac="AC10")),
                  "row7a_unknown_or_undeclared_flag_value"),
    }


@pytest.mark.parametrize("case", [
    "row0", "row0_bash_c", "row2_flag", "row2_signature", "row3_empty", "row3_duplicate",
    "row3_unknown", "row3_malformed", "row4", "row5", "row6", "row7_missing_installer",
    "row7_home_mismatch", "row7a",
])
def test_non_executable_contract_detected(contract, case):
    body, expected_row = _cases(contract)[case]
    verdict = contract.check_runtime_vc_approval_contract(body)
    assert verdict["status"] == "non_executable", (case, verdict)
    assert verdict["row"] == expected_row, (case, verdict)
    assert verdict["reason_codes"], verdict


def test_non_executable_contract_detected_complement_not_applicable(contract):
    plain = (
        "# AC9\n$ uv run --locked python3 scripts/agent-ops/run_worktree_agent_runtime_smoke.py "
        '--runtime claude --mode structured --worktree "$PWD" --prompt-file p.md --output-dir o'
    )
    verdict = contract.check_runtime_vc_approval_contract(_body(None, plain))
    assert verdict == {"status": "not_applicable", "row": "row1_not_applicable", "reason_codes": []}


def test_non_executable_contract_detected_complement_executable(contract):
    verdict = contract.check_runtime_vc_approval_contract(_body(DECL, _good_vc(contract)))
    assert verdict == {"status": "executable", "row": "row8_executable", "reason_codes": []}


def test_non_executable_contract_detected_cli_exit_codes(contract, tmp_path):
    bad = tmp_path / "bad.md"
    bad.write_text(_body(None, _good_vc(contract)), encoding="utf-8")
    good = tmp_path / "good.md"
    good.write_text(_body(DECL, _good_vc(contract)), encoding="utf-8")
    run = lambda p: subprocess.run(  # noqa: E731
        [sys.executable, str(CONTRACT), "--issue-body-file", str(p)], capture_output=True, text=True, check=False,
    )
    assert run(bad).returncode == 1
    assert run(good).returncode == 0
    assert run(tmp_path / "missing.md").returncode == 2


# ---------------------------------------------------------------------------
# PR #2844 OWNER fix_delta (P2a / P2b): invocation 単位の適用判定と quote-aware parser
# ---------------------------------------------------------------------------

REL = ".claude/skills/impl-review-loop/tests/fixtures/fake_proxy_installer.sh"
RUNNER_PATH = "scripts/agent-ops/run_worktree_agent_runtime_smoke.py"
GOOD_PREFIX = (
    f"CLAUDE_GPT_HOME=$PWD/artifacts/runtime-smoke/fixture-home-deny "
    f"CLAUDE_GPT_REPAIR_INSTALLER_URL=file://$PWD/{REL} "
)


def _runner_cmd(ac: str | None, *, prefix: str = GOOD_PREFIX, flags: str = "--mode structured",
                carrier: str | None = PROFILE_ID) -> str:
    """runner 1 行 (AC コメント付き) を組み立てる。``flags`` / ``prefix`` は生の shell text。"""
    head = f"# {ac}\n" if ac else ""
    tail = f" --approval-profile {carrier}" if carrier else ""
    return (
        f"{head}$ {prefix}uv run --locked python3 {RUNNER_PATH} --runtime claude "
        f'--worktree "$PWD" --prompt-file p.md --output-dir o {flags}{tail}'
    )


def _check(contract, *vcs: str, decl: str | None = DECL) -> dict:
    return contract.check_runtime_vc_approval_contract(_body(decl, "\n\n".join(vcs)))


# --- P2a: invocation 単位 ---------------------------------------------------


def test_per_invocation_ac9_ok_ac10_carrier_missing_is_non_executable(contract):
    ac10 = _runner_cmd("AC10", flags="--mode structured --require-hook-chain-evidence", carrier=None)
    verdict = _check(contract, _runner_cmd("AC9"), ac10)
    assert verdict["status"] == "non_executable"
    assert verdict["row"] == "row4a_signature_runner_line_without_flag"


def test_per_invocation_reverse_order_ac9_missing_ac10_ok_is_non_executable(contract):
    ac9 = _runner_cmd("AC9", carrier=None)
    verdict = _check(contract, ac9, _runner_cmd("AC10", flags="--mode structured --require-hook-chain-evidence"))
    assert verdict["status"] == "non_executable"
    assert verdict["row"] == "row4a_signature_runner_line_without_flag"


def test_per_invocation_both_ok_is_executable(contract):
    ac10 = _runner_cmd("AC10", flags="--mode structured --require-hook-chain-evidence --evidence-json e.json")
    verdict = _check(contract, _runner_cmd("AC9", flags='--mode structured --expect-marker "A && B"'), ac10)
    assert verdict == {"status": "executable", "row": "row8_executable", "reason_codes": []}


def test_per_invocation_approval_free_runner_line_alongside_carrier_line_is_not_enforced(contract):
    plain = _runner_cmd("AC1", prefix="", flags='--mode structured --expect-marker "x ; y && z"', carrier=None)
    verdict = _check(contract, plain, _runner_cmd("AC9"))
    assert verdict == {"status": "executable", "row": "row8_executable", "reason_codes": []}


def test_per_invocation_approval_free_issue_reaches_not_applicable_despite_quoted_operators(contract):
    plain = _runner_cmd("AC9", prefix="", flags='--mode structured --expect-marker "A && B" --expect-marker "a | b; c"',
                        carrier=None)
    verdict = _check(contract, plain, decl=None)
    assert verdict == {"status": "not_applicable", "row": "row1_not_applicable", "reason_codes": []}


def test_per_invocation_irrelevant_line_with_real_shell_syntax_is_not_rejected(contract):
    # 承認に関係しない invocation には、この checker 独自の shell 書式制限を課さない。
    for tail in ("&& echo done", "; echo done", "| tee out.log", "> out.log", "$(date)", "`date`"):
        plain = _runner_cmd("AC9", prefix="", flags=f"--mode structured {tail}", carrier=None)
        verdict = _check(contract, plain, decl=None)
        assert verdict["status"] == "not_applicable", (tail, verdict)


def test_per_invocation_non_runner_lines_mentioning_the_runner_file_are_ignored(contract):
    rg_line = f"# AC1\n$ rg -n approval {RUNNER_PATH} && echo ok"
    pytest_line = "# AC2\n$ uv run --locked pytest scripts/agent-ops/tests/test_run_worktree_agent_runtime_smoke.py -q"
    assert _check(contract, rg_line, pytest_line, decl=None)["status"] == "not_applicable"


def test_per_invocation_signature_without_flag_and_without_declaration_is_row2(contract):
    verdict = _check(contract, _runner_cmd("AC9", carrier=None), decl=None)
    assert verdict["row"] == "row2_flag_or_signature_without_declaration"


# --- P2b: quote-aware parser ------------------------------------------------


def test_quote_aware_double_quoted_pwd_expansion_is_accepted(contract):
    prefix = (
        'CLAUDE_GPT_HOME="$PWD/artifacts/runtime-smoke/fixture-home-deny" '
        f'CLAUDE_GPT_REPAIR_INSTALLER_URL="file://$PWD/{REL}" '
    )
    assert _check(contract, _runner_cmd("AC9", prefix=prefix))["status"] == "executable"
    braced = prefix.replace("$PWD/", "${PWD}/")
    assert _check(contract, _runner_cmd("AC9", prefix=braced))["status"] == "executable"


def test_quote_aware_single_quoted_pwd_is_literal_not_expanded(contract):
    home_single = (
        "CLAUDE_GPT_HOME='$PWD/artifacts/runtime-smoke/fixture-home-deny' "
        f"CLAUDE_GPT_REPAIR_INSTALLER_URL=file://$PWD/{REL} "
    )
    verdict = _check(contract, _runner_cmd("AC9", prefix=home_single))
    assert verdict["status"] == "non_executable"
    assert verdict["row"] == "row7_env_prefix_missing_or_mismatch"
    url_single = (
        "CLAUDE_GPT_HOME=$PWD/artifacts/runtime-smoke/fixture-home-deny "
        f"CLAUDE_GPT_REPAIR_INSTALLER_URL='file://$PWD/{REL}' "
    )
    verdict = _check(contract, _runner_cmd("AC9", prefix=url_single))
    assert verdict["row"] == "row7_env_prefix_missing_or_mismatch"
    escaped = GOOD_PREFIX.replace("=$PWD", "=\\$PWD", 1)
    assert _check(contract, _runner_cmd("AC9", prefix=escaped))["row"] == "row7_env_prefix_missing_or_mismatch"


def test_quote_aware_quoted_env_assignment_word_is_not_an_assignment(contract):
    prefix = '"CLAUDE_GPT_HOME=$PWD/artifacts/runtime-smoke/fixture-home-deny" ' + GOOD_PREFIX.split(" ", 1)[1]
    assert _check(contract, _runner_cmd("AC9", prefix=prefix))["row"] == "row7_env_prefix_missing_or_mismatch"


@pytest.mark.parametrize("adapter", [
    '--claude-adapter "claude-gpt"', "--claude-adapter 'claude-gpt'", "--claude-adapter=claude-gpt",
    '--claude-adapter="claude-gpt"', "--claude-adapter claude-gpt",
])
def test_quote_aware_claude_adapter_claude_gpt_is_recognized(contract, adapter):
    verdict = _check(contract, _runner_cmd("AC9", flags=f"--mode structured {adapter} --claude-bin x"))
    assert verdict["status"] == "non_executable"
    assert verdict["row"] == "row6_flag_with_claude_gpt_adapter"


@pytest.mark.parametrize("mode", [
    '--mode "interactive"', "--mode 'interactive'", "--mode=interactive", "--mode interactive",
])
def test_quote_aware_non_structured_mode_is_recognized_with_carrier(contract, mode):
    verdict = _check(contract, _runner_cmd("AC9", flags=mode))
    assert verdict["status"] == "non_executable"
    assert verdict["row"] == "row6a_flag_with_non_structured_mode"


@pytest.mark.parametrize("mode", ['--mode "structured"', "--mode='structured'"])
def test_quote_aware_quoted_structured_mode_is_accepted(contract, mode):
    assert _check(contract, _runner_cmd("AC9", flags=mode))["status"] == "executable"


def test_quote_aware_quoted_profile_value_is_recognized(contract):
    line = _runner_cmd("AC9", carrier=None) + f' --approval-profile "{PROFILE_ID}"'
    assert _check(contract, line)["status"] == "executable"
    line = _runner_cmd("AC9", carrier=None) + " --approval-profile='typo'"
    verdict = _check(contract, _runner_cmd("AC9"), line.replace("# AC9", "# AC10"))
    assert verdict["row"] == "row7a_unknown_or_undeclared_flag_value"


def test_quote_aware_quoted_operator_in_relevant_line_is_not_chaining(contract):
    flags = '--mode structured --expect-marker "A && B" --expect-marker \'x ; y | z\' --expect-marker "eval bash -c"'
    assert _check(contract, _runner_cmd("AC9", flags=flags))["status"] == "executable"


@pytest.mark.parametrize("flags", [
    "--mode structured ; echo hi", "--mode structured && echo hi", "--mode structured || true",
    "--mode structured | tee out", "--mode structured > out.log", "--mode structured $(date)",
    "--mode structured `date`", '--mode structured "$(date)"', "--mode structured --x $HOME",
    '--mode structured --x "$OTHER"', "--mode structured & sleep 1",
])
def test_quote_aware_real_shell_syntax_in_relevant_line_is_fail_closed(contract, flags):
    verdict = _check(contract, _runner_cmd("AC9", flags=flags))
    assert verdict["status"] == "non_executable"
    assert verdict["row"] == "row0_unparseable_runner_line"


def test_quote_aware_bash_c_and_eval_in_relevant_line_are_fail_closed(contract):
    inner = _runner_cmd("AC9").split("\n", 1)[1][2:]
    bash_c = "# AC9\n$ bash -c " + repr(inner)
    eval_line = f"# AC9\n$ eval {inner}"
    for vc in (bash_c, eval_line):
        verdict = _check(contract, vc)
        assert verdict["status"] == "non_executable" and verdict["row"] == "row0_unparseable_runner_line", vc


def test_quote_aware_bash_c_without_approval_relevance_is_not_rejected(contract):
    vc = f"# AC9\n$ bash -c 'uv run python3 {RUNNER_PATH} --runtime claude --mode structured'"
    assert _check(contract, vc, decl=None)["status"] == "not_applicable"


def test_quote_aware_command_substitution_in_env_prefix_is_fail_closed_when_relevant(contract):
    prefix = GOOD_PREFIX.replace("$PWD/artifacts", "$(pwd)/artifacts", 1)
    verdict = _check(contract, _runner_cmd("AC9", prefix=prefix))
    assert verdict["row"] == "row0_unparseable_runner_line"


def test_quote_aware_lexer_records_quote_provenance(contract):
    words, unsupported = contract._lex_words(
        'A="$PWD/x" B=\'$PWD/x\' C=$PWD/x D=\\$PWD/x "quoted flag" \'\' plain'
    )
    assert unsupported == []
    templates = [w["template"] for w in words]
    pwd = contract._PWD
    assert templates == [f"A={pwd}/x", "B=$PWD/x", f"C={pwd}/x", "D=$PWD/x", "quoted flag", "", "plain"]
    assert words[0]["bare_head"] == "A=" and words[1]["bare_head"] == "B="
    assert words[4]["bare_head"] == ""
    _, unsupported = contract._lex_words("a ; b && c | d")
    assert unsupported.count("operator:;") == 1 and "operator:|" in unsupported
    _, unsupported = contract._lex_words("a 'unterminated")
    assert "unterminated_single_quote" in unsupported


def test_quote_aware_declaration_accepts_quoted_ids_without_strip_hack(contract):
    for decl in (f"approval_required_actions: ['{PROFILE_ID}']", f'approval_required_actions: ["{PROFILE_ID}"]'):
        assert _check(contract, _runner_cmd("AC9"), decl=decl)["status"] == "executable", decl
    for decl in (f"approval_required_actions: ['{PROFILE_ID}\"]", f"approval_required_actions: [{PROFILE_ID},]"):
        assert _check(contract, _runner_cmd("AC9"), decl=decl)["row"] == "row3_declaration_invalid", decl


def test_quote_aware_ac10_consumer_command_shape_is_executable(contract):
    """#2810 現行 AC10 の command shape (hook-chain evidence 併用) を executable と判定する。"""
    ac10 = (
        "# AC10\n$ CLAUDE_GPT_HOME=$PWD/artifacts/runtime-smoke/fixture-home-deny "
        f"CLAUDE_GPT_REPAIR_INSTALLER_URL=file://$PWD/{REL} "
        f"uv run --locked python3 {RUNNER_PATH} --runtime claude --mode structured "
        '--worktree "$PWD" --prompt-file p.md --output-dir o --evidence-json e.json '
        "--timeout-seconds 300 --max-turns 8 --agent-type implementation-worker "
        "--require-subagent-causal-evidence --require-min-subagents 1 --require-hook-chain-evidence "
        "--require-observed-runtime-field effective_permission_profile --require-clean-postcondition "
        f"--approval-profile {PROFILE_ID}"
    )
    ac9 = _runner_cmd("AC9", flags='--mode structured --expect-marker "repair OK && verified"')
    assert _check(contract, ac9, ac10) == {"status": "executable", "row": "row8_executable", "reason_codes": []}
