from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[4]
_IMPLEMENTATION_PR = 2578
_REPO = "squne121/loop-protocol"
_spec = importlib.util.spec_from_file_location(
    "human_history_live",
    _ROOT / ".claude/skills/issue-refinement-loop/scripts/publish_termination_report.py",
)
publisher = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(publisher)


def _artifact(payload: dict) -> Path:
    directory = _ROOT / "artifacts"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"runtime-verification-AC2-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.log"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _skip_77(reason: str) -> None:
    _artifact({"ac": "AC2", "result": "SKIP", "exit_code": 77, "reason": reason, "mutation_started": False})
    pytest.skip(f"SKIP exit 77: {reason}")


def test_opt_in_confirmed_draft_pr_create_patch_noop_and_readback():
    """Mutate only Issue #1908's own confirmed Draft PR #2578, never a fallback."""
    if os.environ.get("LOOP_RUNTIME_VERIFICATION") != "true":
        _skip_77("runtime-verification opt-in is absent")
    if os.environ.get("LOOP_RUNTIME_TARGET_PR") != str(_IMPLEMENTATION_PR):
        _skip_77("exact implementation Draft PR binding is absent")
    allowlist_raw = os.environ.get("LOOP_RUNTIME_TARGET_ALLOWLIST", "")
    allowlist = {item.strip() for item in allowlist_raw.split(",") if item.strip()}
    if str(_IMPLEMENTATION_PR) not in allowlist:
        _skip_77("implementation Draft PR is not in the explicit allowlist")
    writer_serial = os.environ.get("LOOP_RUNTIME_WRITER_SERIAL", "")
    if not writer_serial or len(writer_serial) > 128:
        _skip_77("writer serial is absent or invalid")

    probe = subprocess.run(
        [
            "gh", "pr", "view", str(_IMPLEMENTATION_PR), "--repo", _REPO,
            "--json", "number,isDraft,headRefOid,url",
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if probe.returncode != 0:
        _skip_77("authenticated gh PR preflight failed")
    pr = json.loads(probe.stdout)
    target_matches = (
        pr.get("number") == _IMPLEMENTATION_PR
        and pr.get("isDraft") is True
        and isinstance(pr.get("headRefOid"), str)
    )
    if not target_matches:
        _skip_77("exact target is not a confirmed Draft PR")

    identity = {
        "loop_kind": "impl-review-loop",
        "phase": "post-PR-binding",
        "source_issue_number": 1908,
        "target_kind": "pull_request",
        "target_number": _IMPLEMENTATION_PR,
        "route_or_termination_reason": "needs_fix",
        "reviewed_ref": f"refs/pull/{_IMPLEMENTATION_PR}/head@{pr['headRefOid']}",
    }
    events: list[dict] = [
        {
            "preflight": {
                "pr_url": pr["url"],
                "head": pr["headRefOid"],
                "writer_serial": writer_serial,
            }
        }
    ]
    # Issue #1908 fix_delta HIGH-2: the exit code alone cannot prove which
    # remote operation happened (a PATCH against an existing comment also
    # returns 0). The raw controlled-executor `status_detail` is the ground
    # truth for the create/PATCH/noop claim; a same-digest replay on an
    # unchanged HEAD must never be recorded as "created".
    create_receipt: dict = {}
    patch_receipt: dict = {}
    noop_receipt: dict = {}
    outcome = "FAIL"
    try:
        create = publisher.publish_human_history(
            target_number=_IMPLEMENTATION_PR,
            repo=_REPO,
            identity=identity,
            result="live canary の作成を確認しました",
            evidence_refs=[pr["url"]],
            recommended_action="canary を確認してください",
            recommended_reason="controlled lane の作成を検証するためです",
            impact_if_unaddressed="更新経路を確認できません",
            receipt=create_receipt,
        )
        events.append({"create": create, "status_detail": create_receipt.get("status_detail")})
        assert create == 0
        assert create_receipt.get("status_detail") in {"created", "created_reconciled"}
        patch = publisher.publish_human_history(
            target_number=_IMPLEMENTATION_PR,
            repo=_REPO,
            identity=identity,
            result="live canary の更新を確認しました",
            evidence_refs=[pr["url"]],
            recommended_action="canary 結果を確認してください",
            recommended_reason="同一 identity の PATCH を検証するためです",
            impact_if_unaddressed="更新経路を確認できません",
            receipt=patch_receipt,
        )
        events.append({"patch": patch, "status_detail": patch_receipt.get("status_detail")})
        assert patch == 0
        assert patch_receipt.get("status_detail") == "updated"
        noop = publisher.publish_human_history(
            target_number=_IMPLEMENTATION_PR,
            repo=_REPO,
            identity=identity,
            result="live canary の更新を確認しました",
            evidence_refs=[pr["url"]],
            recommended_action="canary 結果を確認してください",
            recommended_reason="同一 identity の PATCH を検証するためです",
            impact_if_unaddressed="更新経路を確認できません",
            receipt=noop_receipt,
        )
        events.append({"noop": noop, "status_detail": noop_receipt.get("status_detail")})
        assert noop == 0
        # A same-identity/same-digest replay must be the executor's own
        # `already_published` noop outcome, never re-reported as "created".
        assert noop_receipt.get("status_detail") == "already_published"
        rendered, error = publisher.render_human_history_comment(
            identity=identity,
            result="live canary の更新を確認しました",
            evidence_refs=[pr["url"]],
            recommended_action="canary 結果を確認してください",
            recommended_reason="同一 identity の PATCH を検証するためです",
            impact_if_unaddressed="更新経路を確認できません",
        )
        assert error == "" and rendered
        readback = subprocess.run(
            ["gh", "pr", "view", str(_IMPLEMENTATION_PR), "--repo", _REPO, "--json", "comments"],
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        assert readback.returncode == 0
        comments = json.loads(readback.stdout)["comments"]
        matches = [comment for comment in comments if rendered["marker"] in comment.get("body", "")]
        assert len(matches) == 1
        assert matches[0]["body"] == rendered["body"]
        events.append(
            {
                "comment_get_readback": {
                    "comment_url": matches[0].get("url"),
                    "body_sha256": hashlib.sha256(matches[0]["body"].encode()).hexdigest(),
                }
            }
        )
        outcome = "PASS"
    finally:
        _artifact(
            {
                "ac": "AC2",
                "result": outcome,
                "events": events,
                "target_pr": _IMPLEMENTATION_PR,
                "writer_serial": writer_serial,
            }
        )
