"""Tests for build_intake_capsule.py."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
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
    TEST_REPO_ROOT
    / ".claude"
    / "skills"
    / "impl-review-loop"
    / "steps"
    / "preparation.md"
)

spec = importlib.util.spec_from_file_location("build_intake_capsule", SCRIPT_PATH)
assert spec is not None and spec.loader is not None
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)  # type: ignore[union-attr]


def _issue_view_json(
    *,
    title: str = "実装: intake capsule テスト",
    body: str = "## Machine-Readable Contract\n\nstatus: full-body\n\n## Allowed Paths\n- tracked.txt\n",
    updated_at: str = "2026-06-19T00:00:00Z",
    labels: list[dict[str, str]] | None = None,
) -> str:
    if labels is None:
        labels = [{"name": "phase/implementation"}]
    return json.dumps(
        {
            "title": title,
            "state": "open",
            "labels": labels,
            "body": body,
            "updatedAt": updated_at,
        }
    )


# #1475: this test module exercises routing / triage / normalization logic,
# not the trust policy itself (that is covered end-to-end in
# test_contract_snapshot_author_binding.py). Default every fixture comment
# to the sole allowlisted TRUSTED_CONTRACT_PUBLISHERS identity so existing
# routing assertions are unaffected by the fix_delta P1 item 1/2 trust gate;
# tests that specifically need an untrusted comment pass author=... overrides.
_TRUSTED_AUTHOR_LOGIN = "squne121"
_TRUSTED_AUTHOR_ID = 63350259
_TRUSTED_AUTHOR_TYPE = "User"
_TRUSTED_AUTHOR_ASSOCIATION = "OWNER"


def _comment_ndjson(
    body: str,
    *,
    comment_id: int = 1,
    created_at: str = "2026-06-19T00:01:00Z",
    author: str | None = _TRUSTED_AUTHOR_LOGIN,
    author_id: int | None = _TRUSTED_AUTHOR_ID,
    author_type: str | None = _TRUSTED_AUTHOR_TYPE,
    author_association: str | None = _TRUSTED_AUTHOR_ASSOCIATION,
) -> str:
    return json.dumps(
        {
            "id": comment_id,
            "html_url": f"https://github.com/squne121/loop-protocol/issues/958#issuecomment-{comment_id}",
            "created_at": created_at,
            "updated_at": created_at,
            "body": body,
            "author": author,
            "author_id": author_id,
            "author_type": author_type,
            "author_association": author_association,
        }
    )


def _run_command_side_effect_factory(commands):
    calls = {"i": 0}

    def _run(cmd):
        index = calls["i"]
        calls["i"] += 1
        return commands[index]

    return _run


def test_ac1_contract_snapshot_missing_routes_to_ensure_contract_snapshot():
    run_cmd = _run_command_side_effect_factory(
        [
            (0, _issue_view_json(), ""),
            (0, "abc\n", ""),
            (0, "main\n", ""),
            (0, "  \n", ""),
            (0, "", ""),
        ]
    )

    with patch.object(mod, "_run_command", side_effect=run_cmd):
        capsule, artifact, exit_code = mod.build_intake_capsule(958, "squne121/loop-protocol", None)

    assert exit_code == 0
    assert capsule["contract_snapshot"]["normalized_status"] == "missing_go"
    assert capsule["next_action"]["route"] == "ensure_contract_snapshot"
    assert artifact["source_integrity"]["parse_warnings"]["invalid_json_lines_count"] == 0


def test_ac2_blocked_categories_from_ensure_contract_snapshot_result(tmp_path):
    ensure_payload = {
        "schema": "CONTRACT_SNAPSHOT_ENSURE_RESULT_V1",
        "status": "blocked_needs_refinement",
        "contract_review_once_result": {
            "vc_preflight_classifications": [
                {
                    "ac": "AC1",
                    "category": "package_manager_no_tty_prompt",
                    "decision": "blocked",
                    "raw_command": "pnpm build",
                },
                {
                    "ac": "AC2",
                    "category": "vc_no_tests_collected",
                    "decision": "blocked",
                    "raw_command": "uv run pytest -k foo",
                    "exit_code": 5,
                    "subreason": "pytest_k_filter_matches_no_tests",
                },
            ]
        },
    }
    ensure_path = tmp_path / "ensure-result.json"
    ensure_path.write_text(json.dumps(ensure_payload), encoding="utf-8")

    run_cmd = _run_command_side_effect_factory(
        [
            (0, _issue_view_json(), ""),
            (0, "abc\n", ""),
            (0, "main\n", ""),
            (0, "", ""),
        ]
    )

    with patch.object(mod, "_run_command", side_effect=run_cmd):
        capsule, _artifact, exit_code = mod.build_intake_capsule(
            958,
            "squne121/loop-protocol",
            ensure_contract_snapshot_result=str(ensure_path),
        )

    assert exit_code == 0
    assert capsule["contract_snapshot"]["normalized_status"] == "missing_go"
    assert capsule["contract_snapshot"]["top_blocked_categories"] == [
        "package_manager_no_tty_prompt",
        "vc_no_tests_collected",
    ]
    assert capsule["contract_snapshot"]["contract_blocker_triage"]["schema"] == "CONTRACT_BLOCKER_TRIAGE_V1"
    assert capsule["next_action"]["route"] == "run_contract_blocker_triage"


def test_ac3_main_dirty_summary_shows_count_and_sample_only():
    run_cmd = _run_command_side_effect_factory(
        [
            (0, _issue_view_json(), ""),
            (0, "abc\n", ""),
            (0, "main\n", ""),
            (0, "M  a.txt\n?? b.txt\nD  c.txt\nA  d.txt\n", ""),
            (0, "", ""),
        ]
    )

    with patch.object(mod, "_run_command", side_effect=run_cmd):
        capsule, _artifact, exit_code = mod.build_intake_capsule(958, "squne121/loop-protocol", None)

    assert exit_code == 0
    summary = capsule["repo_state"]["dirty_paths_summary"]
    assert summary["count"] == 4
    assert summary["sample_paths"] == ["a.txt", "b.txt", "c.txt", "d.txt"]
    assert summary["truncated"] is False


def test_ac4_stdout_budget_and_artifact_path(tmp_path):
    huge_line = "x" * 8000
    capsule = {
        "schema": mod._SCHEMA_NAME,
        "schema_version": mod._SCHEMA_VERSION,
        "issue_number": 958,
        "repo": "squne121/loop-protocol",
        "issue_ready_tuple": {"status": "pass"},
        "contract_snapshot": {"normalized_status": "missing_go", "top_blocked_categories": []},
        "source_integrity": {"parse_warnings": {}, "evidence_complete": True},
        "worktree": {},
        "repo_state": {"dirty": False, "dirty_paths_summary": {"count": 0, "sample_paths": []}},
        "agent_runtime": {},
        "next_action": {"route": "ensure_contract_snapshot"},
        "warnings": [],
        "errors": [],
        "tail": huge_line,
    }
    artifact = {"schema": mod._SCHEMA_NAME, "schema_version": mod._SCHEMA_VERSION, "detail": huge_line}

    with patch.object(mod, "build_intake_capsule", return_value=(capsule, artifact, 0)):
        with patch.object(
            mod.sys,
            "argv",
            [
                "build_intake_capsule.py",
                "--issue-number",
                "958",
                "--artifact-dir",
                str(tmp_path / "artifacts"),
                "--max-stdout-bytes",
                "4096",
            ],
        ):
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                exit_code = mod.main()

    assert exit_code == 0
    output = captured.getvalue()
    assert len(output.encode("utf-8")) <= 4096
    artifact_path = tmp_path / "artifacts" / "intake-capsule-958.json"
    assert artifact_path.exists()
    loaded_artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert loaded_artifact["artifact_settings"]["max_stdout_bytes"] == 4096


def test_ac5_stdout_does_not_include_full_skill_body():
    yaml_body = """
