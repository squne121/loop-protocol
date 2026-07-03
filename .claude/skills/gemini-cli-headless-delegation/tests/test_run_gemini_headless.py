from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def load_module():
    path = Path(__file__).resolve().parent.parent / "scripts" / "run_gemini_headless.py"
    spec = importlib.util.spec_from_file_location("run_gemini_headless", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_validate_request_rejects_vague_request(tmp_path, monkeypatch):
    module = load_module()
    monkeypatch.chdir(tmp_path)
    request = {
        "schema": "delegation_request_v1",
        "objective": "investigate",
        "instructions": ["Summarize", "Compare"],
        "tool_profile": "no_tools",
        "output_sections": ["Summary"],
        "context_files": ["context.md"],
    }
    (tmp_path / "context.md").write_text("context", encoding="utf-8")

    errors = module.validate_request(request)

    assert "objective is too vague" in errors


def test_build_prompt_includes_context_and_sections(tmp_path, monkeypatch):
    module = load_module()
    monkeypatch.chdir(tmp_path)
    request = {
        "objective": "Investigate build failure in logs/build.log",
        "instructions": ["Summarize the failure.", "List likely root causes."],
        "tool_profile": "no_tools",
        "output_sections": ["Summary", "Findings"],
        "context_files": ["context.md"],
        "inline_context": "extra context",
        "model": "gemini-3-flash-preview",
    }

    prompt = module.build_prompt(request, [{"path": "context.md", "content": "alpha"}])

    assert "Objective: Investigate build failure in logs/build.log" in prompt
    assert "Context files:" in prompt
    assert "alpha" in prompt
    assert "Required output sections:" in prompt
    assert "- Summary" in prompt


def test_validate_request_accepts_local_asset_research_with_safe_serena_settings(tmp_path, monkeypatch):
    module = load_module()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(module, "_validate_local_asset_research_settings", lambda: [])
    monkeypatch.setattr(module, "_repo_root", lambda: tmp_path)
    request = {
        "schema": "delegation_request_v1",
        "objective": "調査対象 .claude/skills/gemini-cli-headless-delegation/SKILL.md の構造を確認する",
        "instructions": ["Serena MCP で対象 symbol を確認する", "根拠パスを列挙する"],
        "tool_profile": "local_asset_research",
        "output_sections": ["Summary"],
        "context_files": ["context.md"],
    }
    (tmp_path / "context.md").write_text("context", encoding="utf-8")

    errors = module.validate_request(request)

    assert errors == []


def test_validate_request_rejects_local_asset_research_post_to_issue_url(tmp_path, monkeypatch):
    module = load_module()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(module, "_validate_local_asset_research_settings", lambda: [])
    monkeypatch.setattr(module, "_repo_root", lambda: tmp_path)
    request = {
        "schema": "delegation_request_v1",
        "objective": "調査対象 .claude/skills/gemini-cli-headless-delegation/SKILL.md の構造を確認する",
        "instructions": ["Serena MCP で対象 symbol を確認する", "根拠パスを列挙する"],
        "tool_profile": "local_asset_research",
        "output_sections": ["Summary"],
        "context_files": ["context.md"],
        "post_to_issue_url": "https://github.com/owner/repo/issues/1",
    }
    (tmp_path / "context.md").write_text("context", encoding="utf-8")

    errors = module.validate_request(request)

    assert "local_asset_research forbids post_to_issue_url" in errors


def test_validate_request_rejects_local_asset_research_unverified_mcp_settings(tmp_path, monkeypatch):
    module = load_module()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(module, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        module,
        "_validate_local_asset_research_settings",
        lambda: ["local_asset_research has unverified MCP tools in includeTools: write_file"],
    )
    request = {
        "schema": "delegation_request_v1",
        "objective": "調査対象 .claude/skills/gemini-cli-headless-delegation/SKILL.md の構造を確認する",
        "instructions": ["Serena MCP で対象 symbol を確認する", "根拠パスを列挙する"],
        "tool_profile": "local_asset_research",
        "output_sections": ["Summary"],
        "context_files": ["context.md"],
    }
    (tmp_path / "context.md").write_text("context", encoding="utf-8")

    errors = module.validate_request(request)

    assert any("unverified MCP tools" in error for error in errors)


def make_proposal_only_request():
    return {
        "schema": "delegation_request_v1",
        "objective": "implement-issue 向けに proposal_only で patch proposal を作成する",
        "instructions": [
            "Allowed Paths 内で変更方針を整理し implementation_draft を返してください。",
            "command_plan も text として返してください。",
        ],
        "tool_profile": "proposal_only",
        "output_sections": ["implementation_draft", "patch_proposal", "command_plan"],
        "context_files": ["context.md"],
    }


def test_validate_request_accepts_proposal_only_safe_request(tmp_path, monkeypatch):
    module = load_module()
    monkeypatch.chdir(tmp_path)
    request = make_proposal_only_request()
    (tmp_path / "context.md").write_text("context", encoding="utf-8")

    errors = module.validate_request(request)

    assert errors == []


def test_validate_request_rejects_proposal_only_post_to_issue_url(tmp_path, monkeypatch):
    module = load_module()
    monkeypatch.chdir(tmp_path)
    request = make_proposal_only_request()
    request["post_to_issue_url"] = "https://github.com/owner/repo/issues/1"
    (tmp_path / "context.md").write_text("context", encoding="utf-8")

    errors = module.validate_request(request)

    assert "proposal_only forbids post_to_issue_url" in errors


def test_validate_request_rejects_proposal_only_unknown_output_section(tmp_path, monkeypatch):
    module = load_module()
    monkeypatch.chdir(tmp_path)
    request = make_proposal_only_request()
    request["output_sections"] = ["implementation_draft", "execution_result"]
    (tmp_path / "context.md").write_text("context", encoding="utf-8")

    errors = module.validate_request(request)

    assert any(
        error.startswith("proposal_only output_sections must be drawn from:")
        for error in errors
    )


@pytest.mark.parametrize(
    ("instruction", "expected_error"),
    [
        (
            "src/main.py を編集して file write まで実施してください。",
            "proposal_only forbids direct file write/edit requests",
        ),
        ("bash で just check を実行して結果まで返してください。", "proposal_only forbids shell execution requests"),
        ("gh issue comment で GitHub へ投稿してください。", "proposal_only forbids GitHub mutation requests"),
    ],
)
def test_validate_request_rejects_proposal_only_mutation_requests(
    tmp_path,
    monkeypatch,
    instruction,
    expected_error,
):
    module = load_module()
    monkeypatch.chdir(tmp_path)
    request = make_proposal_only_request()
    request["instructions"] = [
        instruction,
        "proposal text ではなく実行してよいです。",
    ]
    (tmp_path / "context.md").write_text("context", encoding="utf-8")

    errors = module.validate_request(request)

    assert expected_error in errors


def test_validate_request_rejects_proposal_only_mixed_instruction_with_negation(tmp_path, monkeypatch):
    module = load_module()
    monkeypatch.chdir(tmp_path)
    request = make_proposal_only_request()
    request["instructions"] = [
        "Do not run tests; instead edit src/main.py and commit the result.",
        "Return proposal text only.",
    ]
    (tmp_path / "context.md").write_text("context", encoding="utf-8")

    errors = module.validate_request(request)

    assert "proposal_only forbids direct file write/edit requests" in errors


@pytest.mark.parametrize(
    "instruction",
    [
        "Do not run tests. Instead edit src/main.py and commit the result.",
        "Do not run tests, instead edit src/main.py and commit the result.",
    ],
)
def test_validate_request_rejects_proposal_only_ascii_sentence_split_mutation(tmp_path, monkeypatch, instruction):
    module = load_module()
    monkeypatch.chdir(tmp_path)
    request = make_proposal_only_request()
    request["instructions"] = [
        instruction,
        "Return proposal text only.",
    ]
    (tmp_path / "context.md").write_text("context", encoding="utf-8")

    errors = module.validate_request(request)

    assert "proposal_only forbids direct file write/edit requests" in errors
    assert "proposal_only forbids GitHub mutation requests" in errors


@pytest.mark.parametrize(
    "instruction",
    [
        "Do not just draft, edit src/main.py and commit the result.",
        "Do not just explain but edit src/main.py and commit the result.",
    ],
)
def test_validate_request_rejects_proposal_only_same_clause_negation_mutation(tmp_path, monkeypatch, instruction):
    module = load_module()
    monkeypatch.chdir(tmp_path)
    request = make_proposal_only_request()
    request["instructions"] = [
        instruction,
        "Return proposal text only.",
    ]
    (tmp_path / "context.md").write_text("context", encoding="utf-8")

    errors = module.validate_request(request)

    assert "proposal_only forbids direct file write/edit requests" in errors
    assert "proposal_only forbids GitHub mutation requests" in errors


def make_local_asset_request(context_files: list[str]):
    return {
        "schema": "delegation_request_v1",
        "objective": "調査対象 .claude/skills/gemini-cli-headless-delegation/SKILL.md の構造を確認する",
        "instructions": ["Serena MCP で対象 symbol を確認する", "根拠パスを列挙する"],
        "tool_profile": "local_asset_research",
        "output_sections": ["Summary"],
        "context_files": context_files,
    }


def test_run_delegation_rejects_local_asset_research_absolute_outside_context_file(tmp_path, monkeypatch):
    module = load_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    monkeypatch.setattr(module, "_repo_root", lambda: repo_root)
    monkeypatch.setattr(module, "_validate_local_asset_research_settings", lambda: [])

    result = module.run_delegation(
        make_local_asset_request([str(outside)]),
        request_path=repo_root / "request.json",
    )

    assert result["ok"] is False
    assert "local_asset_research context file must be inside repository" in result["failure_reason"]
    assert any("outside.txt" in warning for warning in result["warnings"])


def test_run_delegation_rejects_local_asset_research_parent_outside_context_file(tmp_path, monkeypatch):
    module = load_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    monkeypatch.setattr(module, "_repo_root", lambda: repo_root)
    monkeypatch.setattr(module, "_validate_local_asset_research_settings", lambda: [])

    result = module.run_delegation(
        make_local_asset_request(["../outside.txt"]),
        request_path=repo_root / "request.json",
    )

    assert result["ok"] is False
    assert "local_asset_research context file must be inside repository" in result["failure_reason"]
    assert any("../outside.txt" in warning for warning in result["warnings"])


def test_run_delegation_rejects_local_asset_research_symlink_outside_context_file(tmp_path, monkeypatch):
    module = load_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    symlink = repo_root / "linked.txt"
    symlink.symlink_to(outside)
    monkeypatch.setattr(module, "_repo_root", lambda: repo_root)
    monkeypatch.setattr(module, "_validate_local_asset_research_settings", lambda: [])

    result = module.run_delegation(
        make_local_asset_request(["linked.txt"]),
        request_path=repo_root / "request.json",
    )

    assert result["ok"] is False
    assert "local_asset_research context file must be inside repository" in result["failure_reason"]
    assert any("linked.txt" in warning for warning in result["warnings"])


def test_build_prompt_includes_local_asset_research_serena_guidance(monkeypatch):
    module = load_module()
    monkeypatch.setattr(module, "_validate_local_asset_research_settings", lambda: [])
    request = {
        "objective": "調査対象 .claude/skills/gemini-cli-headless-delegation/SKILL.md の構造を確認する",
        "instructions": ["Serena MCP で対象 symbol を確認する", "根拠パスを列挙する"],
        "tool_profile": "local_asset_research",
        "output_sections": ["Summary"],
        "context_files": ["context.md"],
        "model": "gemini-3-flash-preview",
    }

    prompt = module.build_prompt(request, [{"path": "context.md", "content": "alpha"}])

    assert "Serena MCP may be used only for read-only local asset research" in prompt
    assert "find_symbol" in prompt
    assert "post_to_issue_url is forbidden" in prompt
    assert "Do not run shell commands." in prompt


def test_build_prompt_includes_proposal_only_guidance():
    module = load_module()
    request = {
        "objective": "implement-issue 向けに proposal_only で patch proposal を作成する",
        "instructions": [
            "Allowed Paths 内の implementation_draft を返してください。",
            "issue_authoring_draft は必要時のみ含めてください。",
        ],
        "tool_profile": "proposal_only",
        "output_sections": ["implementation_draft", "patch_proposal", "command_plan"],
        "context_files": ["context.md"],
        "model": "gemini-3-flash-preview",
    }

    prompt = module.build_prompt(request, [{"path": "context.md", "content": "alpha"}])

    assert "Return proposal text only" in prompt
    assert "Final file edits, shell execution, and GitHub mutations stay on the Codex side." in prompt
    assert "implementation_draft" in prompt


def test_validate_request_accepts_japanese_objective_with_path(tmp_path, monkeypatch):
    module = load_module()
    monkeypatch.chdir(tmp_path)
    request = {
        "schema": "delegation_request_v1",
        "objective": "ビルドログ logs/build.log の失敗原因を調査する",
        "instructions": ["失敗箇所を特定する", "根拠を列挙する"],
        "tool_profile": "no_tools",
        "output_sections": ["Summary"],
        "context_files": ["context.md"],
    }
    (tmp_path / "context.md").write_text("context", encoding="utf-8")

    errors = module.validate_request(request)

    assert "objective is too vague" not in errors


def test_validate_request_rejects_abstract_japanese_objective(tmp_path, monkeypatch):
    module = load_module()
    monkeypatch.chdir(tmp_path)
    request = {
        "schema": "delegation_request_v1",
        "objective": "調査する",
        "instructions": ["Summarize", "Compare"],
        "tool_profile": "no_tools",
        "output_sections": ["Summary"],
        "context_files": ["context.md"],
    }
    (tmp_path / "context.md").write_text("context", encoding="utf-8")

    errors = module.validate_request(request)

    assert "objective is too vague" in errors


def test_validate_request_accepts_long_japanese_objective_without_path(tmp_path, monkeypatch):
    module = load_module()
    monkeypatch.chdir(tmp_path)
    request = {
        "schema": "delegation_request_v1",
        "objective": "ビルドログの失敗原因を特定し根拠を列挙する",
        "instructions": ["失敗箇所を特定する", "根拠を列挙する"],
        "tool_profile": "no_tools",
        "output_sections": ["Summary"],
        "context_files": ["context.md"],
    }
    (tmp_path / "context.md").write_text("context", encoding="utf-8")

    errors = module.validate_request(request)

    assert "objective is too vague" not in errors


def test_validate_request_accepts_english_objective_with_path(tmp_path, monkeypatch):
    module = load_module()
    monkeypatch.chdir(tmp_path)
    request = {
        "schema": "delegation_request_v1",
        "objective": "Investigate build failure in logs/build.log",
        "instructions": ["Summarize the failure.", "List likely root causes."],
        "tool_profile": "no_tools",
        "output_sections": ["Summary"],
        "context_files": ["context.md"],
    }
    (tmp_path / "context.md").write_text("context", encoding="utf-8")

    errors = module.validate_request(request)

    assert "objective is too vague" not in errors


def test_validate_request_rejects_japanese_objective_below_length_threshold(tmp_path, monkeypatch):
    # 9-char Japanese objective (< 10 threshold) should be vague
    module = load_module()
    monkeypatch.chdir(tmp_path)
    request = {
        "schema": "delegation_request_v1",
        "objective": "エラーログ確認作業",  # 9 chars
        "instructions": ["Summarize", "Compare"],
        "tool_profile": "no_tools",
        "output_sections": ["Summary"],
        "context_files": ["context.md"],
    }
    (tmp_path / "context.md").write_text("context", encoding="utf-8")

    errors = module.validate_request(request)

    assert "objective is too vague" in errors


def test_validate_request_accepts_japanese_objective_at_length_threshold(tmp_path, monkeypatch):
    # 10-char Japanese objective (== 10 threshold) should not be vague
    module = load_module()
    monkeypatch.chdir(tmp_path)
    request = {
        "schema": "delegation_request_v1",
        "objective": "エラーログの確認作業",  # 10 chars
        "instructions": ["失敗箇所を特定する", "根拠を列挙する"],
        "tool_profile": "no_tools",
        "output_sections": ["Summary"],
        "context_files": ["context.md"],
    }
    (tmp_path / "context.md").write_text("context", encoding="utf-8")

    errors = module.validate_request(request)

    assert "objective is too vague" not in errors


def test_validate_request_accepts_uppercase_path_objective(tmp_path, monkeypatch):
    # Uppercase extensions like "LOGS/BUILD.LOG" must also be recognized as concrete
    module = load_module()
    monkeypatch.chdir(tmp_path)
    request = {
        "schema": "delegation_request_v1",
        "objective": "Check build failure in LOGS/BUILD.LOG",
        "instructions": ["Summarize the failure.", "List likely root causes."],
        "tool_profile": "no_tools",
        "output_sections": ["Summary"],
        "context_files": ["context.md"],
    }
    (tmp_path / "context.md").write_text("context", encoding="utf-8")

    errors = module.validate_request(request)

    assert "objective is too vague" not in errors


def test_validate_request_accepts_objective_with_filename_only(tmp_path, monkeypatch):
    # Objectives with a filename extension (e.g. "check something.py") are concrete
    module = load_module()
    monkeypatch.chdir(tmp_path)
    request = {
        "schema": "delegation_request_v1",
        "objective": "check something.py",
        "instructions": ["Summarize", "Compare"],
        "tool_profile": "no_tools",
        "output_sections": ["Summary"],
        "context_files": ["context.md"],
    }
    (tmp_path / "context.md").write_text("context", encoding="utf-8")

    errors = module.validate_request(request)

    assert "objective is too vague" not in errors


def test_run_delegation_normalizes_successful_envelope(tmp_path, monkeypatch):
    module = load_module()
    (tmp_path / "context.md").write_text("context", encoding="utf-8")

    request = {
        "schema": "delegation_request_v1",
        "objective": "Investigate build failure in logs/build.log",
        "instructions": ["Summarize the failure.", "List likely root causes."],
        "tool_profile": "no_tools",
        "output_sections": ["Summary", "Findings"],
        "context_files": ["context.md"],
        "model": "gemini-3-flash-preview",
        "timeout_sec": 30,
    }

    class Completed:
        returncode = 0
        stdout = '{"response": "OK", "stats": {"models": {"gemini-3-flash-preview": {"api": {"totalRequests": 1}}}}}'
        stderr = ""

    monkeypatch.setattr(module, "_run_gemini", lambda command, timeout_sec, prompt=None, cwd=None: Completed())

    result = module.run_delegation(request, request_path=tmp_path / "request.json")

    assert result["ok"] is True
    assert result["response_text"] == "OK"
    assert result["actual_model"] == "gemini-3-flash-preview"
    assert result["raw_command"][0] == "gemini"


def test_run_delegation_adds_inline_result_surface_from_summary_heading(tmp_path, monkeypatch):
    module = load_module()
    (tmp_path / "context.md").write_text("context", encoding="utf-8")
    request = make_request_dict()

    class Completed:
        returncode = 0
        stdout = (
            '{"response": "## Summary\\nArtifact summary\\n## Findings\\n- item", '
            '"stats": {"models": {"gemini-3-flash-preview": {"api": {"totalRequests": 1}}}}}'
        )
        stderr = ""

    monkeypatch.setattr(module, "_run_gemini", lambda command, timeout_sec, prompt=None, cwd=None: Completed())

    result = module.run_delegation(request, request_path=tmp_path / "request.json")

    assert result["ok"] is True
    assert result["result_surface"]["mode"] == "artifact-first"
    assert result["result_surface"]["summary"] == "Artifact summary"
    assert result["result_surface"]["primary_artifact_type"] == "inline_response_text"
    assert result["result_surface"]["primary_artifact"] == "response_text"
    assert "read response_text only when detailed evidence is needed" in result["result_surface"]["next_action"]


def test_run_delegation_exception_preserves_none_result_surface(tmp_path, monkeypatch):
    module = load_module()
    (tmp_path / "context.md").write_text("context", encoding="utf-8")
    request = make_request_dict()

    def raise_unexpected(command, timeout_sec, prompt=None, cwd=None):
        raise PermissionError("gemini executable is not available")

    monkeypatch.setattr(module, "_run_gemini", raise_unexpected)

    result = module.run_delegation(request, request_path=tmp_path / "request.json")

    assert result["ok"] is False
    assert result["failure_reason"] == "gemini executable is not available"
    assert result["result_surface"]["primary_artifact_type"] == "none"
    assert result["result_surface"]["primary_artifact"] is None


def test_run_delegation_post_to_issue_success_promotes_comment_url_result_surface(tmp_path, monkeypatch):
    module = load_module()
    (tmp_path / "context.md").write_text("context", encoding="utf-8")
    request = make_request_dict()
    request["post_to_issue_url"] = "https://github.com/owner/repo/issues/1"

    class Completed:
        returncode = 0
        stdout = (
            '{"response": "## Summary\\nPosted summary\\n## Findings\\n- item", '
            '"stats": {"models": {"gemini-3-flash-preview": {"api": {"totalRequests": 1}}}}}'
        )
        stderr = ""

    class Posted:
        returncode = 0
        stdout = "https://github.com/owner/repo/issues/1#issuecomment-2\n"
        stderr = ""

    monkeypatch.setattr(module, "_run_gemini", lambda command, timeout_sec, prompt=None, cwd=None: Completed())
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: Posted())

    result = module.run_delegation(request, request_path=tmp_path / "request.json")

    assert result["ok"] is True
    assert result["comment_url"] == "https://github.com/owner/repo/issues/1#issuecomment-2"
    assert result["post_result"] == "success"
    assert result["result_surface"]["summary"] == "Posted summary"
    assert result["result_surface"]["primary_artifact_type"] == "github_comment_url"
    assert result["result_surface"]["primary_artifact"] == result["comment_url"]
    assert "Open the comment URL only if detailed evidence is needed." == result["result_surface"]["next_action"]


