"""test_visual_impact_v2_manifest_seams.py (Issue #2019 / PR #2045 OWNER
fix_delta P1-4 second-round REQUEST_CHANGES)

Closes four producer->consumer seam gaps the OWNER's second-round
REQUEST_CHANGES identified as still open after the first fix_delta:

1. `--mode changed-paths` (real git rename detection) exercised through the
   actual CLI argv wiring (subprocess), not just the Python helper.
2. `parse_git_diff_name_status_z` classifying a rename correctly against a
   REAL git repository operation (`git mv` + `git diff --name-status -z
   --find-renames`), never a synthetic NUL-byte fixture.
3. A real VISUAL_BASELINE_REVIEW_EVIDENCE_V2 manifest -- built via
   `--mode build-evidence-manifest` (the actual CI producer path) -- parsed
   and verified end to end (schema, per-record `manifest_sha256` tamper
   check, and consumption by `evaluate_pr_policy`), never a synthetic
   boolean-only fixture.
4. The registry loader's `yaml.safe_load()`-based duplicate-key rejection
   exercised against a fixture file that ACTUALLY contains a duplicate key
   (not merely "the current registry happens not to have one").
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parent.parent / "resolve_visual_impact.py"
_MODULE_NAME = "resolve_visual_impact_issue_2019_v2_manifest_seams"

_spec = importlib.util.spec_from_file_location(_MODULE_NAME, _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
rvi = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = rvi
_spec.loader.exec_module(rvi)

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "agent-ops" / "resolve_visual_impact.py"
REGISTRY_PATH = REPO_ROOT / "docs" / "dev" / "visual-surfaces.yml"
SCHEMA_PATH = REPO_ROOT / "docs" / "dev" / "visual-surfaces.schema.json"


# ---------------------------------------------------------------------------
# 1 + 3. `--mode build-evidence-manifest` CLI (workflow-shaped invocation).
# ---------------------------------------------------------------------------


def test_build_evidence_manifest_cli_mode_produces_valid_v2_manifest(tmp_path: Path) -> None:
    """GIVEN the real CLI argv the `component-vrt-report` CI job uses WHEN
    invoked as a subprocess THEN it produces a VISUAL_BASELINE_REVIEW_EVIDENCE_V2
    manifest whose baseline_sha256 is genuinely computed from the on-disk
    registered baseline file (not fabricated) and whose per-record
    manifest_sha256 self-verifies."""
    surface_inputs = tmp_path / "surface_inputs.json"
    surface_inputs.write_text(
        json.dumps(
            [
                {
                    "surface_id": "combat-hud-running",
                    "head_sha": "b" * 40,
                    "workflow_run_id": 999,
                    "run_attempt": 1,
                    "actual_sha256": "a" * 64,
                    "mismatched_pixels": 0,
                    "verify_succeeded": True,
                    "update_executed": False,
                    "update_succeeded": False,
                    "expected_artifact_id": "1",
                    "actual_artifact_id": "2",
                    "diff_artifact_id": "3",
                }
            ]
        ),
        encoding="utf-8",
    )
    manifest_output = tmp_path / "manifest_v2.json"

    proc = subprocess.run(
        [
            "uv",
            "run",
            "--locked",
            "python3",
            str(SCRIPT_PATH),
            "--mode",
            "build-evidence-manifest",
            "--registry",
            str(REGISTRY_PATH),
            "--schema",
            str(SCHEMA_PATH),
            "--repo-root",
            str(REPO_ROOT),
            "--surface-inputs-file",
            str(surface_inputs),
            "--manifest-output",
            str(manifest_output),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"

    manifest = json.loads(manifest_output.read_text(encoding="utf-8"))
    assert manifest["schema"] == "VISUAL_BASELINE_REVIEW_EVIDENCE_V2"
    assert len(manifest["surfaces"]) == 1
    record = manifest["surfaces"][0]
    assert record["surface_id"] == "combat-hud-running"

    # baseline_sha256 must be the REAL sha256 of the on-disk registered
    # baseline PNG (never trusted from --surface-inputs-file, which never
    # even supplies it).
    baseline_path = REPO_ROOT / record["baseline_path"]
    expected_sha256 = rvi.sha256_hex(baseline_path.read_bytes())
    assert record["baseline_sha256"] == expected_sha256

    # Tamper-evidence digest must self-verify (production consumer function,
    # not a re-derived comparison).
    assert rvi.verify_evidence_manifest_v2_record_digest(record) is True

    # And a bit-flipped record must fail-closed (never trust an unverifiable record).
    tampered = dict(record)
    tampered["mismatched_pixels"] = 999
    assert rvi.verify_evidence_manifest_v2_record_digest(tampered) is False


def test_build_evidence_manifest_v2_consumed_end_to_end_by_evaluate_pr_policy(tmp_path: Path) -> None:
    """GIVEN a real V2 manifest produced by the CLI producer path WHEN fed
    into `evaluate_pr_policy` (the real policy-check consumer) with trusted
    CheckRun binding params THEN the surface PASSES verified_unchanged."""
    surface_inputs = tmp_path / "surface_inputs.json"
    surface_inputs.write_text(
        json.dumps(
            [
                {
                    "surface_id": "combat-hud-running",
                    "head_sha": "c" * 40,
                    "workflow_run_id": 42,
                    "run_attempt": 1,
                    "actual_sha256": "d" * 64,
                    "mismatched_pixels": 0,
                    "verify_succeeded": True,
                    "update_executed": False,
                    "update_succeeded": False,
                    "expected_artifact_id": "10",
                    "actual_artifact_id": "11",
                    "diff_artifact_id": "12",
                }
            ]
        ),
        encoding="utf-8",
    )
    manifest_output = tmp_path / "manifest_v2.json"
    proc = subprocess.run(
        [
            "uv",
            "run",
            "--locked",
            "python3",
            str(SCRIPT_PATH),
            "--mode",
            "build-evidence-manifest",
            "--registry",
            str(REGISTRY_PATH),
            "--schema",
            str(SCHEMA_PATH),
            "--repo-root",
            str(REPO_ROOT),
            "--surface-inputs-file",
            str(surface_inputs),
            "--manifest-output",
            str(manifest_output),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    manifest = json.loads(manifest_output.read_text(encoding="utf-8"))

    registry_doc = rvi.load_and_validate_registry(REGISTRY_PATH, SCHEMA_PATH, None, REPO_ROOT)
    resolve_result = rvi.ResolveResult(
        changed_paths=["src/ui/combatHud.ts"],
        affected_surfaces=[{"surface_id": "combat-hud-running", "reason": "direct_producer"}],
    )
    declaration_doc = {"surfaces": [{"surface_id": "combat-hud-running", "disposition": "verified_unchanged"}]}

    policy_result = rvi.evaluate_pr_policy(
        resolve_result=resolve_result,
        declaration_doc=declaration_doc,
        registry_doc=registry_doc,
        evidence_manifest=manifest,
        head_sha="c" * 40,
        changed_paths=["src/ui/combatHud.ts"],
        actor="squne121",
        authorized_owners=set(),
        today=date(2026, 8, 9),
        trusted_check_run_id="1",
        trusted_check_conclusion="success",
    )
    assert policy_result["ok"] is True, policy_result["failures"]


def _run_build_evidence_manifest_cli(surface_inputs_path: Path, manifest_output: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "uv",
            "run",
            "--locked",
            "python3",
            str(SCRIPT_PATH),
            "--mode",
            "build-evidence-manifest",
            "--registry",
            str(REGISTRY_PATH),
            "--schema",
            str(SCHEMA_PATH),
            "--repo-root",
            str(REPO_ROOT),
            "--surface-inputs-file",
            str(surface_inputs_path),
            "--manifest-output",
            str(manifest_output),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_build_evidence_manifest_cli_rejects_string_typed_workflow_run_id_and_run_attempt(tmp_path: Path) -> None:
    """Issue #2230 fix_delta P1-2: the real CLI path (`--mode
    build-evidence-manifest`, the actual `component-vrt-report` producer
    invocation) must reject a `surface_inputs.json` whose `workflow_run_id`/
    `run_attempt` are JSON STRINGS -- simulating the real pre-fix `ci.yml`
    heredoc's actual output shape (before its own `int()` fix) -- with a
    clear error message, never silently accepting or coercing them."""
    surface_inputs = tmp_path / "surface_inputs_string_identity.json"
    surface_inputs.write_text(
        json.dumps(
            [
                {
                    "surface_id": "combat-hud-running",
                    "head_sha": "e" * 40,
                    "workflow_run_id": "123456",
                    "run_attempt": "1",
                    "actual_sha256": "f" * 64,
                    "mismatched_pixels": 0,
                    "verify_succeeded": True,
                    "update_executed": False,
                    "update_succeeded": False,
                    "expected_artifact_id": "1",
                    "actual_artifact_id": "2",
                    "diff_artifact_id": "3",
                }
            ]
        ),
        encoding="utf-8",
    )
    manifest_output = tmp_path / "manifest_v2_rejected.json"
    proc = _run_build_evidence_manifest_cli(surface_inputs, manifest_output)
    assert proc.returncode == 1, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "workflow_run_id" in proc.stderr
    assert "run_attempt" in proc.stderr
    assert "must be a JSON integer" in proc.stderr


def test_build_evidence_manifest_cli_accepts_real_int_workflow_run_id_and_run_attempt(tmp_path: Path) -> None:
    """Companion positive case: real JSON integers (the shape `ci.yml`'s
    fixed heredoc now produces after its own `int()` conversion) must be
    accepted and threaded through into the manifest record verbatim."""
    surface_inputs = tmp_path / "surface_inputs_int_identity.json"
    surface_inputs.write_text(
        json.dumps(
            [
                {
                    "surface_id": "combat-hud-running",
                    "head_sha": "1" * 40,
                    "workflow_run_id": 123456,
                    "run_attempt": 2,
                    "actual_sha256": "2" * 64,
                    "mismatched_pixels": 0,
                    "verify_succeeded": True,
                    "update_executed": False,
                    "update_succeeded": False,
                    "expected_artifact_id": "1",
                    "actual_artifact_id": "2",
                    "diff_artifact_id": "3",
                }
            ]
        ),
        encoding="utf-8",
    )
    manifest_output = tmp_path / "manifest_v2_accepted.json"
    proc = _run_build_evidence_manifest_cli(surface_inputs, manifest_output)
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    manifest = json.loads(manifest_output.read_text(encoding="utf-8"))
    record = manifest["surfaces"][0]
    assert record["workflow_run_id"] == 123456
    assert record["run_attempt"] == 2
    assert type(record["workflow_run_id"]) is int
    assert type(record["run_attempt"]) is int


# ---------------------------------------------------------------------------
# 2. Real git repo rename detection (production producer, never synthetic).
# ---------------------------------------------------------------------------


def _init_repo_with_rename(tmp_path: Path) -> tuple[bytes, Path]:
    """Real `git init` + commit + `git mv` + `git diff --name-status -z
    --find-renames` -- returns (raw_nul_output, repo_dir)."""
    repo = tmp_path / "rename_repo"
    repo.mkdir()

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)

    run("init", "-q")
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Test")
    original = repo / "src_original.ts"
    # >50% content preserved so git's rename heuristic (`--find-renames`,
    # default similarity threshold 50%) actually detects this as a rename
    # rather than a delete+add pair.
    original.write_text("export const value = 1\n" * 20, encoding="utf-8")
    run("add", "src_original.ts")
    run("commit", "-q", "-m", "add original")
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    run("mv", "src_original.ts", "src_renamed.ts")
    renamed = repo / "src_renamed.ts"
    # Small edit alongside the rename (still >50% similar).
    renamed.write_text(renamed.read_text(encoding="utf-8") + "export const extra = 2\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-q", "-m", "rename + small edit")
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    diff = subprocess.run(
        ["git", "diff", "--name-status", "-z", "--find-renames", base_sha, head_sha],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    return diff.stdout, repo


def test_parse_git_diff_name_status_z_classifies_real_git_rename() -> None:
    """GIVEN a REAL git repository (git init/commit/git mv/git diff, not a
    hand-written NUL-byte fixture) WHEN the production
    `parse_git_diff_name_status_z` parses its real `--find-renames` output
    THEN the rename is classified with status=renamed and both
    old_path/path populated."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        raw, _ = _init_repo_with_rename(Path(tmp))

    entries, flat = rvi.parse_git_diff_name_status_z(raw)
    renamed = [e for e in entries if e["status"] == "renamed"]
    assert len(renamed) == 1, entries
    assert renamed[0]["old_path"] == "src_original.ts"
    assert renamed[0]["path"] == "src_renamed.ts"
    assert "src_original.ts" in flat
    assert "src_renamed.ts" in flat


def test_changed_paths_cli_mode_workflow_shaped_real_git_rename(tmp_path: Path) -> None:
    """GIVEN the exact `--mode changed-paths` CLI argv shape the
    `visual-impact-policy` CI job's "Compute changed paths" step uses WHEN
    invoked as a subprocess against a real git-produced NUL-delimited diff
    THEN it writes the same typed/flat outputs the CI step consumes."""
    raw, _ = _init_repo_with_rename(tmp_path)
    name_status_file = tmp_path / "changed_paths_name_status.nul"
    name_status_file.write_bytes(raw)
    typed_output = tmp_path / "changed_paths_typed.json"
    flat_output = tmp_path / "changed_paths.txt"

    proc = subprocess.run(
        [
            "uv",
            "run",
            "--locked",
            "python3",
            str(SCRIPT_PATH),
            "--mode",
            "changed-paths",
            "--name-status-file",
            str(name_status_file),
            "--typed-output",
            str(typed_output),
            "--flat-output",
            str(flat_output),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"

    entries = json.loads(typed_output.read_text(encoding="utf-8"))
    renamed = [e for e in entries if e["status"] == "renamed"]
    assert len(renamed) == 1, entries
    assert renamed[0]["old_path"] == "src_original.ts"
    assert renamed[0]["path"] == "src_renamed.ts"

    flat_text = flat_output.read_text(encoding="utf-8")
    assert "src_original.ts" in flat_text
    assert "src_renamed.ts" in flat_text


# ---------------------------------------------------------------------------
# 4. Registry duplicate-key rejection against a REAL duplicate fixture.
# ---------------------------------------------------------------------------


def test_registry_loader_rejects_duplicate_top_level_key(tmp_path: Path) -> None:
    """GIVEN a registry YAML file that ACTUALLY contains a duplicate
    top-level `surfaces:` key (not merely "today's real registry happens not
    to have one") WHEN `load_and_validate_registry` parses it via the real
    `yaml.safe_load()`-based loader THEN it raises RegistryError rather than
    silently applying "last write wins"."""
    duplicate_key_registry = tmp_path / "duplicate_key_registry.yml"
    duplicate_key_registry.write_text(
        "schema_version: 1\n"
        "surfaces:\n"
        "  combat-hud-running:\n"
        "    producers: {modules: [], styles: [], assets: [], config: []}\n"
        "    contracts: {runner: x, spec: a, baseline: b, job: c}\n"
        "surfaces:\n"
        "  combat-hud-running:\n"
        "    producers: {modules: [], styles: [], assets: [], config: []}\n"
        "    contracts: {runner: y, spec: d, baseline: e, job: f}\n",
        encoding="utf-8",
    )
    with pytest.raises(rvi.RegistryError, match="duplicate key"):
        rvi.load_and_validate_registry(duplicate_key_registry, SCHEMA_PATH, None, tmp_path)


def test_registry_loader_rejects_duplicate_nested_surface_id_key(tmp_path: Path) -> None:
    """Same as above but the duplicate key is nested inside `surfaces:`
    (duplicate surface_id mapping key), which is the more realistic
    collision shape for this registry's structure."""
    duplicate_key_registry = tmp_path / "duplicate_nested_key_registry.yml"
    duplicate_key_registry.write_text(
        "schema_version: 1\n"
        "surfaces:\n"
        "  combat-hud-running:\n"
        "    producers: {modules: [], styles: [], assets: [], config: []}\n"
        "    contracts: {runner: x, spec: a, baseline: b, job: c}\n"
        "  combat-hud-running:\n"
        "    producers: {modules: [], styles: [], assets: [], config: []}\n"
        "    contracts: {runner: y, spec: d, baseline: e, job: f}\n",
        encoding="utf-8",
    )
    with pytest.raises(rvi.RegistryError, match="duplicate key"):
        rvi.load_and_validate_registry(duplicate_key_registry, SCHEMA_PATH, None, tmp_path)
