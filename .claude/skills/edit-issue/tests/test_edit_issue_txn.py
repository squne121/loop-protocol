from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import edit_issue_txn as txn  # noqa: E402


@pytest.fixture()
def repo_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "tmp").mkdir()
    (root / "artifacts").mkdir()
    monkeypatch.setattr(txn, "REPO_ROOT", root)
    monkeypatch.setattr(txn, "CONTROLLED_EXEC", root / "scripts" / "agent-guards" / "controlled_skill_mutation_exec.py")
    monkeypatch.setattr(txn, "GUARD_SCRIPT", root / "guard.py")
    monkeypatch.setattr(txn, "HYGIENE_SCRIPT", root / "hygiene.py")
    monkeypatch.setattr(txn, "READINESS_SCRIPT", root / "readiness.py")
    return root


def _minimal_input(repo_tmp: Path, *, comment_mode: dict | None = None, title_required: bool = False) -> dict:
    new_body = repo_tmp / "tmp" / "new_body.md"
    new_body.write_text("new issue body", encoding="utf-8")
    return {
        "schema": "ISSUE_EDIT_TXN_INPUT_V1",
        "issue_number": 1287,
        "repo": "squne121/loop-protocol",
        "new_body_file": "tmp/new_body.md",
        "readiness_forwarding_payload": {
            "readiness_result": {
                "status": "go",
                "body_sha256": "sha256:old",
                "source_checks": ["contract_readiness_check.py --mode static"],
                "errors": [],
                "readiness_result_ref": "artifact.json",
            }
        },
        "comment_mode": comment_mode or {"mode": "skip"},
        "expected_previous_body_sha256": txn._sha256_text("old issue body"),
        "expected_previous_updated_at": "2026-07-03T10:40:51Z",
        "title_update": {
            "required": title_required,
            "proposed_title": "x" if title_required else None,
            "reason": "x" if title_required else None,
        },
    }


def test_schema_contracts_are_closed() -> None:
    docs = (
        Path(__file__).resolve().parents[4] / "docs" / "dev" / "agent-skill-boundaries.md"
    ).read_text(encoding="utf-8")
    assert "### ISSUE_EDIT_TXN_INPUT_V1" in docs
    assert "### ISSUE_EDIT_TXN_RESULT_V1" in docs
    assert docs.count("additionalProperties: false") >= 2
    assert "body_update:" in docs
    assert "comment_publish:" in docs


def test_no_raw_issue_mutation_or_shell_escape_in_production_path() -> None:
    source = (SCRIPTS_DIR / "edit_issue_txn.py").read_text(encoding="utf-8")
    forbidden = [
        "gh issue edit",
        "gh issue comment",
        "gh api --method PATCH",
        "gh api --method POST",
        "shell=True",
        "bash -c",
        "sh -c",
        "python -c",
    ]
    for token in forbidden:
        assert token not in source
    assert "issue_body.update" in source
    assert "issue_comment.publish" in source


