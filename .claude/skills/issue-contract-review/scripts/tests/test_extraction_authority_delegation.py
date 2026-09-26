"""
test_extraction_authority_delegation.py

Issue #2782 AC7 / AC8:

`baseline_vc_preflight.py::extract_verification_commands_section()`（AC7）と
`contract_readiness_check.py::_extract_section_by_canonical_name()`（AC8）が、
`prose_boundary_policy.py` の新設 shared helper
（`extract_level2_section()` / `extract_level2_section_with_bounds()`）へ
実際に委譲しており、section boundary algorithm（fence-aware 見出し境界判定
ループ）の独立コピーをそれぞれのファイル自身に持たないことを固定する回帰
テスト。

AC7 はさらに、`baseline_vc_preflight.py` から `contract_readiness_check.py`
への逆 import が発生しないこと（循環依存回避）も固定する
（`contract_readiness_check.py` は既に `baseline_vc_preflight` から
`extract_verification_commands_section` 等を import しているため、逆方向の
import が発生すると import 時点で循環依存になる）。

Runtime Verification Applicability: not_applicable
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

_HERE = Path(__file__).resolve().parent
_SCRIPTS_DIR = _HERE.parent
# _SCRIPTS_DIR = .../.claude/skills/issue-contract-review/scripts
# parents: [0]=issue-contract-review, [1]=skills, [2]=.claude
_CREATE_ISSUE_SCRIPTS_DIR = (
    _SCRIPTS_DIR.parents[1] / "create-issue" / "scripts"
)

_BASELINE_VC_PREFLIGHT_PATH = _SCRIPTS_DIR / "baseline_vc_preflight.py"
_CONTRACT_READINESS_CHECK_PATH = _SCRIPTS_DIR / "contract_readiness_check.py"
_PROSE_BOUNDARY_POLICY_PATH = _CREATE_ISSUE_SCRIPTS_DIR / "prose_boundary_policy.py"

for _p in (str(_SCRIPTS_DIR), str(_CREATE_ISSUE_SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_prose_boundary_policy = _load_module(
    "prose_boundary_policy", _PROSE_BOUNDARY_POLICY_PATH
)
_baseline_vc_preflight = _load_module(
    "baseline_vc_preflight", _BASELINE_VC_PREFLIGHT_PATH
)
_contract_readiness_check = _load_module(
    "contract_readiness_check", _CONTRACT_READINESS_CHECK_PATH
)


def _module_top_level_import_targets(path: Path) -> set[str]:
    """Return the set of dotted module names referenced by `import X` /
    `from X import ...` statements at any level in the file's AST."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                targets.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                targets.add(node.module)
    return targets


# ---------------------------------------------------------------------------
# AC7: baseline_vc_preflight.py delegates to the shared helper and does not
# import contract_readiness_check.py (no circular dependency)
# ---------------------------------------------------------------------------


class TestBaselineVcPreflightDelegation:
    def test_baseline_vc_preflight_imports_shared_prose_boundary_policy_helper(self):
        targets = _module_top_level_import_targets(_BASELINE_VC_PREFLIGHT_PATH)
        assert "prose_boundary_policy" in targets, (
            "baseline_vc_preflight.py must import the section extraction "
            "authority from prose_boundary_policy.py"
        )

    def test_baseline_vc_preflight_does_not_import_contract_readiness_check(self):
        targets = _module_top_level_import_targets(_BASELINE_VC_PREFLIGHT_PATH)
        assert "contract_readiness_check" not in targets, (
            "baseline_vc_preflight.py must not import contract_readiness_check "
            "(contract_readiness_check.py already imports baseline_vc_preflight; "
            "the reverse import would form a cycle)"
        )

    def test_baseline_vc_preflight_has_no_independent_heading_boundary_primitives(self):
        """AC7: baseline_vc_preflight.py's own source no longer references the
        heading-boundary primitives directly -- it only calls the shared
        prose_boundary_policy helper, so it cannot hold an independent copy
        of the section-boundary algorithm."""
        source = _BASELINE_VC_PREFLIGHT_PATH.read_text(encoding="utf-8")
        assert "parse_atx_heading_line" not in source
        assert "lookup_heading_policy" not in source

    def test_baseline_vc_preflight_delegates_actual_call_to_shared_helper(self):
        """Functional delegation check: patching
        prose_boundary_policy.extract_level2_section() (the exact object
        baseline_vc_preflight.py imported a reference to) changes
        extract_verification_commands_section()'s output -- proving the
        call flows through the shared helper rather than a locally
        reimplemented algorithm."""
        sentinel = "SENTINEL_FROM_SHARED_HELPER"
        calls: list[tuple[str, str]] = []

        def _fake_extract_level2_section(body: str, canonical_en: str):
            calls.append((body, canonical_en))
            return sentinel

        body = "## Verification Commands\n\nreal content\n"
        with patch.object(
            _baseline_vc_preflight,
            "_extract_level2_section",
            _fake_extract_level2_section,
        ):
            result = _baseline_vc_preflight.extract_verification_commands_section(body)

        assert calls == [(body, "Verification Commands")]
        assert result == sentinel


# ---------------------------------------------------------------------------
# AC8: contract_readiness_check.py delegates to the shared helper and has no
# independent fence-aware section boundary loop
# ---------------------------------------------------------------------------


class TestContractReadinessCheckDelegation:
    def test_contract_readiness_check_imports_shared_prose_boundary_policy_helper(self):
        targets = _module_top_level_import_targets(_CONTRACT_READINESS_CHECK_PATH)
        assert "prose_boundary_policy" in targets

    def test_contract_readiness_check_has_no_independent_heading_boundary_primitives(self):
        """AC8: contract_readiness_check.py's own source no longer
        references the heading-boundary primitives directly (it used to
        call parse_atx_heading_line()/lookup_heading_policy() in its own
        fence-aware loop before delegating to the shared helper)."""
        source = _CONTRACT_READINESS_CHECK_PATH.read_text(encoding="utf-8")
        assert "parse_atx_heading_line" not in source
        assert "lookup_heading_policy" not in source
        assert "def _fenced_line_indices" not in source, (
            "contract_readiness_check.py must not hold its own "
            "_fenced_line_indices() -- that primitive now lives only in "
            "prose_boundary_policy.py"
        )

    def test_contract_readiness_check_delegates_actual_call_to_shared_helper(self):
        """Functional delegation check: patching
        prose_boundary_policy.extract_level2_section_with_bounds() (the
        exact object contract_readiness_check.py imported a reference to)
        changes _extract_section_by_canonical_name()'s output."""
        sentinel = ("SENTINEL_TEXT", 3, 7)
        calls: list[tuple[str, str]] = []

        def _fake_extract_level2_section_with_bounds(body: str, canonical_en: str):
            calls.append((body, canonical_en))
            return sentinel

        body = "## Verification Commands\n\nreal content\n"
        with patch.object(
            _contract_readiness_check,
            "_extract_level2_section_with_bounds",
            _fake_extract_level2_section_with_bounds,
        ):
            result = _contract_readiness_check._extract_section_by_canonical_name(
                body, "Verification Commands"
            )

        assert calls == [(body, "Verification Commands")]
        assert result == sentinel

    def test_contract_readiness_check_import_direction_toward_preflight_module_preserved(self):
        """Existing import direction (contract_readiness_check.py ->
        preflight module) is preserved (Issue #2782 explicitly keeps this
        direction; only the reverse direction is disallowed)."""
        targets = _module_top_level_import_targets(_CONTRACT_READINESS_CHECK_PATH)
        assert "baseline_vc_preflight" in targets
