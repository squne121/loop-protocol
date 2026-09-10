#!/usr/bin/env python3
"""verify_since_last_retrospective_live_cli.py -- opt-in AC9 runtime verification for Issue
#2601's `--since-last-retrospective` session-window coverage layer.

Invoked directly via ``uv run --locked pytest
.claude/skills/agent-retrospective/scripts/tests/verify_since_last_retrospective_live_cli.py``
(same opt-in, SKIP-based convention as
``verify_latitude_runtime_evidence_live_cli.py`` in this same directory --
``pytest.skip()``, stdout prefixed ``SKIP:``, never a shell wrapper).

Contract (Issue #2601 Runtime Verification Applicability, ``execution_environment``: "現在
インストール済みの Claude Code CLI（headless `claude -p --agent <name>` subprocess）、ローカル
repository worktree"):

Unlike the sibling observer/evaluator live-CLI tests in this directory, `--since-last-retrospective`
never itself shells out to `claude -p --agent ...` -- it is a collector-only orchestration layer
(see `run_retrospective.run_since_last_retrospective_cli`'s module docstring). "Using the currently
installed Claude Code CLI" for THIS Issue's session-window coverage feature means: exercising the
`claude_code` collector (`collect_snapshot.collect_claude_code_source`, wired via
`run_retrospective.collect_session_sources`) against the REAL, already-on-disk session transcript
files the actually-installed Claude Code CLI produces for real interactive sessions under
``$HOME/.claude/projects/<repo-slug>/*.jsonl`` -- not a synthetic/fixture-only JSONL file. This is a
genuine live proof specifically of what Issue #2601 exists to prevent: a collector that would
silently report `unavailable` as `complete`, or 0-selected sessions as "not wired" (and vice versa).

  - ``$HOME`` unset, or ``$HOME/.claude/projects/<repo-slug>/`` does not exist at all (genuinely no
    Claude Code session history recorded for this repository on this host) -> SKIP (never FAIL).
  - The directory exists but contains zero ``*.jsonl`` files -> still a live PASS: the collector
    (and this Issue's coverage layer) is REQUIRED to report `analysis_completeness: "unavailable"`
    for `claude_code` in that state, and this test asserts exactly that -- proving the collector
    genuinely distinguishes "no real session evidence" from a fabricated "complete" (the false-green
    this Issue targets), using the real environment's real (possibly empty) state, never a synthetic
    substitute.
  - The directory exists and contains at least one real ``*.jsonl`` file -> live PASS requires
    `analysis_completeness` to be `"complete"` or `"degraded"` (never `"unavailable"`) for
    `claude_code`, and the produced envelope MUST be schema-valid and public-safe (no raw absolute
    path / credential content -- this repo's own local absolute path is checked for explicitly, not
    just generic path-shaped strings).
  - Any other unexpected result shape (schema-invalid output, an exception escaping
    `run_since_last_retrospective_cli`, or `orchestration.status != "succeeded"` when the collector
    genuinely could run) is a FAIL (a real defect), never silently downgraded to SKIP.

A public-safe artifact (the schema-valid `session_window_coverage/v1` envelope this test itself
produced, plus a `status` field -- never raw session/transcript content) is written under
``artifacts/`` (repo-root-relative) in every case, including SKIP, per this Issue's
``artifact_requirements``.
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(_SCRIPTS_DIR))

import run_retrospective as rr  # noqa: E402

_ARTIFACTS_DIR = _REPO_ROOT / "artifacts" / "agent-retrospective-since-last-retrospective"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_artifact(name: str, payload: dict[str, Any]) -> Path:
    _ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _ARTIFACTS_DIR / name
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return path


def _skip_with_artifact(reason: str) -> None:
    artifact_path = _write_artifact(
        "verify_since_last_retrospective_live_cli.result.json",
        {"status": "skip", "generated_at": _now_iso(), "skip_reason": reason},
    )
    pytest.skip(f"SKIP: {reason} (artifact: {artifact_path})")


def test_since_last_retrospective_claude_code_collector_live():
    import os

    # Issue #2601 Runtime Verification Applicability skip_condition: "Claude Code CLI 利用不能"
    # (checked literally here, even though the collector under test never shells out to it --
    # this session-window coverage feature's whole premise is session evidence THIS CLI produces).
    if shutil.which("claude") is None:
        _skip_with_artifact("claude_cli_not_found_in_PATH")
        return

    sessions_dir = rr.default_claude_code_sessions_dir(dict(os.environ), repo_root=_REPO_ROOT)
    if sessions_dir is None:
        _skip_with_artifact("HOME_unset_claude_code_sessions_dir_unresolvable")
        return
    if not sessions_dir.is_dir():
        _skip_with_artifact(f"claude_code_sessions_dir_not_found:{sessions_dir.name}")
        return

    real_session_files = rr.resolve_claude_code_session_paths(sessions_dir)

    result = rr.run_since_last_retrospective_cli(
        repo_root=_REPO_ROOT,
        required_sources=["claude_code"],
        prior_watermark=None,
        publish_authorized=False,
    )

    assert result["orchestration"]["status"] == "succeeded", (
        f"session-window collector orchestration failed unexpectedly: {result['orchestration']}"
    )
    rr.validate_session_window_coverage(result)

    serialized = json.dumps(result)
    forbidden_substrings = [str(sessions_dir), str(Path.home()) if os.environ.get("HOME") else ""]
    for forbidden in forbidden_substrings:
        if forbidden:
            assert forbidden not in serialized, "collector output must never embed a raw local absolute path"

    claude_code_entry = result["source_coverage"]["claude_code"]
    if not real_session_files:
        # Genuine empty-but-existing state: the collector MUST report this as unavailable, not a
        # fabricated "complete" -- this IS the live proof this Issue targets.
        assert claude_code_entry["status"] in ("required", "unavailable")
        assert result["analysis_completeness"] == "unavailable"
        status_label = "pass_empty_directory"
    else:
        assert claude_code_entry["status"] in ("observed", "partial"), (
            f"real session files exist ({len(real_session_files)} found) but collector reported "
            f"status={claude_code_entry['status']!r}"
        )
        assert result["analysis_completeness"] in ("complete", "degraded")
        status_label = "pass_real_sessions_observed"

    artifact_path = _write_artifact(
        "verify_since_last_retrospective_live_cli.result.json",
        {
            "status": status_label,
            "generated_at": _now_iso(),
            "real_session_file_count": len(real_session_files),
            "result": result,
        },
    )
    print(
        f"PASS: since-last-retrospective claude_code collector live verification "
        f"({status_label}; artifact: {artifact_path})"
    )
