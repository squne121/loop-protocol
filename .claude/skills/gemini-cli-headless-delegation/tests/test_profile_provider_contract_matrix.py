"""Tests for profile_provider_contract_matrix.yaml (Issue #1806, PR #1823
fix_delta for human REQUEST_CHANGES on 2026-07-27).

AC coverage:
  AC1: config/profile_provider_contract_matrix.yaml exists with
       schema: profile_provider_contract_matrix/v1
  AC2: matrix schema validation (5 profile x 2 provider = 10 cells, 4-value
       enum, evidence/known_gaps keys present, evidence entries resolve to
       real repo-relative files or well-formed Issue/PR references, invalid
       values/missing keys/extra keys/duplicate keys fail).

This module implements a single validator function,
``validate_profile_provider_contract_matrix(data, repo_root)``, that both the
positive (real matrix file) and negative (synthetic fixture) tests route
through -- so the negative tests actually exercise the same fail-closed code
path used to validate the production matrix, rather than duplicating
self-contained assertions (PR #1823 review Blocker 2).

The YAML loader used here (``_load_matrix_text`` / ``_StrictUniqueKeyLoader``)
rejects duplicate mapping keys at parse time. Plain ``yaml.safe_load()``
silently keeps the last occurrence of a duplicate key, which would make
duplicate-key fixtures vacuously pass; this loader closes that gap
(PR #1823 review Blocker 2).

Evidence entries are typed (``kind: repo_file`` / ``kind: test`` /
``kind: issue`` / ``kind: pr``) rather than free-form strings classified by a
path-looking heuristic. ``repo_file`` / ``test`` entries are resolved via
``Path.resolve()`` and checked to stay within the repo root and to point at a
real file (rejecting absolute paths, ``..`` traversal, directories, and
symlinks that resolve outside the repo root). ``issue`` / ``pr`` entries are
checked against a strict reference pattern (PR #1823 review Blocker 3).
"""
from __future__ import annotations

import copy
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
import yaml

_SKILL_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = Path(__file__).resolve().parents[4]
_MATRIX_PATH = (
    _SKILL_DIR / "config" / "profile_provider_contract_matrix.yaml"
)

_EXPECTED_SCHEMA = "profile_provider_contract_matrix/v1"
_TOP_LEVEL_KEYS = {"schema", "profiles"}
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
_EVIDENCE_KINDS = {"repo_file", "test", "issue", "pr"}
_EVIDENCE_KEYS_BY_KIND = {
    "repo_file": {"kind", "path"},
    "test": {"kind", "path"},
    "issue": {"kind", "ref"},
    "pr": {"kind", "ref"},
}
_ISSUE_REF_RE = re.compile(r"^#[1-9]\d*$")
_PR_REF_RE = re.compile(r"^PR #[1-9]\d*$")


class MatrixValidationError(ValueError):
    """Raised by validate_profile_provider_contract_matrix() on any
    structural or evidence-integrity violation."""


# ---------------------------------------------------------------------------
# Strict (duplicate-key-rejecting) YAML loader
#
# yaml.safe_load() follows the PyYAML default of silently keeping the last
# occurrence of a duplicate mapping key. YAML 1.2.2 requires mapping keys to
# be unique, so this loader enforces that by overriding construct_mapping()
# on a SafeLoader subclass (PR #1823 review Blocker 2).
# ---------------------------------------------------------------------------


class _DuplicateKeyError(yaml.YAMLError):
    pass


class _StrictUniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping_no_dup(
    loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in mapping:
            raise _DuplicateKeyError(f"duplicate mapping key: {key!r}")
        value = loader.construct_object(value_node, deep=deep)
        mapping[key] = value
    return mapping


_StrictUniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping_no_dup,
)


def _load_matrix_text(text: str) -> Any:
    return yaml.load(text, Loader=_StrictUniqueKeyLoader)  # noqa: S506