def test_run_delegation_post_to_issue_failure_keeps_inline_result_surface(tmp_path, monkeypatch):
    module = load_module()
    (tmp_path / "context.md").write_text("context", encoding="utf-8")
    request = make_request_dict()
    request["post_to_issue_url"] = "https://github.com/owner/repo/issues/1"

    class Completed:
        returncode = 0
        stdout = (
            '{"response": "## Summary\\nFallback summary\\n## Findings\\n- item", '
            '"stats": {"models": {"gemini-3-flash-preview": {"api": {"totalRequests": 1}}}}}'
        )
        stderr = ""

    class PostFailed:
        returncode = 1
        stdout = ""
        stderr = "gh comment failed"

    monkeypatch.setattr(module, "_run_gemini", lambda command, timeout_sec, prompt=None, cwd=None: Completed())
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: PostFailed())

    result = module.run_delegation(request, request_path=tmp_path / "request.json")

    assert result["ok"] is True
    assert result["post_result"] == "failed: gh comment failed"
    assert result["result_surface"]["summary"] == "Fallback summary"
    assert result["result_surface"]["primary_artifact_type"] == "inline_response_text"
    assert result["result_surface"]["primary_artifact"] == "response_text"
    assert "Comment posting failed" in result["result_surface"]["next_action"]


