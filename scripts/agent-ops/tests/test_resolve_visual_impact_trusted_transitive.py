"""Issue #2099: trusted-side TS import graph transitive reachability
(git-object-backed, read-only static import resolver) and git plumbing
failure-taxonomy separation.

Tests `resolve_trusted_transitive_graph()` / `resolve_trusted_minimum()`
(with `candidate_head_ref` set) / `read_git_blob_at_ref()` in
`resolve_visual_impact.py`. Every git-object-backed test spins up a REAL,
isolated (tmp_path-scoped) git repository and fetches/reads REAL commit
objects from it -- never mocked git plumbing -- so a genuine `git`
subprocess is exercised end to end, matching this Issue's Runtime
Verification Applicability (`decision: immediate`).
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parent.parent / "resolve_visual_impact.py"
_MODULE_NAME = "resolve_visual_impact_issue_2099_trusted_transitive"

_spec = importlib.util.spec_from_file_location(_MODULE_NAME, _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
rvi = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = rvi
_spec.loader.exec_module(rvi)

REPO_ROOT = Path(__file__).resolve().parents[3]
VISUAL_IMPACT_SCHEMA_PATH = REPO_ROOT / "docs" / "dev" / "visual-impact.schema.json"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "visual-impact-trusted-consumer.yml"

EXPECTED_REPOSITORY = "squne121/loop-protocol"
EXPECTED_PR_NUMBER = 2099
SURFACE_ID = "combat-hud-running"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "candidate_repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    return repo


def _commit_files(repo: Path, files: dict[str, str]) -> str:
    for rel_path, content in files.items():
        target = repo / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "test commit")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _registry_doc(*, producer_paths: list[str], coverage_roots: list[str] | None = None) -> dict:
    return {
        "surfaces": {SURFACE_ID: {"producers": {"modules": producer_paths}}},
        "coverage_roots": coverage_roots or [],
    }


def _decision(*, head_sha: str, changed_path_entries: list[dict], affected_surfaces: list[dict]) -> dict:
    return rvi.build_decision(
        repository=EXPECTED_REPOSITORY,
        pull_request_number=EXPECTED_PR_NUMBER,
        base_sha="a" * 40,
        head_sha=head_sha,
        base_registry_blob_sha="c" * 40,
        head_registry_blob_sha="d" * 40,
        pr_body="dummy",
        changed_path_entries=changed_path_entries,
        affected_surfaces=affected_surfaces,
        component_vrt_report_check_run_id=None,
        github_actions_app_identity="github-actions[bot]",
        artifact_id=None,
        artifact_digest=None,
    )


def _verify(
    *,
    head_sha: str,
    decision: dict,
    trusted_rederivation: "rvi.TrustedRederivation",
) -> "rvi.TrustedArtifactVerdict":
    decision_raw = json.dumps(decision).encode("utf-8")
    return rvi.verify_trusted_artifact(
        decision_raw=decision_raw,
        evidence_manifest_raw=None,
        visual_impact_schema_path=VISUAL_IMPACT_SCHEMA_PATH,
        expected_head_sha=head_sha,
        expected_repository=EXPECTED_REPOSITORY,
        expected_pr_number=EXPECTED_PR_NUMBER,
        trusted_rederivation=trusted_rederivation,
    )


# --- AC1: end-to-end adversarial (final verdict, not just the graph walk) --


def test_end_to_end_adversarial_forged_empty_affected_surfaces_rejected(git_repo: Path) -> None:
    """AC1: `src/helper.ts` is reachable from the registered entry module
    `src/entry.ts` ONLY via a TS static import (never listed directly as a
    producer, never under any coverage_root). A producer decision that
    self-reports `affected_surfaces: []` for a PR that changes only
    `src/helper.ts` must be rejected by `verify_trusted_artifact()`'s FINAL
    verdict end to end."""
    head_sha = _commit_files(
        git_repo,
        {
            "src/entry.ts": "import { helper } from './helper'\nexport const x = helper()\n",
            "src/helper.ts": "export function helper() { return 1 }\n",
        },
    )
    registry_doc = _registry_doc(producer_paths=["src/entry.ts"])
    changed_entries = [{"status": "modified", "path": "src/helper.ts"}]
    decision = _decision(head_sha=head_sha, changed_path_entries=changed_entries, affected_surfaces=[])
    trusted = rvi.TrustedRederivation(
        changed_path_entries=changed_entries,
        base_registry_doc=registry_doc,
        head_registry_doc=registry_doc,
        candidate_head_ref=head_sha,
        repo_root=git_repo,
    )
    verdict = _verify(head_sha=head_sha, decision=decision, trusted_rederivation=trusted)
    assert verdict.ok is False
    assert any(code.startswith("affected_surfaces_undercount") for code in verdict.reason_codes)


def test_end_to_end_adversarial_genuine_no_impact_passes(git_repo: Path) -> None:
    """AC1 negative-of-the-negative: a genuinely no-visual-impact change
    (touches a file NOT reachable from any registered entry point) must not
    be falsely rejected by the transitive check."""
    head_sha = _commit_files(
        git_repo,
        {
            "src/entry.ts": "import { helper } from './helper'\nexport const x = helper()\n",
            "src/helper.ts": "export function helper() { return 1 }\n",
            "src/unrelated.ts": "export const y = 1\n",
        },
    )
    registry_doc = _registry_doc(producer_paths=["src/entry.ts"])
    changed_entries = [{"status": "modified", "path": "src/unrelated.ts"}]
    decision = _decision(head_sha=head_sha, changed_path_entries=changed_entries, affected_surfaces=[])
    trusted = rvi.TrustedRederivation(
        changed_path_entries=changed_entries,
        base_registry_doc=registry_doc,
        head_registry_doc=registry_doc,
        candidate_head_ref=head_sha,
        repo_root=git_repo,
    )
    verdict = _verify(head_sha=head_sha, decision=decision, trusted_rederivation=trusted)
    assert verdict.ok is True
    assert verdict.reason_codes == []


# --- AC2/AC5: confined resolver + transitive-only detection fixtures ------


def test_confined_resolver_transitive_css_and_asset_reachability(git_repo: Path) -> None:
    """AC2: a single entry point reaches a CSS file transitively (via a
    module-level side-effect import) and that CSS file's `url()` asset --
    both path-ingress kinds must be walked through the same confined
    resolver."""
    head_sha = _commit_files(
        git_repo,
        {
            "src/entry.ts": "import './styles/theme.css'\n",
            "src/styles/theme.css": "body { background: url('./bg.png'); }\n",
            "src/styles/bg.png": "binarydata",
        },
    )
    entries, errors = rvi.git_ls_tree(git_repo, head_sha, max_entries=10_000)
    assert errors == []
    reachable, unknown = rvi.resolve_trusted_transitive_graph(["src/entry.ts"], entries, git_repo)
    assert "src/styles/theme.css" in reachable
    assert "src/styles/bg.png" in reachable
    assert unknown == []


def test_transitive_only_change_detected_never_direct_producer(git_repo: Path) -> None:
    """AC5: `src/deep.ts` is reachable only via a TWO-hop transitive import
    chain (entry -> mid -> deep), never listed as a direct producer/contract
    path and never under coverage_roots -- `resolve_trusted_minimum()` must
    still mark the surface affected via `producer_reachable_transitive`."""
    head_sha = _commit_files(
        git_repo,
        {
            "src/entry.ts": "import { mid } from './mid'\nexport const x = mid()\n",
            "src/mid.ts": "import { deep } from './deep'\nexport function mid() { return deep() }\n",
            "src/deep.ts": "export function deep() { return 42 }\n",
        },
    )
    registry_doc = _registry_doc(producer_paths=["src/entry.ts"])
    affected, unmapped = rvi.resolve_trusted_minimum(
        ["src/deep.ts"], registry_doc, registry_doc, candidate_head_ref=head_sha, repo_root=git_repo
    )
    assert affected.get(SURFACE_ID) == "producer_reachable_transitive"
    assert unmapped == []


# --- AC3: negative controls -- absolute / .. / symlink / leading-`/` ------


def test_symlink_escape_rejected(git_repo: Path) -> None:
    """AC3: a tracked symlink entry (git mode 120000) whose target text
    points outside the repo root must never be silently followed -- it is
    rejected as `symlink_entry_rejected`, and the walk never reaches
    whatever it points at."""
    entry = git_repo / "src" / "entry.ts"
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text("import './escape_link'\n", encoding="utf-8")
    link = git_repo / "src" / "escape_link.ts"
    link.symlink_to("/etc/passwd")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-q", "-m", "symlink commit")
    head_sha = _git(git_repo, "rev-parse", "HEAD").stdout.strip()

    entries, errors = rvi.git_ls_tree(git_repo, head_sha, max_entries=10_000)
    assert errors == []
    assert entries["src/escape_link.ts"].mode == "120000"
    reachable, unknown = rvi.resolve_trusted_transitive_graph(["src/entry.ts"], entries, git_repo)
    assert "src/escape_link.ts" not in reachable
    assert any(u["kind"] == "symlink_entry_rejected" for u in unknown)


def test_absolute_specifier_rejected(git_repo: Path) -> None:
    """AC3: a `/`-leading specifier is rejected outright -- unlike
    `resolve_visual_impact.mjs`'s `isRelativeSpecifier()`, it is NEVER
    treated as relative and resolved via a path-join (the exact confinement
    bug flagged in this Issue's Current Validated Scope)."""
    head_sha = _commit_files(
        git_repo,
        {"src/entry.ts": "import x from '/etc/passwd'\n"},
    )
    entries, _errors = rvi.git_ls_tree(git_repo, head_sha, max_entries=10_000)
    reachable, unknown = rvi.resolve_trusted_transitive_graph(["src/entry.ts"], entries, git_repo)
    assert reachable == {"src/entry.ts"}
    assert any(u["kind"] == "absolute_specifier_rejected" for u in unknown)


def test_dot_dot_escape_rejected(git_repo: Path) -> None:
    """AC3: a `../../` specifier that would normalize outside the repo
    root is rejected -- never silently resolved to a path outside the
    confined tree."""
    head_sha = _commit_files(
        git_repo,
        {"src/nested/entry.ts": "import x from '../../../../outside'\n"},
    )
    entries, _errors = rvi.git_ls_tree(git_repo, head_sha, max_entries=10_000)
    reachable, unknown = rvi.resolve_trusted_transitive_graph(["src/nested/entry.ts"], entries, git_repo)
    assert reachable == {"src/nested/entry.ts"}
    assert any(u["kind"] == "path_escape_rejected" for u in unknown)


# --- AC4: resource bounds (real subprocess / real wall-clock) -------------


def test_resource_bound_max_graph_depth_exceeded(git_repo: Path) -> None:
    """AC4: a deliberately long linear import chain exceeding
    `TRUSTED_TRANSITIVE_MAX_GRAPH_DEPTH` raises
    `TrustedTransitiveResourceBoundExceeded` -- never a silently-truncated
    partial reachable set."""
    depth = rvi.TRUSTED_TRANSITIVE_MAX_GRAPH_DEPTH + 5
    files: dict[str, str] = {}
    for i in range(depth):
        nxt = f"./f{i + 1}" if i + 1 < depth else "./fend"
        files[f"src/chain/f{i}.ts"] = f"import x from '{nxt}'\n"
    files["src/chain/fend.ts"] = "export const end = 1\n"
    head_sha = _commit_files(git_repo, files)
    entries, _errors = rvi.git_ls_tree(git_repo, head_sha, max_entries=10_000)
    with pytest.raises(rvi.TrustedTransitiveResourceBoundExceeded):
        rvi.resolve_trusted_transitive_graph(["src/chain/f0.ts"], entries, git_repo)


def test_resource_bound_per_file_bytes_exceeded(git_repo: Path) -> None:
    """AC4: a single file exceeding `TRUSTED_TRANSITIVE_MAX_FILE_BYTES`
    raises fail-closed rather than being silently truncated/skipped."""
    huge = "export const big = 1\n" + ("x" * (rvi.TRUSTED_TRANSITIVE_MAX_FILE_BYTES + 1))
    head_sha = _commit_files(git_repo, {"src/entry.ts": "import './huge'\n", "src/huge.ts": huge})
    entries, _errors = rvi.git_ls_tree(git_repo, head_sha, max_entries=10_000)
    with pytest.raises(rvi.TrustedTransitiveResourceBoundExceeded):
        rvi.resolve_trusted_transitive_graph(["src/entry.ts"], entries, git_repo)


def test_resource_bound_wall_time_exceeded(git_repo: Path) -> None:
    """AC4: an (artificially tiny) wall-clock budget is genuinely enforced
    via real `time.monotonic()` elapsed time, not a mocked clock."""
    head_sha = _commit_files(
        git_repo,
        {
            "src/entry.ts": "import { mid } from './mid'\nexport const x = mid()\n",
            "src/mid.ts": "export function mid() { return 1 }\n",
        },
    )
    entries, _errors = rvi.git_ls_tree(git_repo, head_sha, max_entries=10_000)
    walker = rvi.TrustedGraphWalker(entries, git_repo, deadline=time.monotonic() - 1.0)
    with pytest.raises(rvi.TrustedTransitiveResourceBoundExceeded):
        walker.visit("src/entry.ts", 0)


def test_resource_bound_file_count_exceeded_at_ls_tree(git_repo: Path) -> None:
    """AC4: the file-count bound is enforced at `git ls-tree` enumeration
    time (before any blob content is even read)."""
    head_sha = _commit_files(git_repo, {"src/entry.ts": "export const x = 1\n"})
    entries, errors = rvi.git_ls_tree(git_repo, head_sha, max_entries=0)
    assert entries == {}
    assert errors == ["resource_bound_exceeded:file_count"]


def test_resource_bound_exceeded_fails_closed_in_verify_trusted_artifact(git_repo: Path) -> None:
    """AC4 integration: a resource-bound violation while walking the
    candidate head's graph propagates as an unconditional `ok=False`
    verdict from `verify_trusted_artifact()`, never a silent skip."""
    huge = "export const big = 1\n" + ("x" * (rvi.TRUSTED_TRANSITIVE_MAX_FILE_BYTES + 1))
    head_sha = _commit_files(git_repo, {"src/entry.ts": "import './huge'\n", "src/huge.ts": huge})
    registry_doc = _registry_doc(producer_paths=["src/entry.ts"])
    changed_entries = [{"status": "modified", "path": "docs/README.md"}]
    decision = _decision(head_sha=head_sha, changed_path_entries=changed_entries, affected_surfaces=[])
    trusted = rvi.TrustedRederivation(
        changed_path_entries=changed_entries,
        base_registry_doc=registry_doc,
        head_registry_doc=registry_doc,
        candidate_head_ref=head_sha,
        repo_root=git_repo,
    )
    verdict = _verify(head_sha=head_sha, decision=decision, trusted_rederivation=trusted)
    assert verdict.ok is False
    assert any(code.startswith("trusted_transitive_resource_bound_exceeded") for code in verdict.reason_codes)


# --- AC7: git plumbing failure taxonomy (missing path vs. plumbing error) -


def test_git_read_missing_path_is_normal(git_repo: Path) -> None:
    """AC7: a path that genuinely does not exist at `ref` is `MISSING`
    (normal case) -- never conflated with a plumbing failure."""
    head_sha = _commit_files(git_repo, {"src/entry.ts": "export const x = 1\n"})
    outcome, content, _message = rvi.read_git_blob_at_ref(git_repo, head_sha, "does/not/exist.yml")
    assert outcome == rvi.GitReadOutcome.MISSING
    assert content is None


def test_git_plumbing_error_unfetched_commit_object(git_repo: Path) -> None:
    """AC7: a `ref` that was never fetched/committed at all (an unresolvable
    commit-ish, e.g. a shallow-fetch gap) is a genuine `PLUMBING_ERROR` --
    distinct from a normal missing path -- and must never be silently
    treated the same as "file doesn't exist"."""
    _commit_files(git_repo, {"src/entry.ts": "export const x = 1\n"})
    bogus_ref = "0" * 40
    outcome, content, message = rvi.read_git_blob_at_ref(git_repo, bogus_ref, "src/entry.ts")
    assert outcome == rvi.GitReadOutcome.PLUMBING_ERROR
    assert content is None
    assert message != ""


def test_git_read_ok_for_existing_path(git_repo: Path) -> None:
    head_sha = _commit_files(git_repo, {"docs/dev/visual-surfaces.yml": "surfaces: {}\n"})
    outcome, content, _message = rvi.read_git_blob_at_ref(git_repo, head_sha, "docs/dev/visual-surfaces.yml")
    assert outcome == rvi.GitReadOutcome.OK
    assert content == b"surfaces: {}\n"


def test_resolve_trusted_registry_blob_cli_missing_is_exit_zero(git_repo: Path, tmp_path: Path) -> None:
    """AC7 CLI surface: `--mode resolve-trusted-registry-blob` exits 0 (and
    removes any stale output) for the normal "file missing at this ref"
    case."""
    head_sha = _commit_files(git_repo, {"src/entry.ts": "export const x = 1\n"})
    out_file = tmp_path / "out.yml"
    out_file.write_text("stale", encoding="utf-8")
    argv = [
        "--mode",
        "resolve-trusted-registry-blob",
        "--ref",
        head_sha,
        "--path",
        "docs/dev/visual-surfaces.yml",
        "--output-file",
        str(out_file),
    ]
    exit_code = _run_main_in_repo(git_repo, argv)
    assert exit_code == 0
    assert not out_file.exists()


def test_resolve_trusted_registry_blob_cli_plumbing_error_is_exit_one(git_repo: Path, tmp_path: Path) -> None:
    """AC7 CLI surface: an unresolvable `--ref` is a non-zero exit --
    fail-closed, never silently treated as "missing file"."""
    _commit_files(git_repo, {"src/entry.ts": "export const x = 1\n"})
    out_file = tmp_path / "out.yml"
    argv = [
        "--mode",
        "resolve-trusted-registry-blob",
        "--ref",
        "0" * 40,
        "--path",
        "docs/dev/visual-surfaces.yml",
        "--output-file",
        str(out_file),
    ]
    exit_code = _run_main_in_repo(git_repo, argv)
    assert exit_code == 1
    assert not out_file.exists()


def _run_main_in_repo(repo_root: Path, argv: list[str]) -> int:
    """Invoke `rvi.main()` with `REPO_ROOT` monkeypatched to `repo_root`
    (module-level constant used by `_run_resolve_trusted_registry_blob`)."""
    original_repo_root = rvi.REPO_ROOT
    rvi.REPO_ROOT = repo_root
    try:
        return rvi.main(argv)
    finally:
        rvi.REPO_ROOT = original_repo_root


# --- AC8: TypeScript dependency bootstrap policy ---------------------------


def test_typescript_bootstrap_policy_avoids_node_dependency_in_trusted_workflow() -> None:
    """AC8: this Issue's trusted-side transitive resolver is implemented as
    a standalone regex-based static-import walker directly in
    `resolve_visual_impact.py` (git-object-backed, `git`/`python3`/`uv`
    only) -- it never brings the `typescript` npm package (or any Node
    dependency) into the trusted-consumer workflow, so the existing
    pnpm/npm/npx-forbidding regression
    (`test_visual_impact_trusted_consumer.py::test_no_pr_head_execution_no_package_scripts`)
    remains satisfied without modification."""
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    for fragment in ("pnpm ", "npm ", "npx "):
        assert fragment not in text, f"trusted workflow must never invoke {fragment!r} (AC8)"
    assert "resolve_visual_impact.py" in text
