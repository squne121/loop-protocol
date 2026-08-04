"""Static contract-drift test for the Codex bridge surfaces of pr-reviewer /
pr-review-judge (#1744, AC10).

Verifies that `.agents/skills/pr-review-judge/SKILL.md` (Codex bridge) and
`.codex/agents/pr-reviewer.toml` do not contradict the controlled publisher
contract (GitHub posting is owned by the trusted orchestrator / control-plane,
never by the reviewing agent itself) and the current `LOOP_VERDICT` minimal
convention (`verdict` / `reviewed_head_sha` / `blockers` / `warnings`).
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
CANONICAL_SKILL_PATH = REPO_ROOT / ".claude" / "skills" / "pr-review-judge" / "SKILL.md"
BRIDGE_SKILL_PATH = REPO_ROOT / ".agents" / "skills" / "pr-review-judge" / "SKILL.md"
CODEX_TOML_PATH = REPO_ROOT / ".codex" / "agents" / "pr-reviewer.toml"

_MINIMAL_CONVENTION_FIELDS = ["verdict", "reviewed_head_sha", "blockers", "warnings"]

_FORBIDDEN_SELF_MUTATION_SUBSTRINGS = [
    "本 agent が `gh pr review`",
    "本 agent が `gh issue edit`",
]


def test_bridge_skill_is_symlinked_to_canonical_and_free_of_drift() -> None:
    """GIVEN the Codex bridge SKILL.md
    WHEN resolved
    THEN it resolves to (or is byte-identical to) the canonical
      pr-review-judge SKILL.md, so bridge/canonical drift is structurally
      impossible."""
    assert BRIDGE_SKILL_PATH.is_file()
    resolved = BRIDGE_SKILL_PATH.resolve()
    if resolved == CANONICAL_SKILL_PATH.resolve():
        return
    # Fallback: if not a symlink in this checkout mode, content must match.
    assert BRIDGE_SKILL_PATH.read_text(encoding="utf-8") == CANONICAL_SKILL_PATH.read_text(
        encoding="utf-8"
    )


def test_bridge_skill_matches_minimal_convention_and_publisher_contract() -> None:
    """GIVEN the Codex bridge SKILL.md content
    WHEN scanned
    THEN it documents the LOOP_VERDICT minimal convention fields and states
      that raw `gh pr review` is not called directly by the reviewing
      agent."""
    text = BRIDGE_SKILL_PATH.read_text(encoding="utf-8")
    for field in _MINIMAL_CONVENTION_FIELDS:
        assert f"`{field}`" in text, f"missing minimal convention field: {field}"
    assert "生の `gh pr review` を直接呼び出してはならない" in text


def test_bridge_description_matches_controlled_publisher_contract() -> None:
    """GIVEN `.codex/agents/pr-reviewer.toml`
    WHEN scanned
    THEN it explicitly states the controlled publisher contract (GitHub
      posting owned by trusted orchestrator / control-plane, not by this
      agent), matches the LOOP_VERDICT minimal convention fields, and does
      not claim self-mutation via `gh pr review` / `gh issue edit`."""
    text = CODEX_TOML_PATH.read_text(encoding="utf-8")

    for field in _MINIMAL_CONVENTION_FIELDS:
        assert f"`{field}`" in text, f"missing minimal convention field: {field}"

    assert "CONTROLLED_PUBLISHER_CONTRACT" in text
    assert "trusted orchestrator" in text or "control-plane" in text
    assert "gh pr review" in text
    assert "gh issue edit" in text
    assert "本agent自身が行わない" in text.replace(" ", "") or "本 agent 自身が行わない" in text

    for forbidden in _FORBIDDEN_SELF_MUTATION_SUBSTRINGS:
        assert forbidden not in text, f"toml wrongly claims self-mutation: {forbidden!r}"


def test_codex_toml_does_not_self_report_prohibited_fields() -> None:
    """GIVEN `.codex/agents/pr-reviewer.toml`
    WHEN scanned
    THEN it explicitly disclaims self-reporting the retired fields
      (`merge_ready`, `mergeability`, `required_auto_actions`,
      `allowed_paths_gate`), matching the pr-review-judge Output Contract."""
    text = CODEX_TOML_PATH.read_text(encoding="utf-8")
    for retired_field in (
        "merge_ready",
        "mergeability",
        "required_auto_actions",
        "allowed_paths_gate",
    ):
        assert f"`{retired_field}`" in text, f"missing retired-field disclaimer: {retired_field}"