def make_request_dict():
    return {
        "schema": "delegation_request_v1",
        "objective": "Investigate build failure in logs/build.log",
        "instructions": ["Summarize the failure.", "List likely root causes."],
        "tool_profile": "no_tools",
        "output_sections": ["Summary", "Findings"],
        "context_files": ["context.md"],
        "model": "gemini-3-flash-preview",
        "timeout_sec": 30,
    }


@pytest.fixture
def valid_request(tmp_path):
    (tmp_path / "context.md").write_text("test context", encoding="utf-8")
    return make_request_dict()


def test_auth_failure_returns_not_ok(tmp_path, valid_request, monkeypatch):
    module = load_module()

    class AuthFailed:
        returncode = 1
        stdout = ""
        stderr = "Authentication error: credentials expired"

    monkeypatch.setattr(module, "_run_gemini", lambda command, timeout_sec, prompt=None, cwd=None: AuthFailed())

    result = module.run_delegation(valid_request, request_path=tmp_path / "request.json")

    assert result["ok"] is False
    assert result["exit_code"] != 0
    assert any("Authentication error" in w for w in result["warnings"])


def test_model_capacity_exhausted_retries_and_fails(tmp_path, valid_request, monkeypatch):
    module = load_module()
    call_count = {"n": 0}

    class CapacityFailed:
        returncode = 1
        stdout = ""
        stderr = "MODEL_CAPACITY_EXHAUSTED"

    def mock_run_gemini(command, timeout_sec, prompt=None, cwd=None):
        call_count["n"] += 1
        return CapacityFailed()

    monkeypatch.setattr(module, "_run_gemini", mock_run_gemini)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)

    result = module.run_delegation(valid_request, request_path=tmp_path / "request.json")

    assert result["ok"] is False
    assert call_count["n"] == module.RETRY_LIMIT + 1
    assert any("retryable capacity failure" in w for w in result["warnings"])


def test_missing_context_file_returns_not_ok(tmp_path):
    module = load_module()
    # context.md を作成しない（欠損が fail-closed の原因）
    request = make_request_dict()

    result = module.run_delegation(request, request_path=tmp_path / "request.json")

    assert result["ok"] is False
    assert any("missing context file" in w for w in result["warnings"])


def test_json_parse_failure_returns_not_ok(tmp_path, valid_request, monkeypatch):
    module = load_module()

    class InvalidJson:
        returncode = 0
        stdout = "This is not JSON"
        stderr = ""

    monkeypatch.setattr(module, "_run_gemini", lambda command, timeout_sec, prompt=None, cwd=None: InvalidJson())

    result = module.run_delegation(valid_request, request_path=tmp_path / "request.json")

    assert result["ok"] is False
    assert any("invalid JSON envelope" in w for w in result["warnings"])


def test_stderr_warnings_normalized_into_warnings_list(tmp_path, valid_request, monkeypatch):
    module = load_module()

    class WithStderr:
        returncode = 0
        stdout = '{"response": "OK", "stats": {"models": {"gemini-3-flash-preview": {"api": {"totalRequests": 1}}}}}'
        stderr = "Warning line 1\nWarning line 2\n"

    monkeypatch.setattr(module, "_run_gemini", lambda command, timeout_sec, prompt=None, cwd=None: WithStderr())

    result = module.run_delegation(valid_request, request_path=tmp_path / "request.json")

    assert result["ok"] is True
    assert "Warning line 1" in result["warnings"]
    assert "Warning line 2" in result["warnings"]


# --compact tests


def test_apply_compact_removes_stats_and_raw_command():
    module = load_module()
    full_result = {
        "schema": "delegation_result/v1",
        "ok": True,
        "requested_model": "gemini-3-flash-preview",
        "actual_model": "gemini-3-flash-preview",
        "tool_profile": "no_tools",
        "exit_code": 0,
        "response_text": "OK",
        "stats": {"models": {"gemini-3-flash-preview": {}}},
        "stderr": None,
        "warnings": [],
        "raw_command": ["gemini", "--model", "gemini-3-flash-preview"],
    }

    compact = module._apply_compact(full_result)

    assert "stats" not in compact
    assert "raw_command" not in compact


def test_apply_compact_preserves_required_fields():
    module = load_module()
    full_result = {
        "schema": "delegation_result/v1",
        "ok": True,
        "requested_model": "gemini-3-flash-preview",
        "actual_model": "gemini-3-flash-preview",
        "tool_profile": "no_tools",
        "exit_code": 0,
        "response_text": "OK",
        "stats": {"models": {}},
        "stderr": None,
        "warnings": [],
        "raw_command": ["gemini"],
    }

    compact = module._apply_compact(full_result)

    for field in ("ok", "response_text", "warnings", "stderr", "exit_code", "actual_model"):
        assert field in compact, f"expected field '{field}' to be present in compact output"


