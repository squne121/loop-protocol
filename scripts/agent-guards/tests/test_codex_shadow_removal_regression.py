"""Issue #1612 AC5: the Codex-specific shadow-mode would-block recording
mechanism (`appendShadowLog()` / `shadowLogPath` / the `CODEX_ALLOWED_PATHS_MODE`
`shadow` branch) must be fully removed from `scripts/check-codex-agents.mjs`,
while `.guard_shadow_log.jsonl` itself and the OTHER producer
(`.claude/hooks/shadow_log.py`, Issue #1563/PR #1572) must continue to write
successfully. This is a regression test, not a new feature test -- it exists
to catch an accidental re-introduction of the Codex shadow writer, or an
accidental breakage of the unrelated shadow_log.py producer / the
`skill_runtime_exec.py` `_SHADOW_LOG_EXACT_REL` consumer policy while removing
it (both explicitly Out of Scope for Issue #1612).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CHECK_SCRIPT = REPO_ROOT / "scripts" / "check-codex-agents.mjs"
SHADOW_LOG_WRITER = REPO_ROOT / ".claude" / "hooks" / "shadow_log.py"

_GUARDS_DIR = Path(__file__).resolve().parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

import skill_runtime_exec  # noqa: E402


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
    effect."""
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


def test_other_shadow_log_producer_still_writes_successfully(tmp_path) -> None:
    """Regression: the OTHER producer of `.guard_shadow_log.jsonl`
    (`.claude/hooks/shadow_log.py`, Issue #1563/PR #1572) must be completely
    unaffected by removing the Codex-specific shadow writer -- it must still
    be able to append a well-formed JSONL entry successfully."""
    log_file = tmp_path / ".guard_shadow_log.jsonl"
    fields = {"event": "would_block_probe", "source": "test_codex_shadow_removal_regression"}

    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(SHADOW_LOG_WRITER),
            "--log-file",
            str(log_file),
            "--fields-json",
            json.dumps(fields),
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, f"shadow_log.py failed: {result.stdout}\n{result.stderr}"
    assert log_file.exists()

    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["event"] == "would_block_probe"
    assert entry["source"] == "test_codex_shadow_removal_regression"
    assert "schema_version" in entry
    assert "timestamp" in entry


def test_skill_runtime_exec_shadow_log_exact_rel_policy_untouched() -> None:
    """Regression: Issue #1612 must never touch
    `scripts/agent-guards/skill_runtime_exec.py`'s `_SHADOW_LOG_EXACT_REL`
    typed exact-file policy (Out of Scope / Stop Condition)."""
    assert skill_runtime_exec._SHADOW_LOG_EXACT_REL == ".guard_shadow_log.jsonl"
