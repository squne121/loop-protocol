"""Static contract validation for agent/skill markdown files.

Catches argparse-level contract drift (wrong CLI flags, missing schema,
forbidden patterns) before runtime.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
AGENTS_DIR = REPO_ROOT / ".claude" / "agents"
SKILLS_DIR = REPO_ROOT / ".claude" / "skills"

WEB_RESEARCHER_MD = AGENTS_DIR / "web-researcher.md"
ISSUE_REFINEMENT_LOOP_MD = SKILLS_DIR / "issue-refinement-loop" / "SKILL.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestWebResearcherAgent:
    """web-researcher.md のフロントマター・CLI 引数・スキーマを検証する。"""

    def test_exists(self):
        assert WEB_RESEARCHER_MD.exists(), f"{WEB_RESEARCHER_MD} が存在しない"

    def test_model_is_haiku(self):
        text = _read(WEB_RESEARCHER_MD)
        assert re.search(r"^model:\s*haiku\s*$", text, re.MULTILINE), (
            "web-researcher.md の model は haiku でなければならない"
        )

    def test_request_file_flag_used(self):
        text = _read(WEB_RESEARCHER_MD)
        assert "--request-file" in text, (
            "web-researcher.md は --request-file を使用しなければならない"
        )

    def test_bare_request_flag_not_used(self):
        text = _read(WEB_RESEARCHER_MD)
        # --request-file は許可、 --request<空白 or 改行> は不可
        hits = re.findall(r"--request(?!-file)\b", text)
        assert not hits, (
            f"web-researcher.md に誤った --request フラグが {len(hits)} 箇所ある"
        )

    def test_output_file_flag_used(self):
        text = _read(WEB_RESEARCHER_MD)
        assert "--output-file" in text, (
            "web-researcher.md は --output-file を使用しなければならない"
        )

    def test_web_research_result_v1_schema_defined(self):
        text = _read(WEB_RESEARCHER_MD)
        assert "WEB_RESEARCH_RESULT_V1" in text, (
            "web-researcher.md に WEB_RESEARCH_RESULT_V1 スキーマが定義されていない"
        )

    def test_setup_check_step_present(self):
        text = _read(WEB_RESEARCHER_MD)
        assert "setup_check.py" in text, (
            "web-researcher.md に setup_check ステップが含まれていない"
        )

    def test_preflight_step_present(self):
        text = _read(WEB_RESEARCHER_MD)
        assert "preflight_gemini_headless.py" in text, (
            "web-researcher.md に preflight ステップが含まれていない"
        )

    def test_disallowed_tools_includes_webfetch_websearch(self):
        text = _read(WEB_RESEARCHER_MD)
        # フロントマターの disallowedTools リストに WebFetch と WebSearch が含まれることを確認
        frontmatter = re.split(r"^---\s*$", text, maxsplit=2, flags=re.MULTILINE)
        assert len(frontmatter) >= 3, "フロントマターが見つからない"
        fm = frontmatter[1]
        for tool in ("WebFetch", "WebSearch"):
            assert tool in fm, (
                f"web-researcher.md の disallowedTools に {tool} が含まれていない"
            )

    def test_antigravity_note_present(self):
        text = _read(WEB_RESEARCHER_MD)
        assert "Antigravity" in text, (
            "web-researcher.md に Antigravity CLI 互換性ノートが含まれていない"
        )

    def test_gemini_api_key_wording_weakened(self):
        text = _read(WEB_RESEARCHER_MD)
        # 旧: "GEMINI_API_KEY は使わない" のような断定表現を禁止
        assert not re.search(r"GEMINI_API_KEY\s+は使わない", text), (
            "web-researcher.md の GEMINI_API_KEY 表現が強すぎる。"
            "「既定経路では必須ではない」程度に弱めること。"
        )


class TestIssueRefinementLoop:
    """issue-refinement-loop/SKILL.md の LOOP_STATE と Step 1b を検証する。"""

    def test_exists(self):
        assert ISSUE_REFINEMENT_LOOP_MD.exists(), (
            f"{ISSUE_REFINEMENT_LOOP_MD} が存在しない"
        )

    def test_loop_state_has_web_research(self):
        text = _read(ISSUE_REFINEMENT_LOOP_MD)
        assert "web_research:" in text, (
            "LOOP_STATE に web_research フィールドがない"
        )

    def test_loop_state_has_critical_claims(self):
        text = _read(ISSUE_REFINEMENT_LOOP_MD)
        assert "critical_claims" in text, (
            "LOOP_STATE に critical_claims フィールドがない"
        )

    def test_step1b_human_escalation_on_critical_failure(self):
        text = _read(ISSUE_REFINEMENT_LOOP_MD)
        assert "human_escalation" in text and "critical" in text, (
            "Step 1b に critical claim 失敗時の human_escalation パスがない"
        )

    def test_web_research_result_v1_referenced(self):
        text = _read(ISSUE_REFINEMENT_LOOP_MD)
        assert "WEB_RESEARCH_RESULT_V1" in text, (
            "issue-refinement-loop に WEB_RESEARCH_RESULT_V1 の参照がない"
        )