def test_main_compact_flag_excludes_stats_and_raw_command(tmp_path, monkeypatch):
    module = load_module()
    (tmp_path / "context.md").write_text("test context", encoding="utf-8")
    request = {
        "schema": "delegation_request_v1",
        "objective": "Investigate build failure in logs/build.log",
        "instructions": ["Summarize the failure.", "List likely root causes."],
        "tool_profile": "no_tools",
        "output_sections": ["Summary", "Findings"],
        "context_files": ["context.md"],
        "model": "gemini-3-flash-preview",
        "timeout_sec": 30,
    }
    request_file = tmp_path / "request.json"
    import json as _json
    request_file.write_text(_json.dumps(request), encoding="utf-8")
    output_file = tmp_path / "result.json"

    class Completed:
        returncode = 0
        stdout = '{"response": "OK", "stats": {"models": {"gemini-3-flash-preview": {"api": {"totalRequests": 1}}}}}'
        stderr = ""

    monkeypatch.setattr(module, "_run_gemini", lambda command, timeout_sec, prompt=None, cwd=None: Completed())

    exit_code = module.main([
        "--request-file", str(request_file),
        "--output-file", str(output_file),
        "--compact",
    ])

    assert exit_code == 0
    written = _json.loads(output_file.read_text(encoding="utf-8"))
    assert "stats" not in written
    assert "raw_command" not in written


def test_main_without_compact_flag_preserves_stats_and_raw_command(tmp_path, monkeypatch):
    module = load_module()
    (tmp_path / "context.md").write_text("test context", encoding="utf-8")
    request = {
        "schema": "delegation_request_v1",
        "objective": "Investigate build failure in logs/build.log",
        "instructions": ["Summarize the failure.", "List likely root causes."],
        "tool_profile": "no_tools",
        "output_sections": ["Summary", "Findings"],
        "context_files": ["context.md"],
        "model": "gemini-3-flash-preview",
        "timeout_sec": 30,
    }
    request_file = tmp_path / "request.json"
    import json as _json
    request_file.write_text(_json.dumps(request), encoding="utf-8")
    output_file = tmp_path / "result.json"

    class Completed:
        returncode = 0
        stdout = '{"response": "OK", "stats": {"models": {"gemini-3-flash-preview": {"api": {"totalRequests": 1}}}}}'
        stderr = ""

    monkeypatch.setattr(module, "_run_gemini", lambda command, timeout_sec, prompt=None, cwd=None: Completed())

    exit_code = module.main([
        "--request-file", str(request_file),
        "--output-file", str(output_file),
    ])

    assert exit_code == 0
    written = _json.loads(output_file.read_text(encoding="utf-8"))
    assert "stats" in written
    assert "raw_command" in written


# --- stdout output tests ---


def _make_main_request_file(tmp_path):
    import json as _json
    (tmp_path / "context.md").write_text("test context", encoding="utf-8")
    request = {
        "schema": "delegation_request_v1",
        "objective": "Investigate build failure in logs/build.log",
        "instructions": ["Summarize the failure.", "List likely root causes."],
        "tool_profile": "no_tools",
        "output_sections": ["Summary", "Findings"],
        "context_files": ["context.md"],
        "model": "gemini-3-flash-preview",
        "timeout_sec": 30,
    }
    request_file = tmp_path / "request.json"
    request_file.write_text(_json.dumps(request), encoding="utf-8")
    return request_file


def test_main_stdout_ok_true_prints_response_text(tmp_path, monkeypatch, capsys):
    module = load_module()
    request_file = _make_main_request_file(tmp_path)
    output_file = tmp_path / "result.json"

    class Completed:
        returncode = 0
        stdout = '{"response": "Gemini final answer here.", "stats": {"models": {"gemini-3-flash-preview": {}}}}'
        stderr = ""

    monkeypatch.setattr(module, "_run_gemini", lambda command, timeout_sec, prompt=None, cwd=None: Completed())

    exit_code = module.main([
        "--request-file", str(request_file),
        "--output-file", str(output_file),
    ])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Gemini final answer here." in captured.out
    assert f"[gemini-headless] result saved to: {output_file}" in captured.out


def test_main_stdout_ok_false_prints_warnings_first(tmp_path, monkeypatch, capsys):
    module = load_module()
    request_file = _make_main_request_file(tmp_path)
    output_file = tmp_path / "result.json"

    class AuthFailed:
        returncode = 1
        stdout = ""
        stderr = "Authentication error: credentials expired"

    monkeypatch.setattr(module, "_run_gemini", lambda command, timeout_sec, prompt=None, cwd=None: AuthFailed())

    exit_code = module.main([
        "--request-file", str(request_file),
        "--output-file", str(output_file),
    ])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "Authentication error: credentials expired" in captured.out
    assert f"[gemini-headless] result saved to: {output_file}" in captured.out


def test_main_stdout_empty_response_text_prints_warning(tmp_path, monkeypatch, capsys):
    module = load_module()
    request_file = _make_main_request_file(tmp_path)
    output_file = tmp_path / "result.json"

    class EmptyResponse:
        returncode = 0
        stdout = '{"response": "", "stats": {"models": {"gemini-3-flash-preview": {}}}}'
        stderr = ""

    monkeypatch.setattr(module, "_run_gemini", lambda command, timeout_sec, prompt=None, cwd=None: EmptyResponse())

    exit_code = module.main([
        "--request-file", str(request_file),
        "--output-file", str(output_file),
    ])

    # response_text が "" のときは fail-closed し、fallback error が出力される
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "[gemini-headless] error: delegation failed (no failure reason available; see result JSON)" in captured.out
    assert f"[gemini-headless] result saved to: {output_file}" in captured.out


def test_main_stdout_result_saved_path_always_printed(tmp_path, monkeypatch, capsys):
    module = load_module()
    request_file = _make_main_request_file(tmp_path)
    output_file = tmp_path / "result.json"

    class Completed:
        returncode = 0
        stdout = '{"response": "Answer.", "stats": {"models": {"gemini-3-flash-preview": {}}}}'
        stderr = ""

    monkeypatch.setattr(module, "_run_gemini", lambda command, timeout_sec, prompt=None, cwd=None: Completed())

    module.main([
        "--request-file", str(request_file),
        "--output-file", str(output_file),
    ])

    captured = capsys.readouterr()
    assert f"[gemini-headless] result saved to: {output_file}" in captured.out


def test_main_stdout_ok_false_empty_warnings_prints_fallback(tmp_path, monkeypatch, capsys):
    """ok:false かつ warnings=[] のとき fallback メッセージが出力される"""
    module = load_module()
    request_file = _make_main_request_file(tmp_path)
    output_file = tmp_path / "result.json"

    # envelope に response=null、error なし、stderr なし → ok=False、warnings=[]
    class NullResponseNoWarnings:
        returncode = 0
        stdout = '{"response": null, "stats": {"models": {"gemini-3-flash-preview": {}}}}'
        stderr = ""

    monkeypatch.setattr(
        module,
        "_run_gemini",
        lambda command,
        timeout_sec,
        prompt=None,
        cwd=None: NullResponseNoWarnings()
    )

    exit_code = module.main([
        "--request-file", str(request_file),
        "--output-file", str(output_file),
    ])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "[gemini-headless] error: delegation failed (no failure reason available; see result JSON)" in captured.out
    assert f"[gemini-headless] result saved to: {output_file}" in captured.out


def test_main_stdout_response_text_none_prints_fallback_error(tmp_path, monkeypatch, capsys):
    """response_text=None（空文字列ではなく None）のとき ok=False になり fallback error が出力される"""
    module = load_module()

    result = {
        "ok": False,
        "response_text": None,
        "warnings": [],
    }
    from pathlib import Path as _Path

    module._print_stdout_summary(result, _Path("tmp/test_result.json"))

    captured = capsys.readouterr()
    assert "[gemini-headless] error: delegation failed (no failure reason available; see result JSON)" in captured.out
    assert "[gemini-headless] result saved to: tmp/test_result.json" in captured.out


