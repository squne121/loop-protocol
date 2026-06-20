"""
conftest.py for tests/context_mode/

pytest_sessionfinish で deny-negative-test.json artifact を生成する。
テスト実行中に _EVIDENCE に記録された実際の検証結果を集約する。
"""

from __future__ import annotations

import pytest


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """
    テストセッション終了後に deny-negative-test.json artifact を生成する。
    実際のテスト結果（_EVIDENCE）を集約して artifact を書き出す。
    exitstatus 0 (PASS) または 1 (FAIL) に関わらず実行する（証跡保存のため）。
    """
    try:
        from tests.context_mode.test_deny_secrets import create_evidence_artifact
        create_evidence_artifact()
    except Exception:
        # artifact 生成の失敗はテスト自体を失敗させない
        pass
