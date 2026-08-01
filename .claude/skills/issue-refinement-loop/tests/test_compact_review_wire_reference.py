"""CLI-level wire-reference regression coverage for Issue #1887.

The producer and validator are intentionally exercised as separate subprocesses:
the regression is a split-brain failure at that boundary, not a helper-only
path-validation failure.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
FIXTURES_DIR = SKILL_ROOT / "fixtures"
PRODUCER = SCRIPTS_DIR / "compact_review_result.py"
VALIDATOR = SCRIPTS_DIR / "validate_review_compact_output.py"


def _produce_then_validate(
    *, repo_root: Path, artifact_dir: Path, issue_number: int = 1887
) -> tuple[subprocess.CompletedProcess[str], subprocess.CompletedProcess[str]]:
    producer = subprocess.run(
        [
            sys.executable,
            str(PRODUCER),
            "--input-file",
            str(FIXTURES_DIR / "review_result_approve.json"),
            "--artifact-dir",
            str(artifact_dir),
            "--repo-root",
            str(repo_root),
            "--issue-number",
            str(issue_number),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    validator = subprocess.run(
        [sys.executable, str(VALIDATOR), "--issue-number", str(issue_number)],
        input=producer.stdout,
        capture_output=True,
        text=True,
        check=False,
    )
    return producer, validator


def test_absolute_canonical_artifact_dir_producer_validator_subprocess_parity(tmp_path: Path) -> None:
    """An absolute filesystem destination must yield a relative wire reference.

    This is the exact Issue #1887 incident shape: a caller supplies both an
    absolute canonical artifact directory and its repository root.  The
    producer's stdout must remain validator-valid and disclose neither root.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    artifact_dir = repo_root / ".claude" / "artifacts" / "issue-refinement-loop"

    producer, validator = _produce_then_validate(
        repo_root=repo_root,
        artifact_dir=artifact_dir,
    )

    assert producer.returncode == 0, producer.stderr
    assert str(repo_root) not in producer.stdout
    assert "EVIDENCE: .claude/artifacts/issue-refinement-loop/1887/" in producer.stdout
    assert "ARTIFACT: compact_review_result_v1=.claude/artifacts/issue-refinement-loop/1887/" in producer.stdout

    assert validator.returncode == 0, validator.stdout
    payload = json.loads(validator.stdout)
    assert payload["validation_status"] == "valid"
    assert payload["artifact_path_policy"]["status"] == "valid"


