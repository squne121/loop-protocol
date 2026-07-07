"""Static contract validation for agent/skill markdown files.

Catches argparse-level contract drift (wrong CLI flags, missing schema,
forbidden patterns) before runtime.
"""
from __future__ import annotations

import importlib.util
import re
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
AGENTS_DIR = REPO_ROOT / ".claude" / "agents"
SKILLS_DIR = REPO_ROOT / ".claude" / "skills"

WEB_RESEARCHER_MD = AGENTS_DIR / "web-researcher.md"
ISSUE_REFINEMENT_LOOP_MD = SKILLS_DIR / "issue-refinement-loop" / "SKILL.md"

GEMINI_SKILL_DIR = SKILLS_DIR / "gemini-cli-headless-delegation"
REFERENCES_DIR = GEMINI_SKILL_DIR / "references"
PROVIDER_MAPPING_MD = REFERENCES_DIR / "provider-mapping.md"
RUNTIME_PORTABILITY_MD = REFERENCES_DIR / "runtime-portability.md"
USAGE_CONTRACT_MD = REFERENCES_DIR / "usage-contract.md"
RUN_GEMINI_HEADLESS_PY = GEMINI_SKILL_DIR / "scripts" / "run_gemini_headless.py"


def _load_run_gemini_headless() -> types.ModuleType:
    """Load run_gemini_headless.py under a unique module name (hermetic).

    Uses a distinct name from other test files\' module loads
    (e.g. test_agy_provider.py uses "run_gemini_headless") to avoid
    sys.modules collisions when both test files run in the same session.
    """
    spec = importlib.util.spec_from_file_location(
        "run_gemini_headless_docs_drift_check", RUN_GEMINI_HEADLESS_PY
    )
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


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


# ---------------------------------------------------------------------------
# references/*.md 固有: provider-mapping / runtime-portability / usage-contract
# の docs/runtime drift 検査（Issue #1268）
# ---------------------------------------------------------------------------


