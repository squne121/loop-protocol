"""
tests/ci/test_visual_artifact_pipeline_e2e_core_ownership.py

Issue #2119 AC13: visual artifact producer authority is `e2e-core`, and
`scripts/check-visual-artifact-pipeline.py` follows that move — the
aggregate `e2e` job must not carry an upload-artifact/download-artifact
compatibility shim impersonating the pre-#2119 `jobs["e2e"]` contract.
"""
from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys
import types

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
VALIDATOR = REPO_ROOT / "scripts" / "check-visual-artifact-pipeline.py"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _load_validator_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("check_visual_artifact_pipeline", VALIDATOR)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_visual_artifact_producer_authority_is_e2e_core_with_no_aggregate_reupload_shim():
    mod = _load_validator_module()
    assert mod.VISUAL_ARTIFACT_PRODUCER_JOB == "e2e-core"

    # 1) The validator run against the REAL, current workflow must pass
    #    (producer authority correctly resolved to e2e-core).
    proc = subprocess.run(
        [sys.executable, str(VALIDATOR), str(WORKFLOW_PATH)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, (
        f"check-visual-artifact-pipeline.py must pass against jobs.e2e-core "
        f"(exit {proc.returncode}): stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert "status: pass" in proc.stdout

    # 2) Adversarial control: a mutated workflow where the AGGREGATE `e2e`
    #    job carries a re-upload shim must be REJECTED (proves the guard
    #    added in this Issue actually fires, not just that the real
    #    workflow happens not to trip it).
    doc = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    doc["jobs"]["e2e"].setdefault("steps", []).append(
        {
            "name": "shim: re-upload e2e-core artifacts under jobs.e2e",
            "uses": "actions/upload-artifact@v6",
            "with": {"name": "playwright-report", "path": "playwright-report/"},
        }
    )
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as fh:
        yaml.safe_dump(doc, fh, sort_keys=False)
        mutated_file = fh.name

    try:
        adversarial = subprocess.run(
            [sys.executable, str(VALIDATOR), mutated_file],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert adversarial.returncode != 0, (
            "check-visual-artifact-pipeline.py must reject an aggregate jobs.e2e "
            "carrying an upload-artifact re-upload shim"
        )
        assert "jobs.e2e" in adversarial.stdout
    finally:
        pathlib.Path(mutated_file).unlink(missing_ok=True)