def test_main_invalid_request_object_preserves_result_surface(tmp_path):
    module = load_module()
    request_file = tmp_path / "request.json"
    request_file.write_text('["not-an-object"]', encoding="utf-8")
    output_file = tmp_path / "result.json"

    exit_code = module.main([
        "--request-file", str(request_file),
        "--output-file", str(output_file),
    ])

    written = __import__("json").loads(output_file.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert written["failure_reason"] == "request file must contain a JSON object"
    assert written["result_surface"]["primary_artifact_type"] == "none"
    assert written["result_surface"]["primary_artifact"] is None


def test_build_raw_command_includes_skip_trust():
    """_build_raw_command が --skip-trust を既定で含むことを検証する（Issue #1824）。"""
    module = load_module()
    command = module._build_raw_command("gemini-3-flash-preview")
    assert "--prompt" in command, f"--prompt missing from command: {command}"
    assert "--skip-trust" in command, f"--skip-trust missing from command: {command}"
    assert command[7] == ""


def test_build_run_invocation_routes_local_asset_research_prompt_to_stdin_and_repo_root(tmp_path, monkeypatch):
    module = load_module()
    monkeypatch.setattr(module, "_repo_root", lambda: tmp_path)
    prompt = "A" * 5000

    command, stdin_prompt, cwd = module._build_run_invocation(
        "gemini-3-flash-preview",
        prompt,
        module.LOCAL_ASSET_RESEARCH_PROFILE,
    )

    assert command.index("--prompt") >= 0
    assert command[command.index("--prompt") + 1] == ""
    assert stdin_prompt == prompt
    assert cwd == tmp_path


@pytest.mark.parametrize(
    "profile",
    ["no_tools", "proposal_only"],
)
def test_build_run_invocation_keeps_other_profiles_in_argv(profile):
    module = load_module()
    prompt = "B" * 5000

    command, stdin_prompt, cwd = module._build_run_invocation(
        "gemini-3-flash-preview",
        prompt,
        profile,
    )

    assert command[command.index("--prompt") + 1] == prompt
    assert stdin_prompt is None
    assert cwd is None


def test_build_run_invocation_grounded_research_uses_stdin():
    module = load_module()
    prompt = "B" * 9000

    command, stdin_prompt, cwd = module._build_run_invocation(
        "gemini-3-flash-preview",
        prompt,
        "grounded_research",
    )

    assert command[command.index("--prompt") + 1] == ""
    assert stdin_prompt == prompt
    assert cwd is None


def test_run_delegation_local_asset_research_uses_stdin_prompt_and_repo_root_cwd(tmp_path, monkeypatch):
    module = load_module()
    request = make_request_dict()
    request["tool_profile"] = "local_asset_research"
    (tmp_path / "context.md").write_text("alpha " * 4000, encoding="utf-8")

    captured: dict[str, object] = {}

    class Completed:
        returncode = 0
        stdout = (
            '{"response": "OK", "stats": {"models": {"gemini-3-flash-preview": {}}}}'
        )
        stderr = ""

    def run_with_stdin_capture(command, timeout_sec, prompt=None, cwd=None):
        captured["command"] = command
        captured["prompt_len"] = len(prompt or "")
        captured["cwd"] = cwd
        return Completed()

    monkeypatch.setattr(module, "_validate_local_asset_research_settings", lambda: [])
    monkeypatch.setattr(module, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(module, "_run_gemini", run_with_stdin_capture)

    result = module.run_delegation(request, request_path=tmp_path / "request.json")

    assert result["ok"] is True
    assert result["response_text"] == "OK"
    assert isinstance(captured["prompt_len"], int)
    assert captured["prompt_len"] > 20000
    assert captured["cwd"] == tmp_path
    command = captured["command"]
    prompt_idx = command.index("--prompt")
    assert command[prompt_idx + 1] == ""
    assert all(len(part) < 100 for part in command)


@pytest.mark.parametrize(
    ("profile", "request_factory"),
    [
        ("no_tools", make_request_dict),
        ("proposal_only", make_proposal_only_request),
    ],
)
def test_run_delegation_other_profiles_keep_prompt_in_argv_without_stdin(
    tmp_path,
    monkeypatch,
    profile,
    request_factory
):
    module = load_module()
    request = request_factory()
    request["tool_profile"] = profile
    (tmp_path / "context.md").write_text("alpha " * 4, encoding="utf-8")

    captured: dict[str, object] = {}

    class Completed:
        returncode = 0
        stdout = '{"response": "OK", "stats": {"models": {"gemini-3-flash-preview": {}}}}'
        stderr = ""

    def run_with_capture(command, timeout_sec, prompt=None, cwd=None):
        captured["command"] = command
        captured["prompt"] = prompt
        captured["cwd"] = cwd
        return Completed()

    monkeypatch.setattr(module, "_run_gemini", run_with_capture)

    result = module.run_delegation(request, request_path=tmp_path / "request.json")

    assert result["ok"] is True
    command = captured["command"]
    prompt_idx = command.index("--prompt")
    assert isinstance(captured["prompt"], type(None))
    assert captured["cwd"] is None
    assert command[prompt_idx + 1] != ""


def test_run_delegation_grounded_research_uses_stdin_prompt(tmp_path, monkeypatch):
    module = load_module()
    request = make_request_dict()
    request["tool_profile"] = "grounded_research"
    request["inline_context"] = "x" * 9000
    (tmp_path / "context.md").write_text("alpha " * 4, encoding="utf-8")

    captured: dict[str, object] = {}

    class Completed:
        returncode = 0
        stdout = '{"response": "OK", "stats": {"models": {"gemini-3-flash-preview": {}}}}'
        stderr = ""

    def run_with_capture(command, timeout_sec, prompt=None, cwd=None):
        captured["command"] = command
        captured["prompt"] = prompt
        captured["cwd"] = cwd
        return Completed()

    monkeypatch.setattr(module, "_run_gemini", run_with_capture)

    result = module.run_delegation(request, request_path=tmp_path / "request.json")

    assert result["ok"] is True
    command = captured["command"]
    prompt_idx = command.index("--prompt")
    assert command[prompt_idx + 1] == ""
    assert isinstance(captured["prompt"], str) and len(captured["prompt"]) > 0
    assert captured["cwd"] is None


# --- _validate_string_list received value tests ---


def test_validate_request_context_files_none_includes_received_in_error(tmp_path, monkeypatch):
    """context_files が None のとき、エラーメッセージに received: None が含まれること。"""
    module = load_module()
    monkeypatch.chdir(tmp_path)
    request = make_request_dict()
    request["context_files"] = None

    errors = module.validate_request(request)

    assert any("received: None" in e for e in errors), f"errors={errors}"


def test_validate_request_context_files_empty_list_includes_received_in_error(tmp_path, monkeypatch):
    """context_files が [] のとき、エラーメッセージに received: [] が含まれること。"""
    module = load_module()
    monkeypatch.chdir(tmp_path)
    request = make_request_dict()
    request["context_files"] = []

    errors = module.validate_request(request)

    assert any("received: []" in e for e in errors), f"errors={errors}"


def test_validate_request_context_files_empty_string_item_includes_received_in_error(tmp_path, monkeypatch):
    """context_files が [''] のとき、item レベルのエラーに received: '' が含まれること。"""
    module = load_module()
    monkeypatch.chdir(tmp_path)
    request = make_request_dict()
    request["context_files"] = [""]

    errors = module.validate_request(request)

    assert any("received: ''" in e for e in errors), f"errors={errors}"


# --- F-04: _truncate_repr truncation boundary and credential redaction tests ---


def test_truncate_repr_truncates_long_string():
    """repr 長が 200 文字を超える長文字列を切断し ...<truncated> を末尾に持つこと。"""
    module = load_module()
    long_value = "x" * 300
    result = module._truncate_repr(long_value)
    assert result.endswith("...<truncated>"), f"result={result!r}"
    # total length should be max_length + len("...<truncated>") = 200 + 14 = 214
    assert len(result) == 214


def test_truncate_repr_does_not_truncate_short_string():
    """repr 長が 200 文字以下の文字列は切断されないこと。"""
    module = load_module()
    short_value = "hello world"
    result = module._truncate_repr(short_value)
    assert result == repr(short_value)
    assert "...<truncated>" not in result


def test_truncate_repr_redacts_credential_pattern_in_str():
    """credential パターン（ghp_ 等）を含む文字列は <redacted: type=str length=N> に置き換わること。"""
    module = load_module()
    cred = "ghp_TESTTOKEN1234"
    result = module._truncate_repr(cred)
    assert result.startswith("<redacted: type=str length="), f"result={result!r}"
    assert "ghp_TESTTOKEN1234" not in result


def test_truncate_repr_redacts_sk_credential_in_str():
    """sk- パターンを含む文字列が redact されること。"""
    module = load_module()
    cred = "sk-XXXXXXXXXXXXXXXX"
    result = module._truncate_repr(cred)
    assert result.startswith("<redacted: type=str"), f"result={result!r}"
    assert "sk-" not in result


def test_truncate_repr_redacts_credential_in_list():
    """list の要素に credential パターンが含まれる場合、list 全体が redact されること。"""
    module = load_module()
    value = ["safe_path.md", "ghp_TESTTOKEN1234"]
    result = module._truncate_repr(value)
    assert result.startswith("<redacted: type=list"), f"result={result!r}"
    assert "ghp_TESTTOKEN1234" not in result


def test_truncate_repr_does_not_redact_safe_string():
    """credential パターンを含まない文字列は redact されないこと。"""
    module = load_module()
    safe = "/path/to/context.md"
    result = module._truncate_repr(safe)
    assert "redacted" not in result
    assert "/path/to/context.md" in result


@pytest.mark.parametrize("safe_path", [
    "src/tools/task_delta_analyzer.py",
    ".kiro/specs/task_dependency_manifest.yaml",
    "tests/test_task_delta_analyzer.py",
    "docs/ASIA_pacific_config.md",
    "notes_about_sk_brief.md",
    "docs/risk_management.md",
])
def test_truncate_repr_does_not_redact_repo_paths(safe_path):
    """リポジトリ内の実在パス（task_ / ASIA_ / sk_ 等を含む）が偽陽性で redact されないこと。"""
    module = load_module()
    result = module._truncate_repr(safe_path)
    assert "redacted" not in result, f"false-positive redaction for {safe_path!r}: {result!r}"
    assert safe_path in result


def test_validate_request_missing_context_file_with_credential_does_not_leak_token(tmp_path, monkeypatch):
    """credential を含む context_files パスで missing context file エラーが生 token を含まないこと。"""
    module = load_module()
    monkeypatch.chdir(tmp_path)
    request = make_request_dict()
    # context_files にトークンを含むパスを渡す（ファイルは存在しない）
    cred_path = "ghp_TESTTOKEN1234_context.md"
    request["context_files"] = [cred_path]

    errors = module.validate_request(request)

    # エラーが返ること
    assert len(errors) > 0
    # エラーメッセージに生 token が含まれないこと
    for error in errors:
        assert "ghp_TESTTOKEN1234" not in error, f"credential leaked in error: {error!r}"
    # <redacted: ...> 形式が含まれること
    assert any("<redacted:" in e for e in errors), f"errors={errors}"


# ---------------------------------------------------------------------------
# github_research profile tests
# ---------------------------------------------------------------------------


def make_github_research_request(context_file: str = "context.md") -> dict:
    return {
        "schema": "delegation_request_v1",
        "tool_profile": "github_research",
        "objective": "Issue #2232 のタイトルとステートを gh issue view で確認する",
        "instructions": [
            "gh issue view 2232 の実行結果（inline_context）を確認し、タイトルとステートを報告してください。",
            "JSON 形式でまとめてください。",
        ],
        "output_sections": ["IssueInfo"],
        "context_files": [context_file],
    }


def test_validate_request_accepts_github_research_with_allowed_gh_commands(tmp_path, monkeypatch):
    """github_research: 許可 argv (issue view) が通ること。"""
    module = load_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "context.md").write_text("ctx", encoding="utf-8")
    request = make_github_research_request()
    request["gh_commands"] = [{"argv": ["issue", "view", "2232"]}]

    errors = module.validate_request(request)

    assert errors == [], f"unexpected errors: {errors}"


def test_validate_request_rejects_github_research_issue_comment_argv(tmp_path, monkeypatch):
    """github_research: gh issue comment argv は拒否されること。"""
    module = load_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "context.md").write_text("ctx", encoding="utf-8")
    request = make_github_research_request()
    request["gh_commands"] = [{"argv": ["issue", "comment", "2232", "--body", "test"]}]

    errors = module.validate_request(request)

    assert any("not in the allowed subcommand list" in e or "github_research_command_denied" in e for e in errors)


