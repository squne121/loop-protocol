from __future__ import annotations

import importlib.util
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[4]
_spec = importlib.util.spec_from_file_location(
    "human_history_template", _ROOT / ".claude/skills/issue-refinement-loop/scripts/publish_termination_report.py"
)
publisher = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(publisher)


def _identity():
    return {
        "loop_kind": "issue-refinement-loop", "phase": "review-complete",
        "source_issue_number": 1908, "target_kind": "issue", "target_number": 1908,
        "route_or_termination_reason": "completed", "reviewed_ref": "a" * 64,
    }


def test_public_safe_japanese_template_and_secret_sanitization():
    rendered, error = publisher.render_human_history_comment(
        identity=_identity(), result="レビューを完了しました",
        evidence_refs=["https://github.com/squne121/loop-protocol/issues/1908"],
        recommended_action="実装を開始してください", recommended_reason="受け入れ条件を満たします",
        impact_if_unaddressed="次の判断が遅れます",
    )
    assert error == "" and rendered
    for text in ("実施内容", "推奨アクション", "推奨する理由", "対応しない場合の影響", "evidence refs"):
        assert text in rendered["body"]
    for unsafe in ("ghp_abcdefghijklmnopqrstuvwxyz012345", "/home/operator/private.txt", "```raw transcript```"):
        _, unsafe_error = publisher.render_human_history_comment(
            identity=_identity(), result=unsafe, evidence_refs=["https://github.com/x"],
            recommended_action="確認してください", recommended_reason="理由です", impact_if_unaddressed="影響です",
        )
        assert unsafe_error.startswith("human_history_public_safe_text_invalid")
