"""scripts/claude-gpt/tests/test_minimal_default_contract.py

Issue #2925: Claude-GPT launcher を upstream Minimal client contract へ縮退したことの
focused test。実 `scripts/claude-gpt/launch.sh` を subprocess で駆動し、fake `claude` が
**実際に受け取った** env / argv を観測する（token の grep だけでは hollow な実装を通すため、
観測は全て behavioral にしている）。静的検査は補助としてのみ使う。

選択子（Verification Commands）:
  -k default_env_contract              AC1
  -k compensating_layers_removed       AC2
  -k ambient_surface                   AC6（hermetic 部分。runtime 部分は AC4/AC5 の live 実行内で確認）
  -k legacy_launcher_not_a_fallback    AC8
  -k role_routing_and_auto_mode_env    AC9(a)
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

_HARNESS_PATH = Path(__file__).resolve().parent / "_launcher_harness.py"
_spec = importlib.util.spec_from_file_location("claude_gpt_launcher_harness_2925_minimal", _HARNESS_PATH)
assert _spec is not None and _spec.loader is not None
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)

# Minimal Default Contract + 現行 contract として残す設定（Issue #2925 本文）。これが
# launcher が追加・変更してよい env の全てである。
EXPECTED_ADDED_ENV = {
    "ANTHROPIC_BASE_URL": None,  # 接続先 server の URL（テストごとに異なる）
    "ANTHROPIC_AUTH_TOKEN": "unused",
    "ANTHROPIC_MODEL": "gpt-6-sol[1m]",
    "ANTHROPIC_SMALL_FAST_MODEL": "gpt-6-luna[1m]",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "272000",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK": "1",
    # role-based model routing（現行値を維持）
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "gpt-6-sol[1m]",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "gpt-6-sol[1m]",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "gpt-6-luna[1m]",
    # Auto mode 互換設定
    "CLAUDE_CODE_AUTO_MODE_SERVER": "0",
    # 非 behavior-changing な runtime identification（既存 consumer が識別に使う 2 個）:
    #   Task Context の runtime flavor、session manifest hook の runtime_lane（旧 launcher からの既存 carrier）
    "LOOP_TASK_CONTEXT_RUNTIME_VARIANT": "claude_gpt",
    "CLAUDE_GPT_CLAUDE_BIN": "<fake-claude>",
}


@pytest.fixture()
def server():
    with H.FakeServer() as srv:
        yield srv


def _launch(tmp_path, server, extra_args=(), **env_overrides):
    proc, observed, env = H.run_launcher_with_fake_claude(tmp_path, server.url, extra_args, **env_overrides)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert observed is not None, "launcher never exec'd claude"
    return proc, observed, env


def _strip_hint(model: str) -> str:
    return re.sub(r"\[[^\]]*\]$", "", model)


# ---------------------------------------------------------------------------
# AC1 -- default path minimalization
# ---------------------------------------------------------------------------


def test_default_env_contract_exact_added_env_and_no_injected_flags(tmp_path, server):
    _proc, observed, env = _launch(tmp_path, server, ("-p", "hello"))
    child_env = observed["env"]

    # launcher が追加・変更した env key は EXPECTED_ADDED_ENV に限られる（FAKE_CLAUDE_* は harness 由来）。
    # ANTHROPIC_BASE_URL は harness が親 env で接続先として与えるため差分には現れない
    # （値の一致は下で別途検証する）。LC_CTYPE は fake claude の python が付与する。
    harness_keys = {"FAKE_CLAUDE_OUT", "FAKE_CLAUDE_EXIT", "LC_CTYPE"}
    changed = {
        key
        for key in set(child_env) | set(env)
        if child_env.get(key) != env.get(key)
    } - harness_keys - {"PWD", "OLDPWD", "SHLVL", "_"}
    expected_changed = set(EXPECTED_ADDED_ENV) - {"ANTHROPIC_BASE_URL"}
    assert changed == expected_changed, sorted(changed ^ expected_changed)

    for key, expected in EXPECTED_ADDED_ENV.items():
        if expected is None:
            assert child_env[key] == server.url
        elif expected == "<fake-claude>":
            assert child_env[key] == str(tmp_path / "fake-claude")
        else:
            assert child_env[key] == expected, key

    # claude へ渡る argv は `--` 以降の caller 引数そのもの。launcher は flag を 1 つも注入しない。
    assert observed["argv"] == ["-p", "hello"]
    for injected in ("--settings", "--mcp-config", "--strict-mcp-config", "--permission-mode", "--agents"):
        assert injected not in observed["argv"]


def test_default_env_contract_does_not_isolate_home_xdg_or_config_dir(tmp_path, server):
    _proc, observed, env = _launch(tmp_path, server)
    for key in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "CLAUDE_CONFIG_DIR"):
        assert observed["env"][key] == env[key], f"{key} was rewritten by the launcher"


def test_default_env_contract_does_not_inject_ccp_auto_review_model_or_other_server_policy(tmp_path, server):
    _proc, observed, _env = _launch(tmp_path, server)
    server_side_keys = [k for k in observed["env"] if k.startswith("CCP_")]
    assert server_side_keys == [], server_side_keys
    assert "CCP_AUTO_REVIEW_MODEL" not in observed["env"]


def test_default_env_contract_passes_caller_flags_through_unchanged(tmp_path, server):
    # 旧 launcher が拒否していた flag も、Native と同じく claude へそのまま渡る。
    args = ("--settings", '{"x":1}', "--mcp-config", "m.json", "--agents", "{}", "--permission-mode", "plan", "-p", "q")
    _proc, observed, _env = _launch(tmp_path, server, args)
    assert observed["argv"] == list(args)


@pytest.mark.parametrize(
    "flag",
    [
        ("--dangerously-skip-permissions",),
        ("--allow-dangerously-skip-permissions",),
        ("--permission-mode", "bypassPermissions"),
        ("--permission-mode=bypassPermissions",),
    ],
)
def test_default_env_contract_never_enables_permission_bypass(tmp_path, server, flag):
    proc, observed, _env = H.run_launcher_with_fake_claude(tmp_path, server.url, flag)
    assert proc.returncode == 2, (proc.stdout, proc.stderr)
    assert observed is None, "claude must not be reached when a bypass flag is requested"
    assert "permission_bypass_flag_rejected" in proc.stderr


def test_default_env_contract_dry_run_has_no_side_effects(tmp_path, server):
    env = H.base_env(tmp_path, ANTHROPIC_BASE_URL=server.url)
    proc = H.run_launcher(["--dry-run"], env)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["status"] == "dry_run"
    assert payload["isolation"] == "none" and payload["generated_settings"] is False
    assert set(payload["launch_env"]) == set(EXPECTED_ADDED_ENV)
    assert not (tmp_path / "claude-gpt-home").exists()


# ---------------------------------------------------------------------------
# AC2 -- compensating layers removed, ordinary inheritance kept
# ---------------------------------------------------------------------------


def test_compensating_layers_removed_ordinary_child_env_is_inherited_not_reinjected(tmp_path, server):
    ambient = {
        "GH_CONFIG_DIR": str(tmp_path / "native-gh"),
        "GH_TOKEN": "ambient-token-value",
        "SSH_AUTH_SOCK": str(tmp_path / "agent.sock"),
        "BUN_OPTIONS": "--ambient",
        "GIT_ASKPASS": "ambient-askpass",
    }
    _proc, observed, _env = _launch(tmp_path, server, **ambient)
    for key, value in ambient.items():
        assert observed["env"].get(key) == value, f"{key}: ordinary inheritance must be preserved"


def test_compensating_layers_removed_no_carrier_is_synthesized_when_ambient_is_empty(tmp_path, server):
    _proc, observed, _env = _launch(tmp_path, server)
    forbidden_prefixes = ("AGY_OAUTH_TOKEN_HANDOFF", "CLAUDE_GPT_NATIVE_SETTINGS_PATH", "CLAUDE_GPT_HOME_ROOT",
                          "CLAUDE_GPT_LATITUDE", "LATITUDE_", "SPARK_LIFECYCLE", "CLAUDE_GPT_HOOK_SINK",
                          "ISSUE_EDITOR_PERMISSION_REQUEST_HOOK", "CLAUDE_GPT_SPARK")
    synthesized = [k for k in observed["env"] if k.startswith(forbidden_prefixes)]
    assert synthesized == [], synthesized
    for key in ("GH_CONFIG_DIR", "GH_TOKEN", "LOOP_TASK_CONTEXT_STATE_ROOT", "LOOP_TASK_CONTEXT_SCOPE",
                "CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_FORK_SUBAGENT", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS",
                "CLAUDE_CODE_ALWAYS_ENABLE_EFFORT", "HERDR_AGENT", "BUN_OPTIONS", "STRICT_MCP_MODE"):
        assert key not in observed["env"], f"{key} must not be synthesized by the launcher"


def test_compensating_layers_removed_launcher_does_not_unset_ambient_surface(tmp_path, server):
    # 旧 launcher が unset していた変数も、Native と同じく ambient 値のまま届く。
    ambient = {
        "CLAUDE_CODE_FORK_SUBAGENT": "1",
        "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "0",
        "CLAUDE_CODE_SUBAGENT_MODEL": "ambient-model",
        "LOOP_TASK_CONTEXT_STATE_ROOT": str(tmp_path / "task-context-root"),
        "LOOP_TASK_CONTEXT_SCOPE": "runtime_smoke",
    }
    _proc, observed, _env = _launch(tmp_path, server, **ambient)
    for key, value in ambient.items():
        assert observed["env"].get(key) == value, key


def test_compensating_layers_removed_creates_no_launcher_owned_state_on_disk(tmp_path, server):
    _proc, observed, env = _launch(tmp_path, server)
    # isolated HOME / config root / generated settings / MCP file / hook files は作られない。
    assert not Path(env["CLAUDE_GPT_HOME"]).exists()
    for root in (Path(env["HOME"]), Path(env["CLAUDE_CONFIG_DIR"]), Path(env["XDG_CONFIG_HOME"])):
        assert list(root.rglob("*")) == [], f"launcher wrote under {root}"


def test_compensating_layers_removed_from_production_sources():
    # 補助的な静的検査: 撤去した層の実装が active code に残っていないこと（コメントは除外）。
    forbidden = (
        "CLAUDE_ISOLATED", "settings.local.json", "mcp-empty.json", "--strict-mcp-config", "--mcp-config",
        "--settings", "--agents", "--permission-mode", "AGY_OAUTH_TOKEN_HANDOFF", "CLAUDE_GPT_NATIVE_SETTINGS_PATH",
        "LATITUDE", "BUN_OPTIONS", "GIT_ASKPASS", "SSH_AUTH_SOCK", "HERDR_AGENT", "umask 077",
        "autoMode", "CCP_AUTO_REVIEW_MODEL", "env -i", "serve --port",
    )
    for name in ("launch.sh", "preflight.sh"):
        code = _active_code(H.SCRIPT_DIR / name)
        for token in forbidden:
            if token == "--permission-mode":
                continue  # launch.sh は bypass 値の拒否判定にだけ使う（注入はしない）。argv 観測で検証済み。
            assert token not in code, f"{name} still contains active code for removed layer: {token}"
    lib_code = _active_code(H.LIB_SH)
    for token in ("CLAUDE_ISOLATED", "settings.local.json", "mcp-empty.json", "AGY_OAUTH_TOKEN_HANDOFF",
                  "autoMode", "CCP_AUTO_REVIEW_MODEL", "LATITUDE", "classify_all_shell", "link_native_sessions"):
        assert token not in lib_code, f"lib.sh still contains active code for removed layer: {token}"


def _active_code(path: Path) -> str:
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# AC6 -- no silent feature loss (ambient surface shared with Native)
# ---------------------------------------------------------------------------


def test_ambient_surface_project_settings_and_user_config_remain_reachable(tmp_path, server):
    # fake claude の cwd は呼び出し元 cwd のまま（repo root の project .claude/ が見える）。
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    fake = H.write_fake_claude(tmp_path / "fake-claude")
    out = tmp_path / "out.json"
    env = H.base_env(tmp_path, ANTHROPIC_BASE_URL=server.url, FAKE_CLAUDE_OUT=str(out))
    proc = H.run_launcher(["--claude-bin", str(fake), "--", "-p", "x"], env, cwd=project)
    assert proc.returncode == 0, proc.stderr
    observed = json.loads(out.read_text(encoding="utf-8"))
    assert Path(observed["cwd"]).resolve() == project.resolve()
    assert (Path(observed["cwd"]) / ".claude" / "settings.json").is_file()
    # user config root は ambient のまま（Skills / SubAgents / hooks / plugins / MCP を Native と共有）。
    assert observed["env"]["CLAUDE_CONFIG_DIR"] == env["CLAUDE_CONFIG_DIR"]
    assert observed["env"]["HOME"] == env["HOME"]
    # launcher は project/user settings を書き換えない。
    assert not (project / ".claude" / "settings.local.json").exists()
    assert not (Path(env["CLAUDE_CONFIG_DIR"]) / "settings.local.json").exists()


def test_ambient_surface_github_auth_environment_reaches_the_child(tmp_path, server):
    _proc, observed, _env = _launch(
        tmp_path, server, GH_CONFIG_DIR=str(tmp_path / "gh"), GH_TOKEN="t", GH_HOST="github.com"
    )
    for key in ("GH_CONFIG_DIR", "GH_TOKEN", "GH_HOST"):
        assert key in observed["env"]


def test_ambient_surface_claude_exit_code_and_stdin_pass_through(tmp_path, server):
    fake = H.write_fake_claude(tmp_path / "fake-claude")
    env = H.base_env(
        tmp_path, ANTHROPIC_BASE_URL=server.url, FAKE_CLAUDE_OUT=str(tmp_path / "o.json"), FAKE_CLAUDE_EXIT="37"
    )
    proc = H.run_launcher(["--claude-bin", str(fake), "--", "-p"], env)
    assert proc.returncode == 37


# ---------------------------------------------------------------------------
# AC8 -- the legacy launcher is not kept as a fallback
# ---------------------------------------------------------------------------


def test_legacy_launcher_not_a_fallback_no_switch_selects_old_behavior(tmp_path, server):
    # 旧 profile へ戻る option / env は存在しない。
    for option in ("--legacy", "--legacy-full", "--isolated", "--full"):
        proc = H.run_launcher([option], H.base_env(tmp_path, ANTHROPIC_BASE_URL=server.url))
        assert proc.returncode == 2, option
        assert "unknown_launcher_option" in proc.stderr
    # ambient な「legacy」系 env を与えても default path は変わらない。
    _proc, observed, env = _launch(
        tmp_path,
        server,
        CLAUDE_GPT_LEGACY="1",
        CLAUDE_GPT_ISOLATE_HOME="1",
        CLAUDE_GPT_RUNTIME_SMOKE_HOOKS="subagent-start-stop",
    )
    assert observed["env"]["HOME"] == env["HOME"]
    assert observed["argv"] == []


def test_legacy_launcher_not_a_fallback_removed_files_stay_removed():
    # `auto_mode_canary.py` は意図的に含めない: Allowed Paths 外の consumer
    # （`.claude/agents/tests/test_issue_editor_runtime_smoke.py`）が import しているため、
    # blind delete せず follow-up で整理する（Issue #2925 Allowed Paths 注記）。
    removed = (
        "latitude_hook.py", "live_issue_create_canary.sh",
        "test_launch_strict_mcp_config_normalization.py", "test_launch_transport_policy.py",
    )
    for name in removed:
        assert not (H.SCRIPT_DIR / name).exists(), name
    assert not any(H.SCRIPT_DIR.glob("*legacy*")) and not any(H.SCRIPT_DIR.glob("*.orig"))
    assert not any((H.SCRIPT_DIR / "tests").glob("test_latitude_*"))


def test_legacy_launcher_not_a_fallback_lib_has_no_legacy_profile_functions():
    lib = H.LIB_SH.read_text(encoding="utf-8")
    for token in ("claude_gpt_auto_mode_json_fragment", "claude_gpt_smoke_canary_agents_json_fragment",
                  "claude_gpt_resolve_task_context_state_root", "claude_gpt_claude_isolated_home_dir",
                  "CLAUDE_GPT_FORBIDDEN_EXTRA_FLAGS"):
        assert token not in lib, token


# ---------------------------------------------------------------------------
# AC9(a) -- role routing and Auto mode compatibility env are pinned
# ---------------------------------------------------------------------------


def test_role_routing_and_auto_mode_env_values(tmp_path, server):
    _proc, observed, _env = _launch(tmp_path, server)
    child_env = observed["env"]
    assert _strip_hint(child_env["ANTHROPIC_DEFAULT_OPUS_MODEL"]) == "gpt-6-sol"
    assert _strip_hint(child_env["ANTHROPIC_DEFAULT_SONNET_MODEL"]) == "gpt-6-sol"
    assert _strip_hint(child_env["ANTHROPIC_DEFAULT_HAIKU_MODEL"]) == "gpt-6-luna"
    assert _strip_hint(child_env["ANTHROPIC_MODEL"]) == "gpt-6-sol"
    assert _strip_hint(child_env["ANTHROPIC_SMALL_FAST_MODEL"]) == "gpt-6-luna"
    assert child_env["CLAUDE_CODE_AUTO_MODE_SERVER"] == "0"
    assert "CCP_AUTO_REVIEW_MODEL" not in child_env


def test_role_routing_and_auto_mode_env_overrides_a_hostile_ambient_value(tmp_path, server):
    # ambient が Auto mode server 経路を有効化していても、現行 contract の 0 で上書きされる。
    _proc, observed, _env = _launch(tmp_path, server, CLAUDE_CODE_AUTO_MODE_SERVER="1")
    assert observed["env"]["CLAUDE_CODE_AUTO_MODE_SERVER"] == "0"


def test_role_routing_and_auto_mode_env_agent_models_resolve_to_pinned_aliases(tmp_path, server):
    # `.claude/agents/*.md` の model alias が、固定された role routing の解決先へ届くこと。
    resolved = {"sonnet": "gpt-6-sol", "opus": "gpt-6-sol", "haiku": "gpt-6-luna"}
    _proc, observed, _env = _launch(tmp_path, server)
    seen_roles = set()
    for agent in (H.REPO_ROOT / ".claude" / "agents").glob("*.md"):
        match = re.search(r"^model:\s*(\w+)\s*$", agent.read_text(encoding="utf-8"), re.MULTILINE)
        if not match:
            continue
        role = match.group(1)
        assert role in resolved, f"{agent.name}: unexpected model alias {role!r}"
        seen_roles.add(role)
        assert _strip_hint(observed["env"][f"ANTHROPIC_DEFAULT_{role.upper()}_MODEL"]) == resolved[role]
    assert {"sonnet", "haiku"} <= seen_roles


def test_role_routing_and_auto_mode_env_launcher_source_never_sets_ccp_auto_review_model():
    for name in ("launch.sh", "lib.sh", "preflight.sh", "runtime_smoke_test.sh", "repair_proxy.sh"):
        assert "CCP_AUTO_REVIEW_MODEL" not in _active_code(H.SCRIPT_DIR / name), name