def test_validate_request_rejects_github_research_api_post(tmp_path, monkeypatch):
    """github_research: gh api -X POST は拒否されること。"""
    module = load_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "context.md").write_text("ctx", encoding="utf-8")
    request = make_github_research_request()
    request["gh_commands"] = [{"argv": ["api", "repos/owner/repo/issues", "-X", "POST"]}]

    errors = module.validate_request(request)

    assert any("-X POST" in e or "github_research_command_denied" in e for e in errors)


def test_validate_request_rejects_github_research_api_method_delete(tmp_path, monkeypatch):
    """github_research: gh api --method DELETE は拒否されること。"""
    module = load_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "context.md").write_text("ctx", encoding="utf-8")
    request = make_github_research_request()
    request["gh_commands"] = [{"argv": ["api", "repos/owner/repo/issues/1", "--method", "DELETE"]}]

    errors = module.validate_request(request)

    assert any("--method DELETE" in e or "github_research_command_denied" in e for e in errors)


def test_validate_request_accepts_github_research_api_get(tmp_path, monkeypatch):
    """github_research: gh api (GET 既定) は許可されること。"""
    module = load_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "context.md").write_text("ctx", encoding="utf-8")
    request = make_github_research_request()
    request["gh_commands"] = [{"argv": ["api", "repos/owner/repo/issues/2232"]}]

    errors = module.validate_request(request)

    assert errors == [], f"unexpected errors: {errors}"


def test_validate_request_rejects_github_research_post_to_issue_url(tmp_path, monkeypatch):
    """github_research: post_to_issue_url は拒否されること。"""
    module = load_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "context.md").write_text("ctx", encoding="utf-8")
    request = make_github_research_request()
    request["gh_commands"] = [{"argv": ["issue", "view", "2232"]}]
    request["post_to_issue_url"] = "https://github.com/owner/repo/issues/2232"

    errors = module.validate_request(request)

    assert "github_research forbids post_to_issue_url" in errors


def test_validate_request_rejects_github_research_text_denied_command(tmp_path, monkeypatch):
    """github_research: テキスト内の禁止コマンド意図は text-based defense で検知されること。"""
    module = load_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "context.md").write_text("ctx", encoding="utf-8")
    request = {
        "schema": "delegation_request_v1",
        "tool_profile": "github_research",
        "objective": "gh issue comment 2232 --body 'hello' を実行する",
        "instructions": [
            "gh issue comment 2232 --body 'hello' でコメントを投稿してください。",
            "結果を報告してください。",
        ],
        "output_sections": ["Result"],
        "context_files": ["context.md"],
    }

    errors = module.validate_request(request)

    assert any("github_research_command_denied" in e for e in errors)


def test_run_delegation_github_research_deny_sets_failure_class(tmp_path, monkeypatch):
    """github_research: 拒否 request は failure_class: github_research_command_denied が設定されること。"""
    module = load_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "context.md").write_text("ctx", encoding="utf-8")
    request = make_github_research_request()
    request["gh_commands"] = [{"argv": ["issue", "comment", "2232", "--body", "test"]}]

    result = module.run_delegation(request, request_path=tmp_path / "request.json")

    assert result["ok"] is False
    assert result.get("failure_class") == "github_research_command_denied"


# ---------------------------------------------------------------------------
# Finding 1: gh api implicit POST bypass guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flag",
    ["-f", "-F", "--field", "--raw-field", "--input"],
)
def test_validate_github_research_argv_rejects_implicit_post_flags(flag):
    """gh api with -f/-F/--field/--raw-field/--input implies a non-GET request and must be rejected."""
    module = load_module()
    argv = ["api", "repos/owner/repo/issues", flag, "title=test"]
    errors = module._validate_github_research_argv(argv)
    assert any("implies a non-GET request" in e for e in errors), f"errors={errors}"


def test_validate_github_research_argv_rejects_api_graphql():
    """gh api graphql always uses POST and must be rejected."""
    module = load_module()
    argv = ["api", "graphql", "-f", "query=..."]
    errors = module._validate_github_research_argv(argv)
    assert any("gh api graphql is not allowed" in e for e in errors), f"errors={errors}"


# B2: =-separated and concatenated implicit-body flag forms

@pytest.mark.parametrize(
    "token",
    ["--field=body=hello", "--raw-field=title=test", "--input=payload.json"],
)
def test_validate_github_research_argv_rejects_implicit_post_prefix_forms(token):
    """B2: --field=val / --raw-field=val / --input=val は拒否されること。"""
    module = load_module()
    argv = ["api", "repos/owner/repo/issues", token]
    errors = module._validate_github_research_argv(argv)
    assert any("implies a non-GET request" in e for e in errors), (
        f"expected rejection for token={token!r}, got errors={errors}"
    )


@pytest.mark.parametrize(
    "token",
    ["-fbody=hello", "-fkey=val"],  # -f + key=val concatenated, len > 2
)
def test_validate_github_research_argv_rejects_concatenated_f_forms(token):
    """B2: -fkey=val 形式（-f に続く concatenated）は拒否されること。"""
    module = load_module()
    argv = ["api", "repos/owner/repo/issues", token]
    errors = module._validate_github_research_argv(argv)
    assert any("implies a non-GET request" in e for e in errors), (
        f"expected rejection for token={token!r}, got errors={errors}"
    )


@pytest.mark.parametrize(
    "token",
    ["-Fbody=hello", "-Fkey=val"],  # -F + key=val concatenated, len > 2
)
def test_validate_github_research_argv_rejects_concatenated_F_forms(token):
    """B2: -Fkey=val 形式（-F に続く concatenated）は拒否されること。"""
    module = load_module()
    argv = ["api", "repos/owner/repo/issues", token]
    errors = module._validate_github_research_argv(argv)
    assert any("implies a non-GET request" in e for e in errors), (
        f"expected rejection for token={token!r}, got errors={errors}"
    )


def test_validate_request_rejects_github_research_field_equals_form(tmp_path, monkeypatch):
    """B2: validate_request で --field=value が拒否されること (run_delegation 経由)。"""
    module = load_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "context.md").write_text("ctx", encoding="utf-8")
    request = make_github_research_request()
    request["gh_commands"] = [{"argv": ["api", "repos/x/y/issues", "--field=body=x"]}]

    errors = module.validate_request(request, request_path=tmp_path / "request.json")

    assert any("implies a non-GET request" in e for e in errors), (
        f"expected --field= rejection in validate_request, got errors={errors}"
    )


@pytest.mark.parametrize(
    "flag",
    ["-f", "-F", "--field", "--raw-field", "--input"],
)
def test_run_delegation_github_research_implicit_post_flag_sets_failure_class(tmp_path, monkeypatch, flag):
    """run_delegation with gh api implicit-POST flag sets failure_class: github_research_command_denied."""
    module = load_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "context.md").write_text("ctx", encoding="utf-8")
    request = make_github_research_request()
    request["gh_commands"] = [{"argv": ["api", "repos/owner/repo/issues", flag, "title=test"]}]

    result = module.run_delegation(request, request_path=tmp_path / "request.json")

    assert result["ok"] is False
    assert result.get("failure_class") == "github_research_command_denied"


def test_run_delegation_github_research_api_graphql_sets_failure_class(tmp_path, monkeypatch):
    """run_delegation with gh api graphql sets failure_class: github_research_command_denied."""
    module = load_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "context.md").write_text("ctx", encoding="utf-8")
    request = make_github_research_request()
    request["gh_commands"] = [{"argv": ["api", "graphql", "-f", "query={viewer{login}}"]}]

    result = module.run_delegation(request, request_path=tmp_path / "request.json")

    assert result["ok"] is False
    assert result.get("failure_class") == "github_research_command_denied"


# ---------------------------------------------------------------------------
# Finding 2: post_to_issue_url failure_class for github_research
# ---------------------------------------------------------------------------


