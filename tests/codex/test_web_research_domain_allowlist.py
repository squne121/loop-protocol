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

AC4 (the `NETWORK_ENFORCEMENT_BOUNDARY` heading + `web__run` boundary
language in `web-researcher.toml`) is verified via a plain substring check
against the `developer_instructions` TOML string value, per the
Verification Commands in the Issue contract (`grep -n
"NETWORK_ENFORCEMENT_BOUNDARY" .codex/agents/web-researcher.toml`).
"""

from __future__ import annotations

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
