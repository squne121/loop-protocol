"""#2842 AC2: URL destinations cannot become repository path literals."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from scope_signal_delta import _extract_path_literals_from_text  # noqa: E402

REAL = "scripts/agent-guards/skill_runtime_exec.py"


@pytest.mark.parametrize("link", [
    "[Claude Code](https://code.claude.com/docs/en/hooks)",
    "[docs](<https://example.org/docs/en/hooks>)",
    "<https://example.org/docs/en/hooks>",
    "[guide]: https://example.org/docs/en/hooks",
    "[guide]: <https://example.org/docs/en/hooks>",
    "https://example.org/docs/en/hooks",
    "http://example.org/docs/en/hooks",
    "www.example.org/docs/en/hooks",
    "[guide](https://example.org/docs/en/(hooks))",
    "https://example.org/docs/en/(hooks)",
])
def test_given_url_destination_when_paths_extracted_then_only_adjacent_repo_paths_remain(link):
    text = f"Allowed Paths に `{REAL}` を追加する。{link} また `{REAL}` を確認する。"
    assert _extract_path_literals_from_text(text) == [REAL]


def test_given_url_only_when_paths_extracted_then_no_fake_literal():
    assert _extract_path_literals_from_text("Allowed Paths: [Claude Code](https://code.claude.com/docs/en/hooks)") == []


def test_given_unsafe_explicit_path_when_paths_extracted_then_mixed_directive_still_rejected():
    assert _extract_path_literals_from_text(f"Allowed Paths: `{REAL}` `../../escape.py`") == []
