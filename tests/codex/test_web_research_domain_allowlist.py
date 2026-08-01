"""web-researcher domain allowlist regression tests (Issue #1924).

`.codex/config.toml`'s `loop-protocol-web-research` profile (web-researcher
only) previously lacked `docs.github.com` / `raw.githubusercontent.com` in
its `network.domains` allowlist, even though those domains are exactly the
kind of official documentation / spec source the agent is meant to fact
check against. This module verifies (via strict `tomllib` parsing, not
grep/regex) that:

- AC1/AC2: the two domains were added to `loop-protocol-web-research` with
  the value `"allow"`.
- AC3: the shared `loop-protocol-readonly` profile (used by 7 other agents)
  was NOT touched by this change (negative invariant).
- AC5: `.codex/agents/web-researcher.toml`'s `OUTPUT_CONTRACT` section was
  NOT extended with `research_route` / `selected_provider` /
  `fallback_reason` keys, which remain out of scope for this Issue and are
  owned by Issue #1886 instead.

AC4 (`NETWORK_ENFORCEMENT_BOUNDARY` content correctness, fix_delta for PR
#1937 OWNER REQUEST_CHANGES P2-1) is verified semantically, via `tomllib`
parsing of the isolated `NETWORK_ENFORCEMENT_BOUNDARY` section string, not
just a heading-only substring/grep check. A heading-only check would let CI
stay green even if the section body were gutted or made internally
inconsistent with the runtime smoke it describes. The semantic checks
verify that the section:

- mentions `web__run` (the model-hosted web browsing route).
- distinguishes the local subprocess proxy egress route (`exec_command` /
  subprocess) from the model-hosted `web__run` route as two separate paths.
- does not describe `network.domains` as a comprehensive security boundary
  that governs every outbound route the agent can use.
- does NOT claim that `network.mode = "full"` bypasses / disables the
  domain allowlist (negative invariant on the corrected P1-1 misstatement
  from the original PR body).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_TOML = REPO_ROOT / ".codex" / "config.toml"
WEB_RESEARCHER_TOML = REPO_ROOT / ".codex" / "agents" / "web-researcher.toml"

WEB_RESEARCH_PROFILE = "loop-protocol-web-research"
READONLY_PROFILE = "loop-protocol-readonly"

NEW_DOMAINS = ("docs.github.com", "raw.githubusercontent.com")


def _load_config() -> dict:
    with CONFIG_TOML.open("rb") as fh:
        return tomllib.load(fh)


def _load_web_researcher_agent() -> dict:
    with WEB_RESEARCHER_TOML.open("rb") as fh:
        return tomllib.load(fh)


def _network_enforcement_boundary_section() -> str:
    """Isolate the `NETWORK_ENFORCEMENT_BOUNDARY` section body from
    web-researcher.toml's developer_instructions, from the heading up to
    (but excluding) the next top-level heading (`Known limitation`)."""
    agent = _load_web_researcher_agent()
    instructions = agent["developer_instructions"]

    start = instructions.index("NETWORK_ENFORCEMENT_BOUNDARY")
    end = instructions.index("Known limitation", start)
    return instructions[start:end]


def test_web_research_profile_has_docs_github_domain() -> None:
    """GIVEN the loop-protocol-web-research profile
    WHEN network.domains is parsed with tomllib
    THEN docs.github.com is present with value "allow"."""
    config = _load_config()
    domains = config["permissions"][WEB_RESEARCH_PROFILE]["network"]["domains"]
    assert domains["docs.github.com"] == "allow"


def test_web_research_profile_has_raw_githubusercontent_domain() -> None:
    """GIVEN the loop-protocol-web-research profile
    WHEN network.domains is parsed with tomllib
    THEN raw.githubusercontent.com is present with value "allow"."""
    config = _load_config()
    domains = config["permissions"][WEB_RESEARCH_PROFILE]["network"]["domains"]
    assert domains["raw.githubusercontent.com"] == "allow"


def test_readonly_profile_domains_unchanged() -> None:
    """GIVEN the shared loop-protocol-readonly profile (7 other agents)
    WHEN network.domains is parsed with tomllib
    THEN neither docs.github.com nor raw.githubusercontent.com was added
    (negative invariant: this Issue scopes the widened allowlist to
    web-researcher only)."""
    config = _load_config()
    domains = config["permissions"][READONLY_PROFILE]["network"]["domains"]
    for domain in NEW_DOMAINS:
        assert domain not in domains, (
            f"{domain} must not be added to {READONLY_PROFILE} "
            "(shared by 7 non-web-researcher agents); Issue #1924 scopes "
            f"the allowlist widening to {WEB_RESEARCH_PROFILE} only"
        )


def test_web_researcher_output_contract_unchanged() -> None:
    """GIVEN web-researcher.toml's developer_instructions
    WHEN the OUTPUT_CONTRACT section is isolated
    THEN it does not contain research_route / selected_provider /
    fallback_reason keys (negative invariant: those fields are Issue
    #1886's scope, not this Issue's)."""
    agent = _load_web_researcher_agent()
    instructions = agent["developer_instructions"]

    start = instructions.index("OUTPUT_CONTRACT")
    end = instructions.index("EXECUTION_POLICY", start)
    output_contract_section = instructions[start:end]

    forbidden_keys = ("research_route", "selected_provider", "fallback_reason")
    for key in forbidden_keys:
        assert key not in output_contract_section, (
            f"{key} must not appear in OUTPUT_CONTRACT; it is owned by "
            "Issue #1886 (provider metadata), out of scope for Issue #1924"
        )


def test_network_enforcement_boundary_mentions_web_run() -> None:
    """GIVEN the NETWORK_ENFORCEMENT_BOUNDARY section
    WHEN it is isolated from developer_instructions
    THEN it explicitly names the `web__run` model-hosted web route (AC4,
    semantic check; a heading-only grep cannot detect this content being
    silently removed)."""
    section = _network_enforcement_boundary_section()
    assert "web__run" in section


def test_network_enforcement_boundary_distinguishes_local_proxy_from_model_hosted_web() -> None:
    """GIVEN the NETWORK_ENFORCEMENT_BOUNDARY section
    WHEN it is isolated from developer_instructions
    THEN it names both the local subprocess proxy egress route
    (`exec_command`) and the model-hosted `web__run` route, and states that
    they are separate/independent routes (AC4)."""
    section = _network_enforcement_boundary_section()
    assert "exec_command" in section
    assert "web__run" in section
    separateness_markers = ("別経路", "独立に検証")
    assert any(marker in section for marker in separateness_markers), (
        "NETWORK_ENFORCEMENT_BOUNDARY must state that the local subprocess "
        "proxy egress route and the model-hosted web__run route are "
        "separate/independent, not just mention both terms in isolation"
    )


def test_network_enforcement_boundary_does_not_treat_domains_as_comprehensive_boundary() -> None:
    """GIVEN the NETWORK_ENFORCEMENT_BOUNDARY section
    WHEN it is isolated from developer_instructions
    THEN it explicitly disclaims `network.domains` as a comprehensive
    security boundary over every outbound route the agent can use (AC4)."""
    section = _network_enforcement_boundary_section()
    assert "network.domains" in section
    assert "security boundary" in section
    assert "扱わない" in section


def test_network_enforcement_boundary_does_not_claim_mode_full_bypasses_allowlist() -> None:
    """GIVEN the NETWORK_ENFORCEMENT_BOUNDARY section
    WHEN it is isolated from developer_instructions
    THEN it does NOT claim that `network.mode = "full"` bypasses/disables
    the domain allowlist (negative invariant; PR #1937 OWNER
    REQUEST_CHANGES P1-1: `mode = "full"` is an HTTP method policy, not a
    domain allowlist bypass, and this misstatement must not be persisted
    as agent instruction)."""
    section = _network_enforcement_boundary_section()
    # Only flags the *causal, non-negated* claim ("mode=full" CAUSES the
    # allowlist to be bypassed/disabled). A sentence that quotes that
    # erroneous claim purely in order to reject it (e.g. "...という説明は
    # 誤りであり..." / "...ではない") must NOT trip this check, so any match
    # is discarded if a negation/correction marker appears shortly after it.
    causal_markers = ("のため", "によって", "により", "から")
    bypass_markers = ("バイパス", "bypass", "無効化")
    negation_markers = ("誤り", "ではない", "でない", "しない", "採用しない")
    forbidden_pattern = re.compile(
        r"mode\s*=?\s*[\"']?full[\"']?[^\n]{0,40}("
        + "|".join(causal_markers)
        + r")[^\n]{0,40}(" + "|".join(bypass_markers) + r")"
        r"|(" + "|".join(bypass_markers) + r")[^\n]{0,40}("
        + "|".join(causal_markers) + r")[^\n]{0,40}mode\s*=?\s*[\"']?full[\"']?",
        re.IGNORECASE,
    )
    violation = None
    for match in forbidden_pattern.finditer(section):
        lookahead = section[match.end() : match.end() + 30]
        if any(marker in lookahead for marker in negation_markers):
            continue
        violation = match
        break
    assert violation is None, (
        "NETWORK_ENFORCEMENT_BOUNDARY must not claim mode=\"full\" "
        f"bypasses/disables the domain allowlist; matched: {violation!r}"
    )
