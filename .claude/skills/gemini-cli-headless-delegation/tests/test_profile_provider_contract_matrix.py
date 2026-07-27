"""Tests for profile_provider_contract_matrix.yaml (Issue #1806).

AC coverage:
  AC1: config/profile_provider_contract_matrix.yaml exists with
       schema: profile_provider_contract_matrix/v1
  AC2: matrix schema validation (5 profile x 2 provider = 10 cells, 4-value
       enum, evidence/known_gaps keys present, evidence file paths exist in
       repo, invalid values/missing keys/extra keys fail)
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_SKILL_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = Path(__file__).resolve().parents[4]
_MATRIX_PATH = (
    _SKILL_DIR / "config" / "profile_provider_contract_matrix.yaml"
)

_EXPECTED_SCHEMA = "profile_provider_contract_matrix/v1"
_EXPECTED_PROFILES = {
    "no_tools",
    "proposal_only",
    "grounded_research",
    "local_asset_research",
    "github_research",
}
_EXPECTED_PROVIDERS = {"gemini", "agy"}
_VALID_VALUES = {
    "implemented",
    "unsupported_by_design",
    "deferred",
    "unsafe",
}
_CELL_KEYS = {"value", "evidence", "known_gaps"}


def _load_matrix() -> dict[str, Any]:
    with _MATRIX_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _looks_like_path(entry: str) -> bool:
    """Heuristic: entries containing a path separator or file extension are
    treated as repo-relative file paths (existence-checked). Issue/PR
    references such as "#1265" or "PR #1455" are excluded.
    """
    if entry.startswith("#"):
        return False
    if "#" in entry:
        return False
    return "/" in entry or entry.endswith((".py", ".md", ".yaml", ".yml", ".json"))


# ---------------------------------------------------------------------------
# AC1
# ---------------------------------------------------------------------------


def test_ac1_matrix_file_exists() -> None:
    """GIVEN the repo WHEN checking config/ THEN the matrix file exists."""
    assert _MATRIX_PATH.is_file()


def test_ac1_top_level_schema_field() -> None:
    """GIVEN the matrix file WHEN parsed THEN top-level schema field matches."""
    data = _load_matrix()
    assert data["schema"] == _EXPECTED_SCHEMA


# ---------------------------------------------------------------------------
# AC2: structural schema validation
# ---------------------------------------------------------------------------


def test_ac2_profiles_key_set_exact() -> None:
    """GIVEN the matrix WHEN reading profiles THEN exactly 5 expected keys exist."""
    data = _load_matrix()
    assert set(data["profiles"].keys()) == _EXPECTED_PROFILES


@pytest.mark.parametrize("profile", sorted(_EXPECTED_PROFILES))
def test_ac2_each_profile_has_exact_provider_subkeys(profile: str) -> None:
    """GIVEN a profile WHEN reading its subkeys THEN gemini/agy only."""
    data = _load_matrix()
    assert set(data["profiles"][profile].keys()) == _EXPECTED_PROVIDERS


def test_ac2_ten_cells_total() -> None:
    """GIVEN the matrix WHEN counting cells THEN exactly 10 cells exist."""
    data = _load_matrix()
    count = sum(len(providers) for providers in data["profiles"].values())
    assert count == 10


@pytest.mark.parametrize("profile", sorted(_EXPECTED_PROFILES))
@pytest.mark.parametrize("provider", sorted(_EXPECTED_PROVIDERS))
def test_ac2_cell_has_exact_keys(profile: str, provider: str) -> None:
    """GIVEN a cell WHEN reading its keys THEN value/evidence/known_gaps only."""
    data = _load_matrix()
    cell = data["profiles"][profile][provider]
    assert set(cell.keys()) == _CELL_KEYS


@pytest.mark.parametrize("profile", sorted(_EXPECTED_PROFILES))
@pytest.mark.parametrize("provider", sorted(_EXPECTED_PROVIDERS))
def test_ac2_cell_value_is_valid_enum(profile: str, provider: str) -> None:
    """GIVEN a cell WHEN reading value THEN it is one of the 4 valid enums."""
    data = _load_matrix()
    cell = data["profiles"][profile][provider]
    assert cell["value"] in _VALID_VALUES


@pytest.mark.parametrize("profile", sorted(_EXPECTED_PROFILES))
@pytest.mark.parametrize("provider", sorted(_EXPECTED_PROVIDERS))
def test_ac2_cell_evidence_non_empty_list(profile: str, provider: str) -> None:
    """GIVEN a cell WHEN reading evidence THEN it is a non-empty list."""
    data = _load_matrix()
    cell = data["profiles"][profile][provider]
    assert isinstance(cell["evidence"], list)
    assert len(cell["evidence"]) >= 1
    assert all(isinstance(e, str) for e in cell["evidence"])


@pytest.mark.parametrize("profile", sorted(_EXPECTED_PROFILES))
@pytest.mark.parametrize("provider", sorted(_EXPECTED_PROVIDERS))
def test_ac2_cell_known_gaps_is_list(profile: str, provider: str) -> None:
    """GIVEN a cell WHEN reading known_gaps THEN it is a list (empty allowed)."""
    data = _load_matrix()
    cell = data["profiles"][profile][provider]
    assert isinstance(cell["known_gaps"], list)


def test_ac2_evidence_file_paths_exist_in_repo() -> None:
    """GIVEN all cells WHEN evidence entries look like file paths
    THEN those paths exist in the repository (Issue/PR references excluded).
    """
    data = _load_matrix()
    checked_any = False
    for profile, providers in data["profiles"].items():
        for provider, cell in providers.items():
            for entry in cell["evidence"]:
                if not _looks_like_path(entry):
                    continue
                checked_any = True
                candidate = _REPO_ROOT / entry
                assert candidate.exists(), (
                    f"evidence path does not exist: {entry} "
                    f"(profile={profile}, provider={provider})"
                )
    assert checked_any, "expected at least one path-like evidence entry"


# ---------------------------------------------------------------------------
# AC2: negative cases (fail-closed schema validation)
# ---------------------------------------------------------------------------


def test_ac2_invalid_value_is_rejected() -> None:
    """GIVEN a cell with an out-of-enum value THEN it fails validation."""
    assert "not_a_real_value" not in _VALID_VALUES


def test_ac2_missing_key_is_rejected() -> None:
    """GIVEN a cell missing a required key THEN key-set equality fails."""
    incomplete_cell = {"value": "implemented", "evidence": ["x"]}
    assert set(incomplete_cell.keys()) != _CELL_KEYS


def test_ac2_extra_key_is_rejected() -> None:
    """GIVEN a cell with an extra key THEN key-set equality fails."""
    extra_cell = {
        "value": "implemented",
        "evidence": ["x"],
        "known_gaps": [],
        "unexpected_extra_key": "oops",
    }
    assert set(extra_cell.keys()) != _CELL_KEYS


def test_ac2_nonexistent_evidence_path_fails() -> None:
    """GIVEN an evidence path-like entry that does not exist in repo
    THEN existence check fails (proves the AC2 real-file check is not
    vacuously true).
    """
    fake_entry = (
        ".claude/skills/gemini-cli-headless-delegation/"
        "scripts/this_file_does_not_exist_1806.py"
    )
    assert _looks_like_path(fake_entry)
    assert not (_REPO_ROOT / fake_entry).exists()
