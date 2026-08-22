"""docs/dev/extension-surface-runtime-policy.yaml が同名 JSON Schema に対して
valid であることを検証する pytest テスト（Issue #2283 AC10 / AC13）。

VC preflight の allowlist は `python3 -c` インライン実行を許可しないため、
`uv run --locked pytest docs/dev/tests/test_extension_surface_runtime_policy_schema.py`
経由で検証する。
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
POLICY_YAML_PATH = REPO_ROOT / "docs" / "dev" / "extension-surface-runtime-policy.yaml"
POLICY_SCHEMA_PATH = (
    REPO_ROOT / "docs" / "dev" / "extension-surface-runtime-policy.schema.json"
)


def _load_policy() -> dict:
    with POLICY_YAML_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _load_schema() -> dict:
    with POLICY_SCHEMA_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def test_policy_yaml_exists() -> None:
    assert POLICY_YAML_PATH.is_file()


def test_schema_json_exists() -> None:
    assert POLICY_SCHEMA_PATH.is_file()


def test_schema_json_is_draft_2020_12() -> None:
    schema = _load_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def test_policy_yaml_validates_against_schema() -> None:
    policy = _load_policy()
    schema = _load_schema()
    # jsonschema.validate() raises on failure; a clean return means valid.
    jsonschema.validate(instance=policy, schema=schema)


def test_all_rule_verification_profile_references_exist() -> None:
    """AC13: 各 rule の verification_profile 参照先が top-level
    verification_profiles に実在することを、schema validation とは別に
    直接クロス参照して確認する。
    """
    policy = _load_policy()
    profile_ids = set(policy["verification_profiles"].keys())
    for rule in policy["rules"]:
        assert rule["verification_profile"] in profile_ids, (
            f"rule {rule['id']!r} references unknown verification_profile "
            f"{rule['verification_profile']!r}"
        )


def test_all_rules_have_at_least_one_project_and_one_runtime_resolved_selector() -> None:
    """AC12: 各 rule の selectors[] に project（repository 相対）と
    runtime-resolved（user/managed/plugin/session/cli のいずれか）の
    両方が区別して存在することを確認する。
    """
    policy = _load_policy()
    for rule in policy["rules"]:
        scopes = {selector["source_scope"] for selector in rule["selectors"]}
        assert "project" in scopes, f"rule {rule['id']!r} missing project selector"
        runtime_resolved_scopes = scopes - {"project"}
        assert runtime_resolved_scopes, (
            f"rule {rule['id']!r} missing a runtime-resolved selector"
        )


def test_auto_mode_assumption_min_version_is_2_1_83_or_higher() -> None:
    """AC15: Auto mode 関連 claim の min_claude_code_version が 2.1.83 以上。"""
    policy = _load_policy()
    auto_mode_claims = [
        a for a in policy["assumptions"] if a.get("applicability") == "auto_mode"
    ]
    assert auto_mode_claims, "no auto_mode assumption claim found"
    for claim in auto_mode_claims:
        version = claim["min_claude_code_version"]
        assert tuple(int(p) for p in version.split(".")) >= (2, 1, 83), (
            f"claim {claim['claim_id']!r} min_claude_code_version {version!r} "
            "is below 2.1.83"
        )
