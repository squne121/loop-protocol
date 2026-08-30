"""scripts/claude-gpt/tests/test_spark_auth_deny_runtime_smoke.py

Issue #2440 AC4/AC5: actual Claude Code binary / permission parser を使う
hermetic runtime smoke。

`launch.sh --check-only` が test-owned isolated `CLAUDE_GPT_HOME` に実際に生成
した `settings.local.json` を、実 `claude` CLI に読み込ませて:

- AC4: legacy `Write(<spark-auth>/**) is not matched by file permission checks`
  相当の warning が発生しないこと
- AC5: canonical `Edit(<spark-auth>/**)` deny が spark-auth 配下への実 file
  作成を実 tool boundary（Write tool による新規ファイル作成）で拒否すること
  （既存ファイルへの Edit tool 呼び出しは Read(path) deny によっても
  ブロックされるため Edit(...) deny 自体の効果を証明できない -- PR #2458
  review, P0 参照）

を確認する。static fixture / string grep のみを PASS 根拠にしない
（`docs/dev/runtime-verification-policy.md` Runtime Verification
Applicability: `decision: immediate`, `applicable_acs: [AC4, AC5]`）。

iteration 2（impl-review-loop control-plane による実 `claude` CLI 手動再現で
発見）: 生成される canonical settings は spark-auth に対して常に `Read(...)`
と `Edit(...)` の両方の deny を持つ。この環境の実 `claude` CLI では
`Read(...)` deny だけでも Write tool による新規ファイル作成がブロックされる
ことが実機確認されているため（後述 `test_canonical_edit_deny_blocks_...`
docstring 参照）、両方が揃った production settings に対する Write tool
new-file-creation の block 確認だけでは、"Edit(...) deny が実際にブロックの
原因になっている" ことを "Read(...) deny 単体でも同一の PASS 結果になる
（Edit(...) deny 側の文法/エントリが将来 silently 壊れても検知できない）"
から区別できない。そのため、生成済み settings から spark-auth 向け
`Read(...)` deny エントリのみを機械的に除去した Edit(...)-only variant を
追加で用いる sub-check を設け、Edit(...) deny 自体の効果を独立に証明する
（PR #2458 review, P0 iteration 2）。

実 `claude` binary が test environment で利用不能な場合は `pytest.skip` する
（SKIP。既存 `test_auto_mode_policy.py` の
`@pytest.mark.skipif(REAL_CLAUDE_BIN is None, ...)` および
`latitude_live_helpers.is_environment_available()` の live 系 SKIP semantics
と同じ扱い。SKIP を PASS に昇格しない）。

live credential / ChatGPT auth material / production spark-auth content は
使用しない。test-owned isolated `CLAUDE_GPT_HOME` / spark-auth directory /
canary file のみを使う（`_latitude_check_only_helper.run_check_only` を再利用
し、hermetic `launch.sh --check-only` 起動ロジックを重複実装しない -- DRY）。
実 `claude` CLI 自体のモデル呼び出しは ambient Claude Code アカウント認証を
使う（`test_auto_mode_policy.py` の
`test_generated_settings_defaults_readback_via_real_claude_cli` および
`latitude_live_helpers.run_claude_gpt_canary` と同じ既存パターン。ChatGPT
subscription / spark-auth credential とは無関係）。
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

pytestmark = pytest.mark.skipif(
    REAL_CLAUDE_BIN is None,
    reason="SKIP: claude CLI not available in test environment (AC4/AC5 runtime smoke prerequisite)",
)

# P2 follow-up (PR #2458 review): include `claude --version` in failure
# evidence so a future upstream permission-matcher regression can be
# triaged against a known binary version. Guarded by `REAL_CLAUDE_BIN is
# None` since module-level code still executes even when pytestmark skips
# the individual test functions.
CLAUDE_VERSION = (
    subprocess.run(
        [REAL_CLAUDE_BIN, "--version"], capture_output=True, text=True, timeout=10
    ).stdout.strip()
    if REAL_CLAUDE_BIN is not None
    else "unknown (claude CLI not available)"
)


def _run_claude_print(
    settings_path: Path,
    prompt: str,
    *,
    permission_mode: str = "default",
    timeout: float = 60.0,
    cwd: Path | None = None,
    tools: str | None = None,
) -> subprocess.CompletedProcess:
    # `--setting-sources ""` makes the invocation hermetic: only `--settings
    # <generated-settings>` is honored and ambient user/project/local
    # settings files are never consulted, even if they happen to exist on
    # the host running this test (P1 follow-up, PR #2458 review).
    cmd = [
        REAL_CLAUDE_BIN,
        "--settings",
        str(settings_path),
        "--setting-sources",
        "",
        "--permission-mode",
        permission_mode,
    ]
    if tools is not None:
        # AC5 restricts the built-in tool set to `Write` only, so the
        # smoke test proves the canonical `Edit(...)` deny blocks the
        # `Write` tool specifically (not merely that some other tool
        # failed to run).
        cmd += ["--tools", tools]
    cmd += ["-p", prompt, "--output-format", "text"]
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_no_legacy_write_deny_warning_with_generated_canonical_settings(tmp_path):
    """GIVEN launch.sh --check-only が test-owned isolated CLAUDE_GPT_HOME に
    生成した settings.local.json（spark-auth deny は canonical Edit(...) のみ）
    WHEN 実 claude CLI にこの settings を渡して -p で1ターン実行する
    THEN "... is not matched by file permission checks" 相当の legacy warning が
    stdout/stderr のいずれにも出力されない (AC4)
    """
    result, settings_path = run_check_only(tmp_path)
    assert settings_path.exists(), result.stderr

    proc = _run_claude_print(settings_path, "Reply with exactly one word: OK")
    combined = proc.stdout + proc.stderr
    assert LEGACY_WARNING_SNIPPET not in combined, f"claude --version: {CLAUDE_VERSION}\n{combined}"


def test_synthetic_legacy_write_only_rule_reproduces_warning_negative_control(tmp_path):
    """GIVEN launch.sh の heredoc とは独立に手作りした legacy-only settings
    （spark-auth 向け deny が Write(path) のみで Edit(path) を含まない -- 本 Issue
    修正前の launch.sh が実際に生成していた形と等価）
    WHEN 実 claude CLI にこの settings を渡して -p で1ターン実行する
    THEN "... is not matched by file permission checks" 相当の warning が実際に
    出力される（negative control: 上のテストが偽陽性でないことを本テスト自身が
    証明する。warning 判定ロジック／fixture が壊れていれば本テストが失敗し検知
    できる）
    """
    spark_auth_dir = tmp_path / "legacy-spark-auth"
    spark_auth_dir.mkdir(parents=True)
    settings_path = tmp_path / "legacy-settings.json"
    settings_path.write_text(
        "{\n"
        '  "permissions": {\n'
        '    "deny": [\n'
        f'      "Write(/{spark_auth_dir}/**)"\n'
        "    ]\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )

    proc = _run_claude_print(settings_path, "Reply with exactly one word: OK")
    combined = proc.stdout + proc.stderr
    assert LEGACY_WARNING_SNIPPET in combined, f"claude --version: {CLAUDE_VERSION}\n{combined}"


def _spark_auth_read_deny_entries(deny: list[str]) -> list[str]:
    return [rule for rule in deny if rule.startswith("Read(") and "spark-auth" in rule]


def _write_edit_only_settings(settings_path: Path) -> Path:
    """Derive an `Edit(...)`-only variant of the real generated settings by
    programmatically removing the pre-existing spark-auth `Read(...)` deny
    entry from the real generated `permissions.deny` list (PR #2458 review,
    iteration 2, P0 follow-up).

    Deliberately *not* hand-authored from scratch: the returned settings
    stay coupled to whatever `launch.sh --check-only` actually emitted
    (same unrelated `Read(...)` entries for `proxy-config/`, `state/`,
    `proxy-home/`, same canonical `Edit(<spark-auth>/**)` entry), with only
    the spark-auth `Read(...)` entry surgically removed. This isolates
    `Edit(...)` deny's own effect from `Read(...)` deny's effect, which the
    full-production-settings check (both present) cannot do by itself --
    this repo's own runtime smoke independently found that `Read(...)` deny
    ALONE also blocks Write-tool new-file-creation in this environment (see
    the module-level iteration-2 docstring note above), so a PASS against
    full settings does not, on its own, prove `Edit(...)` deny is doing
    anything.
    """
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    deny = settings["permissions"]["deny"]
    spark_auth_read_entries = _spark_auth_read_deny_entries(deny)
    assert spark_auth_read_entries, (
        "expected launch.sh --check-only to generate a pre-existing spark-auth "
        f"Read(...) deny entry to remove for the isolation variant; deny={deny}"
    )
    settings["permissions"]["deny"] = [r for r in deny if r not in spark_auth_read_entries]
    edit_only_settings_path = settings_path.parent / "settings.local.edit-only-isolation.json"
    edit_only_settings_path.write_text(json.dumps(settings), encoding="utf-8")
    return edit_only_settings_path


def test_canonical_edit_deny_blocks_spark_auth_new_file_write_at_tool_boundary(tmp_path):
    """GIVEN launch.sh --check-only が生成した canonical Edit(...) deny を含む
    settings と、spark-auth 配下にまだ存在しない新規ファイルパス、対照用の
    spark-auth 外の新規ファイルパス
    WHEN --permission-mode acceptEdits（明示 deny 以外の file-editing tool
    呼び出しは自動承認される）・--tools "Write"（built-in tool を Write のみに
    絞る）で、両パスへの新規ファイル作成を Write tool で実 claude CLI に指示する
    THEN spark-auth 配下の新規ファイルは実際には作成されない一方、spark-auth 外
    の control file は Write tool で新規作成される（acceptEdits + Write tool
    自体は機能しており、本テストが false-negative でないことの positive
    control）(AC5: config の文字列検査だけを PASS としない)

    既存ファイルの Edit ではなく未作成ファイルへの Write を使う理由（PR #2458
    review, P0 iteration 1）: 既存ファイルへの Edit tool 呼び出しは、同じパスへの
    Read(path) deny によってもブロックされる。今回生成される settings は
    spark-auth に対して Read(...) と Edit(...) の両方の deny を持つため、既存
    ファイルへの Edit tool 呼び出しを検証対象にすると、たとえ Edit(...) deny
    自体を意図的に外しても Read(...) deny だけでブロックされてしまい、証明すべき
    Edit(...) deny の効果を証明できない（false PASS しうる）。

    このテスト本体のチェック（full production settings = Read(...) + Edit(...)
    両方が揃った状態）は、spark-auth 全体が実 tool boundary で実際にブロックされる
    ことの end-to-end 確認であり、これ自体は有効な evidence である。ただし
    iteration 2（PR #2458 review, P0 iteration 2, impl-review-loop control-plane
    による実 claude CLI 手動再現）で、この環境の実 claude CLI では Write tool に
    よる新規ファイル作成は Read(...) deny 単体でもブロックされることが実機確認
    された（Read(path) deny が Write tool による新規ファイル作成を一般にブロック
    しない、という一般則を本リポジトリ独自に検証・保証することはできない）。その
    ため full production settings（Read(...) + Edit(...) 両方）に対する block 確認
    だけでは、"Edit(...) deny が実際にブロックの原因になっている" ことと
    "Read(...) deny 単体でも同一の PASS 結果になる（Edit(...) deny 側が将来
    silently 壊れても検知できない）" ことを区別できない。そのため下段で
    `_write_edit_only_settings` により spark-auth の Read(...) deny エントリだけを
    機械的に除去した Edit(...)-only settings variant を追加し、Edit(...) deny
    自体の効果を独立に証明する sub-check を行う。これが AC5 の causal claim
    （Edit(...) deny 自体の効果）を実際に証明する evidence であり、full
    production settings の check は end-to-end 確認としての補助的 evidence
    という位置づけになる。
    """
    result, settings_path = run_check_only(tmp_path)
    assert settings_path.exists(), result.stderr

    # settings_path == <CLAUDE_GPT_HOME>/claude/settings.local.json
    # (`_latitude_check_only_helper.run_check_only` 参照)。
    # SPARK_AUTH_DIR_TARGET == <CLAUDE_GPT_HOME>/spark-auth と一致させる。
    claude_gpt_home = settings_path.parent.parent
    spark_auth_dir = claude_gpt_home / "spark-auth"
    assert spark_auth_dir.is_dir(), "launch.sh must have created SPARK_AUTH_DIR_TARGET"

    blocked_path = spark_auth_dir / f"blocked-{uuid.uuid4().hex}.txt"
    assert not blocked_path.exists(), "blocked_path must not pre-exist (Write = new-file creation)"

    control_path = tmp_path / f"allowed-{uuid.uuid4().hex}.txt"
    assert not control_path.exists(), "control_path must not pre-exist (Write = new-file creation)"

    prompt = (
        f"Use the Write tool to create a new file at {blocked_path} with the exact "
        f"content BLOCKED_CONTENT. Then use the Write tool to create a new file at "
        f"{control_path} with the exact content ALLOWED_CONTENT. Attempt both writes "
        "even if one fails; do not ask for confirmation; report the outcome of each "
        "attempt."
    )
    proc = _run_claude_print(
        settings_path,
        prompt,
        permission_mode="acceptEdits",
        timeout=90.0,
        cwd=tmp_path,
        tools="Write",
    )
    transcript = f"claude --version: {CLAUDE_VERSION}\nclaude stdout:\n{proc.stdout}\nclaude stderr:\n{proc.stderr}"

    assert not blocked_path.exists(), (
        "spark-auth new file must not be created -- full production settings "
        f"(Read+Edit both present) must block the Write tool at the real tool "
        f"boundary end-to-end.\n{transcript}"
    )
    assert control_path.exists() and "ALLOWED_CONTENT" in control_path.read_text(encoding="utf-8"), (
        "control file outside spark-auth must be created by the Write tool under "
        f"acceptEdits -- otherwise this is not a valid positive control proving the "
        f"Write tool boundary was actually exercised.\n{transcript}"
    )

    # --- iteration 2 (PR #2458 review, P0 iteration 2) ---
    # Isolate Edit(...) deny's own effect from Read(...) deny's effect using
    # an Edit(...)-only settings variant derived from the real generated
    # settings (spark-auth Read(...) deny entry removed; see
    # `_write_edit_only_settings` docstring for why this is necessary).
    edit_only_settings_path = _write_edit_only_settings(settings_path)

    edit_only_blocked_path = spark_auth_dir / f"blocked-edit-only-{uuid.uuid4().hex}.txt"
    assert not edit_only_blocked_path.exists(), "edit_only_blocked_path must not pre-exist"

    edit_only_control_path = tmp_path / f"allowed-edit-only-{uuid.uuid4().hex}.txt"
    assert not edit_only_control_path.exists(), "edit_only_control_path must not pre-exist"

    edit_only_prompt = (
        f"Use the Write tool to create a new file at {edit_only_blocked_path} with the "
        f"exact content BLOCKED_CONTENT. Then use the Write tool to create a new file at "
        f"{edit_only_control_path} with the exact content ALLOWED_CONTENT. Attempt both "
        "writes even if one fails; do not ask for confirmation; report the outcome of "
        "each attempt."
    )
    edit_only_proc = _run_claude_print(
        edit_only_settings_path,
        edit_only_prompt,
        permission_mode="acceptEdits",
        timeout=90.0,
        cwd=tmp_path,
        tools="Write",
    )
    edit_only_transcript = (
        f"claude --version: {CLAUDE_VERSION}\n"
        f"claude stdout:\n{edit_only_proc.stdout}\nclaude stderr:\n{edit_only_proc.stderr}"
    )

    assert not edit_only_blocked_path.exists(), (
        "spark-auth new file must not be created even with the spark-auth Read(...) "
        "deny entry removed -- this directly and unambiguously proves Edit(...) "
        "deny's own effect at the real tool boundary, independent of Read(...) deny "
        f"(PR #2458 review, P0 iteration 2).\n{edit_only_transcript}"
    )
    assert edit_only_control_path.exists() and "ALLOWED_CONTENT" in edit_only_control_path.read_text(
        encoding="utf-8"
    ), (
        "control file outside spark-auth must be created by the Write tool under "
        "acceptEdits with the Edit(...)-only settings too -- otherwise this is not a "
        "valid positive control proving the Write tool boundary was actually exercised "
        f"for the isolated variant.\n{edit_only_transcript}"
    )
