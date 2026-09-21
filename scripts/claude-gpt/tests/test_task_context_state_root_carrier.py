"""scripts/claude-gpt/tests/test_task_context_state_root_carrier.py

Issue #2567 AC1/AC2/AC3 -- the Claude-GPT launcher resolves the Task Context
canonical state root from AMBIENT XDG/HOME *before* it swaps HOME/XDG to the
isolated Claude-GPT profile, and explicitly carries the result (plus a fixed
`LOOP_TASK_CONTEXT_RUNTIME_VARIANT=claude_gpt`) into the child `claude`
process's own environment -- so a normal Claude-GPT operator resolves the
SAME canonical Task Context DB Native Claude would (AC1/AC2), and an
inherited override (e.g. `worktree-agent-runtime-smoke`'s
`LOOP_TASK_CONTEXT_STATE_ROOT`/`LOOP_TASK_CONTEXT_SCOPE`) is never
overwritten (AC3).

- AC2 (fixture-level): `lib.sh`'s `claude_gpt_resolve_task_context_state_root`
  resolution logic in isolation (no subprocess chain through the whole
  launcher).
- AC1/AC2 (subprocess-level): a real `launch.sh` normal-mode invocation with
  a fake authenticated proxy + fake `claude` that records ITS OWN observed
  environment before exiting -- the actual process boundary the isolated
  HOME swap crosses.
- AC3 (subprocess-level, negative-control-adjacent): the same real launch
  with `LOOP_TASK_CONTEXT_STATE_ROOT`/`LOOP_TASK_CONTEXT_SCOPE` already set
  in the outer (ambient) environment.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # scripts/claude-gpt/
LAUNCH_SH = SCRIPT_DIR / "launch.sh"
LIB_SH = SCRIPT_DIR / "lib.sh"
REPO_ROOT = SCRIPT_DIR.parent.parent
TASK_CONTEXT_CONFIG_PY = REPO_ROOT / "scripts" / "task-context" / "task_context_config.py"

# --- Reuse ONLY the fake-proxy component from the existing Latitude helper
#     (same pattern as test_self_launch_runtime_root_propagation.py). ---
_HELPER_PATH = Path(__file__).resolve().parent / "_latitude_check_only_helper.py"
_spec = importlib.util.spec_from_file_location(
    "claude_gpt_latitude_check_only_helper_2567_state_root_carrier", _HELPER_PATH
)
_helper = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules[_spec.name] = _helper
_spec.loader.exec_module(_helper)

FAKE_PROXY_SOURCE = _helper.FAKE_PROXY_SOURCE
write_executable = _helper.write_executable


def _resolve_state_root(home: Path) -> Path:
    """Independently derive the expected canonical Task Context state root
    for `home`, using the SAME `task_context_config.resolve_state_root()`
    SSOT the launcher itself calls -- never a hand-rolled duplicate
    computation -- but invoked directly (no subprocess, no launcher) as an
    independent oracle."""
    spec = importlib.util.spec_from_file_location(
        "claude_gpt_task_context_config_oracle_2567", TASK_CONTEXT_CONFIG_PY
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    prior_home_env = os.environ.get("HOME")
    prior_xdg = os.environ.pop("XDG_STATE_HOME", None)
    os.environ["HOME"] = str(home)
    try:
        spec.loader.exec_module(module)
        return module.resolve_state_root(cwd=str(REPO_ROOT))
    finally:
        if prior_home_env is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = prior_home_env
        if prior_xdg is not None:
            os.environ["XDG_STATE_HOME"] = prior_xdg


# ---------------------------------------------------------------------------
# AC2: fixture-level, lib.sh 単体の resolution 検証。
# ---------------------------------------------------------------------------


def test_ac2_lib_sh_resolves_ambient_home_derived_state_root(tmp_path):
    """GIVEN HOME=<ambient-home> のみが設定され、LOOP_TASK_CONTEXT_STATE_ROOT
    は unset である
    WHEN lib.sh の claude_gpt_resolve_task_context_state_root を呼ぶ
    THEN task_context_config.resolve_state_root() を ambient HOME で直接
    呼んだ場合と同じ絶対パスへ解決される
    """
    ambient_home = tmp_path / "ambient-home"
    ambient_home.mkdir()
    expected = _resolve_state_root(ambient_home)

    script = (
        f'. "{LIB_SH}"; '
        f'claude_gpt_resolve_task_context_state_root "{TASK_CONTEXT_CONFIG_PY}" "{REPO_ROOT}"'
    )
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(ambient_home)}
    result = subprocess.run(["sh", "-c", script], env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout == str(expected)


def test_ac2_lib_sh_resolves_empty_when_python3_unavailable(tmp_path):
    """GIVEN python3 が PATH 上に存在しない
    WHEN claude_gpt_resolve_task_context_state_root を呼ぶ
    THEN fail-open で空文字列を返す（launch 自体を止めない）
    """
    ambient_home = tmp_path / "ambient-home"
    ambient_home.mkdir()
    empty_path_dir = tmp_path / "empty-path"
    empty_path_dir.mkdir()

    sh_bin = shutil.which("sh") or "/bin/sh"
    script = (
        f'. "{LIB_SH}"; '
        f'claude_gpt_resolve_task_context_state_root "{TASK_CONTEXT_CONFIG_PY}" "{REPO_ROOT}"'
    )
    # PATH only contains the empty directory, so `command -v python3` inside
    # lib.sh genuinely fails -- but the harness still invokes the real `sh`
    # binary by absolute path (never relying on PATH lookup for `sh` itself).
    env = {"PATH": str(empty_path_dir), "HOME": str(ambient_home)}
    result = subprocess.run([sh_bin, "-c", script], env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


# ---------------------------------------------------------------------------
# AC1/AC2/AC3: subprocess-level, 実 launch.sh normal-mode invocation.
# ---------------------------------------------------------------------------

FAKE_CLAUDE_ENV_RECORDER_SOURCE = r"""#!/usr/bin/env python3
import json
import os
import sys

