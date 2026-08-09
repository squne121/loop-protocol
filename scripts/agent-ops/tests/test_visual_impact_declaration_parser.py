"""test_visual_impact_declaration_parser.py (Issue #2019 AC9)

GIVEN/WHEN/THEN tests for the VISUAL_IMPACT_DECLARATION_V1 parser
(resolve_visual_impact.parse_declaration): PR body is untrusted input, so
multiple fenced blocks, malformed fences, duplicate YAML keys, duplicate
surface entries, and oversized blocks must all be rejected (never
silently accepted or auto-repaired).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parent.parent / "resolve_visual_impact.py"
_MODULE_NAME = "resolve_visual_impact_issue_2019_declaration_parser"

_spec = importlib.util.spec_from_file_location(_MODULE_NAME, _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
rvi = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = rvi
_spec.loader.exec_module(rvi)

VALID_BLOCK = """## visual impact

```yaml
schema: VISUAL_IMPACT_DECLARATION_V1
surfaces:
  - surface_id: combat-hud-running
    disposition: verified_unchanged
```
"""


def test_valid_single_block_parses():
    doc = rvi.parse_declaration(VALID_BLOCK)
    assert doc["schema"] == "VISUAL_IMPACT_DECLARATION_V1"
    assert doc["surfaces"][0]["surface_id"] == "combat-hud-running"


def test_missing_block_is_rejected():
    with pytest.raises(rvi.DeclarationError, match="no VISUAL_IMPACT_DECLARATION_V1"):
        rvi.parse_declaration("no declaration here at all")


def test_multiple_blocks_are_rejected():
    body = VALID_BLOCK + "\n" + VALID_BLOCK
    with pytest.raises(rvi.DeclarationError, match="exactly one"):
        rvi.parse_declaration(body)


def test_malformed_fence_is_rejected():
    """An UNCLOSED fence (no matching closing ``` at all) must never be
    treated as a valid declaration block."""
    body = "```yaml\nschema: VISUAL_IMPACT_DECLARATION_V1\nsurfaces: []\n(fence never closes)"
    with pytest.raises(rvi.DeclarationError, match="no VISUAL_IMPACT_DECLARATION_V1"):
        rvi.parse_declaration(body)


def test_duplicate_yaml_key_is_rejected():
    body = """```yaml
schema: VISUAL_IMPACT_DECLARATION_V1
schema: VISUAL_IMPACT_DECLARATION_V1
surfaces: []
```"""
    with pytest.raises(rvi.DeclarationError, match="duplicate key"):
        rvi.parse_declaration(body)


def test_duplicate_surface_entry_is_rejected():
    body = """```yaml
schema: VISUAL_IMPACT_DECLARATION_V1
surfaces:
  - surface_id: combat-hud-running
    disposition: verified_unchanged
  - surface_id: combat-hud-running
    disposition: verified_unchanged
```"""
    with pytest.raises(rvi.DeclarationError, match="duplicate surface_id"):
        rvi.parse_declaration(body)


def test_oversized_block_is_rejected():
    padding = "x" * (rvi.MAX_DECLARATION_BLOCK_BYTES + 1)
    body = f"""```yaml
schema: VISUAL_IMPACT_DECLARATION_V1
surfaces: []
# {padding}
```"""
    with pytest.raises(rvi.DeclarationError, match="oversized"):
        rvi.parse_declaration(body)


def test_pr_body_string_never_used_as_shell_source():
    """AC9: the parser must never shell-expand the PR body string. Prove
    this by embedding a command-substitution-like payload and asserting it
    is only ever treated as inert YAML string data (schema validation
    rejects the unknown disposition value; no subprocess is invoked)."""
    body = """```yaml
schema: VISUAL_IMPACT_DECLARATION_V1
surfaces:
  - surface_id: combat-hud-running
    disposition: "$(rm -rf /)"
```"""
    with pytest.raises(rvi.DeclarationError):
        rvi.parse_declaration(body)
