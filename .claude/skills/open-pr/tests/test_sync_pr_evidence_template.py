from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _run(cmd: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        capture_output=True,
        check=False,
    )


def _write_template(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_sync_pr_evidence_template_check_pass(tmp_path):
    repo_root = Path(__file__).resolve().parents[4]
    script = repo_root / "scripts" / "sync-pr-evidence-template.py"
    canonical = tmp_path / "canonical.md"
    mirror = tmp_path / "mirror.md"

    _write_template(
        canonical,
        "# Canonical\n## Linked Issue\n\n## Summary\n\n"
        "## Acceptance Criteria -> Evidence\n\n## Commands Run\n\n"
        "## Changed Paths\n\n",
    )
    _write_template(
        mirror,
        "# Mirror\n\n## 使うタイミング\n\n## Linked Issue\n\n## Summary\n\n"
        "## Acceptance Criteria -> Evidence\n\n## Commands Run\n\n## Changed Paths\n",
    )

    result = _run(
        [sys.executable, str(script), "--canonical", str(canonical), "--mirror", str(mirror), "--check"],
        cwd=repo_root,
    )

    assert result.returncode == 0
    assert "OK: PR evidence mirror is synchronized" in result.stdout


def test_sync_pr_evidence_template_check_detects_drift(tmp_path):
    repo_root = Path(__file__).resolve().parents[4]
    script = repo_root / "scripts" / "sync-pr-evidence-template.py"
    canonical = tmp_path / "canonical.md"
    mirror = tmp_path / "mirror.md"

    _write_template(
        canonical,
        "## Linked Issue\n## Summary\n## Acceptance Criteria -> Evidence\n",
    )
    _write_template(
        mirror,
        "## Linked Issue\n## Acceptance Criteria -> Evidence\n## Summary\n",
    )

    result = _run(
        [sys.executable, str(script), "--canonical", str(canonical), "--mirror", str(mirror), "--check"],
        cwd=repo_root,
    )

    assert result.returncode == 1
    assert "ERROR: PR evidence mirror drift detected." in result.stderr
    assert "canonical-sync-sections:" in result.stderr
    assert "mirror-sync-sections:" in result.stderr


def test_sync_pr_evidence_template_write_reorders_sections_and_inserts_default(tmp_path):
    repo_root = Path(__file__).resolve().parents[4]
    script = repo_root / "scripts" / "sync-pr-evidence-template.py"
    canonical = tmp_path / "canonical.md"
    mirror = tmp_path / "mirror.md"

    _write_template(
        canonical,
        "# Canonical\n## Linked Issue\n\n## Summary\n\n## Acceptance Criteria -> Evidence\n\n",
    )
    _write_template(
        mirror,
        "# Mirror\n\n## Linked Issue\n\n## Summary\n\n",
    )

    result = _run(
        [sys.executable, str(script), "--canonical", str(canonical), "--mirror", str(mirror), "--write"],
        cwd=repo_root,
    )

    assert result.returncode == 0
    body = mirror.read_text(encoding="utf-8")
    assert "## Linked Issue" in body
    assert "## Summary" in body
    assert "## Acceptance Criteria -> Evidence" in body
    assert "- 未記入" in body
