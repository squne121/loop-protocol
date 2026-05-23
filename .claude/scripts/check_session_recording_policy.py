#!/usr/bin/env python3
"""Structural checker for session-recording-policy.md (session_recording_policy/v1).

Validates:
1.  Fenced YAML block (```yaml) can be parsed with yaml.safe_load
2.  schema == session_recording_policy/v1
3.  source_of_truth has both secret_policy and manifest_schema keys
4.  All taxonomy_mapping modes have public_full_transcript_allowed: false
5.  kill_switch.required_end_state has all required keys
6.  kill_switch has trigger_conditions (non-empty)
7.  public_surfaces.github_issue_comment.raw_transcript_allowed == false
8.  checkpoint_remote.fail_closed_on_unknown_visibility == true

Usage:
    python3 .claude/scripts/check_session_recording_policy.py docs/dev/session-recording-policy.md

Exit codes:
    0 - all checks passed
    1 - one or more checks failed
    2 - file not found / argument error
"""
from __future__ import annotations

import argparse
import re
import sys
from typing import Any, List, Tuple

try:
    import yaml
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False


# ---------------------------------------------------------------------------
# Required keys for kill_switch.required_end_state
# ---------------------------------------------------------------------------

REQUIRED_END_STATE_KEYS = [
    "session_recording_tool_enabled",
    "git_hooks_recording_enabled",
    "public_checkpoint_branch_present",
    "auto_push_sessions_allowed",
    "full_transcript_remote_visibility",
    "leaked_credentials_rotated_or_revoked",
]