class TestReferencesDocsRuntimeDrift:
    """provider-mapping.md / runtime-portability.md / usage-contract.md が
    run_gemini_headless.py の現行実装と矛盾しないことを検査する。"""

    def test_provider_mapping_exists(self):
        assert PROVIDER_MAPPING_MD.exists(), f"{PROVIDER_MAPPING_MD} が存在しない"

    def test_runtime_portability_exists(self):
        assert RUNTIME_PORTABILITY_MD.exists(), f"{RUNTIME_PORTABILITY_MD} が存在しない"

    def test_usage_contract_exists(self):
        assert USAGE_CONTRACT_MD.exists(), f"{USAGE_CONTRACT_MD} が存在しない"

    def test_usage_contract_no_delegation_result_v1_underscore(self):
        text = _read(USAGE_CONTRACT_MD)
        assert "delegation_result_v1" not in text, (
            "usage-contract.md に古いアンダースコア表記 delegation_result_v1 が残っている。"
            " delegation_result/v1 に統一すること。"
        )

    def test_usage_contract_has_delegation_result_slash_v1(self):
        text = _read(USAGE_CONTRACT_MD)
        assert "delegation_result/v1" in text, (
            "usage-contract.md に正規の delegation_result/v1 表記がない"
        )

    def test_runtime_portability_no_legacy_104_permanent_reference(self):
        text = _read(RUNTIME_PORTABILITY_MD)
        assert "恒久対応は parent Issue #104" not in text, (
            "runtime-portability.md が parent Issue #104 を恒久対応の正本として"
            " 参照したままになっている。#1265 / current references に更新すること。"
        )

    def test_runtime_portability_mentions_shell_false(self):
        text = _read(RUNTIME_PORTABILITY_MD)
        assert "shell=False" in text, (
            "runtime-portability.md に agy 実行の shell=False 制約が明記されていない"
        )

    def test_usage_contract_post_to_issue_url_is_issue_only(self):
        text = _read(USAGE_CONTRACT_MD)
        assert "GitHub Issue/PR" not in text, (
            "usage-contract.md の post_to_issue_url が Issue/PR 両対応であるかのように"
            " 記述されている。GitHub Issue URL only（pulls/<number> 不可）と明記すること。"
        )

    def test_usage_contract_gh_commands_not_claimed_for_non_github_research(self):
        text = _read(USAGE_CONTRACT_MD)
        assert not re.search(
            r"local_asset_research.*完全実装済み|proposal_only.*完全実装済み",
            text,
        ), (
            "usage-contract.md が gh_commands は local_asset_research / proposal_only"
            " でも完全実装済みと記述している。runtime は tool_profile='github_research'"
            " のみ許可し、それ以外は fail-closed であるため記述を修正すること。"
        )

    def test_usage_contract_gh_commands_github_research_only_stated(self):
        text = _read(USAGE_CONTRACT_MD)
        assert "gh_commands is only allowed with tool_profile" in text, (
            "usage-contract.md に gh_commands が github_research profile のみ許可される"
            " runtime の fail-closed メッセージが明記されていない"
        )

    def test_provider_mapping_github_research_unsupported_for_agy(self):
        text = _read(PROVIDER_MAPPING_MD)
        assert re.search(
            r"`github_research`\s*\|\s*\*\*unsupported_provider_profile\*\*", text
        ), (
            "provider-mapping.md の agy 対応表で github_research が"
            " unsupported_provider_profile と明記されていない"
        )

    def test_provider_mapping_agy_supported_profiles_match_runtime(self):
        """provider-mapping.md の agy supported profile 一覧が
        run_gemini_headless.AGY_SUPPORTED_PROFILES と一致することを確認する。"""
        rgh = _load_run_gemini_headless()
        text = _read(PROVIDER_MAPPING_MD)
        for profile in rgh.AGY_SUPPORTED_PROFILES:
            assert re.search(
                rf"`{re.escape(profile)}`\s*\|\s*(supported|\*\*supported\*\*)", text
            ), (
                f"provider-mapping.md の agy 対応表に runtime AGY_SUPPORTED_PROFILES の"
                f" '{profile}' が supported として記載されていない"
            )
        # github_research is intentionally excluded from AGY_SUPPORTED_PROFILES.
        assert rgh.GITHUB_RESEARCH_PROFILE not in rgh.AGY_SUPPORTED_PROFILES, (
            "runtime AGY_SUPPORTED_PROFILES に github_research が含まれるようになった。"
            " docs 側の unsupported 記述を見直すこと。"
        )

    def test_usage_contract_agy_auth_precondition_documented(self):
        text = _read(USAGE_CONTRACT_MD)
        assert "provider=agy" in text and "OAuth" in text, (
            "usage-contract.md に provider=agy の OAuth 系認証前提が明記されていない"
        )

    # -----------------------------------------------------------------------
    # PR #1362 OWNER REQUEST_CHANGES (#1268 fix_delta) 追加テスト
    # -----------------------------------------------------------------------

    def test_runtime_portability_no_serena_mcp_server_command_token(self):
        text = _read(RUNTIME_PORTABILITY_MD)
        assert "serena-mcp-server" not in text, (
            "runtime-portability.md に旧コマンド名 'serena-mcp-server' が残っている。"
            " 現行 contract は 'serena start-mcp-server' である。"
        )

    def test_runtime_portability_no_unpinned_serena_source(self):
        text = _read(RUNTIME_PORTABILITY_MD)
        unpinned = re.findall(r"git\+https://github\.com/oraios/serena(?!@)", text)
        assert not unpinned, (
            "runtime-portability.md に unpinned 'git+https://github.com/oraios/serena'"
            "（@<ref> なし）が残っている。pinned ref を明示すること。"
        )

    def test_runtime_portability_mentions_agy_mcp_config_contract(self):
        text = _read(RUNTIME_PORTABILITY_MD)
        for token in (
            ".agents/mcp_config.json",
            "excludeTools",
            "pinned_ref",
            "serena start-mcp-server",
        ):
            assert token in text, (
                f"runtime-portability.md に現行 Serena MCP contract のトークン"
                f" '{token}' が含まれていない"
            )

    def test_runtime_portability_separates_common_and_local_asset_research_prereqs(self):
        text = _read(RUNTIME_PORTABILITY_MD)
        assert "provider=agy`（共通前提" in text, (
            "runtime-portability.md に provider=agy の共通前提の節見出しがない"
        )
        assert "local_asset_research`（wrapper-side Serena 前提）" in text, (
            "runtime-portability.md に provider=agy + local_asset_research の"
            " wrapper-side Serena 前提を分離した節見出しがない"
        )

    def test_provider_mapping_tool_profiles_table_has_github_research_row(self):
        text = _read(PROVIDER_MAPPING_MD)
        assert re.search(r"\|\s*`github_research`\s*\|", text), (
            "provider-mapping.md の Tool Profiles 表に github_research 行がない"
        )

    def test_provider_mapping_no_unconditional_no_model_fallback_claim(self):
        text = _read(PROVIDER_MAPPING_MD)
        assert "既定は `gemini-3-flash-preview` で、別 model への自動 fallback はしない" not in text, (
            "provider-mapping.md に model fallback なしという無条件記述が残っている。"
            " 明示 model 指定時 / role・model_chain 時 / provider=auto 時を分けて記述すること。"
        )

    def test_provider_mapping_documents_provider_auto_policy(self):
        text = _read(PROVIDER_MAPPING_MD)
        assert "provider_auto_policy_v1" in text, (
            "provider-mapping.md に provider_auto_policy_v1 の説明がない"
        )
        assert 'provider=auto`' in text or "provider=\"auto\"" in text, (
            "provider-mapping.md に runtime provider=auto の説明がない"
        )

    def test_provider_mapping_distinguishes_setup_check_auto_from_runtime_auto(self):
        text = _read(PROVIDER_MAPPING_MD)
        assert "setup_check.py --provider auto" in text and "provider_auto_dispatch" in text, (
            "provider-mapping.md が setup_check.py --provider auto（環境 probe）と"
            " runtime provider=auto（provider_auto_dispatch）を区別して記述していない"
        )

    def test_usage_contract_documents_provider_auto_result_fields(self):
        text = _read(USAGE_CONTRACT_MD)
        for field in (
            "selected_provider",
            "provider_attempts",
            "fallback_reason",
            "fallback_policy_version",
            "attempts_by_model",
        ):
            assert field in text, (
                f"usage-contract.md に provider=auto の result field '{field}' が"
                " 明記されていない"
            )
        assert re.search(r"provider=\"auto\".*場合のみ存在|provider=\"auto\"\s*の場合のみ", text), (
            "usage-contract.md が provider=auto の result field を条件付き field として"
            " 明記していない"
        )