argv = sys.argv[1:]

# --- preflight.sh --auto-mode-check invocations must succeed so the outer
#     normal-mode launch reaches the point where it actually execs the main
#     claude invocation below (same stub shape as
#     test_self_launch_runtime_root_propagation.py). ---
if argv and argv[0] == "--version":
    print(os.environ.get("FAKE_CLAUDE_VERSION") or "2.1.211 (Claude Code)")
    sys.exit(0)

if "auto-mode" in argv:
    idx = argv.index("auto-mode")
    subcommand = argv[idx + 1] if idx + 1 < len(argv) else ""
    baseline = {
        "environment": ["defaults-env-baseline"],
        "allow": ["defaults-allow-baseline"],
        "hard_deny": ["defaults-hard-deny-baseline"],
        "soft_deny": ["defaults-soft-deny-baseline"],
        "classifyAllShell": False,
    }
    if subcommand == "defaults":
        print(json.dumps(baseline))
        sys.exit(0)
    if subcommand == "config":
        config = dict(baseline)
        settings_path = None
        for i, tok in enumerate(argv):
            if tok == "--settings" and i + 1 < len(argv):
                settings_path = argv[i + 1]
        if settings_path and os.path.exists(settings_path):
            with open(settings_path, encoding="utf-8") as fh:
                settings = json.load(fh)
            auto_mode = settings.get("autoMode", {})

            def _merge(key):
                entries = auto_mode.get(key)
                if entries is None:
                    return
                merged = []
                for entry in entries:
                    if entry == "$defaults":
                        merged.extend(baseline[key])
                    else:
                        merged.append(entry)
                config[key] = merged

            _merge("environment")
            _merge("allow")
            _merge("hard_deny")
            if auto_mode.get("classifyAllShell"):
                config["classifyAllShell"] = True
        print(json.dumps(config))
        sys.exit(0)

# --- Main invocation: record this process's OWN observed environment
#     (never mutate/echo secrets -- only the three carrier vars + HOME under
#     test) and exit 0. ---
view_path = os.environ.get("FAKE_CLAUDE_ENV_VIEW_PATH")
if view_path:
    view = {
        "home": os.environ.get("HOME"),
        "state_root_present": "LOOP_TASK_CONTEXT_STATE_ROOT" in os.environ,
        "state_root_value": os.environ.get("LOOP_TASK_CONTEXT_STATE_ROOT"),
        "runtime_variant_present": "LOOP_TASK_CONTEXT_RUNTIME_VARIANT" in os.environ,
        "runtime_variant_value": os.environ.get("LOOP_TASK_CONTEXT_RUNTIME_VARIANT"),
        "scope_present": "LOOP_TASK_CONTEXT_SCOPE" in os.environ,
        "scope_value": os.environ.get("LOOP_TASK_CONTEXT_SCOPE"),
    }
    with open(view_path, "w", encoding="utf-8") as fh:
        json.dump(view, fh)
