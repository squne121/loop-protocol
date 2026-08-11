"""Issue #2015 AC13: hermetic tests for ``resolve_serena_mcp.py``'s
priority-ordered resolution (explicit override -> PATH -> user-local managed
install -> exact-pinned uvx fallback). No live ``serena``/``uvx`` process is
ever spawned by these tests -- ``shutil.which`` and the user-local marker
file are monkeypatched/faked.
"""
from __future__ import annotations

import importlib.util
import json
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RESOLVER_PATH = REPO_ROOT / "scripts" / "agent-ops" / "resolve_serena_mcp.py"


def _load_module(path: Path, name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def resolver() -> types.ModuleType:
    return _load_module(RESOLVER_PATH, "test_resolve_serena_mcp_module")


@pytest.fixture()
def manifest_path(tmp_path: Path) -> Path:
    manifest = {
        "schema": "serena_tool_manifest_v1",
        "pinned_ref": "deadbeef" * 5,
        "mcp_command": [
            "uvx",
            "--from",
            f"git+https://github.com/oraios/serena@{'deadbeef' * 5}",
            "serena",
            "start-mcp-server",
            "--project-from-cwd",
        ],
    }
    path = tmp_path / "serena-tool-manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


class TestResolutionPriority:
    def test_env_serena_bin_wins_over_everything(self, resolver, manifest_path, monkeypatch):
        monkeypatch.setattr(resolver.shutil, "which", lambda _name: "/usr/local/bin/serena")
        result = resolver.resolve(
            manifest_path=manifest_path,
            env={"SERENA_BIN": "/opt/custom/serena"},
        )
        assert result["resolution_source"] == resolver.RESOLUTION_SOURCE_ENV_BIN
        assert result["executable"] == "/opt/custom/serena"
        assert result["fallback_used"] is False

    def test_env_serena_mcp_command_json_array_override(self, resolver, manifest_path):
        override = json.dumps(["custom-serena", "start-mcp-server"])
        result = resolver.resolve(
            manifest_path=manifest_path,
            env={"SERENA_MCP_COMMAND": override},
        )
        assert result["resolution_source"] == resolver.RESOLUTION_SOURCE_ENV_COMMAND
        assert result["executable"] == "custom-serena"
        assert "--tool-timeout" in result["effective_argv"]
        assert "--project-from-cwd" in result["effective_argv"]

    def test_malformed_env_command_override_is_ignored_not_crashed(self, resolver, manifest_path, monkeypatch):
        monkeypatch.setattr(resolver.shutil, "which", lambda _name: None)
        monkeypatch.setattr(resolver, "_user_local_install_marker", lambda: Path("/nonexistent/marker"))
        result = resolver.resolve(
            manifest_path=manifest_path,
            env={"SERENA_MCP_COMMAND": "not valid json"},
        )
        # Falls through to the uvx pinned fallback rather than crashing.
        assert result["resolution_source"] == resolver.RESOLUTION_SOURCE_UVX_PINNED

    def test_path_installed_serena_wins_over_user_local_and_uvx(self, resolver, manifest_path, monkeypatch):
        monkeypatch.setattr(resolver.shutil, "which", lambda name: "/usr/bin/serena" if name == "serena" else None)
        result = resolver.resolve(manifest_path=manifest_path, env={})
        assert result["resolution_source"] == resolver.RESOLUTION_SOURCE_PATH
        assert result["executable"] == "/usr/bin/serena"

    def test_user_local_managed_install_used_when_no_path_binary(
        self, resolver, manifest_path, tmp_path, monkeypatch
    ):
        fake_bin = tmp_path / "serena-managed"
        fake_bin.write_text("#!/bin/sh\necho fake\n", encoding="utf-8")
        marker = tmp_path / "marker"
        marker.write_text(str(fake_bin), encoding="utf-8")
        monkeypatch.setattr(resolver.shutil, "which", lambda _name: None)
        monkeypatch.setattr(resolver, "_user_local_install_marker", lambda: marker)
        result = resolver.resolve(manifest_path=manifest_path, env={})
        assert result["resolution_source"] == resolver.RESOLUTION_SOURCE_USER_LOCAL
        assert result["executable"] == str(fake_bin)

    def test_falls_back_to_exact_pinned_uvx_when_nothing_else_resolves(
        self, resolver, manifest_path, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(resolver.shutil, "which", lambda _name: None)
        monkeypatch.setattr(resolver, "_user_local_install_marker", lambda: tmp_path / "absent-marker")
        result = resolver.resolve(manifest_path=manifest_path, env={})
        assert result["resolution_source"] == resolver.RESOLUTION_SOURCE_UVX_PINNED
        assert result["fallback_used"] is True
        assert result["pinned_ref"] == "deadbeef" * 5
        assert any("deadbeef" * 5 in arg for arg in result["effective_argv"])


class TestEffectiveArgvWiring:
    def test_tool_timeout_and_project_from_cwd_always_present(self, resolver, manifest_path, monkeypatch):
        monkeypatch.setattr(resolver.shutil, "which", lambda _name: None)
        monkeypatch.setattr(resolver, "_user_local_install_marker", lambda: Path("/nonexistent"))
        result = resolver.resolve(manifest_path=manifest_path, env={}, tool_timeout_sec=45)
        assert "--tool-timeout" in result["effective_argv"]
        idx = result["effective_argv"].index("--tool-timeout")
        assert result["effective_argv"][idx + 1] == "45"
        assert "--project-from-cwd" in result["effective_argv"]

    def test_does_not_duplicate_project_from_cwd_already_in_manifest(self, resolver, manifest_path, monkeypatch):
        monkeypatch.setattr(resolver.shutil, "which", lambda _name: None)
        monkeypatch.setattr(resolver, "_user_local_install_marker", lambda: Path("/nonexistent"))
        result = resolver.resolve(manifest_path=manifest_path, env={})
        assert result["effective_argv"].count("--project-from-cwd") == 1

    def test_uv_cache_dir_reported_when_present_in_env(self, resolver, manifest_path, monkeypatch):
        monkeypatch.setattr(resolver.shutil, "which", lambda _name: None)
        monkeypatch.setattr(resolver, "_user_local_install_marker", lambda: Path("/nonexistent"))
        result = resolver.resolve(
            manifest_path=manifest_path, env={"UV_CACHE_DIR": "/var/cache/uv-serena"}
        )
        assert result["uv_cache_dir"] == "/var/cache/uv-serena"


class TestPinnedRefIntegrityGuard:
    def test_refuses_uvx_fallback_when_mcp_command_does_not_embed_pinned_ref(
        self, resolver, tmp_path
    ):
        """A manifest whose ``mcp_command`` has drifted away from its own
        declared ``pinned_ref`` must fail closed, never silently launch an
        unpinned/moving-target uvx invocation."""
        manifest = {
            "pinned_ref": "cafebabe" * 5,
            "mcp_command": [
                "uvx", "--from", "git+https://github.com/oraios/serena", "serena",
                "start-mcp-server", "--project-from-cwd",
            ],
        }
        path = tmp_path / "drifted-manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(ValueError, match="pinned_ref"):
            resolver.resolve(manifest_path=path, env={})


class TestCliReportMode:
    def test_report_mode_prints_json_and_exits_zero(self, resolver, manifest_path, monkeypatch, capsys):
        monkeypatch.setattr(resolver, "MANIFEST_PATH", manifest_path)
        monkeypatch.setattr(resolver.shutil, "which", lambda _name: None)
        monkeypatch.setattr(resolver, "_user_local_install_marker", lambda: Path("/nonexistent"))
        rc = resolver.main(["--report"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["schema"] == "resolve_serena_mcp_v1"
        assert out["resolution_source"] == resolver.RESOLUTION_SOURCE_UVX_PINNED

    def test_install_user_local_failure_is_reported_not_crashed(self, resolver, monkeypatch, capsys):
        monkeypatch.setattr(
            resolver,
            "install_user_local",
            lambda manifest_path=resolver.MANIFEST_PATH: {"ok": False, "error": "network unavailable"},
        )
        rc = resolver.main(["--install-user-local"])
        assert rc == 1
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is False
