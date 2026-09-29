"""Issue #2810 fix_delta -- Bash tool_input の literal drift 回帰 test.

実 runtime evidence (permission_denials) では、契約 literal
`bash scripts/claude-gpt/repair_proxy.sh` に worker が `</dev/null` / `; echo ...` /
`git status` / 変数代入を足していた。doc と smoke fixture が Bash tool に渡す command 文字列を
exact に固定していることを静的に検証する。
"""

from __future__ import annotations

import importlib.util
import re
import shlex
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
REPO_ROOT = Path(__file__).resolve().parents[4]
WORKER_PATH = REPO_ROOT / ".claude/agents/implementation-worker.md"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
FIXTURE_NAMES = [
    "runtime_migration_worker_smoke_prompt.md",
    "runtime_migration_worker_deny_smoke_prompt.md",
]

_spec = importlib.util.spec_from_file_location(
    "impl_review_loop_classify_runtime_migration_2810_command_literal",
    SCRIPTS_DIR / "classify_runtime_migration.py",
)
mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(mod)

EXACT = mod.EXACT_REPAIR_COMMAND
CLASSIFIER = ".claude/skills/impl-review-loop/scripts/classify_runtime_migration.py"
SHELL_OPERATORS = {";", "&&", "||", "|", "&"}
ALL_TEXTS = [("worker", WORKER_PATH)] + [(n, FIXTURES_DIR / n) for n in FIXTURE_NAMES]


def _normalized(path: Path) -> str:
    return re.sub(r"[ \t]*\n[ \t]*", " ", path.read_text(encoding="utf-8"))


def _pre_check_commands(path: Path) -> list[str]:
    """pre-repair-check command (inline code / fenced 1 行) を全て抜き出す。"""
    raw = path.read_text(encoding="utf-8")
    pattern = r"uv run --locked python3 " + re.escape(CLASSIFIER) + r" pre-repair-check[^\n`]*"
    found = []
    for m in re.finditer(pattern, raw):
        cmd = m.group(0)
        if cmd.rstrip().endswith("..."):  # frontmatter 内の省略記法は対象外
            continue
        found.append(cmd.rstrip(". "))
    return found


def test_exact_repair_command_literal_is_the_contract():
    assert EXACT == "bash scripts/claude-gpt/repair_proxy.sh"


@pytest.mark.parametrize("label,path", ALL_TEXTS)
def test_doc_and_fixtures_instruct_exact_repair_literal_in_code(label, path):
    raw = path.read_text(encoding="utf-8")
    assert f"`{EXACT}`" in raw or f"\n{EXACT}\n" in raw, label


@pytest.mark.parametrize("label,path", ALL_TEXTS)
def test_no_legacy_repair_instruction_with_redirect_or_chaining_remains(label, path):
    raw = path.read_text(encoding="utf-8")
    text = _normalized(path)
    # repair command と同一 line で redirect / 連結が続く旧形式の指示は残っていない
    assert not re.search(re.escape(EXACT) + r"[^\n]*?(</dev/null|;|&&|\|)", raw), label
    assert "stdin from /dev/null" not in text, label
    assert "stdin/tty なしで" not in text, label
    assert "worker 自身が 確認して結果に含める" not in text, label


@pytest.mark.parametrize("label,path", ALL_TEXTS)
def test_forbidden_additions_are_stated_explicitly(label, path):
    text = _normalized(path)
    for token in ("</dev/null", "`;`", "`&&`", "`||`", "`|`", "echo", "git status"):
        assert token in text, (label, token)
    assert "Do NOT append" in text or "付加" in text or "禁止" in text, label


@pytest.mark.parametrize("label,path", ALL_TEXTS)
def test_exit_code_and_log_are_read_from_bash_tool_result(label, path):
    text = _normalized(path)
    assert "Bash tool result" in text or "Bash tool の result" in text, label
    assert "sudo required" in text, label


@pytest.mark.parametrize("label,path", ALL_TEXTS)
def test_pre_repair_check_is_a_single_command_without_assignment_or_chaining(label, path):
    commands = _pre_check_commands(path)
    assert commands, label
    for cmd in commands:
        tokens = shlex.split(cmd)
        assert tokens[:4] == ["uv", "run", "--locked", "python3"], (label, cmd)
        assert tokens[4] == CLASSIFIER and tokens[5] == "pre-repair-check", (label, cmd)
        assert tokens[6] == "--expected-claude-gpt-home", (label, cmd)
        assert tokens[8] == "--pre-repair-evidence-json", (label, cmd)
        assert len(tokens) == 10, (label, cmd)
        assert not (set(tokens) & SHELL_OPERATORS), (label, cmd)


def test_worker_doc_pre_repair_check_block_is_one_line_without_continuation():
    raw = WORKER_PATH.read_text(encoding="utf-8")
    m = re.search(r"```bash\n(uv run --locked python3 [^\n]*pre-repair-check[^\n]*)\n```", raw)
    assert m, "pre-repair-check must be a one-line fenced block"
    assert "\\" not in m.group(1)


def test_worker_does_not_run_git_status_and_result_has_no_git_status_field():
    text = _normalized(WORKER_PATH)
    assert "worker は `git status` を含む repository 状態確認 command を一切 実行しない" in text
    assert "--require-clean-postcondition" in text
    sub = re.search(r"# runtime_migration:\n((?:#   .*\n)+)", WORKER_PATH.read_text(encoding="utf-8"))
    assert sub is not None
    assert "git_status" not in sub.group(1)
    assert "porcelain" not in sub.group(1)


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fixtures_forbid_worker_git_status_but_keep_parent_value_command(name):
    text = _normalized(FIXTURES_DIR / name)
    assert "Do not run `git status`" in text or "do NOT run `git status`" in text
    # 親 agent 側の値取得 1 回コマンドは既存 test が固定しているため維持する
    assert "git rev-parse HEAD; printf" in text


def test_deny_fixture_keeps_negative_control_and_window_order():
    text = (FIXTURES_DIR / "runtime_migration_worker_deny_smoke_prompt.md").read_text(encoding="utf-8")
    i_pre = text.index("Step 0 (pre-check")
    i_repair = text.index("Step 1 (in-contract")
    i_deny = text.index("Step 2 (deliberately out-of-contract")
    assert i_pre < i_repair < i_deny
    assert "attempt to run `printenv`" in text[i_deny:]
    assert "Do not retry or work" in text[i_deny:]


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fixtures_state_provenance_facts_without_authority_claims(name):
    text = _normalized(FIXTURES_DIR / name)
    # (i) repository-tracked local file installer, not an external download
    assert "fake_proxy_installer.sh" in text
    assert "file://" in text
    assert "repository-tracked" in text
    assert "not a download-and-execute of any external URL" in text
    # (ii) mutation target is only the fixture home
    assert "artifacts/runtime-smoke/fixture-home" in text
    assert "real `~/.claude-gpt` is not modified" in text
    # (iii) no network installer
    assert "no network installer is used" in text
    # 権限・承認を主張する文言を含まない
    assert not re.search(r"\b(approved|authori[sz]ed|pre-approved|permission granted|user consent)\b", text, re.I)