def test_title_update_rejected_without_controlled_executor(repo_tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"fetch": 0}

    def _fetch(*_args: object, **_kwargs: object) -> tuple[dict | None, str]:
        called["fetch"] += 1
        return {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:40:51Z"}, ""

    monkeypatch.setattr(txn, "_fetch_issue", _fetch)
    result = txn.run_transaction(_minimal_input(repo_tmp, title_required=True))
    assert result["status"] == "failed_no_mutation"
    assert result["mutation_started"] is False
    assert result["errors"][0]["code"] == "title_update_requested_without_controlled_title_executor"
    assert called["fetch"] == 0


@pytest.mark.parametrize(
    ("variant", "guard_rc", "readiness_rc"),
    [
        ("stale", 0, 0),
        ("guard", 2, 0),
        ("readiness", 0, 1),
    ],
)
def test_no_mutation_before_guard_readiness_or_stale_precondition(
    repo_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
    guard_rc: int,
    readiness_rc: int,
) -> None:
    events: list[str] = []

    def _fetch(*_args: object, **_kwargs: object) -> tuple[dict | None, str]:
        body = "old issue body" if variant != "stale" else "different body"
        return {"title": "old", "body": body, "updatedAt": "2026-07-03T10:40:51Z"}, ""

    class _CP:
        def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def _run(args: list[str]) -> _CP:
        if str(txn.GUARD_SCRIPT) in args:
            events.append("guard")
            return _CP(guard_rc, stderr="guard failed")
        if str(txn.HYGIENE_SCRIPT) in args:
            events.append("hygiene")
            return _CP(1)
        if str(txn.READINESS_SCRIPT) in args:
            events.append("readiness")
            return _CP(readiness_rc, stderr="readiness failed")
        pytest.fail(f"unexpected command: {args}")

    def _invoke(*_args: object, **_kwargs: object) -> object:
        pytest.fail("controlled executor must not be invoked")

    monkeypatch.setattr(txn, "_fetch_issue", _fetch)
    monkeypatch.setattr(txn, "_run_command", _run)
    monkeypatch.setattr(txn, "_invoke_controlled_exec", _invoke)

    result = txn.run_transaction(_minimal_input(repo_tmp))
    assert result["status"] == "failed_no_mutation"
    assert result["mutation_started"] is False
    assert result["body_update"]["attempted"] is False
    assert result["body_update"]["artifact_ref"] is None
    if variant == "stale":
        assert events == []
    elif variant == "guard":
        assert events == ["guard"]
    else:
        assert events == ["guard", "hygiene", "readiness"]


@pytest.mark.parametrize("failure_mode", ["comment_failure", "final_readback_failure"])
def test_body_update_success_comment_or_readback_failure_maps_failed_after_mutation(
    repo_tmp: Path, monkeypatch: pytest.MonkeyPatch, failure_mode: str
) -> None:
    comment_body = repo_tmp / "tmp" / "comment.md"
    comment_body.write_text("comment body <!-- marker -->", encoding="utf-8")
    payload = _minimal_input(
        repo_tmp,
        comment_mode={"mode": "publish", "comment_body_file": "tmp/comment.md", "marker": "<!-- marker -->"},
    )

    class _CP:
        def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    fetch_calls = {"count": 0}

    def _fetch(*_args: object, **_kwargs: object) -> tuple[dict | None, str]:
        fetch_calls["count"] += 1
        if fetch_calls["count"] == 1:
            return {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:40:51Z"}, ""
        if failure_mode == "final_readback_failure":
            return {"title": "old", "body": "unexpected remote body", "updatedAt": "2026-07-03T10:41:51Z"}, ""
        return {"title": "old", "body": "new issue body", "updatedAt": "2026-07-03T10:41:51Z"}, ""

    def _run(args: list[str]) -> _CP:
        if str(txn.GUARD_SCRIPT) in args:
            return _CP(0, stdout='{"status":"pass"}')
        if str(txn.HYGIENE_SCRIPT) in args:
            return _CP(1)
        if str(txn.READINESS_SCRIPT) in args:
            return _CP(0)
        pytest.fail(f"unexpected command: {args}")

    def _invoke(command_id: str, *_args: object, **_kwargs: object) -> _CP:
        if command_id == "issue_body.update":
            return _CP(0, stdout='{"status":"ok"}')
        if command_id == "issue_comment.publish":
            if failure_mode == "comment_failure":
                return _CP(1, stderr="child stderr with secret")
            return _CP(0, stdout='{"status":"ok"}')
        pytest.fail(command_id)

    monkeypatch.setattr(txn, "_fetch_issue", _fetch)
    monkeypatch.setattr(txn, "_run_command", _run)
    monkeypatch.setattr(txn, "_invoke_controlled_exec", _invoke)
    result = txn.run_transaction(payload)
    assert result["status"] == "failed_after_mutation"
    assert result["mutation_started"] is True
    assert result["body_update"]["attempted"] is True
    if failure_mode == "comment_failure":
        assert result["comment_publish"]["attempted"] is True
        assert result["comment_publish"]["status"] == "failed"
    else:
        assert result["comment_publish"]["attempted"] is False
        assert result["body_update"]["remote_current_body_sha256"] == txn._sha256_text("unexpected remote body")


def test_stdout_single_bounded_json_no_body_or_child_output_leak(
    repo_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo_tmp / "tmp" / "new_body.md").write_text("very secret new issue body", encoding="utf-8")
    (repo_tmp / "tmp" / "input.json").write_text(json.dumps(_minimal_input(repo_tmp)), encoding="utf-8")

    def _run_transaction(_data: dict) -> dict:
        return {
            "schema": "ISSUE_EDIT_TXN_RESULT_V1",
            "status": "failed_no_mutation",
            "issue_number": 1287,
            "repo": "squne121/loop-protocol",
            "mutation_started": False,
            "rollback_attempted": False,
            "body_update": {
                "attempted": False,
                "status": "not_run",
                "previous_body_sha256": None,
                "new_body_sha256": None,
                "remote_current_body_sha256": None,
                "artifact_ref": None,
            },
            "comment_publish": {
                "attempted": False,
                "status": "not_run",
                "comment_id": None,
                "comment_url": None,
                "artifact_ref": None,
            },
            "errors": [{"code": "x", "message": "child stderr should be bounded and raw payload hidden"}],
        }

    monkeypatch.setattr(txn, "run_transaction", _run_transaction)
    rc = txn.main(["--input-file", "tmp/input.json"])
    out = capsys.readouterr().out
    assert rc == 1
    assert out.count("\n") == 1
    assert "very secret new issue body" not in out
    assert "child stderr should be bounded" in out
    parsed = json.loads(out)
    assert parsed["schema"] == "ISSUE_EDIT_TXN_RESULT_V1"


def test_executor_inputs_under_issue_metadata_namespace(repo_tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo_tmp / "tmp" / "comment.md").write_text("comment <!-- marker -->", encoding="utf-8")
    payload = _minimal_input(
        repo_tmp,
        comment_mode={"mode": "publish", "comment_body_file": "tmp/comment.md", "marker": "<!-- marker -->"},
    )

    class _CP:
        def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    calls: list[str] = []

    def _fetch(*_args: object, **_kwargs: object) -> tuple[dict | None, str]:
        return {
            "title": "old",
            "body": "old issue body" if not calls else "new issue body",
            "updatedAt": "2026-07-03T10:40:51Z",
        }, ""

    def _run(args: list[str]) -> _CP:
        if str(txn.GUARD_SCRIPT) in args:
            return _CP(0, stdout='{"status":"pass"}')
        if str(txn.HYGIENE_SCRIPT) in args:
            return _CP(1)
        if str(txn.READINESS_SCRIPT) in args:
            return _CP(0)
        pytest.fail(f"unexpected command: {args}")

    def _invoke(command_id: str, issue_number: int, repo: str, input_ref: str) -> _CP:
        calls.append(input_ref)
        assert input_ref.startswith(f"artifacts/{issue_number}/issue-metadata/{command_id}/")
        return _CP(0, stdout='{"status":"ok"}')

    monkeypatch.setattr(txn, "_fetch_issue", _fetch)
    monkeypatch.setattr(txn, "_run_command", _run)
    monkeypatch.setattr(txn, "_invoke_controlled_exec", _invoke)
    result = txn.run_transaction(payload)
    assert result["status"] == "ok"
    assert len(calls) == 2


def test_skill_and_issue_author_no_raw_existing_issue_mutation_contract() -> None:
    skill = (Path(__file__).resolve().parents[1] / "SKILL.md").read_text(encoding="utf-8")
    agent = (Path(__file__).resolve().parents[3] / "agents" / "issue-author.md").read_text(encoding="utf-8")
    forbidden = ["gh issue edit", "gh issue comment", "gh api --method PATCH", "gh api --method POST"]
    for token in forbidden:
        assert token not in skill
        assert token not in agent
    assert "edit_issue_txn.py" in skill
    assert "edit_issue_txn.py" in agent


def test_dependency_policy_separates_txn_helper_from_end_to_end_raw_removal() -> None:
    skill = (Path(__file__).resolve().parents[1] / "SKILL.md").read_text(encoding="utf-8")
    assert "required_for_txn_helper" in skill
    assert "required_for_end_to_end_raw_mutation_removal" in skill
    assert "#1284 / PR #1295" in skill
    assert "#1291 / PR #1298" in skill