def test_relative_canonical_artifact_dir_remains_validator_valid(tmp_path: Path, monkeypatch) -> None:
    """The existing relative artifact-dir invocation remains valid."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    monkeypatch.chdir(repo_root)

    producer, validator = _produce_then_validate(
        repo_root=repo_root,
        artifact_dir=Path(".claude/artifacts/issue-refinement-loop"),
    )

    assert producer.returncode == 0, producer.stderr
    assert validator.returncode == 0, validator.stdout


def test_no_repo_root_rejects_relative_traversal_and_noncanonical_dir(tmp_path: Path) -> None:
    """No-root CLI invocations accept only the canonical relative base."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    outside = tmp_path / "outside"

    for artifact_dir in (Path("../outside"), Path("arbitrary/artifacts")):
        producer = subprocess.run(
            [
                sys.executable,
                str(PRODUCER),
                "--input-file",
                str(FIXTURES_DIR / "review_result_approve.json"),
                "--artifact-dir",
                str(artifact_dir),
                "--issue-number",
                "1887",
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )

        assert producer.returncode == 2
        assert "STATUS: failed" in producer.stdout
        assert "../outside" not in producer.stdout
        assert "arbitrary/artifacts" not in producer.stdout
        assert str(repo_root) not in producer.stdout
        assert not outside.exists()
        assert not (repo_root / "arbitrary").exists()


def test_canonical_issue_slot_symlink_fails_closed_without_path_disclosure(tmp_path: Path) -> None:
    """A same-base issue alias cannot redirect a canonical wire namespace."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    canonical_base = repo_root / ".claude" / "artifacts" / "issue-refinement-loop"
    canonical_base.mkdir(parents=True)
    sibling_slot = canonical_base / "1888"
    sibling_slot.mkdir()
    (canonical_base / "1887").symlink_to(sibling_slot, target_is_directory=True)

    producer, _validator = _produce_then_validate(
        repo_root=repo_root,
        artifact_dir=canonical_base,
    )
    assert producer.returncode == 2
    assert "STATUS: failed" in producer.stdout
    assert str(repo_root) not in producer.stdout
    assert str(sibling_slot) not in producer.stdout
    assert not list(sibling_slot.iterdir())


def test_outside_artifact_dir_fails_closed_without_path_disclosure(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    outside_producer, _ = _produce_then_validate(
        repo_root=repo_root,
        artifact_dir=outside,
    )
    assert outside_producer.returncode == 2
    assert str(repo_root) not in outside_producer.stdout
    assert str(outside) not in outside_producer.stdout


def test_canonical_artifact_base_symlink_escape_fails_closed(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    base_parent = repo_root / ".claude" / "artifacts"
    base_parent.mkdir(parents=True)
    (base_parent / "issue-refinement-loop").symlink_to(outside, target_is_directory=True)

    producer, _ = _produce_then_validate(
        repo_root=repo_root,
        artifact_dir=base_parent / "issue-refinement-loop",
    )
    assert producer.returncode == 2
    assert str(repo_root) not in producer.stdout
    assert str(outside) not in producer.stdout


def test_canonical_artifact_base_symlink_within_repo_fails_closed(tmp_path: Path) -> None:
    """Containment alone is insufficient: the base itself must not alias."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    aliased_base = repo_root / "other-artifacts"
    aliased_base.mkdir()
    base_parent = repo_root / ".claude" / "artifacts"
    base_parent.mkdir(parents=True)
    (base_parent / "issue-refinement-loop").symlink_to(aliased_base, target_is_directory=True)

    producer, _validator = _produce_then_validate(
        repo_root=repo_root,
        artifact_dir=base_parent / "issue-refinement-loop",
    )

    assert producer.returncode == 2
    assert "STATUS: failed" in producer.stdout
    assert str(repo_root) not in producer.stdout
    assert str(aliased_base) not in producer.stdout
    assert not list(aliased_base.iterdir())


def test_success_write_oserror_emits_failure_envelope_without_path_disclosure(tmp_path: Path) -> None:
    """A regular file at the issue directory cannot leak a write traceback."""
    repo_root = tmp_path / "repo"
    issue_slot = repo_root / ".claude" / "artifacts" / "issue-refinement-loop" / "1887"
    issue_slot.parent.mkdir(parents=True)
    issue_slot.write_text("not a directory", encoding="utf-8")

    producer, _validator = _produce_then_validate(
        repo_root=repo_root,
        artifact_dir=repo_root / ".claude" / "artifacts" / "issue-refinement-loop",
    )

    assert producer.returncode == 2
    assert "STATUS: failed" in producer.stdout
    assert "REASON_CODE: schema_mismatch" in producer.stdout
    assert "Traceback" not in producer.stderr
    assert str(repo_root) not in producer.stdout
    assert str(repo_root) not in producer.stderr


def test_cli_requires_positive_issue_number_without_creating_artifacts(tmp_path: Path) -> None:
    """Omitted, zero, and negative issue numbers fail before producer output."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    base_args = [
        sys.executable,
        str(PRODUCER),
        "--input-file",
        str(FIXTURES_DIR / "review_result_approve.json"),
    ]

    for suffix in ([], ["--issue-number", "0"], ["--issue-number", "-1"]):
        producer = subprocess.run(
            [*base_args, *suffix],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        assert producer.returncode == 2
        assert producer.stdout == ""
        assert "--issue-number" in producer.stderr
        assert not (repo_root / ".claude" / "artifacts").exists()


def test_producer_failure_envelope_uses_canonical_relative_artifact_reference(tmp_path: Path) -> None:
    """A semantic producer failure is distinct from an absolute-path violation."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    artifact_dir = repo_root / ".claude" / "artifacts" / "issue-refinement-loop"
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{", encoding="utf-8")

    producer = subprocess.run(
        [
            sys.executable,
            str(PRODUCER),
            "--input-file",
            str(malformed),
            "--artifact-dir",
            str(artifact_dir),
            "--repo-root",
            str(repo_root),
            "--issue-number",
            "1887",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    validator = subprocess.run(
        [sys.executable, str(VALIDATOR), "--issue-number", "1887"],
        input=producer.stdout,
        capture_output=True,
        text=True,
        check=False,
    )

    assert producer.returncode == 2
    assert "ARTIFACT: producer_failure_v1=.claude/artifacts/issue-refinement-loop/1887/" in producer.stdout
    assert str(repo_root) not in producer.stdout
    assert validator.returncode == 1
    payload = json.loads(validator.stdout)
    assert payload["envelope_kind"] == "producer_failure"
    assert payload["artifact_path_policy"]["status"] == "valid"
    assert all(v["code"] != "artifact_absolute_path_rejected" for v in payload["violations"])