sys.exit(0)
"""


def _run_normal_launch(tmp_path: Path, *, extra_outer_env: dict[str, str] | None = None):
    ambient_home = tmp_path / "ambient-home"
    ambient_home.mkdir()

    fake_proxy = write_executable(tmp_path / "fake-claude-code-proxy", FAKE_PROXY_SOURCE)
    fake_claude = write_executable(
        tmp_path / "fake-claude-env-recorder", FAKE_CLAUDE_ENV_RECORDER_SOURCE
    )
    env_view_path = tmp_path / "env-view.json"

    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(ambient_home),
        "CLAUDE_GPT_PROXY_BIN": str(fake_proxy),
        "CLAUDE_GPT_CLAUDE_BIN": str(fake_claude),
        "FAKE_CLAUDE_ENV_VIEW_PATH": str(env_view_path),
    }
    if extra_outer_env:
        env.update(extra_outer_env)

    result = subprocess.run(
        [str(LAUNCH_SH), "--", "-p", "hello", "--output-format", "text", "--no-session-persistence"],
        cwd=str(SCRIPT_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    return result, ambient_home, env_view_path


def test_ac1_ac2_normal_launch_carries_ambient_derived_state_root_and_variant_to_child(tmp_path):
    """GIVEN LOOP_TASK_CONTEXT_STATE_ROOT が outer env で unset のまま、実
    launch.sh を normal mode（fake authenticated proxy + fake claude）で
    起動する
    WHEN fake claude（child）が自身の環境を観測する
    THEN child が観測する LOOP_TASK_CONTEXT_STATE_ROOT は ambient HOME から
    直接 resolve_state_root() を呼んだ場合と同一の絶対パスであり、isolated
    HOME 配下の別 root ではない。LOOP_TASK_CONTEXT_RUNTIME_VARIANT=claude_gpt
    も child 環境に現れる。
    """
    result, ambient_home, env_view_path = _run_normal_launch(tmp_path)
    assert result.returncode == 0, result.stdout + "\n---stderr---\n" + result.stderr
    assert env_view_path.exists(), "fake claude (env recorder) was never invoked as the main process"

    view = json.loads(env_view_path.read_text(encoding="utf-8"))
    expected_state_root = _resolve_state_root(ambient_home)

    assert view["state_root_present"] is True
    assert view["state_root_value"] == str(expected_state_root)
    # Never the isolated-HOME-derived root the child's OWN (isolated) HOME
    # would otherwise resolve to.
    isolated_home = Path(view["home"])
    assert view["home"] != str(ambient_home)
    assert view["state_root_value"] != str(isolated_home / ".local" / "state")

    assert view["runtime_variant_present"] is True
    assert view["runtime_variant_value"] == "claude_gpt"


def test_ac3_inherited_state_root_and_scope_are_preserved_verbatim(tmp_path):
    """GIVEN outer env に LOOP_TASK_CONTEXT_STATE_ROOT（runtime-smoke override
    相当）と LOOP_TASK_CONTEXT_SCOPE=runtime_smoke が既に設定されている
    WHEN 実 launch.sh を normal mode で起動する
    THEN child が観測する両方の値は outer の値のまま一切変更されない
    （AC3: launcher 通過後も上書きされない）
    """
    inherited_root = tmp_path / "inherited-runtime-smoke-state-root"
    inherited_root.mkdir()
    result, _ambient_home, env_view_path = _run_normal_launch(
        tmp_path,
        extra_outer_env={
            "LOOP_TASK_CONTEXT_STATE_ROOT": str(inherited_root),
            "LOOP_TASK_CONTEXT_SCOPE": "runtime_smoke",
        },
    )
    assert result.returncode == 0, result.stdout + "\n---stderr---\n" + result.stderr
    assert env_view_path.exists(), env_view_path

    view = json.loads(env_view_path.read_text(encoding="utf-8"))
    assert view["state_root_value"] == str(inherited_root)
    assert view["scope_present"] is True
    assert view["scope_value"] == "runtime_smoke"
    assert view["runtime_variant_value"] == "claude_gpt"