def _load_matrix() -> dict[str, Any]:
    return _load_matrix_text(_MATRIX_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Validator (single source of truth for both positive and negative tests)
# ---------------------------------------------------------------------------


def validate_profile_provider_contract_matrix(
    data: Any, repo_root: Path
) -> None:
    """Validate a parsed profile_provider_contract_matrix/v1 document.

    Raises MatrixValidationError on any violation. Does not mutate ``data``.
    """
    if not isinstance(data, dict):
        raise MatrixValidationError("matrix root must be a mapping")

    unknown_top = set(data.keys()) - _TOP_LEVEL_KEYS
    if unknown_top:
        raise MatrixValidationError(
            f"unknown top-level key(s): {sorted(unknown_top)}"
        )
    missing_top = _TOP_LEVEL_KEYS - set(data.keys())
    if missing_top:
        raise MatrixValidationError(
            f"missing top-level key(s): {sorted(missing_top)}"
        )
    if data["schema"] != _EXPECTED_SCHEMA:
        raise MatrixValidationError(
            f"unexpected schema: {data['schema']!r}"
        )

    profiles = data["profiles"]
    if not isinstance(profiles, dict):
        raise MatrixValidationError("profiles must be a mapping")

    unknown_profiles = set(profiles.keys()) - _EXPECTED_PROFILES
    if unknown_profiles:
        raise MatrixValidationError(
            f"unknown profile(s): {sorted(unknown_profiles)}"
        )
    missing_profiles = _EXPECTED_PROFILES - set(profiles.keys())
    if missing_profiles:
        raise MatrixValidationError(
            f"missing profile(s): {sorted(missing_profiles)}"
        )

    for profile, providers in profiles.items():
        if not isinstance(providers, dict):
            raise MatrixValidationError(
                f"profile {profile!r} value must be a mapping"
            )
        unknown_providers = set(providers.keys()) - _EXPECTED_PROVIDERS
        if unknown_providers:
            raise MatrixValidationError(
                f"unknown provider(s) in {profile!r}: "
                f"{sorted(unknown_providers)}"
            )
        missing_providers = _EXPECTED_PROVIDERS - set(providers.keys())
        if missing_providers:
            raise MatrixValidationError(
                f"missing provider(s) in {profile!r}: "
                f"{sorted(missing_providers)}"
            )
        for provider, cell in providers.items():
            _validate_cell(profile, provider, cell, repo_root)


def _validate_cell(
    profile: str, provider: str, cell: Any, repo_root: Path
) -> None:
    label = f"{profile}/{provider}"
    if not isinstance(cell, dict):
        raise MatrixValidationError(f"cell {label} must be a mapping")

    unknown = set(cell.keys()) - _CELL_KEYS
    if unknown:
        raise MatrixValidationError(
            f"unknown key(s) in cell {label}: {sorted(unknown)}"
        )
    missing = _CELL_KEYS - set(cell.keys())
    if missing:
        raise MatrixValidationError(
            f"missing key(s) in cell {label}: {sorted(missing)}"
        )

    if cell["value"] not in _VALID_VALUES:
        raise MatrixValidationError(
            f"invalid value in cell {label}: {cell['value']!r}"
        )

    evidence = cell["evidence"]
    if not isinstance(evidence, list) or len(evidence) == 0:
        raise MatrixValidationError(
            f"evidence must be a non-empty list in cell {label}"
        )
    for entry in evidence:
        _validate_evidence_entry(label, entry, repo_root)

    known_gaps = cell["known_gaps"]
    if not isinstance(known_gaps, list):
        raise MatrixValidationError(
            f"known_gaps must be a list in cell {label}"
        )
    for gap in known_gaps:
        if not isinstance(gap, str):
            raise MatrixValidationError(
                f"known_gaps entries must be strings in cell {label}: "
                f"{gap!r}"
            )


def _validate_evidence_entry(label: str, entry: Any, repo_root: Path) -> None:
    if not isinstance(entry, dict):
        raise MatrixValidationError(
            f"evidence entry must be a mapping in cell {label}: {entry!r}"
        )
    kind = entry.get("kind")
    if kind not in _EVIDENCE_KINDS:
        raise MatrixValidationError(
            f"invalid evidence kind in cell {label}: {kind!r}"
        )
    expected_keys = _EVIDENCE_KEYS_BY_KIND[kind]
    if set(entry.keys()) != expected_keys:
        raise MatrixValidationError(
            f"evidence entry keys mismatch for kind={kind!r} in cell "
            f"{label}: {sorted(entry.keys())}"
        )
    if kind in ("repo_file", "test"):
        _validate_repo_file_evidence(label, entry["path"], repo_root)
    else:
        _validate_ref_evidence(label, kind, entry["ref"])


def _validate_repo_file_evidence(
    label: str, raw_path: Any, repo_root: Path
) -> None:
    if not isinstance(raw_path, str) or raw_path.strip() != raw_path or not raw_path:
        raise MatrixValidationError(
            f"evidence path must be a non-empty, unpadded string in cell "
            f"{label}: {raw_path!r}"
        )
    if os.path.isabs(raw_path):
        raise MatrixValidationError(
            f"evidence path must be repo-relative (absolute path rejected) "
            f"in cell {label}: {raw_path!r}"
        )
    if ".." in PurePosixPath(raw_path).parts:
        raise MatrixValidationError(
            f"evidence path must not contain '..' traversal in cell "
            f"{label}: {raw_path!r}"
        )
    repo_root_resolved = repo_root.resolve()
    candidate = (repo_root_resolved / raw_path).resolve()
    try:
        candidate.relative_to(repo_root_resolved)
    except ValueError as exc:
        raise MatrixValidationError(
            f"evidence path escapes repo root in cell {label}: "
            f"{raw_path!r}"
        ) from exc
    if not candidate.is_file():
        raise MatrixValidationError(
            f"evidence path does not exist as a regular file in cell "
            f"{label}: {raw_path!r}"
        )


def _validate_ref_evidence(label: str, kind: str, ref: Any) -> None:
    if not isinstance(ref, str):
        raise MatrixValidationError(
            f"evidence ref must be a string in cell {label}: {ref!r}"
        )
    pattern = _ISSUE_REF_RE if kind == "issue" else _PR_REF_RE
    if not pattern.match(ref):
        raise MatrixValidationError(
            f"malformed {kind} reference in cell {label}: {ref!r}"
        )


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
# AC2: positive path -- real matrix file must pass the validator wholesale
# ---------------------------------------------------------------------------


def test_ac2_real_matrix_passes_validator() -> None:
    """GIVEN the real matrix file WHEN validated THEN no error is raised."""
    data = _load_matrix()
    validate_profile_provider_contract_matrix(data, _REPO_ROOT)


def test_ac2_profiles_key_set_exact() -> None:
    data = _load_matrix()
    assert set(data["profiles"].keys()) == _EXPECTED_PROFILES


@pytest.mark.parametrize("profile", sorted(_EXPECTED_PROFILES))
def test_ac2_each_profile_has_exact_provider_subkeys(profile: str) -> None:
    data = _load_matrix()
    assert set(data["profiles"][profile].keys()) == _EXPECTED_PROVIDERS


def test_ac2_ten_cells_total() -> None:
    data = _load_matrix()
    count = sum(len(providers) for providers in data["profiles"].values())
    assert count == 10


@pytest.mark.parametrize("profile", sorted(_EXPECTED_PROFILES))
@pytest.mark.parametrize("provider", sorted(_EXPECTED_PROVIDERS))
def test_ac2_cell_has_exact_keys(profile: str, provider: str) -> None:
    data = _load_matrix()
    cell = data["profiles"][profile][provider]
    assert set(cell.keys()) == _CELL_KEYS


@pytest.mark.parametrize("profile", sorted(_EXPECTED_PROFILES))
@pytest.mark.parametrize("provider", sorted(_EXPECTED_PROVIDERS))
def test_ac2_cell_value_is_valid_enum(profile: str, provider: str) -> None:
    data = _load_matrix()
    cell = data["profiles"][profile][provider]
    assert cell["value"] in _VALID_VALUES


@pytest.mark.parametrize("profile", sorted(_EXPECTED_PROFILES))
@pytest.mark.parametrize("provider", sorted(_EXPECTED_PROVIDERS))
def test_ac2_cell_evidence_non_empty_list_of_typed_entries(
    profile: str, provider: str
) -> None:
    data = _load_matrix()
    cell = data["profiles"][profile][provider]
    assert isinstance(cell["evidence"], list)
    assert len(cell["evidence"]) >= 1
    for entry in cell["evidence"]:
        assert isinstance(entry, dict)
        assert entry.get("kind") in _EVIDENCE_KINDS


@pytest.mark.parametrize("profile", sorted(_EXPECTED_PROFILES))
@pytest.mark.parametrize("provider", sorted(_EXPECTED_PROVIDERS))
def test_ac2_cell_known_gaps_is_list(profile: str, provider: str) -> None:
    data = _load_matrix()
    cell = data["profiles"][profile][provider]
    assert isinstance(cell["known_gaps"], list)


def test_ac2_evidence_repo_file_entries_exist_in_repo() -> None:
    """GIVEN all cells WHEN evidence entries have kind repo_file/test
    THEN the resolved path exists as a real file in the repository.
    """
    data = _load_matrix()
    checked_any = False
    for profile, providers in data["profiles"].items():
        for provider, cell in providers.items():
            for entry in cell["evidence"]:
                if entry["kind"] not in ("repo_file", "test"):
                    continue
                checked_any = True
                candidate = _REPO_ROOT / entry["path"]
                assert candidate.is_file(), (
                    f"evidence path does not exist: {entry['path']} "
                    f"(profile={profile}, provider={provider})"
                )
    assert checked_any, "expected at least one repo_file/test evidence entry"


# ---------------------------------------------------------------------------
# AC2: negative cases -- routed through validate_profile_provider_contract_matrix()
# ---------------------------------------------------------------------------

_VALID_CELL: dict[str, Any] = {
    "value": "implemented",
    "evidence": [
        # Deliberately filesystem-independent (kind=issue) so that mutated
        # copies of this fixture used by repo_root-sensitive negative tests
        # (e.g. symlink escape, below) do not spuriously fail on unrelated
        # cells whose evidence would otherwise be resolved against a fake
        # repo_root.
        {"kind": "issue", "ref": "#1806"}
    ],
    "known_gaps": [],
}


def _valid_matrix() -> dict[str, Any]:
    return {
        "schema": _EXPECTED_SCHEMA,
        "profiles": {
            profile: {
                provider: copy.deepcopy(_VALID_CELL)
                for provider in sorted(_EXPECTED_PROVIDERS)
            }
            for profile in sorted(_EXPECTED_PROFILES)
        },
    }


def test_negative_valid_fixture_is_itself_valid() -> None:
    """Sanity check: the fixture builder used by all negative tests below
    passes validation unmodified (so mutated copies fail *because of* the
    mutation, not because the fixture itself is broken)."""
    validate_profile_provider_contract_matrix(_valid_matrix(), _REPO_ROOT)


def test_negative_unknown_top_level_key() -> None:
    data = _valid_matrix()
    data["extra_top_level_key"] = "oops"
    with pytest.raises(MatrixValidationError, match="unknown top-level key"):
        validate_profile_provider_contract_matrix(data, _REPO_ROOT)


def test_negative_missing_schema_key() -> None:
    data = _valid_matrix()
    del data["schema"]
    with pytest.raises(MatrixValidationError, match="missing top-level key"):
        validate_profile_provider_contract_matrix(data, _REPO_ROOT)


def test_negative_missing_profiles_key() -> None:
    data = _valid_matrix()
    del data["profiles"]
    with pytest.raises(MatrixValidationError, match="missing top-level key"):
        validate_profile_provider_contract_matrix(data, _REPO_ROOT)


def test_negative_unknown_profile() -> None:
    data = _valid_matrix()
    data["profiles"]["not_a_real_profile"] = data["profiles"].pop("no_tools")
    with pytest.raises(MatrixValidationError, match="unknown profile"):
        validate_profile_provider_contract_matrix(data, _REPO_ROOT)


def test_negative_unknown_provider() -> None:
    data = _valid_matrix()
    data["profiles"]["no_tools"]["openai"] = data["profiles"]["no_tools"].pop(
        "gemini"
    )
    with pytest.raises(MatrixValidationError, match="unknown provider"):
        validate_profile_provider_contract_matrix(data, _REPO_ROOT)


def test_negative_invalid_enum_value() -> None:
    data = _valid_matrix()
    data["profiles"]["no_tools"]["gemini"]["value"] = "not_a_real_value"
    with pytest.raises(MatrixValidationError, match="invalid value"):
        validate_profile_provider_contract_matrix(data, _REPO_ROOT)


def test_negative_missing_cell_key() -> None:
    data = _valid_matrix()
    del data["profiles"]["no_tools"]["gemini"]["known_gaps"]
    with pytest.raises(MatrixValidationError, match="missing key"):
        validate_profile_provider_contract_matrix(data, _REPO_ROOT)


def test_negative_extra_cell_key() -> None:
    data = _valid_matrix()
    data["profiles"]["no_tools"]["gemini"]["unexpected_extra_key"] = "oops"
    with pytest.raises(MatrixValidationError, match="unknown key"):
        validate_profile_provider_contract_matrix(data, _REPO_ROOT)


def test_negative_empty_evidence_list() -> None:
    data = _valid_matrix()
    data["profiles"]["no_tools"]["gemini"]["evidence"] = []
    with pytest.raises(MatrixValidationError, match="non-empty list"):
        validate_profile_provider_contract_matrix(data, _REPO_ROOT)


def test_negative_evidence_empty_string_path() -> None:
    data = _valid_matrix()
    data["profiles"]["no_tools"]["gemini"]["evidence"] = [
        {"kind": "repo_file", "path": ""}
    ]
    with pytest.raises(MatrixValidationError, match="non-empty, unpadded"):
        validate_profile_provider_contract_matrix(data, _REPO_ROOT)


def test_negative_evidence_non_string_path() -> None:
    data = _valid_matrix()
    data["profiles"]["no_tools"]["gemini"]["evidence"] = [
        {"kind": "repo_file", "path": 12345}
    ]
    with pytest.raises(MatrixValidationError, match="non-empty, unpadded"):
        validate_profile_provider_contract_matrix(data, _REPO_ROOT)


def test_negative_evidence_entry_not_a_mapping() -> None:
    data = _valid_matrix()
    data["profiles"]["no_tools"]["gemini"]["evidence"] = [
        ".claude/skills/gemini-cli-headless-delegation/scripts/run_gemini_headless.py"
    ]
    with pytest.raises(MatrixValidationError, match="must be a mapping"):
        validate_profile_provider_contract_matrix(data, _REPO_ROOT)


def test_negative_known_gaps_non_string_entry() -> None:
    data = _valid_matrix()
    data["profiles"]["no_tools"]["gemini"]["known_gaps"] = [123]
    with pytest.raises(MatrixValidationError, match="known_gaps entries must be strings"):
        validate_profile_provider_contract_matrix(data, _REPO_ROOT)


def test_negative_evidence_path_traversal_escape() -> None:
    data = _valid_matrix()
    data["profiles"]["no_tools"]["gemini"]["evidence"] = [
        {"kind": "repo_file", "path": "../../../../etc/passwd"}
    ]
    with pytest.raises(MatrixValidationError, match="'\\.\\.' traversal"):
        validate_profile_provider_contract_matrix(data, _REPO_ROOT)


def test_negative_evidence_absolute_path_rejected() -> None:
    data = _valid_matrix()
    data["profiles"]["no_tools"]["gemini"]["evidence"] = [
        {"kind": "repo_file", "path": "/etc/passwd"}
    ]
    with pytest.raises(MatrixValidationError, match="absolute path rejected"):
        validate_profile_provider_contract_matrix(data, _REPO_ROOT)


def test_negative_evidence_directory_rejected() -> None:
    data = _valid_matrix()
    data["profiles"]["no_tools"]["gemini"]["evidence"] = [
        {
            "kind": "repo_file",
            "path": ".claude/skills/gemini-cli-headless-delegation/config",
        }
    ]
    with pytest.raises(MatrixValidationError, match="does not exist as a regular file"):
        validate_profile_provider_contract_matrix(data, _REPO_ROOT)


def test_negative_evidence_nonexistent_file_rejected() -> None:
    data = _valid_matrix()
    data["profiles"]["no_tools"]["gemini"]["evidence"] = [
        {
            "kind": "repo_file",
            "path": (
                ".claude/skills/gemini-cli-headless-delegation/"
                "scripts/this_file_does_not_exist_1806.py"
            ),
        }
    ]
    with pytest.raises(MatrixValidationError, match="does not exist as a regular file"):
        validate_profile_provider_contract_matrix(data, _REPO_ROOT)


def test_negative_evidence_symlink_escaping_repo_root_rejected(
    tmp_path: Path,
) -> None:
    """A symlink that lives inside the fake repo but resolves to a target
    outside it must be rejected even though the raw path string contains no
    '..' and is not itself absolute."""
    fake_repo = tmp_path / "fake_repo"
    fake_repo.mkdir()
    outside_target = tmp_path / "outside_secret.txt"
    outside_target.write_text("secret", encoding="utf-8")
    symlink_path = fake_repo / "evidence_symlink.txt"
    symlink_path.symlink_to(outside_target)

    data = _valid_matrix()
    data["profiles"]["no_tools"]["gemini"]["evidence"] = [
        {"kind": "repo_file", "path": "evidence_symlink.txt"}
    ]
    with pytest.raises(MatrixValidationError, match="escapes repo root"):
        validate_profile_provider_contract_matrix(data, fake_repo)


def test_negative_issue_ref_malformed_missing_hash() -> None:
    data = _valid_matrix()
    data["profiles"]["no_tools"]["gemini"]["evidence"] = [
        {"kind": "issue", "ref": "1265"}
    ]
    with pytest.raises(MatrixValidationError, match="malformed issue reference"):
        validate_profile_provider_contract_matrix(data, _REPO_ROOT)


def test_negative_pr_ref_malformed_missing_prefix() -> None:
    data = _valid_matrix()
    data["profiles"]["no_tools"]["gemini"]["evidence"] = [
        {"kind": "pr", "ref": "#1823"}
    ]
    with pytest.raises(MatrixValidationError, match="malformed pr reference"):
        validate_profile_provider_contract_matrix(data, _REPO_ROOT)


def test_negative_evidence_kind_invalid() -> None:
    data = _valid_matrix()
    data["profiles"]["no_tools"]["gemini"]["evidence"] = [
        {"kind": "web_url", "path": "https://example.com"}
    ]
    with pytest.raises(MatrixValidationError, match="invalid evidence kind"):
        validate_profile_provider_contract_matrix(data, _REPO_ROOT)


def test_negative_valid_issue_and_pr_refs_pass() -> None:
    """Positive control for the issue/pr evidence kind: well-formed refs
    must not raise."""
    data = _valid_matrix()
    data["profiles"]["no_tools"]["gemini"]["evidence"] = [
        {"kind": "issue", "ref": "#1265"},
        {"kind": "pr", "ref": "PR #1823"},
    ]
    validate_profile_provider_contract_matrix(data, _REPO_ROOT)


# ---------------------------------------------------------------------------
# AC2: duplicate-key rejection at YAML-load time (loader-level, not
# validator-level, since Python dicts cannot represent duplicate keys once
# parsed)
# ---------------------------------------------------------------------------

_DUP_PROFILES_TEXT = """
schema: profile_provider_contract_matrix/v1
profiles:
  no_tools:
    gemini:
      value: implemented
      evidence:
        - kind: issue
          ref: "#1"
      known_gaps: []
    agy:
      value: implemented
      evidence:
        - kind: issue
          ref: "#1"
      known_gaps: []
  no_tools:
    gemini:
      value: implemented
      evidence:
        - kind: issue
          ref: "#1"
      known_gaps: []
    agy:
      value: implemented
      evidence:
        - kind: issue
          ref: "#1"
      known_gaps: []
"""

_DUP_PROVIDER_TEXT = """
schema: profile_provider_contract_matrix/v1
profiles:
  no_tools:
    gemini:
      value: implemented
      evidence:
        - kind: issue
          ref: "#1"
      known_gaps: []
    gemini:
      value: deferred
      evidence:
        - kind: issue
          ref: "#1"
      known_gaps: []
"""

_DUP_CELL_KEY_TEXT = """
schema: profile_provider_contract_matrix/v1
profiles:
  no_tools:
    gemini:
      value: implemented
      value: deferred
      evidence:
        - kind: issue
          ref: "#1"
      known_gaps: []
"""

_DUP_TOP_LEVEL_KEY_TEXT = """
schema: profile_provider_contract_matrix/v1
schema: profile_provider_contract_matrix/v2
profiles: {}
"""


@pytest.mark.parametrize(
    "text",
    [
        _DUP_PROFILES_TEXT,
        _DUP_PROVIDER_TEXT,
        _DUP_CELL_KEY_TEXT,
        _DUP_TOP_LEVEL_KEY_TEXT,
    ],
    ids=[
        "duplicate_profile_key",
        "duplicate_provider_key",
        "duplicate_cell_key",
        "duplicate_top_level_key",
    ],
)
def test_negative_duplicate_mapping_key_rejected_at_load_time(
    text: str,
) -> None:
    with pytest.raises(yaml.YAMLError):
        _load_matrix_text(text)


def test_negative_plain_safe_load_would_not_catch_duplicate_key() -> None:
    """Documents *why* the strict loader is required: plain yaml.safe_load()
    silently keeps the last occurrence of a duplicate key instead of
    rejecting it."""
    data = yaml.safe_load(_DUP_PROFILES_TEXT)
    assert set(data["profiles"].keys()) == {"no_tools"}


# ---------------------------------------------------------------------------
# Major 6: runtime AGY_SUPPORTED_PROFILES drift detection
#
# The test-side _EXPECTED_PROFILES / _EXPECTED_PROVIDERS constants above are
# independently defined so that a drift between them and runtime code is
# itself detectable (rather than tautologically re-deriving the runtime
# constant). This test cross-checks against the canonical runtime source so
# a future change to AGY_SUPPORTED_PROFILES that is not reflected in the
# matrix's github_research/agy cell is caught (PR #1823 review Major 6).
# ---------------------------------------------------------------------------


def _load_run_gemini_headless_for_drift_check() -> Any:
    import importlib.util

    run_gemini_headless_py = (
        _SKILL_DIR / "scripts" / "run_gemini_headless.py"
    )
    spec = importlib.util.spec_from_file_location(
        "run_gemini_headless_matrix_drift_check", run_gemini_headless_py
    )
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_matrix_agy_unsupported_profile_matches_runtime_agy_supported_profiles() -> (
    None
):
    """GIVEN the runtime AGY_SUPPORTED_PROFILES constant WHEN diffed against
    _EXPECTED_PROFILES THEN the only profile excluded from
    AGY_SUPPORTED_PROFILES is github_research, matching the matrix's
    github_research/agy == unsupported_by_design cell. This detects drift if
    the runtime constant is changed without updating the matrix.
    """
    rgh = _load_run_gemini_headless_for_drift_check()
    runtime_agy_supported = set(rgh.AGY_SUPPORTED_PROFILES)
    unsupported_in_runtime = _EXPECTED_PROFILES - runtime_agy_supported

    data = _load_matrix()
    matrix_unsupported = {
        profile
        for profile, providers in data["profiles"].items()
        if providers["agy"]["value"] == "unsupported_by_design"
    }

    assert unsupported_in_runtime == matrix_unsupported == {"github_research"}, (
        "matrix github_research/agy=unsupported_by_design has drifted from "
        f"runtime AGY_SUPPORTED_PROFILES (runtime-excluded={unsupported_in_runtime}, "
        f"matrix-unsupported={matrix_unsupported})"
    )
