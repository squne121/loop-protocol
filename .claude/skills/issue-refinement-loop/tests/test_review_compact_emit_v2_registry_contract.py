"""
test_review_compact_emit_v2_registry_contract.py

Issue #1541 AC9: `review_compact.emit_v2` command registry entry contract.
Fixes the emitter's argv, `shell: False`, `uv run --locked --offline
--no-sync` execution profile, and `local-only` / `mutation: False`
declarations -- the same registry-integration pattern already established
for `parent_replay.bind` / `review_compact.validate_v2` (Issue #1532).
"""

from __future__ import annotations

import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import command_registry as cr  # noqa: E402


def test_review_compact_emit_v2_registry_contract():
    assert "review_compact.emit_v2" in cr.REGISTRY
    entry = cr.REGISTRY["review_compact.emit_v2"]

    assert entry["id"] == "review_compact.emit_v2"
    assert entry["shell"] is False
    assert entry["mutation"] is False
    assert entry["network_effect"] == "local_only"
    assert entry["cwd_policy"] == "repo_root"

    argv = entry["argv"]
    assert argv[:6] == ["uv", "run", "--locked", "--offline", "--no-sync", "python3"]
    assert argv[6].endswith("emit_parent_review_envelope_v2.py")
    assert "--issue-number" in argv
    assert "--binding-artifact-file" in argv
    assert "--repository-full-name" in argv
    assert "--refinement-session-id" in argv
    assert "--iteration-id" in argv
    assert "--current-body-file" in argv

    required_placeholders = {
        "issue_number",
        "binding_artifact_file",
        "repo",
        "refinement_session_id",
        "iteration_id",
        "current_body_file",
    }
    placeholders = entry["placeholders"]
    assert required_placeholders.issubset(set(placeholders.keys()))
    for name in required_placeholders:
        assert placeholders[name]["required"] is True

    # render_command() must produce a full argv (never a shell string), with
    # no unresolved placeholders and no shell metacharacters.
    rendered = cr.render_command(
        "review_compact.emit_v2",
        {
            "issue_number": 1541,
            "binding_artifact_file": ".claude/artifacts/issue-refinement-loop/1541/binding.json",
            "repo": "squne121/loop-protocol",
            "refinement_session_id": "session-1541",
            "iteration_id": "iteration-1541",
            "current_body_file": ".claude/artifacts/issue-refinement-loop/1541/body.txt",
        },
    )
    assert isinstance(rendered, list)
    assert all(isinstance(token, str) for token in rendered)
    assert not any(token.startswith("{") and token.endswith("}") for token in rendered)
    assert "1541" in rendered
    assert "squne121/loop-protocol" in rendered

    # Registry export round-trips through the CLI-facing JSON shape too.
    exported = cr.export_registry()
    assert exported["schema"] == cr.SCHEMA_VERSION
    assert "review_compact.emit_v2" in exported["commands"]

    # Placeholder validation is fail-closed (AC3 parity): a non-owner_repo
    # value for `repo` must raise, never silently render.
    try:
        cr.render_command(
            "review_compact.emit_v2",
            {
                "issue_number": 1541,
                "binding_artifact_file": ".claude/artifacts/issue-refinement-loop/1541/binding.json",
                "repo": "not-an-owner-repo-shape",
                "refinement_session_id": "session-1541",
                "iteration_id": "iteration-1541",
                "current_body_file": ".claude/artifacts/issue-refinement-loop/1541/body.txt",
            },
        )
        raise AssertionError("expected ValueError for malformed 'repo' placeholder")
    except ValueError:
        pass

    # Sits between parent_replay.bind and review_compact.validate_v2 in the
    # documented command chain -- both siblings must also still be present
    # (this entry does not replace or remove them).
    assert "parent_replay.bind" in cr.REGISTRY
    assert "review_compact.validate_v2" in cr.REGISTRY
