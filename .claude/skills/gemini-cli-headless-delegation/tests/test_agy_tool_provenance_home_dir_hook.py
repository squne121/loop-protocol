"""Tests for the `home_dir` argument of `generate_workspace_hook_config()` (Issue #1768).

Covers AC2: writing the workspace-scoped hooks.json to the canonical
`<home_dir>/.gemini/config/hooks.json` path -- the path the installed Antigravity
CLI (1.1.7) actually discovers in headless print mode, per live investigation
recorded in `references/grounded-research-isolated-workspace-investigation.md`
-- while refusing (fail-closed) to write there when `home_dir` resolves to the
real host `$HOME`.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "agy_tool_provenance.py"
_MODULE_NAME = "agy_tool_provenance_1768_home_dir_hook_test"


def _load_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SCRIPT_PATH)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


prov = _load_module()


def test_generate_workspace_hook_config_home_dir_writes_canonical_path(tmp_path):
    """AC2: when `home_dir` is given, the identical hooks.json content is also written
    to `<home_dir>/.gemini/config/hooks.json` (the path AGY 1.1.7 actually discovers)."""
    workspace_dir = tmp_path / "workspace"
    home_dir = tmp_path / "isolated-home"
    hook_log_path = workspace_dir / "_provenance" / "hook_events.jsonl"
    hook_context_path = workspace_dir / "_provenance" / "hook_context.json"

    prov.generate_workspace_hook_config(
        workspace_dir,
        hook_log_path=hook_log_path,
        hook_context_path=hook_context_path,
        home_dir=home_dir,
    )

    canonical_path = home_dir / ".gemini" / "config" / "hooks.json"
    assert canonical_path.exists()
    canonical_content = json.loads(canonical_path.read_text())

    workspace_path = workspace_dir / ".agents" / "hooks.json"
    assert workspace_path.exists()
    workspace_content = json.loads(workspace_path.read_text())

    assert canonical_content == workspace_content
    assert "agy-tool-provenance" in canonical_content


def test_generate_workspace_hook_config_no_home_dir_skips_canonical_write(tmp_path):
    """Without `home_dir`, only the existing `<workspace_dir>/.agents/hooks.json` write
    happens (back-compat with existing callers/tests)."""
    workspace_dir = tmp_path / "workspace"
    hook_log_path = workspace_dir / "_provenance" / "hook_events.jsonl"
    hook_context_path = workspace_dir / "_provenance" / "hook_context.json"

    prov.generate_workspace_hook_config(
        workspace_dir,
        hook_log_path=hook_log_path,
        hook_context_path=hook_context_path,
    )

    assert (workspace_dir / ".agents" / "hooks.json").exists()
    assert not (tmp_path / ".gemini").exists()


def test_generate_workspace_hook_config_home_dir_refuses_real_host_home(tmp_path, monkeypatch):
    """AC2: `home_dir` resolving to the real host `$HOME` is refused fail-closed --
    this function must never overwrite a developer's real, shared Antigravity hooks
    configuration."""
    real_home = tmp_path / "real-host-home"
    real_home.mkdir()
    monkeypatch.setenv("HOME", str(real_home))

    workspace_dir = tmp_path / "workspace"
    hook_log_path = workspace_dir / "_provenance" / "hook_events.jsonl"
    hook_context_path = workspace_dir / "_provenance" / "hook_context.json"

    with pytest.raises(prov.ProvenanceWorkspaceHookError):
        prov.generate_workspace_hook_config(
            workspace_dir,
            hook_log_path=hook_log_path,
            hook_context_path=hook_context_path,
            home_dir=real_home,
        )

    # No hooks.json must have been written under the real host HOME.
    assert not (real_home / ".gemini" / "config" / "hooks.json").exists()


def test_generate_workspace_hook_config_home_dir_refuses_real_host_home_via_symlink(tmp_path, monkeypatch):
    """The real-host-home refusal is based on resolved (symlink-following) paths, not
    literal string equality, so a symlink alias for the real HOME is also refused."""
    real_home = tmp_path / "real-host-home"
    real_home.mkdir()
    monkeypatch.setenv("HOME", str(real_home))

    alias = tmp_path / "alias-to-real-home"
    alias.symlink_to(real_home)

    workspace_dir = tmp_path / "workspace"
    hook_log_path = workspace_dir / "_provenance" / "hook_events.jsonl"
    hook_context_path = workspace_dir / "_provenance" / "hook_context.json"

    with pytest.raises(prov.ProvenanceWorkspaceHookError):
        prov.generate_workspace_hook_config(
            workspace_dir,
            hook_log_path=hook_log_path,
            hook_context_path=hook_context_path,
            home_dir=alias,
        )