<!-- loop-protocol:contract-snapshot -->

```yaml
CONTRACT_REVIEW_RESULT_V1:
  status: go
  generated_at: "2026-06-19T00:01:00Z"
  generated_by: issue-contract-review
  issue_url: https://github.com/squne121/loop-protocol/issues/958
  body_sha256: "sha256:deadbeef"
```
"""
    run_cmd = _run_command_side_effect_factory(
        [
            (0, _issue_view_json(), ""),
            (0, "abc\n", ""),
            (0, "main\n", ""),
            (0, "", ""),
            (0, _comment_ndjson(yaml_body), ""),
        ]
    )

    with patch.object(mod, "_run_command", side_effect=run_cmd):
        capsule, _artifact, _ = mod.build_intake_capsule(958, "squne121/loop-protocol", None)

    printed = mod._render_stdout(capsule, 4096)
    assert "## Machine-Readable Contract" not in printed
    assert "status: full-body" not in printed
    assert "<!-- loop-protocol:contract-snapshot -->" not in printed


def test_latest_blocked_routes_to_triage():
    blocked_body = """
```yaml
CONTRACT_REVIEW_RESULT_V1:
  status: blocked
  generated_at: "2026-06-19T00:01:00Z"
  generated_by: issue-contract-review
  issue_url: https://github.com/squne121/loop-protocol/issues/958
```
"""
    run_cmd = _run_command_side_effect_factory(
        [
            (0, _issue_view_json(), ""),
            (0, "abc\n", ""),
            (0, "main\n", ""),
            (0, "", ""),
            (0, _comment_ndjson(blocked_body), ""),
        ]
    )

    with patch.object(mod, "_run_command", side_effect=run_cmd):
        capsule, _artifact, exit_code = mod.build_intake_capsule(958, "squne121/loop-protocol", None)

    assert exit_code == 0
    assert capsule["contract_snapshot"]["normalized_status"] == "latest_blocked"
    assert capsule["next_action"]["route"] == "run_contract_blocker_triage"


def test_malformed_ndjson_sets_parse_warning():
    run_cmd = _run_command_side_effect_factory(
        [
            (0, _issue_view_json(), ""),
            (0, "abc\n", ""),
            (0, "main\n", ""),
            (0, "", ""),
            (0, '{"id":1,"body":"ok"}\nnot-json\n', ""),
        ]
    )

    with patch.object(mod, "_run_command", side_effect=run_cmd):
        capsule, artifact, exit_code = mod.build_intake_capsule(958, "squne121/loop-protocol", None)

    assert exit_code == 0
    assert "invalid_json_lines_count:1" in capsule["warnings"]
    assert artifact["source_integrity"]["parse_warnings"]["invalid_json_lines_count"] == 1


# ---------------------------------------------------------------------------
# Source-bound contract fingerprint routing (Issue #1537 AC3)
# ---------------------------------------------------------------------------


def _fingerprint_yaml_block(
    comment_id: int = 1, body_sha256: str | None = None, paths_hash: str | None = None
) -> str:
    return f"""
  expected_contract_fingerprint:
    issue_number: 958
    contract_source_kind: issue_comment
    contract_source_id: "{comment_id}"
    contract_body_sha256: "{body_sha256 or 'sha256:' + 'a' * 64}"
    allowed_paths_normalized_sha256: "{paths_hash or 'b' * 64}"
    base_ref: main
    base_sha_at_snapshot: "{'c' * 40}"
