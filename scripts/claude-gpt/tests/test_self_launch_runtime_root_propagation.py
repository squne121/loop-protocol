"""scripts/claude-gpt/tests/test_self_launch_runtime_root_propagation.py

Issue #2455: Claude-GPT launcher が child Claude process へ canonical
runtime root（`CLAUDE_GPT_HOME`）を明示的に export しないため、child Claude
から同じ `launch.sh`/`lib.sh` を self-launch すると `CLAUDE_GPT_HOME` が
child の isolated `HOME` を基準に再導出されてしまう（nested
`<isolated-claude-home>/.claude-gpt` root への再基準化）。

- AC2（fixture-level）: `lib.sh` の resolution ロジック単体（no subprocess
  chain）。既存 `_latitude_check_only_helper.run_check_only()` のように
  `CLAUDE_GPT_HOME` を事前設定するヘルパーは defect を隠すため使わない
  （fake proxy の component 自体だけ再利用する）。
- AC3（subprocess-level）: 実 `launch.sh` を fake authenticated proxy + fake
  claude で normal mode 起動し、fake claude が同じ `launch.sh --check-only`
  を self-launch する実 process boundary を再現する。outer/nested の両方が
  同じ `<root>/proxy-config` / `<root>/proxy-home` を参照することを、
  test-owned log（stderr の `CLAUDE_GPT_PROXY_LOG=` 行、nested の
  `--check-only` JSON stdout）から独立に検証する。
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # scripts/claude-gpt/
LAUNCH_SH = SCRIPT_DIR / "launch.sh"
LIB_SH = SCRIPT_DIR / "lib.sh"

# --- Reuse ONLY the fake-proxy component from the existing Latitude helper
#     (never its `run_check_only()` convenience wrapper, which presets
#     `CLAUDE_GPT_HOME` and would hide the exact defect under test here --
#     Issue #2455 AC2). Unique module name to avoid sys.modules collisions
#     with other test files that also load this same helper path. ---
_HELPER_PATH = Path(__file__).resolve().parent / "_latitude_check_only_helper.py"
_spec = importlib.util.spec_from_file_location(
    "claude_gpt_latitude_check_only_helper_2455_runtime_root", _HELPER_PATH
)
_helper = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules[_spec.name] = _helper
_spec.loader.exec_module(_helper)

FAKE_PROXY_SOURCE = _helper.FAKE_PROXY_SOURCE
write_executable = _helper.write_executable


# ---------------------------------------------------------------------------
# Shell-syntax preflight (bash -n equivalent, run as subprocess-based pytest
# cases per the Issue's Verification Commands note -- `bash` itself is
# outside the VC preflight allowlist, so it must never appear as a bare VC
# line).
# ---------------------------------------------------------------------------


def test_launch_sh_passes_bash_syntax_check():
    """GIVEN scripts/claude-gpt/launch.sh
    WHEN `bash -n` を実行する
    THEN 構文エラーなしで exit 0 になる
    """
    result = subprocess.run(
        ["bash", "-n", str(LAUNCH_SH)], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stderr


def test_lib_sh_passes_bash_syntax_check():
    """GIVEN scripts/claude-gpt/lib.sh
    WHEN `bash -n` を実行する
    THEN 構文エラーなしで exit 0 になる
    """
    result = subprocess.run(
        ["bash", "-n", str(LIB_SH)], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# AC2: fixture-level, lib.sh 単体の resolution 検証（fast, no subprocess
# chain across launch.sh/proxy/claude -- only sourcing lib.sh itself）。
# ---------------------------------------------------------------------------


def _minimal_env(*, home: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    """A deliberately minimal process environment: only PATH + HOME, so that
    `CLAUDE_GPT_HOME`/`CLAUDE_GPT_HOME_ROOT` are genuinely absent (never
    merely overridden) unless `extra` adds them back explicitly."""
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home)}
    if extra:
        env.update(extra)
    return env


def _resolve_claude_gpt_home(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    script = f'. "{LIB_SH}"; printf "%s" "$CLAUDE_GPT_HOME"'
    return subprocess.run(
        ["sh", "-c", script], env=env, capture_output=True, text=True, timeout=20
    )


def test_ac2_lib_sh_resolves_home_based_root_when_carrier_and_mirror_both_unset(tmp_path):
    """GIVEN HOME=<test-owned-native-home> のみが設定され、CLAUDE_GPT_HOME と
    CLAUDE_GPT_HOME_ROOT は unset である
    WHEN lib.sh を source し $CLAUDE_GPT_HOME を確認する
    THEN <test-owned-native-home>/.claude-gpt に解決される
    """
    native_home = tmp_path / "native-home"
    native_home.mkdir()
    env = _minimal_env(home=native_home)
    assert "CLAUDE_GPT_HOME" not in env
    assert "CLAUDE_GPT_HOME_ROOT" not in env

    result = _resolve_claude_gpt_home(env)
    assert result.returncode == 0, result.stderr
    assert result.stdout == str(native_home / ".claude-gpt")


def test_ac2_lib_sh_does_not_promote_claude_gpt_home_root_to_fallback_authority(tmp_path):
    """GIVEN CLAUDE_GPT_HOME は unset のまま CLAUDE_GPT_HOME_ROOT に、実 root と
    区別可能な別の distinguishable path が設定されている（derived telemetry
    mirror が本来 authority ではないことの回帰検出。#2455 In Scope）
    WHEN lib.sh を source し $CLAUDE_GPT_HOME を確認する
    THEN 依然として <test-owned-native-home>/.claude-gpt に解決され、
    CLAUDE_GPT_HOME_ROOT の値には一致しない
    """
    native_home = tmp_path / "native-home"
    native_home.mkdir()
    distinguishable_mirror = tmp_path / "distinguishable-telemetry-mirror-root"
    env = _minimal_env(
        home=native_home, extra={"CLAUDE_GPT_HOME_ROOT": str(distinguishable_mirror)}
    )
    assert "CLAUDE_GPT_HOME" not in env

    result = _resolve_claude_gpt_home(env)
    assert result.returncode == 0, result.stderr
    resolved = result.stdout
    assert resolved == str(native_home / ".claude-gpt")
    assert resolved != str(distinguishable_mirror)


# ---------------------------------------------------------------------------
# AC1 (fixture-level corollary of AC3): the single restoration path is an
# explicit `export CLAUDE_GPT_HOME` -- verified directly by observing a
# grandchild process's own environment via `env`, never by re-implementing
# the check via string search on lib.sh's source.
# ---------------------------------------------------------------------------


def test_ac1_lib_sh_exports_claude_gpt_home_so_child_processes_inherit_it(tmp_path):
    """GIVEN CLAUDE_GPT_HOME を持たないプロセスで lib.sh を source する
    WHEN 直後に `env` を実行する子プロセスを起動する（子プロセスへの明示的な
    値渡しは一切しない）
    THEN 子プロセスの envp に CLAUDE_GPT_HOME= が現れる。これは単なる shell
    変数ではなく、実際に export 属性を持つことの直接証拠になる
    """
    native_home = tmp_path / "native-home"
    native_home.mkdir()
    env = _minimal_env(home=native_home)
    script = f'. "{LIB_SH}"; env | grep -c "^CLAUDE_GPT_HOME="'
    result = subprocess.run(
        ["sh", "-c", script], env=env, capture_output=True, text=True, timeout=20
    )
    assert result.stdout.strip() == "1", (result.stdout, result.stderr)


# ---------------------------------------------------------------------------
# AC3: subprocess-level, 実 process boundary の検証。
# ---------------------------------------------------------------------------

FAKE_CLAUDE_SELF_LAUNCH_SOURCE = r"""#!/usr/bin/env python3
import json
import os
import subprocess
import sys

