#!/usr/bin/env python3
"""Behavioral tests for the ``ghs_`` GitHub App installation token redaction
gap in ``.claude/scripts/check_session_recording_runtime_safety.py``
(Issue #2734, follow-up to Issue #2725 / PR #2722).

The legacy ``ghs_[0-9A-Za-z]+`` matcher only consumes the token up to the
first ``_`` after the numeric APP_ID, so the new-format GitHub App
installation token — ``ghs_<APP_ID>_<JWT-header>.<payload>.<signature>`` —
is only partially redacted: the trailing ``_`` plus the whole JWS Compact
Serialization (header/payload/signature and their ``.`` delimiters) leaks
into diagnostic output unredacted.

These tests verify:

- AC2: a deterministic synthetic new-format token is fully redacted to
  exactly ``[REDACTED]`` (not merely "does not contain the raw string").
- AC3: ``_self_check_redaction()`` actually detects partial redaction
  (i.e. it is not a false-green check) when bound to a legacy-only
  matcher set equivalent to the pre-fix implementation.
- AC4: the existing short-form/legacy ``ghs_`` redaction behavior is
  unchanged by this fix (regression guard).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = REPO_ROOT / ".claude" / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
import check_session_recording_runtime_safety as srrs  # noqa: E402


def _build_new_format_installation_token() -> str:
    """Build a deterministic synthetic new-format GitHub App installation
    token fixture: ``ghs_<APP_ID>_<header>.<payload>.<signature>``.

    This is a *synthetic* fixture only — it is not, and must never be, a
    real credential. Character classes mirror the base64url alphabet used
    by real JWS Compact Serialization segments (``[A-Za-z0-9_-]``), plus
    the ``.`` segment delimiters mandated by the JWS Compact Serialization
    format.
    """
    app_id = "123456"
    header = "H" * 50
    payload = ("P" * 300) + "-" + ("Q" * 100)
    signature = "S" * 60
    return f"ghs_{app_id}_{header}.{payload}.{signature}"


def test_redact_fully_redacts_new_installation_token() -> None:
    """GIVEN a synthetic new-format ghs_ installation token fixture
    WHEN it is embedded in surrounding diagnostic text and passed to redact()
    THEN the entire token is replaced with a single [REDACTED] marker and no
    fragment of the original token (including the JWS "." delimiters and any
    trailing segments) remains in the output.
    """
    token = _build_new_format_installation_token()

    # Confirm the fixture itself has the new-format token's structural
    # properties before asserting on redaction behavior.
    assert token.count(".") == 2
    assert "_" in token
    assert "-" in token
    assert len(token) >= 520

    text = f"before {token} after"
    redacted = srrs.redact(text)

    # Exact whole-string check: not merely "the raw token is absent", but
    # that the surrounding text is preserved and the token collapses to
    # exactly one [REDACTED] marker with nothing left over.
    assert redacted == "before [REDACTED] after"


def test_self_check_redaction_detects_partial_redaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GIVEN _SECRET_PATTERNS bound to a legacy-only matcher set (no
    specific-before-legacy broad ghs_ matcher — equivalent to the pre-fix
    implementation)
    WHEN _self_check_redaction() runs
    THEN it returns False, because a legacy-only matcher only partially
    redacts the new-format token and the self-check must catch that
    false-green instead of reporting success.

    This does not mutate the production module file; it monkeypatches the
    live `_SECRET_PATTERNS` list for the duration of this test only, so the
    fix under test (the broad matcher itself) is never touched.
    """
    legacy_only_patterns = [
        pattern
        for pattern in srrs._SECRET_PATTERNS
        if "{36,}" not in pattern.pattern
    ]

    # Sanity: the legacy-only list must still contain the pre-existing
    # legacy ghs_ matcher (otherwise this test would not reproduce the
    # pre-fix scenario), and must have dropped the new broad matcher.
    assert any(p.pattern == r"ghs_[0-9A-Za-z]+" for p in legacy_only_patterns)
    assert not any("{36,}" in p.pattern for p in legacy_only_patterns)
    assert len(legacy_only_patterns) == len(srrs._SECRET_PATTERNS) - 1

    monkeypatch.setattr(srrs, "_SECRET_PATTERNS", legacy_only_patterns)

    assert srrs._self_check_redaction() is False


def test_redact_preserves_legacy_short_form_ghs_behavior() -> None:
    """GIVEN an existing legacy/short-form ghs_ token (no JWT segments)
    WHEN it is embedded in surrounding diagnostic text and passed to redact()
    THEN it is fully redacted to [REDACTED], matching pre-fix behavior
    (regression guard for AC4).
    """
    legacy_token = "ghs_abc123XYZ"

    text = f"before {legacy_token} after"
    redacted = srrs.redact(text)

    assert redacted == "before [REDACTED] after"
    assert legacy_token not in redacted
