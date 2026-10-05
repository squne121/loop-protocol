"""Issue #2938: Claude-GPT launcher 縮退（#2925 / PR #2932）で撤去された surface を参照する
consumer・文書が、現行の check-only receipt（`connected_server`）を authority とする内容へ
移行されたことを固定する static regression test。

stale concept の literal 不在は下限であり、文書の完了条件ではない（意味論は current code
`scripts/claude-gpt/{launch,lib,preflight}.sh` と照合して再記述している）。この file 自体は
旧 literal を検査対象として含むため、AC1 の `rg` 検査の対象外である。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]

BODY_AUTHORING = REPO_ROOT / ".claude/skills/create-issue/references/body-authoring.md"
EVIDENCE_POLICY = REPO_ROOT / ".claude/skills/pr-review-judge/references/evidence-policy.md"
PASSTHROUGH_SCRIPT = (
    REPO_ROOT / ".claude/skills/agent-retrospective/scripts/tests/verify_claude_gpt_transport_passthrough.sh"
)
LIVE_SMOKE = REPO_ROOT / ".claude/skills/agent-retrospective/scripts/tests/verify_agent_retrospective_live_smoke.py"
SECURITY_BOUNDARY_TEST = REPO_ROOT / ".claude/skills/agent-retrospective/scripts/tests/test_security_boundary.py"
CAPABILITY_GAPS = REPO_ROOT / "docs/dev/agent-capability-gaps.md"
RUNTIME_POLICY = REPO_ROOT / "docs/dev/runtime-verification-policy.md"
SECRET_POLICY = REPO_ROOT / "docs/dev/secret-policy.md"


def _read(path: Path) -> str:
    assert path.is_file(), f"expected file not found: {path}"
    return path.read_text(encoding="utf-8")


def _assert_absent(text: str, literals: list[str], label: str) -> None:
    present = [literal for literal in literals if literal in text]
    assert not present, f"{label}: stale literal(s) still present: {present}"


def _runtime_policy_section_12(text: str) -> str:
    match = re.search(r"(?ms)^## 12\. .*?(?=^## \d+\. |\Z)", text)
    assert match is not None, "runtime-verification-policy.md section 12 heading not found"
    return match.group(0)


def test_ac2_evidence_docs_authority_migration():
    stale = [
        "SMOKE_RESULT_V1.proxy",
        ".proxy.absolute_path",
        ".proxy.version",
        "selected proxy",
        "proxy_model_catalog_incompatible",
        "proxy-selection",
        "proxy identity",
        "fixture proxy",
        "通常の binary resolution",
        "fake proxy",
        "binary resolution",
    ]
    for path in (BODY_AUTHORING, EVIDENCE_POLICY):
        text = _read(path)
        _assert_absent(text, stale, path.name)
        assert re.search(r"(?s)connected_server.{0,600}local_proxy_binary_auxiliary.{0,600}未確認", text), (
            f"{path.name}: connected_server / local_proxy_binary_auxiliary / 未確認 must be described in proximity"
        )
        assert "connected_server_model_catalog_incomplete" in text
        assert "connected-server AC" in text
        # `local_proxy_binary_auxiliary` is a non-authority auxiliary diagnostic.
        assert re.search(r"(?s)local_proxy_binary_auxiliary.{0,200}非 authority", text)
        # the sut mapping table is retained.
        for kept in ("sut.git_head", "sut.git_dirty", "sut.launch_sh_sha256"):
            assert kept in text, f"{path.name}: {kept} mapping must be kept"
        # fixture semantics AC / canonical runtime AC separation is retained.
        assert "fixture" in text and "canonical" in text


def test_ac3_passthrough_script_deleted():
    assert not PASSTHROUGH_SCRIPT.exists()
    text = _read(CAPABILITY_GAPS)
    assert (
        "bash .claude/skills/agent-retrospective/scripts/tests/verify_claude_gpt_transport_passthrough.sh"
        not in text
    )


def test_ac4_live_smoke_has_no_removed_launcher_surface():
    live_smoke = _read(LIVE_SMOKE)
    _assert_absent(
        live_smoke,
        [
            "CLAUDE_GPT_RUNTIME_SMOKE_HOOKS",
            "claude_gpt_proxy_sidechannel",
            "claude_gpt_proxy_cleanup_independent",
            'str(launcher), "-C"',
        ],
        LIVE_SMOKE.name,
    )
    # the launcher is invoked alone (no `-C <repo_root>` prefix); cwd is passed to subprocess.run.
    assert "[str(launcher)]" in live_smoke
    assert "cwd=str(repo_root)" in live_smoke
    # the launcher receipt recording is retained.
    assert "claude_gpt_launcher_receipt" in live_smoke
    _assert_absent(
        _read(SECURITY_BOUNDARY_TEST),
        ["claude_gpt_proxy_sidechannel", "claude_gpt_proxy_cleanup_independent"],
        SECURITY_BOUNDARY_TEST.name,
    )
    assert "claude_gpt_launcher_receipt" in _read(SECURITY_BOUNDARY_TEST)


def test_ac5_docs_stale_concepts_absent():
    _assert_absent(
        _read(CAPABILITY_GAPS),
        [
            "chatgpt_auth",
            "proxy.absolute_path",
            "proxy.version",
            "canonical_paths.ok",
            "read_restriction.ok",
            "claude_gpt_build_proxy_env",
        ],
        CAPABILITY_GAPS.name,
    )
    runtime_policy = _read(RUNTIME_POLICY)
    _assert_absent(
        runtime_policy,
        ["home_source", "chatgpt_auth.available", "chatgpt_auth.detail", "not_authenticated"],
        RUNTIME_POLICY.name,
    )
    _assert_absent(
        _read(SECRET_POLICY),
        [
            "claude_gpt_home_config_mcp_plugin_isolation_not_relaxed",
            "空の隔離ディレクトリ",
            "引き続き scrub",
        ],
        SECRET_POLICY.name,
    )
    _assert_absent(
        _runtime_policy_section_12(runtime_policy),
        ["未認証の namespace", "必ず一致"],
        f"{RUNTIME_POLICY.name} section 12",
    )


def test_ac6_docs_current_concepts_present():
    assert re.search(r"(?s)connected_server.{0,400}model_catalog_ok", _read(CAPABILITY_GAPS))
    # version of the connected server is not observable.
    assert re.search(r"(?s)connected_server.{0,1200}未確認", _read(CAPABILITY_GAPS))

    runtime_policy = _read(RUNTIME_POLICY)
    assert re.search(
        r"(?s)connected_server.{0,400}model_catalog_ok.{0,600}実 ChatGPT subscription request",
        runtime_policy,
    )
    section_12 = _runtime_policy_section_12(runtime_policy)
    assert re.search(r"(?s)CLAUDE_GPT_HOME.{0,300}補助 binary", section_12)
    # `CLAUDE_GPT_HOME` is explicitly not a credential namespace.
    assert re.search(r"(?s)CLAUDE_GPT_HOME.{0,600}credential namespace ではない", section_12)

    secret_policy = _read(SECRET_POLICY)
    assert re.search(r"(?s)ambient.{0,600}ANTHROPIC_AUTH_TOKEN.{0,200}placeholder", secret_policy)
    # the launcher neither scrubs nor unsets these ambient values.
    assert re.search(r"(?s)SSH_AUTH_SOCK.{0,200}scrub も unset もせず", secret_policy)
    # the #2426 supersession record carries the #2925 superseded record.
    assert re.search(
        r"(?s)owner_local_observation_supersession_v1:.{0,1500}retained_constraints:.{0,800}2925",
        secret_policy,
    )
    # adjacent constraints and the #2426 authorization are unchanged.
    assert "native_claude_settings_not_full_config_authority_for_claude_gpt" in secret_policy
    assert 'authorized_by: "#2426 owner decision (2026-08-30)"' in secret_policy
    # docs/dev/agent-observation-capability.md still points at the record.
    observation = _read(REPO_ROOT / "docs/dev/agent-observation-capability.md")
    assert "owner_local_observation_supersession_v1" in observation