argv = sys.argv[1:]

# --- preflight.sh --auto-mode-check invocations (--version / `auto-mode
#     defaults` / `auto-mode config`) must succeed so the outer normal-mode
#     launch reaches the point where it actually execs the main claude
#     invocation below. ---
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

# --- Main invocation: this IS the "child Claude" process under test. Record
#     this process's OWN observed environment (never mutate it) before
#     self-launching the SAME launch.sh --check-only, reproducing Issue
#     #2455's outer -> isolated child HOME -> nested launcher scenario. ---
outer_view_path = os.environ.get("FAKE_CLAUDE_OUTER_VIEW_PATH")
if outer_view_path:
    view = {
        "home": os.environ.get("HOME"),
        "claude_gpt_home_present": "CLAUDE_GPT_HOME" in os.environ,
        "claude_gpt_home_value": os.environ.get("CLAUDE_GPT_HOME"),
    }
    with open(outer_view_path, "w", encoding="utf-8") as fh:
        json.dump(view, fh)

nested_env = dict(os.environ)
if os.environ.get("FAKE_CLAUDE_UNSET_CARRIER") == "1":
    # Negative control (Issue #2455 AC3): deliberately unset the carrier in
    # the child before self-launching, to prove this subprocess boundary
    # actually DETECTS a deviation to the nested root when the carrier is
    # absent (never a tautological harness).
    nested_env.pop("CLAUDE_GPT_HOME", None)

