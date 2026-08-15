"""Stable, baseline-addressable entry point for Issue #2136 AC3/AC4."""

from __future__ import annotations

import importlib.util
from pathlib import Path


_LEGACY_TEST_MODULE = Path(__file__).with_name("test_operator_selected_scope_reframe.py")
_SPEC = importlib.util.spec_from_file_location("issue_2136_real_subprocess_assets", _LEGACY_TEST_MODULE)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def test_fixture_human_context_real_subprocess_reaches_contract_update_required_ac3(tmp_path: Path):
    """Run the real-assets, fixture-backed E2E through its stable new-file node."""
    _MODULE.test_fixture_human_context_real_subprocess_reaches_contract_update_required_ac3(tmp_path)
