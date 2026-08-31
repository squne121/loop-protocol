"""scripts/claude-gpt/tests/test_native_settings_path_precedence.py

Issue #2448: `launch.sh` の Native Latitude settings.json path 解決
（`CLAUDE_NATIVE_LATITUDE_SETTINGS_PATH_TARGET`）を、単純な
`${HOME}/.claude/settings.json` 固定から、isolated HOME / Claude-GPT
config-root への切替 *前* の precedence 解決へ置き換える
（PR #2439 owner review P2 note の follow-up、OWNER review
issuecomment-5477101989 の P0 self-launch 誤認指摘を踏まえた設計）。

- AC1 (fixture-level): `lib.sh` の
  `claude_gpt_resolve_native_settings_path()` 単体の precedence 検証
  （inherited carrier -> ambient CLAUDE_CONFIG_DIR -> HOME fallback）。
- AC2/AC3 (fixture-level): 実 `launch.sh --check-only` を CLAUDE_CONFIG_DIR
  を与えて起動し、生成された settings.local.json の `env` から
  `CLAUDE_GPT_NATIVE_SETTINGS_PATH` / `LATITUDE_PROJECT` を独立に読み取り、
  単一 authority が一貫して参照されることを検証する。
- AC5 (fixture-level): 通常ケース（CLAUDE_CONFIG_DIR も inherited carrier も
  未設定）の HOME フォールバックと、settings file 不在／malformed JSON でも
  既存の fail-open semantics が壊れないことを検証する。
- AC4 (runtime, `<!-- runtime-verification: true -->`): 実 `launch.sh` を
  fake authenticated proxy + fake claude で normal mode 起動し、fake claude
  が outer から受け取った resolved carrier を自身の settings 経由で継承した
  上で同じ `launch.sh --check-only` を self-launch する実 process boundary を
  再現し、outer/nested が同一の Native settings path identity を参照する
  ことを、test-owned artifact（stdout/stderr 側チャネル）から独立に検証する。
  negative control として carrier 非伝播時に isolated Claude-GPT 自身の
  config dir へ誤って逸脱することも確認する（harness がタウトロジーでない
  ことの証拠）。
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # scripts/claude-gpt/
LAUNCH_SH = SCRIPT_DIR / "launch.sh"
LIB_SH = SCRIPT_DIR / "lib.sh"

# --- Reuse ONLY the fake-proxy component from the existing Latitude helper
#     (never its `run_check_only()` convenience wrapper, which presets HOME
#     under a fixed `~/.claude` shape and would not exercise the
#     CLAUDE_CONFIG_DIR precedence path under test here). Unique module name
#     to avoid sys.modules collisions with other test files that also load
#     this same helper path (existing repo pitfall). ---
_HELPER_PATH = Path(__file__).resolve().parent / "_latitude_check_only_helper.py"
_spec = importlib.util.spec_from_file_location(
    "claude_gpt_latitude_check_only_helper_2448_native_settings_path", _HELPER_PATH
)
_helper = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules[_spec.name] = _helper
_spec.loader.exec_module(_helper)

FAKE_PROXY_SOURCE = _helper.FAKE_PROXY_SOURCE
write_executable = _helper.write_executable


# ---------------------------------------------------------------------------
# Shell-syntax preflight (`bash` itself is outside the VC preflight
# allowlist, so it must never appear as a bare VC line -- run as
# subprocess-based pytest cases, mirroring Issue #2455's precedent).
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
# AC1: `claude_gpt_resolve_native_settings_path()` の precedence 単体検証
# （lib.sh を source するだけの fast path、launch.sh/proxy/claude の
# subprocess chain は含まない）。
# ---------------------------------------------------------------------------


def _resolve_native_settings_path(
    inherited: str, config_dir: str, home: str
) -> subprocess.CompletedProcess[str]:
    script = (
        f'. "{LIB_SH}"; '
        f'claude_gpt_resolve_native_settings_path "{inherited}" "{config_dir}" "{home}"'
    )
    return subprocess.run(
        ["sh", "-c", script], capture_output=True, text=True, timeout=20
    )


def test_ac1_inherited_carrier_takes_precedence_when_non_empty(tmp_path):
    """GIVEN inherited CLAUDE_GPT_NATIVE_SETTINGS_PATH が非空
    WHEN claude_gpt_resolve_native_settings_path を呼ぶ（ambient
    CLAUDE_CONFIG_DIR / HOME もどちらも別の非空値として与える）
    THEN inherited の値がそのまま採用され、他の2つは一切参照されない
    """
    inherited = str(tmp_path / "carrier" / "settings.json")
    config_dir = str(tmp_path / "native-profile")
    home = str(tmp_path / "native-home")
    result = _resolve_native_settings_path(inherited, config_dir, home)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == inherited


def test_ac1_ambient_config_dir_used_when_inherited_carrier_absent(tmp_path):
    """GIVEN inherited carrier は空文字列、ambient CLAUDE_CONFIG_DIR は非空
    WHEN claude_gpt_resolve_native_settings_path を呼ぶ
    THEN "${CLAUDE_CONFIG_DIR}/settings.json" が採用される
    """
    config_dir = str(tmp_path / "native-profile")
    home = str(tmp_path / "native-home")
    result = _resolve_native_settings_path("", config_dir, home)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"{config_dir}/settings.json"


def test_ac1_and_ac5_home_fallback_when_both_unset(tmp_path):
    """GIVEN inherited carrier も ambient CLAUDE_CONFIG_DIR も空文字列
    WHEN claude_gpt_resolve_native_settings_path を呼ぶ
    THEN "${HOME}/.claude/settings.json" フォールバックが採用される
    """
    home = str(tmp_path / "native-home")
    result = _resolve_native_settings_path("", "", home)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"{home}/.claude/settings.json"


# ---------------------------------------------------------------------------
# AC2/AC3/AC5 fixture-level: 実 launch.sh --check-only を起動し、生成された
# settings.local.json の env から resolved path / LATITUDE_PROJECT を
# 独立に読み取る。
# ---------------------------------------------------------------------------


def _run_check_only(
    tmp_path: Path,
    *,
    native_home: Path,
    claude_config_dir: str | None,
    inherited_carrier: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    claude_gpt_home = tmp_path / "claude-gpt-home"
    fake_proxy = write_executable(tmp_path / "fake-claude-code-proxy", FAKE_PROXY_SOURCE)

    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(native_home),
        "CLAUDE_GPT_HOME": str(claude_gpt_home),
        "CLAUDE_GPT_PROXY_BIN": str(fake_proxy),
    }
    if claude_config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = claude_config_dir
    if inherited_carrier is not None:
        env["CLAUDE_GPT_NATIVE_SETTINGS_PATH"] = inherited_carrier

    result = subprocess.run(
        [str(LAUNCH_SH), "--check-only"],
        cwd=str(SCRIPT_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=40,
    )
    settings_local = claude_gpt_home / "claude" / "settings.local.json"
    return result, settings_local


def test_ac2_ac3_native_config_dir_settings_path_and_latitude_project_are_consistent(
    tmp_path,
):
    """GIVEN test-owned Native environment で CLAUDE_CONFIG_DIR=<tmp>/native-
    profile を与え、そこに LATITUDE_PROJECT を設定した settings.json を置く
    WHEN launch.sh --check-only を実行する
    THEN 生成された settings.local.json の env は
    CLAUDE_GPT_NATIVE_SETTINGS_PATH == <tmp>/native-profile/settings.json
    （claude_gpt_native_latitude_project() / 生成済み settings.json の
    consumer 双方が同じ path 文字列を単一 authority として参照している証拠）
    を持ち、かつ test-owned LATITUDE_PROJECT が exact に保持されている
    """
    native_home = tmp_path / "native-home"
    native_home.mkdir()
    native_profile = tmp_path / "native-profile"
    native_profile.mkdir()
    (native_profile / "settings.json").write_text(
        json.dumps({"env": {"LATITUDE_PROJECT": "issue-2448-test-project"}}),
        encoding="utf-8",
    )

    result, settings_local = _run_check_only(
        tmp_path, native_home=native_home, claude_config_dir=str(native_profile)
    )
    assert result.returncode == 0, result.stdout + "\n---stderr---\n" + result.stderr
    assert settings_local.exists()
    data = json.loads(settings_local.read_text(encoding="utf-8"))
    env = data.get("env", {})
    assert env.get("CLAUDE_GPT_NATIVE_SETTINGS_PATH") == str(
        native_profile / "settings.json"
    )
    assert env.get("LATITUDE_PROJECT") == "issue-2448-test-project"


def test_ac1_inherited_carrier_used_by_check_only_end_to_end(tmp_path):
    """GIVEN inherited CLAUDE_GPT_NATIVE_SETTINGS_PATH（あるディレクトリの外の
    絶対パス）と、それとは別の ambient CLAUDE_CONFIG_DIR がどちらも設定されて
    いる
    WHEN launch.sh --check-only を実行する
    THEN 生成された settings.local.json の CLAUDE_GPT_NATIVE_SETTINGS_PATH は
    inherited carrier の値のまま維持され、ambient CLAUDE_CONFIG_DIR 側の
    settings.json では上書きされない（precedence #1 が isolated HOME 切替
    より前に効いていることの end-to-end 確認）
    """
    native_home = tmp_path / "native-home"
    native_home.mkdir()
    native_profile = tmp_path / "native-profile"
    native_profile.mkdir()
    (native_profile / "settings.json").write_text(
        json.dumps({"env": {"LATITUDE_PROJECT": "should-not-be-used"}}),
        encoding="utf-8",
    )
    inherited_carrier_path = tmp_path / "carrier-settings" / "settings.json"
    inherited_carrier_path.parent.mkdir()
    inherited_carrier_path.write_text(
        json.dumps({"env": {"LATITUDE_PROJECT": "carrier-project"}}), encoding="utf-8"
    )

    result, settings_local = _run_check_only(
        tmp_path,
        native_home=native_home,
        claude_config_dir=str(native_profile),
        inherited_carrier=str(inherited_carrier_path),
    )
    assert result.returncode == 0, result.stdout + "\n---stderr---\n" + result.stderr
    data = json.loads(settings_local.read_text(encoding="utf-8"))
    env = data.get("env", {})
    assert env.get("CLAUDE_GPT_NATIVE_SETTINGS_PATH") == str(inherited_carrier_path)
    assert env.get("LATITUDE_PROJECT") == "carrier-project"


def test_ac5_home_fallback_when_config_dir_and_carrier_both_unset(tmp_path):
    """GIVEN CLAUDE_CONFIG_DIR も inherited carrier も設定されていない通常
    ケース（native HOME/.claude/settings.json に test-owned
    LATITUDE_PROJECT を置く）
    WHEN launch.sh --check-only を実行する
    THEN CLAUDE_GPT_NATIVE_SETTINGS_PATH は "${HOME}/.claude/settings.json"
    へフォールバックし、そこに置いた LATITUDE_PROJECT が保持される
    """
    native_home = tmp_path / "native-home"
    claude_dir = native_home / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "settings.json").write_text(
        json.dumps({"env": {"LATITUDE_PROJECT": "home-fallback-project"}}),
        encoding="utf-8",
    )

    result, settings_local = _run_check_only(
        tmp_path, native_home=native_home, claude_config_dir=None
    )
    assert result.returncode == 0, result.stdout + "\n---stderr---\n" + result.stderr
    data = json.loads(settings_local.read_text(encoding="utf-8"))
    env = data.get("env", {})
    assert env.get("CLAUDE_GPT_NATIVE_SETTINGS_PATH") == str(
        claude_dir / "settings.json"
    )
    assert env.get("LATITUDE_PROJECT") == "home-fallback-project"


def test_ac5_missing_native_settings_file_is_fail_open(tmp_path):
    """GIVEN CLAUDE_CONFIG_DIR も inherited carrier も設定されておらず、かつ
    HOME 配下に .claude/settings.json 自体が存在しない
    WHEN launch.sh --check-only を実行する
    THEN launch.sh 自体は失敗せず（既存 Latitude fail-open semantics）、
    生成された settings.local.json に LATITUDE_PROJECT キーは追加されない
    """
    native_home = tmp_path / "native-home"
    native_home.mkdir()
    assert not (native_home / ".claude" / "settings.json").exists()

    result, settings_local = _run_check_only(
        tmp_path, native_home=native_home, claude_config_dir=None
    )
    assert result.returncode == 0, result.stdout + "\n---stderr---\n" + result.stderr
    data = json.loads(settings_local.read_text(encoding="utf-8"))
    env = data.get("env", {})
    assert env.get("CLAUDE_GPT_NATIVE_SETTINGS_PATH") == str(
        native_home / ".claude" / "settings.json"
    )
    assert "LATITUDE_PROJECT" not in env


def test_ac5_malformed_json_native_settings_is_fail_open(tmp_path):
    """GIVEN 解決された Native settings.json のパスに malformed JSON が置かれ
    ている
    WHEN launch.sh --check-only を実行する
    THEN launch.sh 自体は失敗せず、生成された settings.local.json に
    LATITUDE_PROJECT キーは追加されない（既存 fail-open semantics のまま）
    """
    native_home = tmp_path / "native-home"
    claude_dir = native_home / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "settings.json").write_text("{not-valid-json", encoding="utf-8")

    result, settings_local = _run_check_only(
        tmp_path, native_home=native_home, claude_config_dir=None
    )
    assert result.returncode == 0, result.stdout + "\n---stderr---\n" + result.stderr
    data = json.loads(settings_local.read_text(encoding="utf-8"))
    env = data.get("env", {})
    assert env.get("CLAUDE_GPT_NATIVE_SETTINGS_PATH") == str(claude_dir / "settings.json")
    assert "LATITUDE_PROJECT" not in env


# ---------------------------------------------------------------------------
# AC4 (runtime-verification: true): self-launch subprocess boundary.
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
#     invocation below (Issue #2455 precedent). ---
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

# --- Main invocation: this IS the "child Claude" process under test.
#     Real Claude Code applies the `env` block of the settings.json passed
#     via `--settings <path>` to its own tool-execution subprocess
#     environment (this is the actual re-entrant carrier mechanism Issue
#     #2448 relies on for CLAUDE_GPT_NATIVE_SETTINGS_PATH -- this file never
#     exports it directly into the shell process before exec, unlike
#     CLAUDE_GPT_HOME in Issue #2455). This fake claude reproduces exactly
#     that application step (never mutating its OWN os.environ, only the
#     env dict handed to the nested subprocess). ---
own_settings_path = None
for i, tok in enumerate(argv):
    if tok == "--settings" and i + 1 < len(argv):
        own_settings_path = argv[i + 1]

settings_env = {}
if own_settings_path and os.path.exists(own_settings_path):
    with open(own_settings_path, encoding="utf-8") as fh:
        own_settings = json.load(fh)
    candidate = own_settings.get("env")
    if isinstance(candidate, dict):
        settings_env = candidate

nested_env = dict(os.environ)
if os.environ.get("FAKE_CLAUDE_UNSET_CARRIER") == "1":
    # Negative control (Issue #2448 AC4): deliberately do NOT propagate the
    # settings.json env carrier before self-launching, to prove this
    # subprocess boundary actually DETECTS a deviation when the carrier is
    # absent (never a tautological harness).
    settings_env = {
        k: v for k, v in settings_env.items() if k != "CLAUDE_GPT_NATIVE_SETTINGS_PATH"
    }
nested_env.update({k: str(v) for k, v in settings_env.items()})

# PR #2466 owner review (issuecomment-5478243138 P1): simulate the
# self-launch child changing its CWD before re-deriving/carrying the Native
# settings path, to prove a relative `CLAUDE_CONFIG_DIR` carrier is
# absolutized at outer resolution time (and therefore survives this CWD
# change), rather than being silently reinterpreted against the new CWD.
_chdir_target = os.environ.get("FAKE_CLAUDE_CHDIR_BEFORE_SELF_LAUNCH")
if _chdir_target:
    os.chdir(_chdir_target)

outer_view_path = os.environ.get("FAKE_CLAUDE_OUTER_VIEW_PATH")
if outer_view_path:
    view = {
        "home": os.environ.get("HOME"),
        "ambient_claude_config_dir": os.environ.get("CLAUDE_CONFIG_DIR"),
        "carrier_from_settings_env": settings_env.get("CLAUDE_GPT_NATIVE_SETTINGS_PATH"),
    }
    with open(outer_view_path, "w", encoding="utf-8") as fh:
        json.dump(view, fh)

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


def _write_ac4_artifact(payload: dict) -> Path:
    artifact_dir = Path(os.environ.get("RUNTIME_VERIFICATION_ARTIFACT_DIR", "artifacts"))
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact = artifact_dir / (
        "runtime-verification-AC4-issue-2448-"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + ".log"
    )
    artifact.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return artifact


def _run_self_launch(
    tmp_path: Path,
    *,
    unset_carrier_in_child: bool = False,
    outer_cwd: Path | None = None,
    claude_config_dir: str | None = None,
    chdir_before_self_launch: str | None = None,
):
    """Run a real `launch.sh` normal-mode invocation whose `claude` child is
    the fake self-launching binary above. Never presets `CLAUDE_GPT_HOME`/
    `CLAUDE_GPT_NATIVE_SETTINGS_PATH` anywhere in the outer environment (only
    `PATH` + `HOME`, plus optionally `CLAUDE_CONFIG_DIR`) -- the outer
    resolved path is derived exactly the way a real ambient shell would
    derive it (Issue #2448 AC5's normal-case fallback path, or the
    `CLAUDE_CONFIG_DIR` precedence path when `claude_config_dir` is given).

    `outer_cwd` overrides the CWD of the outer `launch.sh` invocation itself
    (defaults to `SCRIPT_DIR`, as before) -- needed to make a relative
    `claude_config_dir` resolve against a test-owned directory.
    `chdir_before_self_launch`, if given, is forwarded to the fake
    self-launching `claude` child so it changes its own CWD immediately
    before re-launching `launch.sh --check-only` (PR #2466 owner review P1).
    """
    native_home = tmp_path / "native-home"
    native_home.mkdir()

    fake_proxy = write_executable(tmp_path / "fake-claude-code-proxy", FAKE_PROXY_SOURCE)
    fake_claude = write_executable(
        tmp_path / "fake-claude-self-launch", FAKE_CLAUDE_SELF_LAUNCH_SOURCE
    )

    outer_view_path = tmp_path / "outer-view.json"
    nested_stdout_path = tmp_path / "nested-stdout.json"
    nested_stderr_path = tmp_path / "nested-stderr.log"

    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(native_home),
        "CLAUDE_GPT_PROXY_BIN": str(fake_proxy),
        "CLAUDE_GPT_CLAUDE_BIN": str(fake_claude),
        "FAKE_CLAUDE_SELF_LAUNCH_PATH": str(LAUNCH_SH),
        "FAKE_CLAUDE_OUTER_VIEW_PATH": str(outer_view_path),
        "FAKE_CLAUDE_NESTED_STDOUT_PATH": str(nested_stdout_path),
        "FAKE_CLAUDE_NESTED_STDERR_PATH": str(nested_stderr_path),
    }
    if unset_carrier_in_child:
        env["FAKE_CLAUDE_UNSET_CARRIER"] = "1"
    if claude_config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = claude_config_dir
    if chdir_before_self_launch is not None:
        env["FAKE_CLAUDE_CHDIR_BEFORE_SELF_LAUNCH"] = chdir_before_self_launch

    try:
        result = subprocess.run(
            [
                str(LAUNCH_SH),
                "--",
                "-p",
                "hello",
                "--output-format",
                "text",
                "--no-session-persistence",
            ],
            cwd=str(outer_cwd) if outer_cwd is not None else str(SCRIPT_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
        )
    except OSError as exc:
        pytest.exit(
            f"SKIP: native_settings_path_precedence AC4 unavailable (outer launch.sh subprocess start failed: {exc})",
            returncode=77,
        )
    return result, native_home, outer_view_path, nested_stdout_path, nested_stderr_path


def test_ac4_self_launch_preserves_outer_native_settings_path_identity(tmp_path):
    """GIVEN 実 launch.sh を fake authenticated proxy + fake claude で normal
    mode 起動し、fake claude が自分自身の settings.json（outer が resolve
    した Native settings path を含む env）を読み、その値を settings.json 適用
    の実挙動どおり子環境へ反映した上で同じ launch.sh --check-only を
    self-launch する
    WHEN outer と nested それぞれが解決した CLAUDE_GPT_NATIVE_SETTINGS_PATH
    を比較する
    THEN 両方とも同一の native HOME 配下 settings.json path を参照し、
    nested が isolated child 自身の config dir/HOME を Native settings と
    誤認しない
    """
    generated_at = datetime.now(timezone.utc).isoformat()
    (
        result,
        native_home,
        outer_view_path,
        nested_stdout_path,
        nested_stderr_path,
    ) = _run_self_launch(tmp_path)

    artifact_payload = {
        "ac": "AC4",
        "issue": 2448,
        "timestamp": generated_at,
        "environment": (
            "pytest tmp_path fake proxy + fake claude self-launch fixture "
            "(hermetic, no live network/credential)"
        ),
        "exit_code": result.returncode,
        "stdout_tail": result.stdout[-2000:],
        "stderr_tail": result.stderr[-2000:],
    }

    assert result.returncode == 0, result.stdout + "\n---stderr---\n" + result.stderr

    expected_native_settings_path = str(native_home / ".claude" / "settings.json")

    assert outer_view_path.exists(), "fake claude was never invoked as the main process"
    outer_view = json.loads(outer_view_path.read_text(encoding="utf-8"))
    artifact_payload["outer_view"] = outer_view
    assert outer_view["carrier_from_settings_env"] == expected_native_settings_path

    assert nested_stdout_path.exists(), (
        "nested launch.sh --check-only produced no stdout: "
        + nested_stderr_path.read_text(encoding="utf-8")
    )
    nested_payload = json.loads(nested_stdout_path.read_text(encoding="utf-8"))
    artifact_payload["nested_check_only_result"] = {
        "schema": nested_payload.get("schema"),
        "status": nested_payload.get("status"),
    }
    assert nested_payload["schema"] == "CLAUDE_GPT_LAUNCH_RESULT_V1"
    assert nested_payload["status"] == "ok"
    assert nested_payload["mode"] == "check_only"

    nested_settings_local = Path(nested_payload["settings_path"])
    nested_data = json.loads(nested_settings_local.read_text(encoding="utf-8"))
    nested_native_settings_path = nested_data.get("env", {}).get(
        "CLAUDE_GPT_NATIVE_SETTINGS_PATH"
    )
    artifact_payload["nested_native_settings_path"] = nested_native_settings_path
    artifact_payload["expected_native_settings_path"] = expected_native_settings_path
    artifact_payload["result"] = "PASS"

    _write_ac4_artifact(artifact_payload)

    assert nested_native_settings_path == expected_native_settings_path


def test_ac4_negative_control_unset_carrier_deviates_to_isolated_config_dir(tmp_path):
    """GIVEN 上記と同じ subprocess boundary だが、fake claude が self-launch
    直前に意図的に settings env から CLAUDE_GPT_NATIVE_SETTINGS_PATH を除外
    する（carrier 不伝播）
    WHEN nested launch.sh --check-only が解決した Native settings path を
    確認する
    THEN nested は ambient CLAUDE_CONFIG_DIR（isolated Claude-GPT 自身の
    config dir。outer launch.sh が isolated HOME 切替前に export 済みの
    値）を native profile と誤認して逸脱する（既存 precedence 自体は正しく
    機能していることの negative control。harness がタウトロジーでないこと
    の証拠。誤って native HOME の settings.json と同じ値には決してならない）
    """
    generated_at = datetime.now(timezone.utc).isoformat()
    (
        result,
        native_home,
        outer_view_path,
        nested_stdout_path,
        nested_stderr_path,
    ) = _run_self_launch(tmp_path, unset_carrier_in_child=True)

    assert result.returncode == 0, result.stdout + "\n---stderr---\n" + result.stderr
    assert nested_stdout_path.exists(), nested_stderr_path.read_text(encoding="utf-8")
    nested_payload = json.loads(nested_stdout_path.read_text(encoding="utf-8"))
    assert nested_payload["status"] == "ok"

    nested_settings_local = Path(nested_payload["settings_path"])
    nested_data = json.loads(nested_settings_local.read_text(encoding="utf-8"))
    nested_native_settings_path = nested_data.get("env", {}).get(
        "CLAUDE_GPT_NATIVE_SETTINGS_PATH"
    )
    expected_native_settings_path = str(native_home / ".claude" / "settings.json")

    _write_ac4_artifact(
        {
            "ac": "AC4-negative-control",
            "issue": 2448,
            "timestamp": generated_at,
            "environment": (
                "pytest tmp_path fake proxy + fake claude self-launch fixture "
                "(hermetic, no live network/credential)"
            ),
            "exit_code": result.returncode,
            "nested_native_settings_path": nested_native_settings_path,
            "expected_native_settings_path_never_matched": expected_native_settings_path,
            "result": "PASS (deviation confirmed)",
        }
    )

    assert nested_native_settings_path != expected_native_settings_path


def test_ac4_relative_claude_config_dir_absolutized_survives_self_launch_cwd_change(
    tmp_path,
):
    """GIVEN outer 環境で相対パスの `CLAUDE_CONFIG_DIR`（例: ".claude-native"）が
    与えられている（Issue #2448 PR #2466 owner review issuecomment-5478243138
    P1 blocker: 相対のまま carrier へ焼き込むと、self-launch 境界を跨いだ CWD
    変化でその文字列が別の filesystem object を指してしまい exact-identity
    保証が壊れる）
    WHEN 実 launch.sh を起動し、fake claude self-launch 子プロセスが self-launch
    直前に別ディレクトリへ CWD を変更してから同じ launch.sh --check-only を
    self-launch する
    THEN outer が解決した `CLAUDE_GPT_NATIVE_SETTINGS_PATH` は絶対パスであり、
    nested の inherited carrier も CWD 変化にかかわらず同一の
    settings.json ファイルを指し続ける（CWD-relative な誤再解釈が起きない）
    """
    generated_at = datetime.now(timezone.utc).isoformat()
    outer_cwd = tmp_path / "outer-cwd"
    outer_cwd.mkdir()
    relative_config_dir = ".claude-native"
    native_profile = outer_cwd / relative_config_dir
    native_profile.mkdir()
    (native_profile / "settings.json").write_text(
        json.dumps({"env": {"LATITUDE_PROJECT": "relative-config-dir-project"}}),
        encoding="utf-8",
    )
    different_cwd = tmp_path / "different-cwd-for-self-launch-child"
    different_cwd.mkdir()

    (
        result,
        native_home,
        outer_view_path,
        nested_stdout_path,
        nested_stderr_path,
    ) = _run_self_launch(
        tmp_path,
        outer_cwd=outer_cwd,
        claude_config_dir=relative_config_dir,
        chdir_before_self_launch=str(different_cwd),
    )

    artifact_payload = {
        "ac": "AC4-relative-config-dir-absolutization",
        "issue": 2448,
        "timestamp": generated_at,
        "environment": (
            "pytest tmp_path fake proxy + fake claude self-launch fixture "
            "(hermetic, no live network/credential)"
        ),
        "exit_code": result.returncode,
        "stdout_tail": result.stdout[-2000:],
        "stderr_tail": result.stderr[-2000:],
    }

    assert result.returncode == 0, result.stdout + "\n---stderr---\n" + result.stderr

    expected_native_settings_path = str(native_profile / "settings.json")

    assert outer_view_path.exists(), "fake claude was never invoked as the main process"
    outer_view = json.loads(outer_view_path.read_text(encoding="utf-8"))
    artifact_payload["outer_view"] = outer_view
    carrier = outer_view["carrier_from_settings_env"]
    assert carrier == expected_native_settings_path
    assert carrier.startswith("/"), "resolved relative CLAUDE_CONFIG_DIR must be absolutized"

    assert nested_stdout_path.exists(), (
        "nested launch.sh --check-only produced no stdout: "
        + nested_stderr_path.read_text(encoding="utf-8")
    )
    nested_payload = json.loads(nested_stdout_path.read_text(encoding="utf-8"))
    artifact_payload["nested_check_only_result"] = {
        "schema": nested_payload.get("schema"),
        "status": nested_payload.get("status"),
    }
    assert nested_payload["schema"] == "CLAUDE_GPT_LAUNCH_RESULT_V1"
    assert nested_payload["status"] == "ok"
    assert nested_payload["mode"] == "check_only"

    nested_settings_local = Path(nested_payload["settings_path"])
    nested_data = json.loads(nested_settings_local.read_text(encoding="utf-8"))
    nested_native_settings_path = nested_data.get("env", {}).get(
        "CLAUDE_GPT_NATIVE_SETTINGS_PATH"
    )
    artifact_payload["nested_native_settings_path"] = nested_native_settings_path
    artifact_payload["expected_native_settings_path"] = expected_native_settings_path
    artifact_payload["chdir_before_self_launch"] = str(different_cwd)
    artifact_payload["result"] = "PASS"

    _write_ac4_artifact(artifact_payload)

    assert nested_native_settings_path == expected_native_settings_path
