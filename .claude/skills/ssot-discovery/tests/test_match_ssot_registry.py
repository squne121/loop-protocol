"""
AC13: ssot-discovery/tests/test_match_ssot_registry.py

Tests for match_ssot.py against the actual ssot-registry.md.
Run with:
  uv run pytest .claude/skills/ssot-discovery/tests/test_match_ssot_registry.py -v
or:
  python3 -m pytest .claude/skills/ssot-discovery/tests/test_match_ssot_registry.py -v
"""
import subprocess
import sys
import yaml
import pytest
from pathlib import Path

# Locate repo root (go up from this test file's location)
SKILLS_DIR = Path(__file__).parent.parent
SCRIPTS_DIR = SKILLS_DIR / "scripts"
MATCH_PY = SCRIPTS_DIR / "match_ssot.py"

# Add scripts dir to sys.path so we can import parse_registry directly
sys.path.insert(0, str(SCRIPTS_DIR))
from match_ssot import parse_registry, get_repo_root


@pytest.fixture(scope="session")
def repo_root():
    return get_repo_root()


@pytest.fixture(scope="session")
def registry_data(repo_root):
    registry_path = repo_root / "docs" / "dev" / "ssot-registry.md"
    assert registry_path.exists(), f"ssot-registry.md not found at {registry_path}"
    entries, directory_mappings, _warnings = parse_registry(registry_path)
    return entries, directory_mappings


def run_match(args):
    """Run match_ssot.py with given args, return parsed YAML result."""
    result = subprocess.run(
        [sys.executable, str(MATCH_PY)] + args,
        capture_output=True, text=True
    )
    assert result.returncode in (0, 1), f"match_ssot.py failed with rc={result.returncode}\nstderr: {result.stderr}\nstdout: {result.stdout}"
    parsed = yaml.safe_load(result.stdout)
    assert parsed is not None, f"Empty output from match_ssot.py. stdout: {result.stdout!r}"
    assert "SSOT_DISCOVERY_RESULT_V1" in parsed, f"Missing SSOT_DISCOVERY_RESULT_V1 key. stdout: {result.stdout!r}"
    return parsed["SSOT_DISCOVERY_RESULT_V1"]


# ---- Test 1: All registry entries are discoverable by their first keyword ----

def test_all_entries_discoverable_by_keyword(registry_data):
    entries, _ = registry_data
    assert len(entries) > 0, "No entries parsed from ssot-registry.md"
    for entry in entries:
        path = entry.get("path", "")
        keywords = entry.get("keywords", [])
        if not keywords:
            pytest.skip(f"Entry {entry.get('id', '?')} has no keywords")
        first_kw = keywords[0]
        r = run_match(["--keywords", first_kw])
        matched_paths = [d["path"] for d in r.get("matched_documents", [])]
        assert path in matched_paths, (
            f"Entry '{entry.get('id')}' path='{path}' not found in matched_documents "
            f"when searching keyword='{first_kw}'. Got: {matched_paths}"
        )


# ---- Test 2: All directory_mappings return all ssots ----

def test_all_directory_mappings_return_ssots(registry_data):
    _, directory_mappings = registry_data
    assert len(directory_mappings) > 0, "No directory_mappings parsed from ssot-registry.md"
    for dm in directory_mappings:
        pattern = dm.get("pattern", "")
        ssots = dm.get("ssots", [])
        assert len(ssots) > 0, f"directory_mapping pattern='{pattern}' has no ssots"
        # Build a test path from the pattern (strip **)
        test_path = pattern.rstrip("/**").rstrip("/") + "/test_file.ts"
        r = run_match(["--paths", test_path])
        matched_paths = [d["path"] for d in r.get("matched_documents", [])]
        for ssot in ssots:
            assert ssot in matched_paths, (
                f"pattern='{pattern}' expected ssot='{ssot}' in matched_documents, "
                f"got: {matched_paths}"
            )


# ---- Test 3: Unknown path returns status != ok ----