"""


def test_ac3_go_with_fingerprint_routes_to_proceed():
    comment_id = 42
    issue_body = "## Machine-Readable Contract\n\nstatus: full-body\n\n## Allowed Paths\n- tracked.txt\n"
    default_body_sha256 = mod._sha256(issue_body)
    paths_hash = mod._live_allowed_paths_hash(issue_body)
    go_body = f"""
```yaml
CONTRACT_REVIEW_RESULT_V1:
  status: go
  generated_at: "2026-06-19T00:01:00Z"
  generated_by: issue-contract-review
  issue_url: https://github.com/squne121/loop-protocol/issues/958
  body_sha256: "{default_body_sha256}"{_fingerprint_yaml_block(comment_id, default_body_sha256, paths_hash)}```
"""
    run_cmd = _run_command_side_effect_factory(
        [
            (0, _issue_view_json(), ""),
            (0, "abc\n", ""),
            (0, "main\n", ""),
            (0, "", ""),
            (0, _comment_ndjson(go_body, comment_id=comment_id), ""),
        ]
    )

    with patch.object(mod, "_run_command", side_effect=run_cmd):
        capsule, _artifact, exit_code = mod.build_intake_capsule(958, "squne121/loop-protocol", None)

    assert exit_code == 0
    assert capsule["contract_snapshot"]["normalized_status"] == "go"
    assert capsule["next_action"]["route"] == "proceed_to_step_1"


def test_ac3_go_without_fingerprint_routes_to_missing_go_not_proceed():
    """A trusted, schema-valid `status: go` that lacks a well-formed
    source-bound expected_contract_fingerprint must never be treated as a
    loop-consumable fresh go -- it must route back to
    ensure_contract_snapshot re-materialization instead."""
    go_body = """
