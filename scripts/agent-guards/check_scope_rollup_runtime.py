#!/usr/bin/env python3
"""Availability-gated runtime evidence for the Codex scope-rollup producer.

This probe deliberately does not manufacture trust: absent a release-pinned
Codex session with the required effective override, it emits a structured
SKIP and exits 77.  A caller may persist the JSON as its private artifact.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


EXPECTED_RELEASE = "0.145.0"
EXPECTED_PROFILE = "loop-protocol-scope-rollup"


def main() -> int:
    codex = shutil.which("codex")
    version = None
    if codex:
        completed = subprocess.run([codex, "--version"], text=True, capture_output=True, check=False)
        version = completed.stdout.strip().splitlines()[0] if completed.returncode == 0 and completed.stdout.strip() else None
    effective_profile = os.environ.get("CODEX_SCOPE_ROLLUP_EFFECTIVE_PROFILE")
    nested_disabled = os.environ.get("CODEX_SCOPE_ROLLUP_NESTED_DELEGATION_DISABLED") == "1"
    hook_trust = os.environ.get("CODEX_SCOPE_ROLLUP_HOOK_TRUST_ACTIVE") == "1"
    available = bool(codex and version and EXPECTED_RELEASE in version and effective_profile == EXPECTED_PROFILE and nested_disabled and hook_trust)
    status = "PASS" if available else "SKIP"
    reason = None if available else "pinned_codex_or_effective_session_features_unavailable"
    result = {
        "SCOPE_ROLLUP_RUNTIME_EVIDENCE_V1": {
            "status": status,
            "reason": reason,
            "release_pin": f"codex-{EXPECTED_RELEASE}",
            "codex_realpath": str(Path(codex).resolve()) if codex else None,
            "codex_version": version,
            "os": platform.platform(),
            "architecture": platform.machine(),
            "effective_parent_permission_profile": effective_profile,
            "required_effective_permission_profile": EXPECTED_PROFILE,
            "nested_delegation_session_disabled": nested_disabled,
            "hook_trust_active": hook_trust,
            "uv_sync_used": False,
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
    }
    print(json.dumps(result, sort_keys=True))
    return 0 if available else 77


if __name__ == "__main__":
    raise SystemExit(main())