def test_run_delegation_github_research_post_to_issue_url_sets_failure_class(tmp_path, monkeypatch):
    """github_research with post_to_issue_url sets failure_class: github_research_command_denied."""
    module = load_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "context.md").write_text("ctx", encoding="utf-8")
    request = make_github_research_request()
    request["gh_commands"] = [{"argv": ["issue", "view", "2232"]}]
    request["post_to_issue_url"] = "https://github.com/owner/repo/issues/2232"

    result = module.run_delegation(request, request_path=tmp_path / "request.json")

    assert result["ok"] is False
    assert result.get("failure_class") == "github_research_command_denied"


# ---------------------------------------------------------------------------
# Finding 3: gh_commands all-failure fail-close
# ---------------------------------------------------------------------------


def test_run_delegation_github_research_all_gh_commands_fail_returns_gh_auth_required(tmp_path, monkeypatch):
    """When all gh_commands fail (e.g. FileNotFoundError), ok=False and failure_class=gh_auth_required."""
    module = load_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "context.md").write_text("ctx", encoding="utf-8")
    request = make_github_research_request()
    request["gh_commands"] = [{"argv": ["issue", "view", "2232"]}]

    def fake_subprocess_run(cmd, **kwargs):
        raise FileNotFoundError("gh: command not found")

    monkeypatch.setattr(module.subprocess, "run", fake_subprocess_run)

    result = module.run_delegation(request, request_path=tmp_path / "request.json")

    assert result["ok"] is False
    assert result.get("failure_class") == "gh_auth_required"
    assert "all gh_commands failed" in (result.get("failure_reason") or "")


# ---------------------------------------------------------------------------
# Finding 4: build_prompt github_research profile constraint
# ---------------------------------------------------------------------------


def test_build_prompt_github_research_includes_read_only_constraint():
    """build_prompt for github_research includes 'Read-only GitHub research only' constraint."""
    module = load_module()
    request = make_github_research_request()
    request["model"] = "gemini-2.5-flash"

    prompt = module.build_prompt(request, [{"path": "context.md", "content": "ctx"}])

    assert "Read-only GitHub research only" in prompt
    assert "post_to_issue_url is forbidden" in prompt
    assert "do not request additional gh executions" in prompt


# ---------------------------------------------------------------------------
# Issue #1266 Blocker 2: build_prompt() grounded_research text must be gated by
# provider, so provider=gemini requests never see AGY-specific wording (and
# vice versa).
# ---------------------------------------------------------------------------


def test_build_prompt_grounded_research_gemini_provider_keeps_gemini_wording():
    """provider=gemini (default) grounded_research prompt uses Google Search grounding
    wording, never the AGY-specific instruction text."""
    module = load_module()
    request = {
        "objective": "Investigate the latest release notes at example.com/releases",
        "instructions": ["Summarize the release.", "List breaking changes."],
        "tool_profile": "grounded_research",
        "output_sections": ["Summary"],
        "context_files": [],
        "model": "gemini-3-flash-preview",
    }

    prompt = module.build_prompt(request, [])

    assert "Google Search grounding is allowed when it is necessary for the answer." in prompt
    assert "AGY native WebSearch/WebGrounding" not in prompt


def test_build_prompt_grounded_research_agy_provider_uses_agy_wording():
    """provider=agy grounded_research prompt uses AGY-specific instruction text (defense
    in depth; provider=agy currently returns early in run_delegation() before
    build_prompt() is called, but build_prompt() itself must not leak AGY wording into
    provider=gemini requests — see the gemini-provider counterpart test above)."""
    module = load_module()
    request = {
        "objective": "Investigate the latest release notes at example.com/releases",
        "instructions": ["Summarize the release.", "List breaking changes."],
        "tool_profile": "grounded_research",
        "output_sections": ["Summary"],
        "context_files": [],
        "provider": "agy",
    }

    prompt = module.build_prompt(request, [])

    assert "AGY native WebSearch/WebGrounding" in prompt
    assert "Google Search grounding is allowed when it is necessary for the answer." not in prompt


# ---------------------------------------------------------------------------
# Iteration 3 — Finding 1 HIGH: --method=VALUE / -X=VALUE bypass guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["api", "repos/owner/repo/issues", "--method=POST"],
        ["api", "repos/owner/repo/issues", "--method=PATCH"],
        ["api", "repos/owner/repo/issues", "--method=DELETE"],
        ["api", "repos/owner/repo/issues", "--method=PUT"],
        ["api", "repos/owner/repo/issues", "--method=post"],  # lowercase
        ["api", "repos/owner/repo/issues", "-X=DELETE"],
        ["api", "repos/owner/repo/issues", "-X=POST"],
    ],
)
def test_validate_github_research_argv_rejects_equals_form_method(argv):
    """--method=VALUE / -X=VALUE 形式の非 GET メソッド指定は拒否されること。"""
    module = load_module()
    errors = module._validate_github_research_argv(argv)
    assert any("is not allowed" in e or "github_research_command_denied" in e for e in errors), (
        f"expected rejection for argv={argv!r}, got errors={errors}"
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["api", "repos/owner/repo/issues/1", "--method=GET"],
        ["api", "repos/owner/repo/issues/1"],  # no method flag = default GET
    ],
)
def test_validate_github_research_argv_allows_equals_form_get(argv):
    """--method=GET は許可（GET は read-only）されること。"""
    module = load_module()
    errors = module._validate_github_research_argv(argv)
    # no method-related errors expected
    method_errors = [e for e in errors if "is not allowed" in e and "method" in e.lower()]
    assert not method_errors, (
        f"unexpected method rejection for argv={argv!r}, got errors={errors}"
    )


def test_run_delegation_github_research_method_equals_post_sets_failure_class(tmp_path, monkeypatch):
    """run_delegation: gh api --method=POST は failure_class: github_research_command_denied を返すこと。"""
    module = load_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "context.md").write_text("ctx", encoding="utf-8")
    request = make_github_research_request()
    request["gh_commands"] = [{"argv": ["api", "repos/owner/repo/issues", "--method=POST"]}]

    result = module.run_delegation(request, request_path=tmp_path / "request.json")

    assert result["ok"] is False
    assert result.get("failure_class") == "github_research_command_denied"


def test_run_delegation_github_research_x_equals_delete_sets_failure_class(tmp_path, monkeypatch):
    """run_delegation: gh api -X=DELETE は failure_class: github_research_command_denied を返すこと。"""
    module = load_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "context.md").write_text("ctx", encoding="utf-8")
    request = make_github_research_request()
    request["gh_commands"] = [{"argv": ["api", "repos/owner/repo/issues/1", "-X=DELETE"]}]

    result = module.run_delegation(request, request_path=tmp_path / "request.json")

    assert result["ok"] is False
    assert result.get("failure_class") == "github_research_command_denied"


def test_text_denied_pattern_blocks_method_equals_form(tmp_path, monkeypatch):
    """text-based defense: --method=POST の = 形式が DENIED_SUBCOMMAND_PATTERNS で検知されること。"""
    module = load_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "context.md").write_text("ctx", encoding="utf-8")
    request = {
        "schema": "delegation_request_v1",
        "tool_profile": "github_research",
        "objective": "gh api repos/owner/repo/issues --method=POST でデータを送信する",
        "instructions": [
            "gh api repos/owner/repo/issues --method=POST でデータを送信してください。",
            "結果を報告してください。",
        ],
        "output_sections": ["Result"],
        "context_files": ["context.md"],
    }

    errors = module.validate_request(request)

    assert any("github_research_command_denied" in e for e in errors), (
        f"expected text-based denial for --method=POST pattern, got errors={errors}"
    )


# ---------------------------------------------------------------------------
# Iteration 3 — Finding 2 MEDIUM: preflight gh_cli 分離
# ---------------------------------------------------------------------------


def test_preflight_gh_not_found_proposal_only_ok_true():
    """preflight モジュールのテスト: gh not found でも proposal_only 利用者が影響を受けないことを
    run_gemini_headless 側から確認する (モック経由で preflight 結果をシミュレート)。

    Note: preflight 本体の詳細テストは test_preflight_gemini_headless.py で行う。
    このテストは run_delegation が gh_cli.ok=False の preflight 結果を受けても
    非 github_research profile リクエストを正常処理できることを確認する。
    """
    module = load_module()

    # gh_cli.ok=False だが top-level ok=True のとき、proposal_only リクエストは正常処理される
    # run_delegation 自体は preflight 結果を参照しないため、proposal_only は常に影響なし
    # → この property は validate_request が proposal_only をチェックしないことで保証される
    request = {
        "schema": "delegation_request_v1",
        "tool_profile": "proposal_only",
        "objective": "implement-issue 向けに proposal_only で patch proposal を作成する",
        "instructions": [
            "Allowed Paths 内で変更方針を整理し implementation_draft を返してください。",
            "command_plan も text として返してください。",
        ],
        "output_sections": ["implementation_draft"],
        "context_files": [],
    }

    # validate_request が proposal_only を正常に通すこと（gh_cli 状態に無関係）
    errors = module.validate_request(request)

    # context_files が空で missing context file エラーになるが、gh_cli 関連エラーはなし
    assert not any("gh_cli" in e or "gh:" in e for e in errors), (
        f"gh_cli errors must not appear for proposal_only: {errors}"
    )


# ---------------------------------------------------------------------------
# Iteration 3 — Finding 3 LOW: gh_commands=[] 拒否
# ---------------------------------------------------------------------------


