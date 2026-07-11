#!/usr/bin/env python3
"""Tests for open_pr.py validator integration."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import open_pr


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "pr_body"


def load_fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def write_temp_body(body: str) -> str:
    handle = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".md", delete=False)
    handle.write(body)
    handle.flush()
    handle.close()
    return handle.name


def test_run_pr_body_validator_non_json_stdout(monkeypatch: pytest.MonkeyPatch):
    body = load_fixture("valid_not_schema_change.md")

    class FakeCP:
        returncode = 0
        stdout = "not json"
        stderr = ""

    monkeypatch.setattr(open_pr.subprocess, "run", lambda *args, **kwargs: FakeCP())
    result = open_pr._run_pr_body_validator(body, ["src/example.ts"], 330)
    assert result["status"] == "internal"


def test_resolve_changed_paths_autoresolve(monkeypatch: pytest.MonkeyPatch):
    class FakeCompleted:
        def __init__(self, stdout: str):
            self.stdout = stdout

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["git", "merge-base", "main"]:
            return FakeCompleted("abc123\n")
        if cmd[:3] == ["git", "diff", "--name-only"]:
            return FakeCompleted(".github/workflows/ci.yml\n.claude/skills/open-pr/scripts/open_pr.py\n")
        raise AssertionError(cmd)

    monkeypatch.setattr(open_pr.subprocess, "run", fake_run)
    paths = open_pr.resolve_changed_paths(None)
    assert paths == [".github/workflows/ci.yml", ".claude/skills/open-pr/scripts/open_pr.py"]
    assert any(cmd[:3] == ["git", "merge-base", "main"] for cmd in calls)
    assert any(cmd[:3] == ["git", "diff", "--name-only"] for cmd in calls)


def test_validator_fail_blocks_create(monkeypatch: pytest.MonkeyPatch):
    body_path = write_temp_body(load_fixture("valid_not_schema_change.md"))
    create_called = {"value": False}
    try:
        monkeypatch.setattr(open_pr, "resolve_repo", lambda: "squne121/loop-protocol")
        monkeypatch.setattr(open_pr, "resolve_branch", lambda: "worktree-issue-330-validate-pr-body")
        monkeypatch.setattr(open_pr, "get_linked_issue_state", lambda repo, issue: "OPEN")
        monkeypatch.setattr(open_pr, "resolve_changed_paths", lambda provided: ["src/example.ts"])
        monkeypatch.setattr(
            open_pr,
            "_run_pr_body_validator",
            lambda body, changed_paths, linked_issue: {
                "status": "fail",
                "errors": [{"rule_id": "LP050"}],
                "message": "validation failed",
            },
        )
        monkeypatch.setattr(open_pr, "find_existing_pr", lambda repo, branch: None)

        def fake_create_pr(*args, **kwargs):
            create_called["value"] = True
            raise AssertionError("create_pr should not be called")

        monkeypatch.setattr(open_pr, "create_pr", fake_create_pr)

        rc = open_pr.main(
            [
                "--pr-title",
                "feat: test",
                "--linked-issue",
                "330",
                "--publish",
                "yes",
                "--pr-body-file",
                body_path,
            ]
        )
        assert rc == 2
        assert create_called["value"] is False
    finally:
        Path(body_path).unlink(missing_ok=True)


def test_non_json_validator_output(monkeypatch: pytest.MonkeyPatch):
    body_path = write_temp_body(load_fixture("valid_not_schema_change.md"))
    try:
        monkeypatch.setattr(open_pr, "resolve_repo", lambda: "squne121/loop-protocol")
        monkeypatch.setattr(open_pr, "resolve_branch", lambda: "worktree-issue-330-validate-pr-body")
        monkeypatch.setattr(open_pr, "get_linked_issue_state", lambda repo, issue: "OPEN")
        monkeypatch.setattr(open_pr, "resolve_changed_paths", lambda provided: ["src/example.ts"])
        monkeypatch.setattr(
            open_pr,
            "_run_pr_body_validator",
            lambda body, changed_paths, linked_issue: {
                "status": "internal",
                "errors": [],
                "message": "Validator returned non-JSON output",
            },
        )
        monkeypatch.setattr(open_pr, "find_existing_pr", lambda repo, branch: None)
        monkeypatch.setattr(
            open_pr,
            "create_pr",
            lambda *args,
            **kwargs: (_ for _ in ()).throw(AssertionError("no create"))
        )
        rc = open_pr.main(
            [
                "--pr-title",
                "feat: test",
                "--linked-issue",
                "330",
                "--publish",
                "yes",
                "--pr-body-file",
                body_path,
            ]
        )
        assert rc == 2
    finally:
        Path(body_path).unlink(missing_ok=True)


def test_validator_receives_final_body_with_closes_reference(monkeypatch: pytest.MonkeyPatch):
    body_path = write_temp_body(load_fixture("valid_not_schema_change.md"))
    observed = {"body": ""}
    try:
        monkeypatch.setattr(open_pr, "resolve_repo", lambda: "squne121/loop-protocol")
        monkeypatch.setattr(open_pr, "resolve_branch", lambda: "worktree-issue-330-validate-pr-body")
        monkeypatch.setattr(open_pr, "get_linked_issue_state", lambda repo, issue: "OPEN")
        monkeypatch.setattr(open_pr, "resolve_changed_paths", lambda provided: ["src/example.ts"])

        def fake_validator(body, changed_paths, linked_issue):
            observed["body"] = body
            return {"status": "pass", "errors": []}

        monkeypatch.setattr(open_pr, "_run_pr_body_validator", fake_validator)
        monkeypatch.setattr(
            open_pr,
            "_run_japanese_content_validator",
            lambda body_text,
            threshold=0.1: {
                "status": "pass",
                "failed_blocks": 0,
                "aggregate_ratio": 0.5,
                "threshold": 0.1,
                "body_sha256": "",
                "stderr": ""
            }
        )
        monkeypatch.setattr(open_pr, "find_existing_pr", lambda repo, branch: {"number": 999, "url": "https://example.com/pr/999"})
        rc = open_pr.main(
            [
                "--pr-title",
                "feat: test",
                "--linked-issue",
                "330",
                "--publish",
                "yes",
                "--pr-body-file",
                body_path,
            ]
        )
        assert rc == 0
        assert "Closes #330" in observed["body"]
    finally:
        Path(body_path).unlink(missing_ok=True)


def test_changed_paths_unavailable(monkeypatch: pytest.MonkeyPatch):
    body_path = write_temp_body(load_fixture("valid_not_schema_change.md"))
    try:
        monkeypatch.setattr(open_pr, "resolve_repo", lambda: "squne121/loop-protocol")
        monkeypatch.setattr(open_pr, "resolve_branch", lambda: "worktree-issue-330-validate-pr-body")
        monkeypatch.setattr(open_pr, "get_linked_issue_state", lambda repo, issue: "OPEN")
        monkeypatch.setattr(open_pr, "resolve_changed_paths", lambda provided: None)
        monkeypatch.setattr(
            open_pr,
            "_run_pr_body_validator",
            lambda body, changed_paths, linked_issue: {
                "status": "fail",
                "errors": [{"rule_id": "LP058"}],
                "message": "changed paths unavailable",
            },
        )
        monkeypatch.setattr(open_pr, "find_existing_pr", lambda repo, branch: None)
        monkeypatch.setattr(
            open_pr,
            "create_pr",
            lambda *args,
            **kwargs: (_ for _ in ()).throw(AssertionError("no create"))
        )
        rc = open_pr.main(
            [
                "--pr-title",
                "feat: test",
                "--linked-issue",
                "330",
                "--publish",
                "yes",
                "--pr-body-file",
                body_path,
            ]
        )
        assert rc == 2
    finally:
        Path(body_path).unlink(missing_ok=True)


def test_b3_validator_schema_mismatch(monkeypatch: pytest.MonkeyPatch):
    """B3: Verify schema field is loop_body_lint/v1."""
    body_path = write_temp_body(load_fixture("valid_not_schema_change.md"))
    try:
        monkeypatch.setattr(open_pr, "resolve_repo", lambda: "squne121/loop-protocol")
        monkeypatch.setattr(open_pr, "resolve_branch", lambda: "worktree-issue-330-validate-pr-body")
        monkeypatch.setattr(open_pr, "get_linked_issue_state", lambda repo, issue: "OPEN")
        monkeypatch.setattr(open_pr, "resolve_changed_paths", lambda provided: ["src/example.ts"])

        class FakeCP:
            returncode = 0
            stdout = json.dumps({"schema": "wrong_schema", "target": "pr", "status": "pass", "errors": []})
            stderr = ""

        monkeypatch.setattr(open_pr.subprocess, "run", lambda *args, **kwargs: FakeCP())
        monkeypatch.setattr(open_pr, "find_existing_pr", lambda repo, branch: None)

        rc = open_pr.main(
            [
                "--pr-title",
                "feat: test",
                "--linked-issue",
                "330",
                "--publish",
                "yes",
                "--pr-body-file",
                body_path,
            ]
        )
        assert rc == 2
    finally:
        Path(body_path).unlink(missing_ok=True)


def test_b3_validator_target_mismatch(monkeypatch: pytest.MonkeyPatch):
    """B3: Verify target field is 'pr'."""
    body_path = write_temp_body(load_fixture("valid_not_schema_change.md"))
    try:
        monkeypatch.setattr(open_pr, "resolve_repo", lambda: "squne121/loop-protocol")
        monkeypatch.setattr(open_pr, "resolve_branch", lambda: "worktree-issue-330-validate-pr-body")
        monkeypatch.setattr(open_pr, "get_linked_issue_state", lambda repo, issue: "OPEN")
        monkeypatch.setattr(open_pr, "resolve_changed_paths", lambda provided: ["src/example.ts"])

        class FakeCP:
            returncode = 0
            stdout = json.dumps({"schema": "loop_body_lint/v1", "target": "issue", "status": "pass", "errors": []})
            stderr = ""

        monkeypatch.setattr(open_pr.subprocess, "run", lambda *args, **kwargs: FakeCP())
        monkeypatch.setattr(open_pr, "find_existing_pr", lambda repo, branch: None)

        rc = open_pr.main(
            [
                "--pr-title",
                "feat: test",
                "--linked-issue",
                "330",
                "--publish",
                "yes",
                "--pr-body-file",
                body_path,
            ]
        )
        assert rc == 2
    finally:
        Path(body_path).unlink(missing_ok=True)


def test_b3_validator_body_sha256_mismatch(monkeypatch: pytest.MonkeyPatch):
    """B3: Verify body_sha256 matches the final body."""
    body_path = write_temp_body(load_fixture("valid_not_schema_change.md"))
    try:
        monkeypatch.setattr(open_pr, "resolve_repo", lambda: "squne121/loop-protocol")
        monkeypatch.setattr(open_pr, "resolve_branch", lambda: "worktree-issue-330-validate-pr-body")
        monkeypatch.setattr(open_pr, "get_linked_issue_state", lambda repo, issue: "OPEN")
        monkeypatch.setattr(open_pr, "resolve_changed_paths", lambda provided: ["src/example.ts"])

        class FakeCP:
            returncode = 0
            stdout = json.dumps({
                "schema": "loop_body_lint/v1",
                "target": "pr",
                "status": "pass",
                "errors": [],
                "body_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000"
            })
            stderr = ""

        monkeypatch.setattr(open_pr.subprocess, "run", lambda *args, **kwargs: FakeCP())
        monkeypatch.setattr(open_pr, "find_existing_pr", lambda repo, branch: None)

        rc = open_pr.main(
            [
                "--pr-title",
                "feat: test",
                "--linked-issue",
                "330",
                "--publish",
                "yes",
                "--pr-body-file",
                body_path,
            ]
        )
        assert rc == 2
    finally:
        Path(body_path).unlink(missing_ok=True)


def test_b5_validator_timeout(monkeypatch: pytest.MonkeyPatch):
    """B5: Handle subprocess.TimeoutExpired gracefully."""
    body_path = write_temp_body(load_fixture("valid_not_schema_change.md"))
    try:
        monkeypatch.setattr(open_pr, "resolve_repo", lambda: "squne121/loop-protocol")
        monkeypatch.setattr(open_pr, "resolve_branch", lambda: "worktree-issue-330-validate-pr-body")
        monkeypatch.setattr(open_pr, "get_linked_issue_state", lambda repo, issue: "OPEN")
        monkeypatch.setattr(open_pr, "resolve_changed_paths", lambda provided: ["src/example.ts"])

        def fake_run_timeout(*args, **kwargs):
            raise open_pr.subprocess.TimeoutExpired(cmd=["validator"], timeout=60)

        monkeypatch.setattr(open_pr.subprocess, "run", fake_run_timeout)
        monkeypatch.setattr(open_pr, "find_existing_pr", lambda repo, branch: None)

        rc = open_pr.main(
            [
                "--pr-title",
                "feat: test",
                "--linked-issue",
                "330",
                "--publish",
                "yes",
                "--pr-body-file",
                body_path,
            ]
        )
        assert rc == 2
    finally:
        Path(body_path).unlink(missing_ok=True)


def test_b5_validator_oserror(monkeypatch: pytest.MonkeyPatch):
    """B5: Handle OSError gracefully."""
    body_path = write_temp_body(load_fixture("valid_not_schema_change.md"))
    try:
        monkeypatch.setattr(open_pr, "resolve_repo", lambda: "squne121/loop-protocol")
        monkeypatch.setattr(open_pr, "resolve_branch", lambda: "worktree-issue-330-validate-pr-body")
        monkeypatch.setattr(open_pr, "get_linked_issue_state", lambda repo, issue: "OPEN")
        monkeypatch.setattr(open_pr, "resolve_changed_paths", lambda provided: ["src/example.ts"])

        def fake_run_oserror(*args, **kwargs):
            raise OSError("Spawn error")

        monkeypatch.setattr(open_pr.subprocess, "run", fake_run_oserror)
        monkeypatch.setattr(open_pr, "find_existing_pr", lambda repo, branch: None)

        rc = open_pr.main(
            [
                "--pr-title",
                "feat: test",
                "--linked-issue",
                "330",
                "--publish",
                "yes",
                "--pr-body-file",
                body_path,
            ]
        )
        assert rc == 2
    finally:
        Path(body_path).unlink(missing_ok=True)


# --- AC1/AC2: E_SCHEMA_CONSUMER_INVENTORY_MISSING classification tests ---


def _run_main_with_validator_result(
    monkeypatch: pytest.MonkeyPatch,
    body_fixture: str,
    validator_result: dict,
    extra_args: list[str] | None = None,
) -> tuple[int, list[str]]:
    """Helper: run open_pr.main with a fixed validator result, capture stdout."""

    body_path = write_temp_body(load_fixture(body_fixture))
    output_lines: list[str] = []
    original_print = print  # noqa: F841

    def capture_print(*args, **kwargs):
        sep = kwargs.get("sep", " ")
        line = sep.join(str(a) for a in args)
        output_lines.append(line)

    try:
        monkeypatch.setattr(open_pr, "resolve_repo", lambda: "squne121/loop-protocol")
        monkeypatch.setattr(open_pr, "resolve_branch", lambda: "worktree-issue-170-test")
        monkeypatch.setattr(open_pr, "get_linked_issue_state", lambda repo, issue: "OPEN")
        monkeypatch.setattr(open_pr, "resolve_changed_paths", lambda provided: ["src/example.ts"])
        monkeypatch.setattr(
            open_pr,
            "_run_pr_body_validator",
            lambda body, changed_paths, linked_issue: validator_result,
        )
        monkeypatch.setattr(open_pr, "find_existing_pr", lambda repo, branch: None)
        monkeypatch.setattr(
            open_pr,
            "create_pr",
            lambda *args,
            **kwargs: (_ for _ in ()).throw(AssertionError("no create"))
        )
        monkeypatch.setattr("builtins.print", capture_print)

        base_args = [
            "--pr-title", "feat: test",
            "--linked-issue", "170",
            "--publish", "yes",
            "--pr-body-file", body_path,
        ]
        if extra_args:
            base_args.extend(extra_args)

        rc = open_pr.main(base_args)
        return rc, output_lines
    finally:
        Path(body_path).unlink(missing_ok=True)


def test_ac1_lp050_classified_as_schema_consumer_inventory_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
):
    """AC1: LP050 failure results in E_SCHEMA_CONSUMER_INVENTORY_MISSING."""
    rc, lines = _run_main_with_validator_result(
        monkeypatch,
        "schema_change_missing_inventory.md",
        {
            "status": "fail",
            "errors": [{"rule_id": "LP050", "message": "Schema change PR requires non-placeholder inventory."}],
            "message": "LP050 failure",
        },
    )
    assert rc == 2
    assert any(line == f"ERROR={open_pr.E_SCHEMA_CONSUMER_INVENTORY_MISSING}" for line in lines), (
        f"Expected ERROR={open_pr.E_SCHEMA_CONSUMER_INVENTORY_MISSING} in output; got: {lines}"
    )
    assert not any(line == f"ERROR={open_pr.E_PR_BODY_VALIDATION_FAILED}" for line in lines), (
        f"Should not emit E_PR_BODY_VALIDATION_FAILED for LP050; got: {lines}"
    )


def test_ac2_lp052_schema_consumer_inventory_classified_correctly(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
):
    """AC2: LP052 missing Schema Consumer Inventory => E_SCHEMA_CONSUMER_INVENTORY_MISSING."""
    rc, lines = _run_main_with_validator_result(
        monkeypatch,
        "missing_schema_consumer_inventory_section.md",
        {
            "status": "fail",
            "errors": [
                {
                    "rule_id": "LP052",
                    "section": "(global)",
                    "message": "Missing required section: Schema Consumer Inventory",
                }
            ],
            "message": "LP052 missing section",
        },
    )
    assert rc == 2
    assert any(line == f"ERROR={open_pr.E_SCHEMA_CONSUMER_INVENTORY_MISSING}" for line in lines), (
        f"Expected ERROR={open_pr.E_SCHEMA_CONSUMER_INVENTORY_MISSING}; got: {lines}"
    )


def test_ac2_lp052_other_section_not_misclassified(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
):
    """AC2 boundary: LP052 missing a different section => E_PR_BODY_VALIDATION_FAILED."""
    rc, lines = _run_main_with_validator_result(
        monkeypatch,
        "missing_summary.md",
        {
            "status": "fail",
            "errors": [
                {
                    "rule_id": "LP052",
                    "section": "(global)",
                    "message": "Missing required section: Summary",
                }
            ],
            "message": "LP052 missing Summary",
        },
    )
    assert rc == 2
    assert any(line == f"ERROR={open_pr.E_PR_BODY_VALIDATION_FAILED}" for line in lines), (
        f"Expected E_PR_BODY_VALIDATION_FAILED for LP052/Summary; got: {lines}"
    )
    assert not any(line == f"ERROR={open_pr.E_SCHEMA_CONSUMER_INVENTORY_MISSING}" for line in lines)


def test_ac3_stdout_includes_validator_rule_ids_on_lp050(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
):
    """AC3: VALIDATOR_RULE_IDS and ERROR=E_SCHEMA_CONSUMER_INVENTORY_MISSING emitted, no gh pr create."""
    rc, lines = _run_main_with_validator_result(
        monkeypatch,
        "schema_change_missing_inventory.md",
        {
            "status": "fail",
            "errors": [{"rule_id": "LP050", "message": "inventory missing"}],
            "message": "LP050",
        },
    )
    assert rc == 2
    assert any(line.startswith("VALIDATOR_RULE_IDS=") for line in lines), (
        f"Expected VALIDATOR_RULE_IDS in stdout; got: {lines}"
    )
    assert any(line == f"ERROR={open_pr.E_SCHEMA_CONSUMER_INVENTORY_MISSING}" for line in lines)


def test_ac4_not_schema_change_na_pass_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
):
    """AC4: not_schema_change + N/A inventory passes without E_SCHEMA_CONSUMER_INVENTORY_MISSING.

    Uses find_existing_pr to return an existing PR to avoid calling create_pr in test.
    """
    body_path = write_temp_body(load_fixture("valid_not_schema_change.md"))
    output_lines: list[str] = []

    def capture_print(*args, **kwargs):
        sep = kwargs.get("sep", " ")
        line = sep.join(str(a) for a in args)
        output_lines.append(line)

    try:
        monkeypatch.setattr(open_pr, "resolve_repo", lambda: "squne121/loop-protocol")
        monkeypatch.setattr(open_pr, "resolve_branch", lambda: "worktree-issue-170-test")
        monkeypatch.setattr(open_pr, "get_linked_issue_state", lambda repo, issue: "OPEN")
        monkeypatch.setattr(open_pr, "resolve_changed_paths", lambda provided: ["src/example.ts"])
        monkeypatch.setattr(
            open_pr,
            "_run_pr_body_validator",
            lambda body, changed_paths, linked_issue: {"status": "pass", "errors": []},
        )
        monkeypatch.setattr(
            open_pr,
            "_run_japanese_content_validator",
            lambda body_text,
            threshold=0.1: {
                "status": "pass",
                "failed_blocks": 0,
                "aggregate_ratio": 0.5,
                "threshold": 0.1,
                "body_sha256": "",
                "stderr": ""
            }
        )
        # Return existing PR so create_pr is not called
        monkeypatch.setattr(
            open_pr,
            "find_existing_pr",
            lambda repo, branch: {"number": 999, "url": "https://example.com/pr/999"},
        )
        monkeypatch.setattr("builtins.print", capture_print)

        rc = open_pr.main(
            [
                "--pr-title", "feat: test",
                "--linked-issue", "170",
                "--publish", "yes",
                "--pr-body-file", body_path,
            ]
        )
        assert rc == 0
        assert not any(line.startswith("ERROR=") for line in output_lines), (
            f"No ERROR expected on pass; got: {output_lines}"
        )
    finally:
        Path(body_path).unlink(missing_ok=True)


def test_ac5_validator_internal_error_not_misclassified(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
):
    """AC5: validator internal error is NOT classified as E_SCHEMA_CONSUMER_INVENTORY_MISSING."""
    rc, lines = _run_main_with_validator_result(
        monkeypatch,
        "valid_not_schema_change.md",
        {
            "status": "internal",
            "errors": [],
            "message": "Validator timeout",
        },
    )
    assert rc == 2
    assert not any(line == f"ERROR={open_pr.E_SCHEMA_CONSUMER_INVENTORY_MISSING}" for line in lines), (
        f"internal error must NOT map to E_SCHEMA_CONSUMER_INVENTORY_MISSING; got: {lines}"
    )


def test_ac5_schema_mismatch_not_misclassified(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
):
    """AC5: schema mismatch (status=internal) is NOT E_SCHEMA_CONSUMER_INVENTORY_MISSING."""
    rc, lines = _run_main_with_validator_result(
        monkeypatch,
        "valid_not_schema_change.md",
        {
            "status": "internal",
            "errors": [],
            "message": "Validator schema mismatch: wrong_schema",
        },
    )
    assert rc == 2
    assert not any(line == f"ERROR={open_pr.E_SCHEMA_CONSUMER_INVENTORY_MISSING}" for line in lines)


def test_ac6_dry_run_also_validates_and_classifies(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
):
    """AC6: --dry-run still applies E_SCHEMA_CONSUMER_INVENTORY_MISSING classification."""
    rc, lines = _run_main_with_validator_result(
        monkeypatch,
        "schema_change_missing_inventory.md",
        {
            "status": "fail",
            "errors": [{"rule_id": "LP050", "message": "inventory missing"}],
            "message": "LP050",
        },
        extra_args=["--dry-run"],
    )
    assert rc == 2
    assert any(line == f"ERROR={open_pr.E_SCHEMA_CONSUMER_INVENTORY_MISSING}" for line in lines), (
        f"dry-run must still classify LP050 as E_SCHEMA_CONSUMER_INVENTORY_MISSING; got: {lines}"
    )


# --- B3: Integration test using real validator subprocess ---


def test_integration_missing_schema_consumer_inventory_uses_real_validator(
    monkeypatch: pytest.MonkeyPatch,
):
    """Real validator subprocess integration: LP052 missing Schema Consumer Inventory
    section yields E_SCHEMA_CONSUMER_INVENTORY_MISSING via open_pr.main.

    _run_pr_body_validator is NOT monkeypatched; only create_pr and gh-related helpers
    are replaced to avoid network calls.
    """
    fixture_path = FIXTURE_DIR / "missing_schema_consumer_inventory_section.md"
    body_path = write_temp_body(fixture_path.read_text(encoding="utf-8"))
    output_lines: list[str] = []

    def capture_print(*args, **kwargs):
        sep = kwargs.get("sep", " ")
        line = sep.join(str(a) for a in args)
        output_lines.append(line)

    try:
        monkeypatch.setattr(open_pr, "resolve_repo", lambda: "squne121/loop-protocol")
        monkeypatch.setattr(open_pr, "resolve_branch", lambda: "worktree-issue-170-test")
        monkeypatch.setattr(open_pr, "get_linked_issue_state", lambda repo, issue: "OPEN")
        monkeypatch.setattr(open_pr, "resolve_changed_paths", lambda provided: ["src/example.ts"])
        monkeypatch.setattr(open_pr, "find_existing_pr", lambda repo, branch: None)
        monkeypatch.setattr(
            open_pr,
            "create_pr",
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("create_pr must not be called")),
        )
        monkeypatch.setattr("builtins.print", capture_print)

        # _run_pr_body_validator is NOT monkeypatched — real subprocess is used
        rc = open_pr.main(
            [
                "--pr-title", "feat: test integration",
                "--linked-issue", "170",
                "--publish", "yes",
                "--pr-body-file", body_path,
            ]
        )

        assert rc == 2, f"Expected exit 2 from validator failure; got {rc}; output: {output_lines}"
        assert any(
            line == f"ERROR={open_pr.E_SCHEMA_CONSUMER_INVENTORY_MISSING}" for line in output_lines
        ), (
            f"Expected ERROR=E_SCHEMA_CONSUMER_INVENTORY_MISSING in stdout; got: {output_lines}"
        )
        assert not any(
            line == f"ERROR={open_pr.E_PR_BODY_VALIDATION_FAILED}" for line in output_lines
        ), (
            f"Should not emit E_PR_BODY_VALIDATION_FAILED for LP052/Schema Consumer Inventory; got: {output_lines}"
        )
    finally:
        Path(body_path).unlink(missing_ok=True)


# --- AC8: Japanese content validation blocks gh pr create ---


def _run_main_with_japanese_result(
    monkeypatch: pytest.MonkeyPatch,
    body_fixture: str,
    japanese_result: dict,
    extra_args: list[str] | None = None,
) -> tuple[int, list[str]]:
    """Helper: run open_pr.main with a fixed japanese validator result, capture stdout."""
    body_path = write_temp_body(load_fixture(body_fixture))
    output_lines: list[str] = []

    def capture_print(*args, **kwargs):
        sep = kwargs.get("sep", " ")
        line = sep.join(str(a) for a in args)
        output_lines.append(line)

    try:
        monkeypatch.setattr(open_pr, "resolve_repo", lambda: "squne121/loop-protocol")
        monkeypatch.setattr(open_pr, "resolve_branch", lambda: "worktree-issue-842-test")
        monkeypatch.setattr(open_pr, "get_linked_issue_state", lambda repo, issue: "OPEN")
        monkeypatch.setattr(open_pr, "resolve_changed_paths", lambda provided: ["src/example.ts"])
        # PR body validator always passes
        monkeypatch.setattr(
            open_pr,
            "_run_pr_body_validator",
            lambda body, changed_paths, linked_issue: {"status": "pass", "errors": []},
        )
        # Japanese validator returns the provided result
        monkeypatch.setattr(
            open_pr,
            "_run_japanese_content_validator",
            lambda body_text, threshold=0.1: japanese_result,
        )
        monkeypatch.setattr(open_pr, "find_existing_pr", lambda repo, branch: None)

        create_called = {"value": False}

        def fake_create_pr(*args, **kwargs):
            create_called["value"] = True
            raise AssertionError("create_pr must not be called when Japanese check fails")

        monkeypatch.setattr(open_pr, "create_pr", fake_create_pr)
        monkeypatch.setattr("builtins.print", capture_print)

        base_args = [
            "--pr-title", "feat: test",
            "--linked-issue", "842",
            "--publish", "yes",
            "--pr-body-file", body_path,
        ]
        if extra_args:
            base_args.extend(extra_args)

        rc = open_pr.main(base_args)
        return rc, output_lines, create_called["value"]
    finally:
        Path(body_path).unlink(missing_ok=True)


def test_ac8_japanese_fail_blocks_gh_pr_create(monkeypatch: pytest.MonkeyPatch):
    """AC8: English prose block -> Japanese check fail -> gh pr create NOT called."""
    rc, lines, create_called = _run_main_with_japanese_result(
        monkeypatch,
        "valid_not_schema_change.md",
        {
            "status": "fail",
            "failed_blocks": 2,
            "aggregate_ratio": 0.02,
            "threshold": 0.1,
            "body_sha256": "sha256:abc123",
            "stderr": "FAIL: 日本語比率不足 (aggregate=0.020, threshold=0.1, failed_blocks=2)",
        },
    )
    assert rc == 2
    assert not create_called, "gh pr create must NOT be called when Japanese check fails"
    assert any(
        line == f"ERROR={open_pr.E_PR_BODY_JAPANESE_VALIDATION_FAILED}" for line in lines
    ), f"Expected ERROR=E_PR_BODY_JAPANESE_VALIDATION_FAILED; got: {lines}"


def test_ac8_japanese_fail_emits_preflight_result_v1(monkeypatch: pytest.MonkeyPatch):
    """AC8: Japanese check fail emits PR_BODY_PREFLIGHT_RESULT_V1 with required fields."""
    import json as _json
    rc, lines, _ = _run_main_with_japanese_result(
        monkeypatch,
        "valid_not_schema_change.md",
        {
            "status": "fail",
            "failed_blocks": 1,
            "aggregate_ratio": 0.05,
            "threshold": 0.1,
            "body_sha256": "sha256:def456",
            "stderr": "FAIL: 日本語比率不足",
        },
    )
    assert rc == 2
    preflight_lines = [ln for ln in lines if ln.startswith("PR_BODY_PREFLIGHT_RESULT_V1=")]
    assert len(preflight_lines) == 1, f"Expected exactly one PR_BODY_PREFLIGHT_RESULT_V1 line; got: {lines}"
    json_str = preflight_lines[0][len("PR_BODY_PREFLIGHT_RESULT_V1="):]
    payload = _json.loads(json_str)
    assert payload.get("schema") == "PR_BODY_PREFLIGHT_RESULT_V1"
    assert payload.get("status") == "fail"
    assert "body_sha256" in payload
    assert "failed_blocks" in payload
    assert "aggregate_ratio" in payload
    assert "threshold" in payload


def test_ac8_japanese_pass_allows_gh_pr_create(monkeypatch: pytest.MonkeyPatch):
    """AC8: Japanese check pass -> gh pr create is NOT blocked (normal flow continues)."""
    body_path = write_temp_body(load_fixture("valid_not_schema_change.md"))
    create_called = {"value": False}
    output_lines = []

    def capture_print(*args, **kwargs):
        sep = kwargs.get("sep", " ")
        line = sep.join(str(a) for a in args)
        output_lines.append(line)

    try:
        monkeypatch.setattr(open_pr, "resolve_repo", lambda: "squne121/loop-protocol")
        monkeypatch.setattr(open_pr, "resolve_branch", lambda: "worktree-issue-842-test")
        monkeypatch.setattr(open_pr, "get_linked_issue_state", lambda repo, issue: "OPEN")
        monkeypatch.setattr(open_pr, "resolve_changed_paths", lambda provided: ["src/example.ts"])
        monkeypatch.setattr(
            open_pr,
            "_run_pr_body_validator",
            lambda body, changed_paths, linked_issue: {"status": "pass", "errors": []},
        )
        monkeypatch.setattr(
            open_pr,
            "_run_japanese_content_validator",
            lambda body_text, threshold=0.1: {
                "status": "pass",
                "failed_blocks": 0,
                "aggregate_ratio": 0.45,
                "threshold": 0.1,
                "body_sha256": "sha256:abc",
                "stderr": "",
            },
        )
        monkeypatch.setattr(open_pr, "find_existing_pr", lambda repo, branch: None)
        # P1-1 (PR #1467 review fix): overlap gate 起動判定用の label 再取得を
        # モックし、labels なし（phase/implementation 無し）を明示する。実
        # gh 呼び出しに fail-closed した場合、overlap gate が強制起動され
        # evidence 未提供で rc=2 になるため、この AC8 テストの意図
        # （Japanese check pass 時に gh pr create がブロックされない）を
        # 検証するには label 取得を空 list でモックする必要がある。
        monkeypatch.setattr(
            open_pr,
            "fetch_current_linked_issue_labels",
            lambda repo, issue_number: ([], None),
        )

        def fake_create_pr(repo, title, body_file, branch, draft):
            create_called["value"] = True
            return "https://github.com/squne121/loop-protocol/pull/999"

        monkeypatch.setattr(open_pr, "create_pr", fake_create_pr)
        monkeypatch.setattr("builtins.print", capture_print)

        rc = open_pr.main(
            [
                "--pr-title", "feat: test",
                "--linked-issue", "842",
                "--publish", "yes",
                "--pr-body-file", body_path,
            ]
        )
        assert rc == 0
        assert create_called["value"] is True, "gh pr create SHOULD be called when Japanese check passes"
        assert not any(
            line.startswith("ERROR=") for line in output_lines
        ), f"No ERROR expected; got: {output_lines}"
    finally:
        Path(body_path).unlink(missing_ok=True)


def test_ac8_japanese_internal_error_blocks_gh_pr_create(monkeypatch: pytest.MonkeyPatch):
    """AC8: Japanese validator internal error -> fail-closed, gh pr create NOT called."""
    rc, lines, create_called = _run_main_with_japanese_result(
        monkeypatch,
        "valid_not_schema_change.md",
        {
            "status": "internal",
            "failed_blocks": 0,
            "aggregate_ratio": 0.0,
            "threshold": 0.1,
            "body_sha256": "sha256:abc",
            "stderr": "Timeout expired",
        },
    )
    assert rc == 2
    assert not create_called, "gh pr create must NOT be called on internal error"
    assert any(
        line == f"ERROR={open_pr.E_PR_BODY_JAPANESE_VALIDATION_FAILED}" for line in lines
    ), f"Expected ERROR=E_PR_BODY_JAPANESE_VALIDATION_FAILED; got: {lines}"


def test_japanese_validator_parity_with_update_pr():
    """open_pr と update_pr の _run_japanese_content_validator が同じシグネチャ・挙動を持つことを確認。"""
    import inspect
    import open_pr
    import update_pr
    open_sig = inspect.signature(open_pr._run_japanese_content_validator)
    update_sig = inspect.signature(update_pr._run_japanese_content_validator)
    assert list(open_sig.parameters.keys()) == list(update_sig.parameters.keys()), (
        f"シグネチャ不一致: open_pr={list(open_sig.parameters.keys())}, "
        f"update_pr={list(update_sig.parameters.keys())}"
    )