launch_sh_path = os.environ["FAKE_CLAUDE_SELF_LAUNCH_PATH"]
nested = subprocess.run(
    [launch_sh_path, "--check-only"],
    env=nested_env,
    capture_output=True,
    text=True,
    timeout=45,
)

nested_stdout_path = os.environ.get("FAKE_CLAUDE_NESTED_STDOUT_PATH")
if nested_stdout_path:
    with open(nested_stdout_path, "w", encoding="utf-8") as fh:
        fh.write(nested.stdout)
nested_stderr_path = os.environ.get("FAKE_CLAUDE_NESTED_STDERR_PATH")
if nested_stderr_path:
    with open(nested_stderr_path, "w", encoding="utf-8") as fh:
        fh.write(nested.stderr)

sys.exit(nested.returncode)
"""


def _run_self_launch(tmp_path: Path, *, unset_carrier_in_child: bool = False):
    """Run a real `launch.sh` normal-mode invocation whose `claude` child is
    the fake self-launching binary above. Never presets `CLAUDE_GPT_HOME`
    anywhere in the outer environment (only `HOME`, matching AC2's
    defect-hiding prohibition) -- the outer root is derived exactly the way
    a real ambient shell would derive it."""
    native_home = tmp_path / "native-home"
    native_home.mkdir()

    fake_proxy = write_executable(tmp_path / "fake-claude-code-proxy", FAKE_PROXY_SOURCE)
    fake_claude = write_executable(
        tmp_path / "fake-claude-self-launch", FAKE_CLAUDE_SELF_LAUNCH_SOURCE
    )

    outer_view_path = tmp_path / "outer-view.json"
    nested_stdout_path = tmp_path / "nested-stdout.json"
    nested_stderr_path = tmp_path / "nested-stderr.log"

    env = dict(os.environ)
    env.pop("CLAUDE_GPT_HOME", None)
    env.pop("CLAUDE_GPT_HOME_ROOT", None)
    env["HOME"] = str(native_home)
    env["CLAUDE_GPT_PROXY_BIN"] = str(fake_proxy)
    env["CLAUDE_GPT_CLAUDE_BIN"] = str(fake_claude)
    env["FAKE_CLAUDE_SELF_LAUNCH_PATH"] = str(LAUNCH_SH)
    env["FAKE_CLAUDE_OUTER_VIEW_PATH"] = str(outer_view_path)
    env["FAKE_CLAUDE_NESTED_STDOUT_PATH"] = str(nested_stdout_path)
    env["FAKE_CLAUDE_NESTED_STDERR_PATH"] = str(nested_stderr_path)
    if unset_carrier_in_child:
        env["FAKE_CLAUDE_UNSET_CARRIER"] = "1"

    result = subprocess.run(
        [str(LAUNCH_SH), "--", "-p", "hello", "--output-format", "text", "--no-session-persistence"],
        cwd=str(SCRIPT_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    expected_root = native_home / ".claude-gpt"
    return result, expected_root, outer_view_path, nested_stdout_path, nested_stderr_path


def _outer_resolved_root_from_stderr(stderr: str) -> Path:
    match = re.search(r"^CLAUDE_GPT_PROXY_LOG=(.+)$", stderr, re.MULTILINE)
    assert match, f"CLAUDE_GPT_PROXY_LOG= line not found in outer stderr:\n{stderr}"
    proxy_log_path = Path(match.group(1))
    # proxy_log_path == <root>/state/launcher-proxy-<tag>.log
    return proxy_log_path.parent.parent


def test_ac3_self_launch_preserves_outer_canonical_runtime_root(tmp_path):
    """GIVEN 実 launch.sh を fake authenticated proxy + fake claude で normal
    mode 起動し、fake claude が outer から受け取った環境を変更せず同じ
    launch.sh --check-only を self-launch する
    WHEN outer と nested それぞれが解決した CLAUDE_GPT_HOME root を比較する
    THEN 両方とも同一の <test-owned-native-home>/.claude-gpt root を参照し、
    nested <isolated-claude-home>/.claude-gpt へ再基準化されない。加えて
    child の HOME は isolated <root>/claude-home のまま維持され、
    CLAUDE_GPT_HOME == <root> が child 自身の環境で同時に成立する（AC1）。
    """
    result, expected_root, outer_view_path, nested_stdout_path, nested_stderr_path = (
        _run_self_launch(tmp_path)
    )
    assert result.returncode == 0, result.stdout + "\n---stderr---\n" + result.stderr

    # --- outer's own resolved root, independently derived from its stderr
    #     side-channel log line (never from the same code path under test). ---
    outer_root = _outer_resolved_root_from_stderr(result.stderr)
    assert outer_root == expected_root, (outer_root, expected_root)
    assert (outer_root / "proxy-config").is_dir()
    assert (outer_root / "proxy-home").is_dir()

    # --- child Claude's own observed environment (AC1: isolated HOME and
    #     CLAUDE_GPT_HOME root authority coexist as distinct values). ---
    assert outer_view_path.exists(), "fake claude was never invoked as the main process"
    outer_view = json.loads(outer_view_path.read_text(encoding="utf-8"))
    assert outer_view["home"] == str(expected_root / "claude-home")
    assert outer_view["claude_gpt_home_present"] is True
    assert outer_view["claude_gpt_home_value"] == str(expected_root)

    # --- nested self-launch's own resolution (the actual defect surface). ---
    assert nested_stdout_path.exists(), (
        "nested launch.sh --check-only produced no stdout: "
        + nested_stderr_path.read_text(encoding="utf-8")
    )
    nested_payload = json.loads(nested_stdout_path.read_text(encoding="utf-8"))
    assert nested_payload["schema"] == "CLAUDE_GPT_LAUNCH_RESULT_V1"
    assert nested_payload["status"] == "ok"
    assert nested_payload["mode"] == "check_only"

    nested_settings_path = Path(nested_payload["settings_path"])
    # nested_settings_path == <nested_root>/claude/settings.local.json
    nested_root = nested_settings_path.parent.parent
    assert nested_root == expected_root, (nested_root, expected_root)
    assert Path(nested_payload["proxy_home_dir"]) == expected_root / "proxy-home"
    assert (
        Path(nested_payload["preflight"]["canonical_paths"]["proxy_config_dir"])
        == expected_root / "proxy-config"
    )


def test_ac3_negative_control_unset_carrier_deviates_to_nested_root(tmp_path):
    """GIVEN 上記と同じ subprocess boundary だが、fake claude が self-launch
    直前に意図的に CLAUDE_GPT_HOME を child env から unset する（carrier 不在）
    WHEN nested launch.sh --check-only が解決した root を確認する
    THEN nested は <root>/claude-home/.claude-gpt という誤った（nested）root
    へ逸脱する（既存 lib.sh の HOME-based derivation 自体は正しく機能して
    いることの negative control。harness がタウトロジーでないことの証拠）
    """
    result, expected_root, outer_view_path, nested_stdout_path, nested_stderr_path = (
        _run_self_launch(tmp_path, unset_carrier_in_child=True)
    )
    assert result.returncode == 0, result.stdout + "\n---stderr---\n" + result.stderr
    assert nested_stdout_path.exists(), nested_stderr_path.read_text(encoding="utf-8")
    nested_payload = json.loads(nested_stdout_path.read_text(encoding="utf-8"))
    assert nested_payload["status"] == "ok"

    nested_settings_path = Path(nested_payload["settings_path"])
    nested_root = nested_settings_path.parent.parent
    deviated_root = expected_root / "claude-home" / ".claude-gpt"
    assert nested_root == deviated_root, (nested_root, deviated_root)
    assert nested_root != expected_root


# ---------------------------------------------------------------------------
# AC6 static regression guard: the GH_CONFIG_DIR carrier line (#2403/PR
# #2407) must remain untouched by this Issue's change.
# ---------------------------------------------------------------------------


def test_ac6_gh_config_dir_carrier_line_unchanged():
    """GIVEN launch.sh のソース
    WHEN GH_CONFIG_DIR export 行を確認する
    THEN #2403/PR #2407 の finite carrier 契約どおり、ambient
    CLAUDE_NATIVE_GH_CONFIG_DIR_TARGET をそのまま export する行が残っている
    """
    source = LAUNCH_SH.read_text(encoding="utf-8")
    assert 'export GH_CONFIG_DIR="$CLAUDE_NATIVE_GH_CONFIG_DIR_TARGET"' in source