REQUIRED_TAXONOMY_MODES = [
    "current",
    "publish_secret",
    "app_runtime_secret",
    "agent_local_secret",
    "checkpoint_token",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def read_file(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None
    except OSError:
        return None


def extract_fenced_yaml_blocks(content: str) -> List[str]:
    """Return all content inside ```yaml ... ``` fences."""
    pattern = re.compile(r"```yaml\s*\n(.*?)```", re.DOTALL)
    return pattern.findall(content)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_yaml_parseable(content: str) -> Tuple[bool, str, dict]:
    """Check 1: fenced YAML block exists and parses to a dict."""
    blocks = extract_fenced_yaml_blocks(content)
    if not blocks:
        return False, "No fenced YAML block (```yaml) found", {}
    if not _YAML_AVAILABLE:
        return False, "PyYAML not installed; cannot parse YAML blocks", {}
    for i, block in enumerate(blocks):
        try:
            data = yaml.safe_load(block)
            if isinstance(data, dict):
                return True, f"YAML block #{i + 1} parsed successfully", data
        except yaml.YAMLError as exc:
            return False, f"YAML block #{i + 1} parse error: {exc}", {}
    return False, "No YAML block parsed to a dict", {}


def check_schema(data: dict) -> Tuple[bool, str]:
    """Check 2: schema == session_recording_policy/v1."""
    schema = data.get("schema")
    if schema == "session_recording_policy/v1":
        return True, "schema == session_recording_policy/v1"
    return False, f"schema field missing or wrong: {schema!r} (expected 'session_recording_policy/v1')"


def check_source_of_truth(data: dict) -> Tuple[bool, str]:
    """Check 3: source_of_truth has secret_policy and manifest_schema."""
    sot = data.get("source_of_truth")
    if not isinstance(sot, dict):
        return False, "source_of_truth key missing or not a mapping"
    missing = []
    if "secret_policy" not in sot:
        missing.append("secret_policy")
    if "manifest_schema" not in sot:
        missing.append("manifest_schema")
    if missing:
        return False, f"source_of_truth missing keys: {missing}"
    return True, f"source_of_truth has secret_policy={sot['secret_policy']!r} and manifest_schema={sot['manifest_schema']!r}"


def check_taxonomy_public_transcript(data: dict) -> Tuple[bool, str]:
    """Check 4: all taxonomy_mapping modes have public_full_transcript_allowed: false."""
    tm = data.get("taxonomy_mapping")
    if not isinstance(tm, dict):
        return False, "taxonomy_mapping key missing or not a mapping"
    violations = []
    missing_modes = []
    for mode in REQUIRED_TAXONOMY_MODES:
        if mode not in tm:
            missing_modes.append(mode)
            continue
        mode_data = tm[mode]
        if not isinstance(mode_data, dict):
            violations.append(f"{mode}: value is not a mapping")
            continue
        val = mode_data.get("public_full_transcript_allowed")
        if val is not False:
            violations.append(f"{mode}: public_full_transcript_allowed={val!r} (expected false)")
    if missing_modes:
        return False, f"taxonomy_mapping missing required modes: {missing_modes}"
    if violations:
        return False, f"public_full_transcript_allowed violations: {violations}"
    return True, f"All {len(REQUIRED_TAXONOMY_MODES)} taxonomy_mapping modes have public_full_transcript_allowed: false"


def check_kill_switch_required_end_state(data: dict) -> Tuple[bool, str]:
    """Check 5: kill_switch.required_end_state has all required keys."""
    ks = data.get("kill_switch")
    if not isinstance(ks, dict):
        return False, "kill_switch key missing or not a mapping"
    res = ks.get("required_end_state")
    if not isinstance(res, dict):
        return False, "kill_switch.required_end_state missing or not a mapping"
    missing = [k for k in REQUIRED_END_STATE_KEYS if k not in res]
    if missing:
        return False, f"kill_switch.required_end_state missing keys: {missing}"
    return True, f"kill_switch.required_end_state has all {len(REQUIRED_END_STATE_KEYS)} required keys"


def check_kill_switch_trigger_conditions(data: dict) -> Tuple[bool, str]:
    """Check 6: kill_switch.trigger_conditions is non-empty."""
    ks = data.get("kill_switch")
    if not isinstance(ks, dict):
        return False, "kill_switch key missing or not a mapping"
    tc = ks.get("trigger_conditions")
    if not tc:
        return False, "kill_switch.trigger_conditions missing or empty"
    if not isinstance(tc, list) or len(tc) == 0:
        return False, f"kill_switch.trigger_conditions must be a non-empty list, got: {tc!r}"
    return True, f"kill_switch.trigger_conditions has {len(tc)} conditions"


def check_raw_transcript_false(data: dict) -> Tuple[bool, str]:
    """Check 7: public_surfaces.github_issue_comment.raw_transcript_allowed == false."""
    ps = data.get("public_surfaces")
    if not isinstance(ps, dict):
        return False, "public_surfaces key missing or not a mapping"
    gic = ps.get("github_issue_comment")
    if not isinstance(gic, dict):
        return False, "public_surfaces.github_issue_comment missing or not a mapping"
    val = gic.get("raw_transcript_allowed")
    if val is not False:
        return False, f"public_surfaces.github_issue_comment.raw_transcript_allowed={val!r} (expected false)"
    return True, "public_surfaces.github_issue_comment.raw_transcript_allowed: false"


def check_fail_closed_on_unknown_visibility(data: dict) -> Tuple[bool, str]:
    """Check 8: checkpoint_remote.fail_closed_on_unknown_visibility == true."""
    cr = data.get("checkpoint_remote")
    if not isinstance(cr, dict):
        return False, "checkpoint_remote key missing or not a mapping"
    val = cr.get("fail_closed_on_unknown_visibility")
    if val is not True:
        return False, f"checkpoint_remote.fail_closed_on_unknown_visibility={val!r} (expected true)"
    av = cr.get("allowed_visibility", [])
    if "private_verified" not in (av if isinstance(av, list) else []):
        return False, f"checkpoint_remote.allowed_visibility does not include 'private_verified': {av!r}"
    return True, "checkpoint_remote: fail_closed_on_unknown_visibility=true, allowed_visibility includes private_verified"


def check_global_safety_flags(data: dict) -> Tuple[bool, str]:
    """Check 9: top-level fail-closed safety flags."""
    expected = {
        "github_public_checkpoint_branch_allowed": False,
        "auto_push_sessions_allowed": False,
        "manual_review_required_before_push": True,
    }
    violations = []
    for key, expected_val in expected.items():
        actual = data.get(key)
        if actual is not expected_val:
            violations.append(f"{key}={actual!r} (expected {expected_val!r})")
    if violations:
        return False, "global safety flag violations: " + "; ".join(violations)
    return True, "global safety flags are fail-closed (checkpoint_branch=false, auto_push=false, manual_review=true)"


def check_source_of_truth_paths(data: dict) -> Tuple[bool, str]:
    """Check 10: source_of_truth paths reference expected schema identifiers."""
    sot = data.get("source_of_truth")
    if not isinstance(sot, dict):
        return False, "source_of_truth key missing or not a mapping"
    violations = []
    sp = sot.get("secret_policy", "")
    if not sp or "secret-policy" not in str(sp) and "secret_policy" not in str(sp):
        violations.append(f"secret_policy={sp!r} does not reference secret-policy")
    ms = sot.get("manifest_schema", "")
    if not ms or "agent-session-manifest" not in str(ms) and "agent_session_manifest" not in str(ms):
        violations.append(f"manifest_schema={ms!r} does not reference agent-session-manifest")
    if violations:
        return False, "source_of_truth path issues: " + "; ".join(violations)
    return True, f"source_of_truth references are valid (secret_policy={sp!r}, manifest_schema={ms!r})"


_VERIFICATION_PATTERNS = [
    ("git ls-remote", r"git ls-remote"),
    ("git hooks / pre-push / pre-commit", r"\.git/hooks|pre-push|pre-commit"),
    ("pushRemote / insteadOf", r"pushRemote|insteadOf|pushInsteadOf|pushurl"),
    ("GitHub comment surface", r"gh issue|gh pr|public.*surface|transcript.*comment|comment.*surface"),
]


def check_verification_commands_presence(content: str) -> Tuple[bool, str]:
    """Check 11: Markdown body contains the 4 required verification command patterns."""
    missing = []
    for label, pattern in _VERIFICATION_PATTERNS:
        if not re.search(pattern, content):
            missing.append(label)
    if missing:
        return False, f"verification command patterns missing in document: {missing}"
    return True, f"all {len(_VERIFICATION_PATTERNS)} required verification command patterns found"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Structural checker for session-recording-policy.md (session_recording_policy/v1)"
    )
    parser.add_argument("policy_path", help="Path to session-recording-policy.md")
    args = parser.parse_args()

    content = read_file(args.policy_path)
    if content is None:
        print(f"Error: file not found: {args.policy_path}", file=sys.stderr)
        return 2

    results: List[Tuple[str, bool, str]] = []

    # 1. YAML parseable
    ok1, msg1, yaml_data = check_yaml_parseable(content)
    results.append(("YAML parseable", ok1, msg1))

    # 2. schema == session_recording_policy/v1
    if ok1:
        ok2, msg2 = check_schema(yaml_data)
    else:
        ok2, msg2 = False, "Skipped (YAML parse failed)"
    results.append(("schema == session_recording_policy/v1", ok2, msg2))

    # 3. source_of_truth keys
    if ok1:
        ok3, msg3 = check_source_of_truth(yaml_data)
    else:
        ok3, msg3 = False, "Skipped (YAML parse failed)"
    results.append(("source_of_truth (secret_policy + manifest_schema)", ok3, msg3))

    # 4. taxonomy_mapping public_full_transcript_allowed: false
    if ok1:
        ok4, msg4 = check_taxonomy_public_transcript(yaml_data)
    else:
        ok4, msg4 = False, "Skipped (YAML parse failed)"
    results.append(("taxonomy_mapping: all modes public_full_transcript_allowed=false", ok4, msg4))

    # 5. kill_switch.required_end_state keys
    if ok1:
        ok5, msg5 = check_kill_switch_required_end_state(yaml_data)
    else:
        ok5, msg5 = False, "Skipped (YAML parse failed)"
    results.append(("kill_switch.required_end_state keys", ok5, msg5))

    # 6. kill_switch.trigger_conditions non-empty
    if ok1:
        ok6, msg6 = check_kill_switch_trigger_conditions(yaml_data)
    else:
        ok6, msg6 = False, "Skipped (YAML parse failed)"
    results.append(("kill_switch.trigger_conditions non-empty", ok6, msg6))

    # 7. raw_transcript_allowed: false
    if ok1:
        ok7, msg7 = check_raw_transcript_false(yaml_data)
    else:
        ok7, msg7 = False, "Skipped (YAML parse failed)"
    results.append(("public_surfaces.github_issue_comment.raw_transcript_allowed=false", ok7, msg7))

    # 8. fail_closed_on_unknown_visibility: true
    if ok1:
        ok8, msg8 = check_fail_closed_on_unknown_visibility(yaml_data)
    else:
        ok8, msg8 = False, "Skipped (YAML parse failed)"
    results.append(("checkpoint_remote.fail_closed_on_unknown_visibility=true", ok8, msg8))

    # 9. global safety flags
    if ok1:
        ok9, msg9 = check_global_safety_flags(yaml_data)
    else:
        ok9, msg9 = False, "Skipped (YAML parse failed)"
    results.append(("global safety flags (checkpoint_branch/auto_push/manual_review)", ok9, msg9))

    # 10. source_of_truth path references
    if ok1:
        ok10, msg10 = check_source_of_truth_paths(yaml_data)
    else:
        ok10, msg10 = False, "Skipped (YAML parse failed)"
    results.append(("source_of_truth path references valid", ok10, msg10))

    # 11. verification command patterns in Markdown body
    ok11, msg11 = check_verification_commands_presence(content)
    results.append(("verification command patterns present in document", ok11, msg11))

    # Print results
    all_pass = True
    for name, passed, message in results:
        status = "PASS" if passed else "FAIL"
        out = sys.stdout if passed else sys.stderr
        print(f"  [{status}] {name}: {message}", file=out)
        if not passed:
            all_pass = False

    if all_pass:
        print("\nAll checks passed.")
        return 0
    else:
        print("\nOne or more checks FAILED.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
