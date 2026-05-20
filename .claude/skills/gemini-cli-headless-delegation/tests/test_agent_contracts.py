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


def _all_agent_mds() -> list[Path]:
    return list(AGENTS_DIR.glob("*.md"))


def _all_skill_mds() -> list[Path]:
    return list(SKILLS_DIR.glob("*/SKILL.md"))


def _frontmatter(text: str) -> str:
    """Return the YAML frontmatter block (between the two --- delimiters)."""
    parts = re.split(r"^---\s*$", text, maxsplit=2, flags=re.MULTILINE)
    return parts[1] if len(parts) >= 3 else ""


def _body(text: str) -> str:
    """Return the body after the closing --- delimiter."""
    parts = re.split(r"^---\s*$", text, maxsplit=2, flags=re.MULTILINE)
    return parts[-1] if len(parts) >= 3 else text


# ---------------------------------------------------------------------------
# web-researcher.md 固有
# ---------------------------------------------------------------------------


class TestWebResearcherAgent:
    """web-researcher.md のフロントマター・CLI 引数・スキーマを検証する。"""

    def test_exists(self):
        assert WEB_RESEARCHER_MD.exists(), f"{WEB_RESEARCHER_MD} が存在しない"

    def test_model_is_haiku(self):
        text = _read(WEB_RESEARCHER_MD)
        assert re.search(r"^model:\s*haiku\s*$", text, re.MULTILINE), (
            "web-researcher.md の model は haiku でなければならない"
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
        fm = _frontmatter(text)
        assert len(fm) > 0, "フロントマターが見つからない"
        for tool in ("WebFetch", "WebSearch"):
            assert tool in fm, (
                f"web-researcher.md の disallowedTools に {tool} が含まれていない"
            )

    def test_antigravity_note_present(self):
        text = _read(WEB_RESEARCHER_MD)
        assert "Antigravity" in text, (
            "web-researcher.md に Antigravity CLI 互換性ノートが含まれていない"
        )

    def test_antigravity_note_is_conservative(self):
        text = _read(WEB_RESEARCHER_MD)
        assert "仮定しない" in text or "未対応" in text, (
            "web-researcher.md の Antigravity ノートが楽観的すぎる。"
            "grounded_research の agy 対応が未確認であることを明記すること。"
        )

    def test_gemini_api_key_wording_weakened(self):
        text = _read(WEB_RESEARCHER_MD)
        assert not re.search(r"GEMINI_API_KEY\s+は使わない", text), (
            "web-researcher.md の GEMINI_API_KEY 表現が強すぎる。"
            "「既定経路では必須ではない」程度に弱めること。"
        )


# ---------------------------------------------------------------------------
# codebase-investigator.md 固有
# ---------------------------------------------------------------------------


class TestCodebaseInvestigatorAgent:
    """codebase-investigator.md の CLI 引数・認証表現を検証する。"""

    def test_exists(self):
        ci = AGENTS_DIR / "codebase-investigator.md"
        assert ci.exists(), f"{ci} が存在しない"

    def test_gemini_api_key_wording_weakened(self):
        ci = AGENTS_DIR / "codebase-investigator.md"
        text = _read(ci)
        assert not re.search(r"GEMINI_API_KEY\s+等の API key\s+は使わない", text), (
            "codebase-investigator.md の GEMINI_API_KEY 表現が強すぎる。"
            "「既定経路では必須ではない」程度に弱めること。"
        )


# ---------------------------------------------------------------------------
# 全 agent 共通: CLI 引数の契約ドリフト検査
# ---------------------------------------------------------------------------


class TestAllAgentCliContracts:
    """すべての .claude/agents/*.md に対して wrapper CLI 引数を検証する。"""

    @pytest.mark.parametrize("path", _all_agent_mds(), ids=lambda p: p.name)
    def test_no_bare_request_flag(self, path: Path):
        text = _read(path)
        # run_gemini_headless.py 呼び出し行のみを対象にする（gh pr review --request-changes 等を除外）
        hits = [
            line for line in text.splitlines()
            if "run_gemini_headless.py" in line and re.search(r"--request(?!-file)\b", line)
        ]
        assert not hits, (
            f"{path.name} に誤った --request フラグが {len(hits)} 箇所ある。"
            " --request-file を使うこと。"
        )

    @pytest.mark.parametrize("path", _all_agent_mds(), ids=lambda p: p.name)
    def test_run_gemini_has_both_flags(self, path: Path):
        text = _read(path)
        if "run_gemini_headless.py" not in text:
            return
        assert "--request-file" in text, (
            f"{path.name}: run_gemini_headless.py の例に --request-file がない"
        )
        assert "--output-file" in text, (
            f"{path.name}: run_gemini_headless.py の例に --output-file がない"
        )

    @pytest.mark.parametrize("path", _all_agent_mds(), ids=lambda p: p.name)
    def test_preflight_no_profile_or_json_flag(self, path: Path):
        text = _read(path)
        if "preflight_gemini_headless.py" not in text:
            return
        assert not re.search(r"preflight_gemini_headless\.py[^\n]*--profile", text), (
            f"{path.name}: preflight_gemini_headless.py に --profile は存在しない"
        )
        assert not re.search(r"preflight_gemini_headless\.py[^\n]*--json\b", text), (
            f"{path.name}: preflight_gemini_headless.py に --json は存在しない。"
            " --output-file + --compact を使うこと。"
        )

    @pytest.mark.parametrize("path", _all_agent_mds(), ids=lambda p: p.name)
    def test_no_gemini_api_key_strong_denial(self, path: Path):
        text = _read(path)
        assert not re.search(r"GEMINI_API_KEY\s+等の API key\s+は使わない", text), (
            f"{path.name}: GEMINI_API_KEY の断定表現が強すぎる。"
            "「既定経路では必須ではない」程度に弱めること。"
        )


# ---------------------------------------------------------------------------
# 全 skill 共通: CLI 引数の契約ドリフト検査
# ---------------------------------------------------------------------------


class TestAllSkillCliContracts:
    """すべての SKILL.md に対して wrapper CLI 引数を検証する。"""

    @pytest.mark.parametrize("path", _all_skill_mds(), ids=lambda p: p.parent.name)
    def test_no_bare_request_flag(self, path: Path):
        text = _read(path)
        # run_gemini_headless.py 呼び出し行のみを対象にする（gh pr review --request-changes 等を除外）
        hits = [
            line for line in text.splitlines()
            if "run_gemini_headless.py" in line and re.search(r"--request(?!-file)\b", line)
        ]
        assert not hits, (
            f"{path.parent.name}/SKILL.md に誤った --request フラグが {len(hits)} 箇所ある。"
            " --request-file を使うこと。"
        )

    @pytest.mark.parametrize("path", _all_skill_mds(), ids=lambda p: p.parent.name)
    def test_run_gemini_has_both_flags(self, path: Path):
        text = _read(path)
        if "run_gemini_headless.py" not in text:
            return
        assert "--request-file" in text, (
            f"{path.parent.name}/SKILL.md: run_gemini_headless.py の例に --request-file がない"
        )
        assert "--output-file" in text, (
            f"{path.parent.name}/SKILL.md: run_gemini_headless.py の例に --output-file がない"
        )

    @pytest.mark.parametrize("path", _all_skill_mds(), ids=lambda p: p.parent.name)
    def test_preflight_no_profile_or_json_flag(self, path: Path):
        text = _read(path)
        if "preflight_gemini_headless.py" not in text:
            return
        assert not re.search(r"preflight_gemini_headless\.py[^\n]*--profile", text), (
            f"{path.parent.name}/SKILL.md: preflight_gemini_headless.py に --profile は存在しない"
        )
        assert not re.search(r"preflight_gemini_headless\.py[^\n]*--json\b", text), (
            f"{path.parent.name}/SKILL.md: preflight_gemini_headless.py に --json は存在しない"
        )


# ---------------------------------------------------------------------------
# issue-refinement-loop/SKILL.md 固有
# ---------------------------------------------------------------------------


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

    def test_step1b_parallel_execution_noted(self):
        text = _read(ISSUE_REFINEMENT_LOOP_MD)
        assert "並列" in text, (
            "Step 1 / 1b の並列実行可能性が明記されていない"
        )

    def test_step1b_hallucination_trigger_present(self):
        text = _read(ISSUE_REFINEMENT_LOOP_MD)
        assert "ハルシネーション" in text or "エビデンス" in text, (
            "Step 1b トリガーにハルシネーション切り分け条件が含まれていない"
        )