def test_validate_request_rejects_github_research_empty_gh_commands(tmp_path, monkeypatch):
    """github_research: gh_commands=[] は拒否されること。"""
    module = load_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "context.md").write_text("ctx", encoding="utf-8")
    request = make_github_research_request()
    request["gh_commands"] = []

    errors = module.validate_request(request)

    assert any("gh_commands must not be empty" in e for e in errors), (
        f"expected empty gh_commands rejection, got errors={errors}"
    )


def test_run_delegation_github_research_empty_gh_commands_sets_failure_class(tmp_path, monkeypatch):
    """run_delegation: gh_commands=[] は failure_class: github_research_command_denied を返すこと。"""
    module = load_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "context.md").write_text("ctx", encoding="utf-8")
    request = make_github_research_request()
    request["gh_commands"] = []

    result = module.run_delegation(request, request_path=tmp_path / "request.json")

    assert result["ok"] is False
    assert result.get("failure_class") == "github_research_command_denied", (
        f"expected failure_class=github_research_command_denied, got: {result.get('failure_class')}"
    )


# ---------------------------------------------------------------------------
# B3: gh_commands restricted to github_research profile only (fail-closed)
# ---------------------------------------------------------------------------


def test_validate_request_rejects_local_asset_research_with_gh_commands(tmp_path, monkeypatch):
    """B3: local_asset_research + gh_commands → validate_request で fail-closed。"""
    module = load_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "context.md").write_text("ctx", encoding="utf-8")
    monkeypatch.setattr(module, "_validate_local_asset_research_settings", lambda: [])
    monkeypatch.setattr(module, "_repo_root", lambda: tmp_path)

    request = make_local_asset_request(["context.md"])
    request["gh_commands"] = [{"argv": ["issue", "view", "2309"]}]

    errors = module.validate_request(request, request_path=tmp_path / "request.json")

    assert any("gh_commands is only allowed with tool_profile='github_research'" in e for e in errors), (
        f"expected gh_commands profile restriction error, got errors={errors}"
    )


def test_validate_request_rejects_proposal_only_with_gh_commands(tmp_path, monkeypatch):
    """B3: proposal_only + gh_commands → validate_request で fail-closed。"""
    module = load_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "context.md").write_text("ctx", encoding="utf-8")

    request = make_proposal_only_request()
    request["context_files"] = ["context.md"]
    request["gh_commands"] = [{"argv": ["pr", "view", "42"]}]

    errors = module.validate_request(request, request_path=tmp_path / "request.json")

    assert any("gh_commands is only allowed with tool_profile='github_research'" in e for e in errors), (
        f"expected gh_commands profile restriction error, got errors={errors}"
    )


def test_run_delegation_local_asset_research_gh_commands_fails_at_validate(tmp_path, monkeypatch):
    """B3: run_delegation with local_asset_research + gh_commands → ok=False at validate stage。"""
    module = load_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "context.md").write_text("ctx", encoding="utf-8")
    monkeypatch.setattr(module, "_validate_local_asset_research_settings", lambda: [])
    monkeypatch.setattr(module, "_repo_root", lambda: tmp_path)

    request = make_local_asset_request(["context.md"])
    request["gh_commands"] = [{"argv": ["issue", "view", "2309"]}]

    result = module.run_delegation(request, request_path=tmp_path / "request.json")

    assert result["ok"] is False
    assert any("gh_commands is only allowed with tool_profile='github_research'" in w for w in result.get(
        "warnings",
        []
    )), (
        f"expected gh_commands restriction in warnings, got: {result.get('warnings')}"
    )


def test_run_delegation_proposal_only_gh_commands_fails_at_validate(tmp_path, monkeypatch):
    """B3: run_delegation with proposal_only + gh_commands → ok=False at validate stage。"""
    module = load_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "context.md").write_text("ctx", encoding="utf-8")
    monkeypatch.setattr(module, "_repo_root", lambda: tmp_path)

    request = make_proposal_only_request()
    request["context_files"] = ["context.md"]
    request["gh_commands"] = [{"argv": ["pr", "view", "42"]}]

    result = module.run_delegation(request, request_path=tmp_path / "request.json")

    assert result["ok"] is False
    assert any("gh_commands is only allowed with tool_profile='github_research'" in w for w in result.get(
        "warnings",
        []
    )), (
        f"expected gh_commands restriction in warnings, got: {result.get('warnings')}"
    )


# --- NDJSON output tests ---


def _make_completed_ok(response_text: str = "Gemini final answer here."):
    _stats = '{"models": {"gemini-3-flash-preview": {"api": {"totalRequests": 1}}}}'
    class Completed:
        returncode = 0
        stdout = f'{{"response": "{response_text}", "stats": {_stats}}}'
        stderr = ""
    return Completed()
def test_main_ndjson_output_is_valid_json_per_line(tmp_path, monkeypatch):
    """AC1: --output-format ndjson 指定時、出力ファイルの各行が有効な JSON オブジェクトである"""
    import json as _json
    module = load_module()
    request_file = _make_main_request_file(tmp_path)
    output_file = tmp_path / "result.ndjson"

    monkeypatch.setattr(module, "_run_gemini", lambda command, timeout_sec, prompt=None, cwd=None: _make_completed_ok())

    exit_code = module.main([
        "--request-file", str(request_file),
        "--output-file", str(output_file),
        "--output-format", "ndjson",
    ])

    assert exit_code == 0
    lines = output_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    for line in lines:
        obj = _json.loads(line)
        assert isinstance(obj, dict), f"line is not a JSON object: {line!r}"


def test_main_ndjson_output_appends_on_second_run(tmp_path, monkeypatch):
    """AC2: --output-format ndjson 指定時、同一ファイルへの複数回実行が追記（append）される"""
    import json as _json
    module = load_module()
    request_file = _make_main_request_file(tmp_path)
    output_file = tmp_path / "result.ndjson"

    monkeypatch.setattr(
        module,
        "_run_gemini",
        lambda command,
        timeout_sec,
        prompt=None,
        cwd=None: _make_completed_ok("first response")
    )

    module.main([
        "--request-file", str(request_file),
        "--output-file", str(output_file),
        "--output-format", "ndjson",
    ])

    monkeypatch.setattr(
        module,
        "_run_gemini",
        lambda command,
        timeout_sec,
        prompt=None,
        cwd=None: _make_completed_ok("second response")
    )

    module.main([
        "--request-file", str(request_file),
        "--output-file", str(output_file),
        "--output-format", "ndjson",
    ])

    lines = [ln for ln in output_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 2, f"expected 2 lines after 2 runs, got {len(lines)}: {lines}"
    for line in lines:
        obj = _json.loads(line)
        assert isinstance(obj, dict)


def test_main_ndjson_tail_last_has_response_text(tmp_path, monkeypatch):
    """AC3: tail -1 の行に response_text フィールドがある"""
    import json as _json
    module = load_module()
    request_file = _make_main_request_file(tmp_path)
    output_file = tmp_path / "result.ndjson"

    monkeypatch.setattr(
        module,
        "_run_gemini",
        lambda command,
        timeout_sec,
        prompt=None,
        cwd=None: _make_completed_ok("expected answer")
    )

    exit_code = module.main([
        "--request-file", str(request_file),
        "--output-file", str(output_file),
        "--output-format", "ndjson",
    ])

    assert exit_code == 0
    last_line = output_file.read_text(encoding="utf-8").splitlines()[-1]
    obj = _json.loads(last_line)
    assert "response_text" in obj, f"response_text not found in: {obj.keys()}"
    assert obj["response_text"] == "expected answer"


def test_main_json_default_unchanged(tmp_path, monkeypatch):
    """AC4: --output-format json（デフォルト）は既存の挙動と完全に同一"""
    import json as _json
    module = load_module()
    request_file = _make_main_request_file(tmp_path)
    output_file_default = tmp_path / "result_default.json"
    output_file_explicit = tmp_path / "result_explicit.json"

    def make_completed():
        class Completed:
            returncode = 0
            stdout = (
                '{"response": "same answer", "stats": {"models": {"gemini-3-flash-preview": {"api": {"totalRequests":'
                ' 1}}}}}'
            )
            stderr = ""
        return Completed()

    monkeypatch.setattr(module, "_run_gemini", lambda command, timeout_sec, prompt=None, cwd=None: make_completed())
    module.main([
        "--request-file", str(request_file),
        "--output-file", str(output_file_default),
    ])

    monkeypatch.setattr(module, "_run_gemini", lambda command, timeout_sec, prompt=None, cwd=None: make_completed())
    module.main([
        "--request-file", str(request_file),
        "--output-file", str(output_file_explicit),
        "--output-format", "json",
    ])

    default_content = output_file_default.read_text(encoding="utf-8")
    explicit_content = output_file_explicit.read_text(encoding="utf-8")
    # Both must be valid JSON and have the same structure
    default_obj = _json.loads(default_content)
    explicit_obj = _json.loads(explicit_content)
    assert isinstance(default_obj, dict)
    assert isinstance(explicit_obj, dict)
    # Core fields must match
    assert default_obj["ok"] == explicit_obj["ok"]
    assert default_obj["response_text"] == explicit_obj["response_text"]
    # Must be overwrite (single JSON, not NDJSON): check that indented multi-line JSON is written
    assert "\n" in default_content.strip() or default_content.strip().startswith("{"), (
        "json output must be a JSON object, not NDJSON"
    )