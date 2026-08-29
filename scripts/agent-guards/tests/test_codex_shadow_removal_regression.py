"""Issue #1612 AC5: the Codex-specific shadow-mode would-block recording
mechanism (`appendShadowLog()` / `shadowLogPath` / the `CODEX_ALLOWED_PATHS_MODE`
`shadow` branch) must be fully removed from `scripts/check-codex-agents.mjs`.

Issue #2252 has since retired `.guard_shadow_log.jsonl` as a repo-local
persistent shadow telemetry mechanism entirely: all production producers
(`.claude/hooks/shadow_log.py`, `.claude/hooks/guard-japanese-prose.sh`'s
persistent shadow logging, `.claude/hooks/rtk_boundary_shadow_guard.sh`) have
been removed, and `scripts/agent-guards/skill_runtime_exec.py` no longer
special-cases `.guard_shadow_log.jsonl` at all -- it is now treated as a
generic repository path subject to the ordinary `unauthorized_write_path`
fail-close policy. This test module therefore no longer asserts anything
about a dedicated shadow-log producer or a typed exact-file consumer policy
(both are gone); it retains only the still-relevant regression coverage for
the Codex-specific writer removal in `scripts/check-codex-agents.mjs`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CHECK_SCRIPT = REPO_ROOT / "scripts" / "check-codex-agents.mjs"

_GUARDS_DIR = Path(__file__).resolve().parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))


def test_check_codex_agents_source_has_no_codex_shadow_writer() -> None:
    """Static regression: none of the Codex-specific shadow-mode tokens may
    appear anywhere in scripts/check-codex-agents.mjs any more."""
    text = CHECK_SCRIPT.read_text(encoding="utf-8")
    forbidden_tokens = [
        "appendShadowLog",
        "shadowLogPath",
        ".guard_shadow_log.jsonl",
        "would_block",
    ]
    for token in forbidden_tokens:
        assert token not in text, (
            f"scripts/check-codex-agents.mjs must not reference {token!r} any more "
            "(Issue #1612 AC5: Codex shadow-mode writer removed)"
        )


def test_check_codex_agents_self_test_does_not_touch_real_shadow_log() -> None:
    """Behavioral regression: running the validator's own self-test suite
    (which exercises many protected-path allow/deny decisions, including
    former shadow-mode-only scenarios) must never write to the real repo's
    `.guard_shadow_log.jsonl`, proving the removed writer has no residual
    effect. This path is now a generic, unauthored repository path (Issue
    #2252): the assertion below simply confirms nothing at all writes to
    it as a side effect of the self-test."""
    real_shadow_log = REPO_ROOT / ".guard_shadow_log.jsonl"
    before_bytes = real_shadow_log.read_bytes() if real_shadow_log.exists() else None

    result = subprocess.run(  # noqa: S603
        ["node", str(CHECK_SCRIPT), "--self-test"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"self-test failed: {result.stdout}\n{result.stderr}"

    after_bytes = real_shadow_log.read_bytes() if real_shadow_log.exists() else None
    assert after_bytes == before_bytes, (
        ".guard_shadow_log.jsonl content changed after running "
        "`node scripts/check-codex-agents.mjs --self-test` -- the removed Codex "
        "shadow writer must have zero residual effect on this shared artifact"
    )
