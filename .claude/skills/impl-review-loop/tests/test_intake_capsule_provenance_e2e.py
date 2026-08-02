"""test_intake_capsule_provenance_e2e.py

Issue #1950 AC11: production-shaped subprocess test verifying, in a single
pipeline, that the CLI arguments introduced for #1950 AC6-AC10 actually
reach the artifact/stdout boundary and that the accompanying Skill/step docs
describe the same lane-separated (human_supplied / agent_generated /
provenance_conflicts) handoff those CLI arguments feed into.

This exercises the real `main()` CLI entrypoint (argparse -> build_intake_capsule
-> artifact write -> stdout projection), not merely the private helper
functions -- only the OS-level `gh` / `git` boundary (`_run_command`) is
patched, matching the existing test harness convention in
`test_build_intake_capsule.py`.

AC6: --human-context-comment-url / --agent-report-comment-url are independent,
     repeatable CLI inputs; origin is decided by argument position only.
AC7: the capsule artifact preserves provenance / body snapshot / hash /
     updated_at for designated comments, and stdout never contains raw body.
AC8: repo/Issue/comment mismatch, dual-lane URLs, nonexistent comments, and
     invalid structured agent reports are all fail-closed (exit 1).
AC9: preparation.md documents human_supplied / agent_generated as separately
     processed, neither granting mutation authorization alone.
AC10: step-1-implementation.md documents technical_recommendation and the
      capsule_artifact_path / human_context_comment_ids /
      agent_report_comment_ids evidence references, and states that raw
      human comment text is never bound as an execution instruction.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

TEST_REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT_PATH = (
    TEST_REPO_ROOT
    / ".claude"
    / "skills"
    / "impl-review-loop"
    / "scripts"
    / "build_intake_capsule.py"
)
PREPARATION_MD = (
    TEST_REPO_ROOT / ".claude" / "skills" / "impl-review-loop" / "steps" / "preparation.md"
)
STEP1_MD = (
    TEST_REPO_ROOT
    / ".claude"
    / "skills"
    / "impl-review-loop"
    / "steps"
    / "step-1-implementation.md"
)
SKILL_MD = TEST_REPO_ROOT / ".claude" / "skills" / "impl-review-loop" / "SKILL.md"

spec = importlib.util.spec_from_file_location("build_intake_capsule_e2e", SCRIPT_PATH)
assert spec is not None and spec.loader is not None
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)  # type: ignore[union-attr]

_REPO = "squne121/loop-protocol"
_ISSUE_NUMBER = 1950
_ISSUE_URL = f"https://github.com/{_REPO}/issues/{_ISSUE_NUMBER}"

_HUMAN_COMMENT_ID = 5155559936
_HUMAN_COMMENT_URL = f"{_ISSUE_URL}#issuecomment-{_HUMAN_COMMENT_ID}"
_AGENT_COMMENT_ID = 5155559999
_AGENT_COMMENT_URL = f"{_ISSUE_URL}#issuecomment-{_AGENT_COMMENT_ID}"
_UNRELATED_COMMENT_ID = 5155550001


def _issue_view_json() -> str:
    return json.dumps(
        {
            "title": "実装: intake capsule provenance テスト",
            "state": "open",
            "labels": [{"name": "phase/implementation"}],
            "body": "## Machine-Readable Contract\n\nstatus: full-body\n\n## Allowed Paths\n- tracked.txt\n",
            "updatedAt": "2026-08-02T00:00:00Z",
        }
    )


def _comment_ndjson_line(
    *,
    comment_id: int,
    body: str,
    html_url: str,
    author: str = "squne121",
    author_id: int = 63350259,
    author_type: str = "User",
    author_association: str = "OWNER",
    updated_at: str = "2026-08-02T00:01:00Z",
) -> str:
    return json.dumps(
        {
            "id": comment_id,
            "html_url": html_url,
            "created_at": updated_at,
            "updated_at": updated_at,
            "body": body,
            "author": author,
            "author_id": author_id,
            "author_type": author_type,
            "author_association": author_association,
        }
    )


def _comments_stdout() -> str:
    human_body = "2 ターンの敵対的レビュー。owner の自由記述コメント本文。"
    agent_body = (
        "```yaml\nIMPLEMENT_RESULT_V1:\n  status: ok\n```\n"
        "structured agent report body."
    )
    unrelated_body = "unrelated normal review comment."
    return "\n".join(
        [
            _comment_ndjson_line(
                comment_id=_HUMAN_COMMENT_ID,
                body=human_body,
                html_url=_HUMAN_COMMENT_URL,
            ),
            _comment_ndjson_line(
                comment_id=_AGENT_COMMENT_ID,
                body=agent_body,
                html_url=_AGENT_COMMENT_URL,
                author="github-actions",
                author_id=41898282,
                author_type="Bot",
                author_association="NONE",
            ),
            _comment_ndjson_line(
                comment_id=_UNRELATED_COMMENT_ID,
                body=unrelated_body,
                html_url=f"{_ISSUE_URL}#issuecomment-{_UNRELATED_COMMENT_ID}",
            ),
        ]
    )


def _run_command_side_effect_factory(commands):
    calls = {"i": 0}

    def _run(cmd):
        index = calls["i"]
        calls["i"] += 1
        return commands[index]

    return _run


def _run_main(argv: list[str], run_cmd) -> tuple[int, str]:
    stdout_buf = io.StringIO()
    with patch.object(mod, "_run_command", side_effect=run_cmd), patch.object(sys, "argv", argv):
        with redirect_stdout(stdout_buf):
            exit_code = mod.main()
    return exit_code, stdout_buf.getvalue()


# ---------------------------------------------------------------------------
# AC6/AC7: CLI args -> capsule artifact, provenance-separated, stdout excludes body
# ---------------------------------------------------------------------------


def test_cli_human_and_agent_context_urls_resolve_into_artifact_not_stdout(tmp_path):
    run_cmd = _run_command_side_effect_factory(
        [
            (0, _issue_view_json(), ""),
            (0, "abc\n", ""),
            (0, "main\n", ""),
            (0, "  \n", ""),
            (0, _comments_stdout(), ""),
        ]
    )
    artifact_dir = tmp_path / "artifacts"
    argv = [
        "build_intake_capsule.py",
        "--issue-number",
        str(_ISSUE_NUMBER),
        "--repo",
        _REPO,
        "--artifact-dir",
        str(artifact_dir),
        # Budget raised above the default 4096 so this AC6/AC7 assertion is
        # not entangled with the (separately-tested) stdout-budget fallback
        # -- a long tmp_path-derived artifact_path can otherwise tip an
        # unrelated test over the default budget.
        "--max-stdout-bytes",
        "65536",
        "--human-context-comment-url",
        _HUMAN_COMMENT_URL,
        "--agent-report-comment-url",
        _AGENT_COMMENT_URL,
    ]

    exit_code, stdout_text = _run_main(argv, run_cmd)

    assert exit_code == 0, stdout_text
    stdout_payload = json.loads(stdout_text)

    # AC7: stdout projection excludes raw comment body.
    assert "context_inputs" in stdout_payload
    stdout_ctx = stdout_payload["context_inputs"]
    assert len(stdout_ctx["human_supplied"]) == 1
    assert stdout_ctx["human_supplied"][0]["comment_id"] == _HUMAN_COMMENT_ID
    assert "body" not in stdout_ctx["human_supplied"][0]
    assert len(stdout_ctx["agent_generated"]) == 1
    assert stdout_ctx["agent_generated"][0]["comment_id"] == _AGENT_COMMENT_ID
    assert "body" not in stdout_ctx["agent_generated"][0]
    assert stdout_ctx["provenance_conflicts"] == []
    assert json.dumps(stdout_payload).find("敵対的レビュー") == -1

    # AC7: the artifact preserves provenance, body snapshot, hash, updated_at.
    artifact_path = artifact_dir / f"intake-capsule-{_ISSUE_NUMBER}.json"
    artifact_payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact_ctx = artifact_payload["context_inputs"]
    human_entry = artifact_ctx["human_supplied"][0]
    assert human_entry["comment_id"] == _HUMAN_COMMENT_ID
    assert human_entry["url"] == _HUMAN_COMMENT_URL
    assert human_entry["author_association"] == "OWNER"
    assert human_entry["updated_at"] == "2026-08-02T00:01:00Z"
    assert human_entry["body_sha256"].startswith("sha256:")
    assert "敵対的レビュー" in human_entry["body"]

    agent_entry = artifact_ctx["agent_generated"][0]
    assert agent_entry["comment_id"] == _AGENT_COMMENT_ID
    assert agent_entry["validated_schema_id"] == "IMPLEMENT_RESULT_V1"
    assert agent_entry["validation_status"] == "ok"
    assert agent_entry["validation_errors"] == []
    assert "IMPLEMENT_RESULT_V1" in agent_entry["body"]


# ---------------------------------------------------------------------------
# AC8: fail-closed cases
# ---------------------------------------------------------------------------


def test_cli_provenance_conflict_same_url_both_lanes_is_fail_closed(tmp_path):
    run_cmd = _run_command_side_effect_factory(
        [
            (0, _issue_view_json(), ""),
            (0, "abc\n", ""),
            (0, "main\n", ""),
            (0, "  \n", ""),
            (0, _comments_stdout(), ""),
        ]
    )
    artifact_dir = tmp_path / "artifacts"
    argv = [
        "build_intake_capsule.py",
        "--issue-number",
        str(_ISSUE_NUMBER),
        "--repo",
        _REPO,
        "--artifact-dir",
        str(artifact_dir),
        "--human-context-comment-url",
        _HUMAN_COMMENT_URL,
        "--agent-report-comment-url",
        _HUMAN_COMMENT_URL,
    ]

    exit_code, stdout_text = _run_main(argv, run_cmd)

    assert exit_code == 1, stdout_text
    stdout_payload = json.loads(stdout_text)
    assert any(
        err.startswith("provenance_conflict:") for err in stdout_payload["fatal_errors"]
    ), stdout_payload


def test_cli_nonexistent_comment_id_is_fail_closed(tmp_path):
    run_cmd = _run_command_side_effect_factory(
        [
            (0, _issue_view_json(), ""),
            (0, "abc\n", ""),
            (0, "main\n", ""),
            (0, "  \n", ""),
            (0, _comments_stdout(), ""),
        ]
    )
    artifact_dir = tmp_path / "artifacts"
    argv = [
        "build_intake_capsule.py",
        "--issue-number",
        str(_ISSUE_NUMBER),
        "--repo",
        _REPO,
        "--artifact-dir",
        str(artifact_dir),
        "--human-context-comment-url",
        f"{_ISSUE_URL}#issuecomment-999999999999",
    ]

    exit_code, stdout_text = _run_main(argv, run_cmd)

    assert exit_code == 1, stdout_text
    stdout_payload = json.loads(stdout_text)
    assert any(
        err.startswith("human_supplied_comment_not_found:") for err in stdout_payload["fatal_errors"]
    ), stdout_payload


def test_cli_agent_report_missing_structured_marker_is_fail_closed(tmp_path):
    run_cmd = _run_command_side_effect_factory(
        [
            (0, _issue_view_json(), ""),
            (0, "abc\n", ""),
            (0, "main\n", ""),
            (0, "  \n", ""),
            (0, _comments_stdout(), ""),
        ]
    )
    artifact_dir = tmp_path / "artifacts"
    # The unrelated comment has no fenced structured block in its body.
    argv = [
        "build_intake_capsule.py",
        "--issue-number",
        str(_ISSUE_NUMBER),
        "--repo",
        _REPO,
        "--artifact-dir",
        str(artifact_dir),
        "--agent-report-comment-url",
        f"{_ISSUE_URL}#issuecomment-{_UNRELATED_COMMENT_ID}",
    ]

    exit_code, stdout_text = _run_main(argv, run_cmd)

    assert exit_code == 1, stdout_text
    stdout_payload = json.loads(stdout_text)
    assert any(
        err.startswith("agent_report_no_structured_block:")
        for err in stdout_payload["fatal_errors"]
    ), stdout_payload


# ---------------------------------------------------------------------------
# PR #1973 (OWNER REQUEST_CHANGES, P1-3): allowlisted schema-validation lane
# for agent_generated -- a bare regex marker match (quoted / unrelated schema
# mention, multiple blocks, non-allowlisted schema id) must fail-closed.
# ---------------------------------------------------------------------------


def test_cli_agent_report_marker_only_inside_quoted_text_is_fail_closed(tmp_path):
    """A comment body that merely mentions a schema-shaped token in quoted
    prose text (not as the actual top-level key of a fenced structured
    block) must NOT satisfy the allowlist check."""
    quoted_only_comment_id = 5155550002
    quoted_only_body = (
        'The previous comment said "IMPLEMENT_RESULT_V1: status ok" but this '
        "is plain prose, not a fenced structured block."
    )
    run_cmd = _run_command_side_effect_factory(
        [
            (0, _issue_view_json(), ""),
            (0, "abc\n", ""),
            (0, "main\n", ""),
            (0, "  \n", ""),
            (
                0,
                "\n".join(
                    [
                        _comments_stdout(),
                        _comment_ndjson_line(
                            comment_id=quoted_only_comment_id,
                            body=quoted_only_body,
                            html_url=f"{_ISSUE_URL}#issuecomment-{quoted_only_comment_id}",
                        ),
                    ]
                ),
                "",
            ),
        ]
    )
    artifact_dir = tmp_path / "artifacts"
    argv = [
        "build_intake_capsule.py",
        "--issue-number",
        str(_ISSUE_NUMBER),
        "--repo",
        _REPO,
        "--artifact-dir",
        str(artifact_dir),
        "--agent-report-comment-url",
        f"{_ISSUE_URL}#issuecomment-{quoted_only_comment_id}",
    ]

    exit_code, stdout_text = _run_main(argv, run_cmd)

    assert exit_code == 1, stdout_text
    stdout_payload = json.loads(stdout_text)
    assert any(
        err.startswith("agent_report_no_structured_block:")
        for err in stdout_payload["fatal_errors"]
    ), stdout_payload


def test_cli_agent_report_non_allowlisted_schema_id_is_fail_closed(tmp_path):
    """A well-formed single fenced block whose top-level key is NOT in
    `_ALLOWED_AGENT_REPORT_SCHEMA_IDS` must fail-closed."""
    unlisted_comment_id = 5155550003
    unlisted_body = "```yaml\nSOME_UNLISTED_SCHEMA_V1:\n  status: ok\n```\n"
    run_cmd = _run_command_side_effect_factory(
        [
            (0, _issue_view_json(), ""),
            (0, "abc\n", ""),
            (0, "main\n", ""),
            (0, "  \n", ""),
            (
                0,
                "\n".join(
                    [
                        _comments_stdout(),
                        _comment_ndjson_line(
                            comment_id=unlisted_comment_id,
                            body=unlisted_body,
                            html_url=f"{_ISSUE_URL}#issuecomment-{unlisted_comment_id}",
                        ),
                    ]
                ),
                "",
            ),
        ]
    )
    artifact_dir = tmp_path / "artifacts"
    argv = [
        "build_intake_capsule.py",
        "--issue-number",
        str(_ISSUE_NUMBER),
        "--repo",
        _REPO,
        "--artifact-dir",
        str(artifact_dir),
        "--agent-report-comment-url",
        f"{_ISSUE_URL}#issuecomment-{unlisted_comment_id}",
    ]

    exit_code, stdout_text = _run_main(argv, run_cmd)

    assert exit_code == 1, stdout_text
    stdout_payload = json.loads(stdout_text)
    assert any(
        err.startswith("agent_report_schema_not_allowlisted:")
        for err in stdout_payload["fatal_errors"]
    ), stdout_payload


def test_cli_agent_report_multiple_blocks_is_fail_closed(tmp_path):
    """More than one top-level fenced block in the comment body must
    fail-closed, even if one of them is allowlisted."""
    multi_block_comment_id = 5155550004
    multi_block_body = (
        "```yaml\nIMPLEMENT_RESULT_V1:\n  status: ok\n```\n"
        "```yaml\nTEST_VERDICT:\n  status: pass\n```\n"
    )
    run_cmd = _run_command_side_effect_factory(
        [
            (0, _issue_view_json(), ""),
            (0, "abc\n", ""),
            (0, "main\n", ""),
            (0, "  \n", ""),
            (
                0,
                "\n".join(
                    [
                        _comments_stdout(),
                        _comment_ndjson_line(
                            comment_id=multi_block_comment_id,
                            body=multi_block_body,
                            html_url=f"{_ISSUE_URL}#issuecomment-{multi_block_comment_id}",
                        ),
                    ]
                ),
                "",
            ),
        ]
    )
    artifact_dir = tmp_path / "artifacts"
    argv = [
        "build_intake_capsule.py",
        "--issue-number",
        str(_ISSUE_NUMBER),
        "--repo",
        _REPO,
        "--artifact-dir",
        str(artifact_dir),
        "--agent-report-comment-url",
        f"{_ISSUE_URL}#issuecomment-{multi_block_comment_id}",
    ]

    exit_code, stdout_text = _run_main(argv, run_cmd)

    assert exit_code == 1, stdout_text
    stdout_payload = json.loads(stdout_text)
    assert any(
        err.startswith("agent_report_multiple_blocks:") for err in stdout_payload["fatal_errors"]
    ), stdout_payload


def test_no_context_urls_leaves_capsule_backward_compatible(tmp_path):
    """Absent --human-context-comment-url / --agent-report-comment-url, the
    capsule must not gain a `context_inputs` key at all (backward
    compatibility for existing consumers)."""
    run_cmd = _run_command_side_effect_factory(
        [
            (0, _issue_view_json(), ""),
            (0, "abc\n", ""),
            (0, "main\n", ""),
            (0, "  \n", ""),
            (0, "", ""),
        ]
    )
    artifact_dir = tmp_path / "artifacts"
    argv = [
        "build_intake_capsule.py",
        "--issue-number",
        str(_ISSUE_NUMBER),
        "--repo",
        _REPO,
        "--artifact-dir",
        str(artifact_dir),
    ]

    exit_code, stdout_text = _run_main(argv, run_cmd)

    assert exit_code == 0, stdout_text
    stdout_payload = json.loads(stdout_text)
    assert "context_inputs" not in stdout_payload


# ---------------------------------------------------------------------------
# AC9/AC10: Skill/step doc routing binds to the same lane separation
# ---------------------------------------------------------------------------


def test_preparation_documents_context_inputs_lane_separation():
    body = PREPARATION_MD.read_text(encoding="utf-8")
    idx_section = body.find("## 1-e. Context Inputs")
    assert idx_section >= 0
    section = body[idx_section:]
    assert "treat_as_untrusted_natural_language" in section
    assert "context_inputs.human_supplied" in section
    assert "context_inputs.agent_generated" in section
    assert "mutation authorization" in section or "mutation_authorization" in section


def test_step1_documents_technical_recommendation_and_evidence_references():
    body = STEP1_MD.read_text(encoding="utf-8")
    assert "technical_recommendation" in body
    assert "capsule_artifact_path" in body
    assert "human_context_comment_ids" in body
    assert "agent_report_comment_ids" in body
    assert "raw human comment" in body or "raw comment body" in body


def test_skill_md_declares_provenance_inputs():
    body = SKILL_MD.read_text(encoding="utf-8")
    assert "human_context_comment_urls" in body
    assert "agent_report_comment_urls" in body


# ---------------------------------------------------------------------------
# PR #1973 (OWNER REQUEST_CHANGES, P0-2): `build_capsule_argv()` is the
# single source of truth for the CLI argv preparation.md "0-a" documents;
# `validate_step1_dispatch_payload()` is the enforceable version of the
# step-1-implementation.md "#1950 AC10" prose.
# ---------------------------------------------------------------------------


def test_build_capsule_argv_materializes_canonical_command_with_additive_flags():
    """`build_capsule_argv()` must produce the exact canonical command
    documented in preparation.md "0-a", with `--human-context-comment-url` /
    `--agent-report-comment-url` appended additively (not a separate code
    path) when those inputs are non-empty."""
    argv_no_context = mod.build_capsule_argv(issue_number=_ISSUE_NUMBER, repo=_REPO)
    assert argv_no_context == [
        "uv",
        "run",
        "python3",
        ".claude/skills/impl-review-loop/scripts/build_intake_capsule.py",
        "--issue-number",
        str(_ISSUE_NUMBER),
        "--repo",
        _REPO,
        "--max-stdout-bytes",
        "4096",
    ]

    argv_with_context = mod.build_capsule_argv(
        issue_number=_ISSUE_NUMBER,
        repo=_REPO,
        human_context_comment_urls=[_HUMAN_COMMENT_URL],
        agent_report_comment_urls=[_AGENT_COMMENT_URL],
    )
    # The base command is an unmodified prefix -- additive, not a fork.
    assert argv_with_context[: len(argv_no_context)] == argv_no_context
    assert argv_with_context[len(argv_no_context) :] == [
        "--human-context-comment-url",
        _HUMAN_COMMENT_URL,
        "--agent-report-comment-url",
        _AGENT_COMMENT_URL,
    ]


def test_build_capsule_argv_e2e_subprocess_invocation_resolves_context_inputs(tmp_path):
    """#1950 AC6/AC7 (P0-2 fix_delta test): actually invoke
    `build_intake_capsule.py` through the EXACT argv `build_capsule_argv()`
    materializes (mirroring the CLI-entrypoint harness already established
    by `_run_main()` / `test_cli_human_and_agent_context_urls_resolve_into_artifact_not_stdout`
    above), and confirm the resulting `context_inputs.human_supplied` /
    `context_inputs.agent_generated` reflect the requested URLs."""
    run_cmd = _run_command_side_effect_factory(
        [
            (0, _issue_view_json(), ""),
            (0, "abc\n", ""),
            (0, "main\n", ""),
            (0, "  \n", ""),
            (0, _comments_stdout(), ""),
        ]
    )
    artifact_dir = tmp_path / "artifacts"
    materialized_argv = mod.build_capsule_argv(
        issue_number=_ISSUE_NUMBER,
        repo=_REPO,
        max_stdout_bytes=65536,
        human_context_comment_urls=[_HUMAN_COMMENT_URL],
        agent_report_comment_urls=[_AGENT_COMMENT_URL],
    )
    # Translate the shell-oriented argv (`uv run python3 <script> ...`) into
    # the in-process sys.argv shape `_run_main()` expects (program name +
    # flags), reusing the SAME flag values `build_capsule_argv()` produced --
    # no separate argv construction.
    cli_argv = ["build_intake_capsule.py", *materialized_argv[4:], "--artifact-dir", str(artifact_dir)]

    exit_code, stdout_text = _run_main(cli_argv, run_cmd)

    assert exit_code == 0, stdout_text
    artifact_path = artifact_dir / f"intake-capsule-{_ISSUE_NUMBER}.json"
    artifact_payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    context_inputs = artifact_payload["context_inputs"]

    assert context_inputs["human_supplied"][0]["url"] == _HUMAN_COMMENT_URL
    assert context_inputs["agent_generated"][0]["url"] == _AGENT_COMMENT_URL
    assert context_inputs["agent_generated"][0]["validated_schema_id"] == "IMPLEMENT_RESULT_V1"

    # (i) message_fields WITH technical_recommendation + evidence IDs -> allowed.
    allowed_ok, errors_ok = mod.validate_step1_dispatch_payload(
        context_inputs,
        {
            "technical_recommendation": "Apply the reviewed fix per repository diff/tests.",
            "human_context_comment_ids": [context_inputs["human_supplied"][0]["comment_id"]],
            "agent_report_comment_ids": [context_inputs["agent_generated"][0]["comment_id"]],
        },
    )
    assert allowed_ok is True, errors_ok
    assert errors_ok == []

    # (ii) message_fields WITHOUT technical_recommendation -> blocked.
    allowed_missing_rec, errors_missing_rec = mod.validate_step1_dispatch_payload(
        context_inputs,
        {
            "human_context_comment_ids": [context_inputs["human_supplied"][0]["comment_id"]],
        },
    )
    assert allowed_missing_rec is False
    assert "step1_dispatch_missing_technical_recommendation" in errors_missing_rec

    # (iii) message_fields embedding a human_supplied comment's raw body
    # verbatim -> blocked (raw human comment must never be bound as an
    # execution instruction directly).
    raw_human_body = context_inputs["human_supplied"][0]["body"]
    allowed_raw_embed, errors_raw_embed = mod.validate_step1_dispatch_payload(
        context_inputs,
        {
            "technical_recommendation": f"Do exactly what the owner said: {raw_human_body}",
            "human_context_comment_ids": [context_inputs["human_supplied"][0]["comment_id"]],
        },
    )
    assert allowed_raw_embed is False
    assert any(err.startswith("step1_dispatch_raw_human_comment_embedded:") for err in errors_raw_embed)


def test_validate_step1_dispatch_payload_noop_when_context_inputs_absent():
    """When `context_inputs` is None/empty, `validate_step1_dispatch_payload()`
    is a no-op (per step-1-implementation.md: "context_inputs が存在しない
    場合、この節は no-op")."""
    allowed, errors = mod.validate_step1_dispatch_payload(None, {})
    assert allowed is True
    assert errors == []

    allowed_empty, errors_empty = mod.validate_step1_dispatch_payload(
        {"human_supplied": [], "agent_generated": []}, {}
    )
    assert allowed_empty is True
    assert errors_empty == []
