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


def test_outside_or_symlink_escape_fails_closed_without_path_disclosure(tmp_path: Path) -> None:
    """Only the canonical resolved subtree is writable, even through a symlink."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    canonical_base = repo_root / ".claude" / "artifacts" / "issue-refinement-loop"
    canonical_base.mkdir(parents=True)
    (canonical_base / "1887").symlink_to(outside, target_is_directory=True)

    producer, _validator = _produce_then_validate(
        repo_root=repo_root,
        artifact_dir=canonical_base,
    )
    assert producer.returncode == 2
    assert "STATUS: failed" in producer.stdout
    assert str(repo_root) not in producer.stdout
    assert str(outside) not in producer.stdout

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
