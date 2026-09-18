"""scripts/claude-gpt/tests/test_spark_auth_deny_runtime_smoke.py

Issue #2651: the Spark explicit-only authorization gate (Issue #2186) and
its `spark-auth` sidecar directory have been retired from `launch.sh`
entirely. This file used to be a hermetic real-`claude`-CLI runtime smoke
proving the legacy-vs-canonical `spark-auth` deny-rule migration (Issue
#2440 AC4/AC5) actually blocked file-tool access to that directory at the
real permission-matcher boundary. There is no `spark-auth` directory, and
no deny rule referencing it, left to protect or verify any more.

Replaced (file path kept, per Issue #2651 Allowed Paths -- no file
deletion) with a negative regression suite:

- AC5 (retired route): `launch.sh --check-only` generates no `spark-auth`
  reference anywhere in `settings.local.json` and creates no `spark-auth`
  directory (static, no real CLI needed).
- Runtime verification (immediate, per this Issue's Runtime Verification
  Applicability): the real `claude` CLI, given the generated settings, can
  now create a file at the path the retired `spark-auth` sidecar directory
  used to occupy -- proving no phantom/leftover deny rule still blocks it
  (a single bounded real-CLI call, not the original file's heavier
  multi-file/two-settings-variant harness, since there is no longer a
  causal claim about two overlapping deny rules to isolate).

実 `claude` binary が test environment で利用不能な場合は `pytest.skip` する
（SKIP を PASS に昇格しない）。live credential / production content は使用しない
（test-owned isolated `CLAUDE_GPT_HOME` のみ。`_latitude_check_only_helper.
run_check_only` を再利用し、hermetic `launch.sh --check-only` 起動ロジックを
重複実装しない -- DRY）。
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

_HELPER_PATH = Path(__file__).resolve().parent / "_latitude_check_only_helper.py"
_spec = importlib.util.spec_from_file_location(
    "claude_gpt_latitude_check_only_helper_2440_spark_auth_runtime_smoke", _HELPER_PATH
)
_helper = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_helper)

run_check_only = _helper.run_check_only

REAL_CLAUDE_BIN = shutil.which("claude")

LEGACY_WARNING_SNIPPET = "is not matched by file permission checks"


def test_generated_settings_have_no_spark_auth_reference(tmp_path):
    """GIVEN launch.sh --check-only が test-owned isolated CLAUDE_GPT_HOME に
    生成した settings.local.json
    WHEN permissions.deny を読む
    THEN spark-auth への参照（Write(...)/Edit(...)/Read(...) いずれの形も）は
    一切含まれない -- Spark 認可 gate と sidecar directory 自体が撤去された
    ため（Issue #2651）。
    """
    result, settings_path = run_check_only(tmp_path)
    assert settings_path.exists(), result.stderr
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    deny = settings["permissions"]["deny"]
    assert not any("spark-auth" in rule for rule in deny), deny


def test_no_spark_auth_directory_created(tmp_path):
    """GIVEN launch.sh --check-only の実行
    WHEN test-owned isolated CLAUDE_GPT_HOME 配下を確認する
    THEN `spark-auth` ディレクトリは作成されない。
    """
    result, settings_path = run_check_only(tmp_path)
    assert settings_path.exists(), result.stderr
    claude_gpt_home = settings_path.parent.parent
    assert not (claude_gpt_home / "spark-auth").exists()


@pytest.mark.skipif(
    REAL_CLAUDE_BIN is None,
    reason="SKIP: claude CLI not available in test environment (runtime-verification prerequisite)",
)
def test_real_claude_cli_write_tool_unblocked_at_former_spark_auth_path(tmp_path):
    """GIVEN launch.sh --check-only が生成した settings（spark-auth 関連 deny
    なし）と、旧 spark-auth sidecar directory が過去に占めていたのと同じ
    相対位置（<CLAUDE_GPT_HOME>/spark-auth/...）にある新規ファイルパス
    WHEN --permission-mode acceptEdits・--tools "Write" で実 claude CLI に
    そのパスへの新規ファイル作成を指示する
    THEN ファイルは実際に作成される（phantom/leftover deny rule が
    tool boundary で残っていないことを実機で証明する）。legacy
    `... is not matched by file permission checks` warning も出力されない
    （AC4 相当の regression 確認を残す）。
    """
    result, settings_path = run_check_only(tmp_path)
    assert settings_path.exists(), result.stderr

    # settings_path == <CLAUDE_GPT_HOME>/claude/settings.local.json
    claude_gpt_home = settings_path.parent.parent
    former_spark_auth_dir = claude_gpt_home / "spark-auth"
    former_spark_auth_dir.mkdir(parents=True, exist_ok=True)

    target_path = former_spark_auth_dir / f"retired-{uuid.uuid4().hex}.txt"
    assert not target_path.exists()

    prompt = (
        f"Use the Write tool to create a new file at {target_path} with the exact "
        "content RETIRED_ROUTE_UNBLOCKED. Do not ask for confirmation."
    )
    proc = subprocess.run(
        [
            REAL_CLAUDE_BIN,
            "--settings",
            str(settings_path),
            "--setting-sources",
            "",
            "--permission-mode",
            "acceptEdits",
            "--tools",
            "Write",
            "-p",
            prompt,
            "--output-format",
            "text",
        ],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=90.0,
    )
    combined = proc.stdout + proc.stderr

    assert LEGACY_WARNING_SNIPPET not in combined, combined
    assert target_path.exists() and "RETIRED_ROUTE_UNBLOCKED" in target_path.read_text(encoding="utf-8"), (
        f"expected the Write tool to succeed at the former spark-auth path with no "
        f"deny rule left to block it.\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
