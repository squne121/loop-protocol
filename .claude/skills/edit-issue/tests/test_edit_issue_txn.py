from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import edit_issue_txn as txn  # noqa: E402


class _CP:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


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


def _normal_input_with_new_body(repo_tmp: Path, new_body: str) -> dict:
    (repo_tmp / "tmp" / "new_body.md").write_text(new_body, encoding="utf-8")
    payload = _minimal_input(repo_tmp)
    return payload


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
    assert "issue_content.update" in source
    assert "issue_comment.publish" in source


def test_title_update_routes_through_content_executor(
    repo_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    readbacks = iter([
        {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:40:51Z"},
        {"title": "x", "body": "new issue body", "updatedAt": "2026-07-03T10:41:51Z"},
    ])
    calls: list[str] = []

    def _fetch(*_args: object, **_kwargs: object) -> tuple[dict | None, str]:
        return next(readbacks), ""

    def _run(args: list[str]) -> _CP:
        if str(txn.GUARD_SCRIPT) in args:
            return _CP(0, stdout='{"status":"pass"}')
        if str(txn.HYGIENE_SCRIPT) in args or str(txn.READINESS_SCRIPT) in args:
            return _CP(0)
        pytest.fail(f"unexpected command: {args}")

    def _invoke(command_id: str, *_args: object, **_kwargs: object) -> tuple[_CP, dict | None]:
        calls.append(command_id)
        return _CP(0), {"new_body_sha256": txn._sha256_text("new issue body")}

    monkeypatch.setattr(txn, "_fetch_issue", _fetch)
    monkeypatch.setattr(txn, "_run_command", _run)
    monkeypatch.setattr(txn, "_invoke_controlled_exec", _invoke)
    result = txn.run_transaction(_minimal_input(repo_tmp, title_required=True))
    assert result["status"] == "ok"
    assert result["mutation_started"] is True
    assert calls == ["issue_content.update"]


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

    def _invoke(*_args: object, **_kwargs: object) -> tuple[_CP, dict | None]:
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


def test_controlled_executor_invoked_with_json_and_parsed(
    repo_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    fetch_calls = {"count": 0}

    def _fetch(*_args: object, **_kwargs: object) -> tuple[dict | None, str]:
        calls.append(["fetch"])
        fetch_calls["count"] += 1
        if fetch_calls["count"] > 1:
            return {"title": "old", "body": "new issue body", "updatedAt": "2026-07-03T10:41:51Z"}, ""
        return {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:40:51Z"}, ""

    def _run(args: list[str]) -> _CP:
        if str(txn.GUARD_SCRIPT) in args:
            return _CP(0, stdout='{"status":"pass"}')
        if str(txn.HYGIENE_SCRIPT) in args:
            return _CP(0)
        if str(txn.READINESS_SCRIPT) in args:
            return _CP(0)
        if str(txn.CONTROLLED_EXEC) in args:
            calls.append(args)
            return _CP(0, stdout='{"new_body_sha256":"sha256:parsed"}')
        pytest.fail(f"unexpected command: {args}")

    monkeypatch.setattr(txn, "_fetch_issue", _fetch)
    monkeypatch.setattr(txn, "_run_command", _run)

    result = txn.run_transaction(_normal_input_with_new_body(repo_tmp, "new issue body"))
    assert any(arg == "--json" for call in calls for arg in call)
    assert result["status"] == "ok"
    assert result["body_update"]["new_body_sha256"] == "sha256:parsed"


def test_comment_publish_success_propagates_comment_id_url_body_sha(
    repo_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comment_body = repo_tmp / "tmp" / "comment.md"
    comment_body.write_text("comment body <!-- marker -->", encoding="utf-8")
    payload = _minimal_input(
        repo_tmp,
        comment_mode={"mode": "publish", "comment_body_file": "tmp/comment.md", "marker": "<!-- marker -->"},
    )

    calls: list[str] = []
    fetch_calls = {"count": 0}

    def _fetch(*_args: object, **_kwargs: object) -> tuple[dict | None, str]:
        fetch_calls["count"] += 1
        if fetch_calls["count"] == 1:
            return {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:40:51Z"}, ""
        return {"title": "old", "body": "new issue body", "updatedAt": "2026-07-03T10:41:51Z"}, ""

    def _run(args: list[str]) -> _CP:
        if str(txn.GUARD_SCRIPT) in args:
            return _CP(0, stdout='{"status":"pass"}')
        if str(txn.HYGIENE_SCRIPT) in args:
            return _CP(0)
        if str(txn.READINESS_SCRIPT) in args:
            return _CP(0)
        if str(txn.CONTROLLED_EXEC) in args:
            if "issue_content.update" in args:
                calls.append("issue_content.update")
                return _CP(0, stdout='{"new_body_sha256":"sha256:new"}')
            if "issue_comment.publish" in args:
                calls.append("issue_comment.publish")
                return _CP(
                    0,
                    stdout=json.dumps(
                        {
                            "comment_id": "c-123",
                            "comment_url": "https://github.com/squne121/loop-protocol/issues/1287#issuecomment-123",
                            "body_sha256": "sha256:comment",
                        }
                    ),
                )
            return _CP(0, stdout="{}")
        pytest.fail(f"unexpected command: {args}")

    monkeypatch.setattr(txn, "_fetch_issue", _fetch)
    monkeypatch.setattr(txn, "_run_command", _run)

    result = txn.run_transaction(payload)
    assert result["status"] == "ok"
    assert calls == ["issue_content.update", "issue_comment.publish"]
    assert result["comment_publish"]["comment_id"] == "c-123"
    assert result["comment_publish"]["comment_url"] == "https://github.com/squne121/loop-protocol/issues/1287#issuecomment-123"
    assert result["comment_publish"]["comment_body_sha256"] == "sha256:comment"


def test_body_unchanged_comment_publish_skips_body_update(
    repo_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comment_body = repo_tmp / "tmp" / "comment.md"
    comment_body.write_text("publish marker", encoding="utf-8")
    payload = _minimal_input(
        repo_tmp,
        comment_mode={"mode": "publish", "comment_body_file": "tmp/comment.md", "marker": "publish marker"},
    )
    (repo_tmp / "tmp" / "new_body.md").write_text("old issue body", encoding="utf-8")
    payload["expected_previous_body_sha256"] = txn._sha256_text("old issue body")

    called: list[str] = []

    def _fetch(*_args: object, **_kwargs: object) -> tuple[dict | None, str]:
        return {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:40:51Z"}, ""

    def _invoke(command_id: str, *_args: object, **_kwargs: object) -> tuple[_CP, dict | None]:
        called.append(command_id)
        if command_id == "issue_comment.publish":
            return (
                _CP(
                    0,
                    stdout=json.dumps(
                        {
                            "comment_id": "c-2",
                            "comment_url": "https://example.com/c2",
                            "body_sha256": "sha256:comment",
                        }
                    ),
                ),
                {
                    "comment_id": "c-2",
                    "comment_url": "https://example.com/c2",
                    "body_sha256": "sha256:comment",
                },
            )
        pytest.fail(command_id)

    monkeypatch.setattr(txn, "_fetch_issue", _fetch)
    monkeypatch.setattr(txn, "_invoke_controlled_exec", _invoke)

    result = txn.run_transaction(payload)
    assert result["status"] == "ok"
    assert result["body_update"]["attempted"] is False
    assert result["comment_publish"]["attempted"] is True
    assert result["comment_publish"]["status"] == "ok"
    assert result["comment_publish"]["comment_id"] == "c-2"
    assert called == ["issue_comment.publish"]


def test_safe_repo_file_rejects_symlink_component_for_input_new_body_comment(repo_tmp: Path) -> None:
    real_dir = repo_tmp / "tmp" / "real"
    real_dir.mkdir()
    (real_dir / "candidate.md").write_text("x", encoding="utf-8")

    link_dir = repo_tmp / "tmp" / "link"
    link_dir.symlink_to(real_dir)

    with pytest.raises(ValueError, match="symlink_not_allowed"):
        txn._safe_repo_file("tmp/link/candidate.md")


def test_safe_repo_file_rejects_repo_prefix_collision(repo_tmp: Path) -> None:
    sibling = repo_tmp.parent / f"{repo_tmp.name}-outside"
    sibling.mkdir()
    candidate = sibling / "outside.md"
    candidate.write_text("outside", encoding="utf-8")

    with pytest.raises(ValueError, match="path_not_found|path_must_not_escape_repo"):
        txn._safe_repo_file(f"../{sibling.name}/outside.md")


def test_body_update_success_comment_or_readback_failure_maps_failed_after_mutation(
    repo_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comment_body = repo_tmp / "tmp" / "comment.md"
    comment_body.write_text("comment body <!-- marker -->", encoding="utf-8")
    payload = _minimal_input(
        repo_tmp,
        comment_mode={"mode": "publish", "comment_body_file": "tmp/comment.md", "marker": "<!-- marker -->"},
    )

    fetch_calls = {"count": 0}

    def _fetch(*_args: object, **_kwargs: object) -> tuple[dict | None, str]:
        fetch_calls["count"] += 1
        if fetch_calls["count"] == 1:
            return {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:40:51Z"}, ""
        return {"title": "old", "body": "new issue body", "updatedAt": "2026-07-03T10:41:51Z"}, ""

    def _run(args: list[str]) -> _CP:
        if str(txn.GUARD_SCRIPT) in args:
            return _CP(0, stdout='{"status":"pass"}')
        if str(txn.HYGIENE_SCRIPT) in args:
            return _CP(0)
        if str(txn.READINESS_SCRIPT) in args:
            return _CP(0)
        pytest.fail(f"unexpected command: {args}")

    def _invoke(command_id: str, *_args: object, **_kwargs: object) -> tuple[_CP, dict | None]:
        if command_id == "issue_content.update":
            return _CP(0, stdout='{"new_body_sha256":"sha256:body"}'), {"new_body_sha256": "sha256:body"}
        if command_id == "issue_comment.publish":
            return _CP(1, stderr="child stderr with secret"), None
        pytest.fail(command_id)

    monkeypatch.setattr(txn, "_fetch_issue", _fetch)
    monkeypatch.setattr(txn, "_run_command", _run)
    monkeypatch.setattr(txn, "_invoke_controlled_exec", _invoke)
    result = txn.run_transaction(payload)
    assert result["status"] == "failed_after_mutation"
    assert result["mutation_started"] is True
    assert result["body_update"]["attempted"] is True
    assert result["comment_publish"]["attempted"] is True
    assert result["comment_publish"]["status"] == "failed"


def test_child_timeout_maps_to_single_bounded_json(
    repo_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_json = repo_tmp / "tmp" / "input.json"
    input_json.write_text(json.dumps(_normal_input_with_new_body(repo_tmp, "new issue body")), encoding="utf-8")

    def _fetch(*_args: object, **_kwargs: object) -> tuple[dict | None, str]:
        return {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:40:51Z"}, ""

    def _run(args: list[str]) -> _CP:
        if str(txn.GUARD_SCRIPT) in args:
            return _CP(0, stdout='{"status":"pass"}')
        if str(txn.HYGIENE_SCRIPT) in args:
            return _CP(0)
        if str(txn.READINESS_SCRIPT) in args:
            return _CP(0)
        if str(txn.CONTROLLED_EXEC) in args:
            return _CP(124, stderr="child command timeout after 30s")
        pytest.fail(f"unexpected command: {args}")

    monkeypatch.setattr(txn, "_fetch_issue", _fetch)
    monkeypatch.setattr(txn, "_run_command", _run)

    rc = txn.main(["--input-file", "tmp/input.json"])
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert rc == 1
    assert parsed["status"] == "mutation_outcome_unknown"
    assert parsed["content_update"]["patch_attempted"] is True
    assert parsed["content_update"]["mutation_outcome"] == "unknown"
    assert len(out.splitlines()) == 1
    assert len(parsed["errors"]) == 1
    assert len(parsed["errors"][0]["message"]) <= 240


def test_needs_fix_forwarding_does_not_mutate_without_resolution_evidence(
    repo_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _minimal_input(repo_tmp)
    payload["readiness_forwarding_payload"]["readiness_result"]["status"] = "needs_fix"
    payload["readiness_forwarding_payload"]["readiness_result"].pop("resolution_evidence", None)

    def _run(*_args: object, **_kwargs: object) -> _CP:
        pytest.fail("child subprocess should not be invoked")

    monkeypatch.setattr(txn, "_run_command", _run)

    result = txn.run_transaction(payload)
    assert result["status"] == "failed_no_mutation"
    assert result["mutation_started"] is False
    assert result["body_update"]["attempted"] is False
    assert result["errors"][0]["code"] == "readiness_needs_fix_without_resolution_evidence"


def _assert_no_child_stdout_stderr_leak(
    repo_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (repo_tmp / "tmp" / "new_body.md").write_text("old issue body", encoding="utf-8")
    (repo_tmp / "tmp" / "input.json").write_text(json.dumps(_minimal_input(repo_tmp)), encoding="utf-8")
    secret = "very secret child stdout " * 30

    def _fetch(*_args: object, **_kwargs: object) -> tuple[dict | None, str]:
        return {"title": "old", "body": "old issue body", "updatedAt": "2026-07-03T10:40:51Z"}, ""

    def _run(args: list[str]) -> _CP:
        if str(txn.GUARD_SCRIPT) in args:
            return _CP(1, stdout=secret)
        pytest.fail(f"unexpected command: {args}")

    monkeypatch.setattr(txn, "_fetch_issue", _fetch)
    monkeypatch.setattr(txn, "_run_command", _run)

    rc = txn.main(["--input-file", "tmp/input.json"])
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert rc == 1
    assert parsed["status"] == "failed_no_mutation"
    assert len(out.splitlines()) == 1
    assert secret not in out
    assert parsed["schema"] == txn.RESULT_SCHEMA
    assert parsed["errors"][0]["message"] != secret
    assert len(parsed["errors"][0]["message"]) <= 240


def test_stdout_leak_real_child_stdout_stderr_not_mocked(
    repo_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _assert_no_child_stdout_stderr_leak(repo_tmp, monkeypatch, capsys)


@pytest.mark.parametrize(
    "_case_name",
    ["stdout_single_bounded_json_no_body_or_child_output_leak"],
    ids=["stdout_single_bounded_json_no_body_or_child_output_leak"],
)
def test_child_output_leak_bounded_json_path(
    _case_name: str,
    repo_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _assert_no_child_stdout_stderr_leak(repo_tmp, monkeypatch, capsys)


def test_executor_inputs_under_issue_metadata_namespace(
    repo_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (repo_tmp / "tmp" / "comment.md").write_text("comment <!-- marker -->", encoding="utf-8")
    payload = _minimal_input(
        repo_tmp,
        comment_mode={"mode": "publish", "comment_body_file": "tmp/comment.md", "marker": "<!-- marker -->"},
    )

    calls: list[str] = []

    fetch_calls = {"count": 0}

    def _fetch(*_args: object, **_kwargs: object) -> tuple[dict | None, str]:
        fetch_calls["count"] += 1
        if fetch_calls["count"] > 1:
            return {
                "title": "old",
                "body": "new issue body",
                "updatedAt": "2026-07-03T10:41:51Z",
            }, ""
        return {
            "title": "old",
            "body": "old issue body",
            "updatedAt": "2026-07-03T10:40:51Z",
        }, ""

    def _run(args: list[str]) -> _CP:
        if str(txn.GUARD_SCRIPT) in args:
            return _CP(0, stdout='{"status":"pass"}')
        if str(txn.HYGIENE_SCRIPT) in args:
            return _CP(0)
        if str(txn.READINESS_SCRIPT) in args:
            return _CP(0)
        if str(txn.CONTROLLED_EXEC) in args:
            if "issue_body.update" in args:
                return _CP(0, stdout='{"new_body_sha256":"sha256:x"}'), {
                    "new_body_sha256": "sha256:x"
                }
            if "issue_comment.publish" in args:
                return (
                    _CP(0, stdout='{"comment_id":"c-1","comment_url":"https://example.com/c1","body_sha256":"sha256:c"}'),
                    {
                        "comment_id": "c-1",
                        "comment_url": "https://example.com/c1",
                        "body_sha256": "sha256:c",
                    },
                )
            pytest.fail("unknown command")
        pytest.fail(f"unexpected command: {args}")

    def _invoke(command_id: str, issue_number: int, repo: str, input_ref: str) -> tuple[_CP, dict | None]:
        calls.append(input_ref)
        assert input_ref.startswith(f"artifacts/{issue_number}/issue-metadata/{command_id}/")
        if command_id == "issue_body.update":
            return _CP(0, stdout='{"new_body_sha256":"sha256:x"}'), {"new_body_sha256": "sha256:x"}
        return _CP(0, stdout='{"comment_id":"c-1","comment_url":"https://example.com/c1","body_sha256":"sha256:c"}'), {
            "comment_id": "c-1",
            "comment_url": "https://example.com/c1",
            "body_sha256": "sha256:c",
        }

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
