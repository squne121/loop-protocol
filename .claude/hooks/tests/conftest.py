import sys
from pathlib import Path

# importlib モードでは sys.path への自動追加が行われないため、
# 同ディレクトリ内の test_guard_api_input 等をインポート可能にする。
sys.path.insert(0, str(Path(__file__).parent))

# Issue #1657 AC8: test_issue1215_*.py / test_issue1241_*.py が使う
# worktree_scope_guard 系ハーネス helper（_bash_payload / _make_repo_with_worktree /
# _run_guard）は tests/agent_guards/worktree_scope_guard_testkit.py（テストファイル
# ではない共有 helper モジュール）へ抽出済みであり、各テストはそこから明示 import
# する（`from test_worktree_scope_guard import ...` の test-to-test bare import は
# 廃止済み）。importlib モードでは tests/agent_guards も自動追加されないため、
# testkit モジュールを解決できるようここへ追加する。
_AGENT_GUARDS_TESTS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "tests" / "agent_guards"
if str(_AGENT_GUARDS_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_GUARDS_TESTS_DIR))