def test_unknown_path_returns_non_ok():
    r = run_match(["--paths", "unknown/nonexistent/path.ts"])
    status = r.get("status")
    assert status != "ok", f"Expected status != 'ok' for unknown path, got '{status}'"
    unmatched = r.get("unmatched_paths", [])
    assert "unknown/nonexistent/path.ts" in unmatched, (
        f"Expected 'unknown/nonexistent/path.ts' in unmatched_paths, got: {unmatched}"
    )


# ---- Test 4: Special character keywords produce valid YAML ----

def test_special_chars_keyword_yaml_safe():
    # These keywords contain YAML-unsafe characters
    r = run_match(["--keywords", 'foo"bar,test[0]'])
    # If we get here without exception, the output was valid YAML
    assert "status" in r, "Missing status field in result"


# ---- Test 5: .claude/skills/** returns multiple SSOTs (Blocker 1 regression) ----

def test_claude_skills_returns_multiple_ssots():
    r = run_match(["--paths", ".claude/skills/some-skill/foo.md"])
    matched_paths = [d["path"] for d in r.get("matched_documents", [])]
    assert "docs/dev/agent-skill-boundaries.md" in matched_paths, (
        f"docs/dev/agent-skill-boundaries.md missing from matched_documents. Got: {matched_paths}"
    )
    assert "docs/dev/workflow.md" in matched_paths, (
        f"docs/dev/workflow.md missing from matched_documents. Got: {matched_paths}"
    )


# ---- Test 6: Output contract fields are present ----

def test_output_contract_fields():
    r = run_match(["--keywords", "workflow"])
    required_fields = [
        "status", "generated_at", "generated_by",
        "inputs", "matched_documents", "unmatched_keywords",
        "unmatched_paths", "notes", "warnings", "errors",
    ]
    for field in required_fields:
        assert field in r, f"Missing required field '{field}' in SSOT_DISCOVERY_RESULT_V1"
    assert isinstance(r["inputs"]["task_keywords"], list)
    assert isinstance(r["inputs"]["target_paths"], list)


# ---- Test 7: expected registry ids are parsed ----

def test_expected_registry_ids_are_parsed(repo_root):
    entries, _ , _ = parse_registry(repo_root / "docs/dev/ssot-registry.md")
    ids = {e["id"] for e in entries}
    expected = {
        "workflow",
        "agent-skill-boundaries",
        "github-ops",
        "milestone-ops",
        "directory-structure",
        "current-focus",
        "runtime-verification-policy",
        "adr-0001-architecture-baseline",
        "adr-0002-sdd-tool-adoption",
        "game-overview",
        "requirements",
    }
    missing = expected - ids
    assert not missing, f"registry entries missing from parse: {missing}"


# ---- Test 8: ssot-catalog.md is deleted (AC1 regression) ----

def test_catalog_file_removed(repo_root):
    catalog = repo_root / ".claude" / "skills" / "ssot-discovery" / "references" / "ssot-catalog.md"
    assert not catalog.exists(), (
        f"ssot-catalog.md should be deleted but found at {catalog}. "
        "This file has been superseded by docs/dev/ssot-registry.md as the sole registry."
    )


# ---- Test 9: runtime,verification keywords return runtime-verification-policy.md (AC3 regression) ----

def test_runtime_verification_keyword_returns_policy():
    r = run_match(["--keywords", "runtime,verification"])
    matched_paths = [d["path"] for d in r.get("matched_documents", [])]
    assert "docs/dev/runtime-verification-policy.md" in matched_paths, (
        f"docs/dev/runtime-verification-policy.md not found when searching 'runtime,verification'. "
        f"Got: {matched_paths}"
    )


# ---- Test 10: directory mapping does not match sibling prefix (regression) ----

def test_directory_mapping_does_not_match_sibling_prefix():
    """src/state/** pattern should not match src/stateful/foo.ts."""
    r = run_match(["--paths", "src/stateful/foo.ts"])
    matched_paths = [d["path"] for d in r.get("matched_documents", [])]
    # src/stateful/ is not in directory_mappings, so it should be unmatched
    unmatched = r.get("unmatched_paths", [])
    assert "src/stateful/foo.ts" in unmatched, (
        f"src/stateful/foo.ts should be in unmatched_paths (no mapping). Got matched: {matched_paths}"
    )