```yaml
CONTRACT_REVIEW_RESULT_V1:
  status: go
  generated_at: "2026-06-19T00:01:00Z"
  generated_by: issue-contract-review
  issue_url: https://github.com/squne121/loop-protocol/issues/958
  body_sha256: "sha256:deadbeef"
```
"""
    run_cmd = _run_command_side_effect_factory(
        [
            (0, _issue_view_json(), ""),
            (0, "abc\n", ""),
            (0, "main\n", ""),
            (0, "", ""),
            (0, _comment_ndjson(go_body), ""),
        ]
    )

    with patch.object(mod, "_run_command", side_effect=run_cmd):
        capsule, _artifact, exit_code = mod.build_intake_capsule(958, "squne121/loop-protocol", None)

    assert exit_code == 0
    assert capsule["contract_snapshot"]["normalized_status"] == "missing_go"
    assert capsule["next_action"]["route"] == "ensure_contract_snapshot"


def test_ac3_go_with_fingerprint_wrong_issue_number_routes_to_missing_go():
    """A fingerprint whose issue_number does not match the issue this parse
    run is scoped to must never be accepted as fingerprint-ready."""
    go_body = f"""
```yaml
CONTRACT_REVIEW_RESULT_V1:
  status: go
  generated_at: "2026-06-19T00:01:00Z"
  generated_by: issue-contract-review
  issue_url: https://github.com/squne121/loop-protocol/issues/958
  body_sha256: "sha256:deadbeef"
  expected_contract_fingerprint:
    issue_number: 1
    contract_source_kind: issue_comment
    contract_source_id: "1"
    contract_body_sha256: "sha256:{'a' * 64}"
    allowed_paths_normalized_sha256: "{'b' * 64}"
    base_ref: main
    base_sha_at_snapshot: "{'c' * 40}"
```
"""
    run_cmd = _run_command_side_effect_factory(
        [
            (0, _issue_view_json(), ""),
            (0, "abc\n", ""),
            (0, "main\n", ""),
            (0, "", ""),
            (0, _comment_ndjson(go_body), ""),
        ]
    )

    with patch.object(mod, "_run_command", side_effect=run_cmd):
        capsule, _artifact, exit_code = mod.build_intake_capsule(958, "squne121/loop-protocol", None)

    assert exit_code == 0
    assert capsule["contract_snapshot"]["normalized_status"] == "missing_go"


def test_ac6_preparation_refers_to_capsule_before_redundant_commands():
    body = PREPARATION_MD.read_text(encoding="utf-8")
    idx_capsule_section = body.find("## 0-a. Intake capsule-first")
    idx_step0_section = body.find("## 0. Intake Gate")
    idx_capsule_doc = body.find("build_intake_capsule.py")

    assert idx_capsule_section >= 0
    assert idx_step0_section > idx_capsule_section
    assert idx_capsule_doc > idx_capsule_section
    assert "同一 loop 内では `gh issue view` / comments fetch / main `git status` / snapshot 探索を再実行しない" in body
    assert "run_contract_blocker_triage" in body
