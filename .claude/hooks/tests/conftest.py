import sys
from pathlib import Path

# importlib モードでは sys.path への自動追加が行われないため、
# 同ディレクトリ内の test_guard_api_input 等をインポート可能にする。
sys.path.insert(0, str(Path(__file__).parent))

# Issue #1657 fix_delta（Blocker 1）: worktree_scope_guard 関連テスト本体が
# tests/agent_guards/ へ移設されたため、test_issue1215_*.py / test_issue1241_*.py
# が行う `from test_worktree_scope_guard import ...` の bare import を解決できる
# よう tests/agent_guards/ も sys.path に追加する（判定ロジックの重複実装では
# なく import 解決のみの hygiene fix）。
_AGENT_GUARDS_TESTS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "tests" / "agent_guards"
if str(_AGENT_GUARDS_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_GUARDS_TESTS_DIR))
