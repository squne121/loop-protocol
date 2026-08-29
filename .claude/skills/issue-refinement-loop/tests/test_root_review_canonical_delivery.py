"""Issue #2380 -- canonical Step 2 directly consumes `produce`'s own
root-verified `compact_result.verdict` / `compact_result.next_action` /
`verified_transport_artifact` and no longer relays the 11-line
`ISSUE_REVIEW_RESULT_COMPACT_V2` wire through the `issue-reviewer` child
agent.

AC1: `run_root_review_pipeline.py produce` (a REAL `_cmd_produce()` CLI
invocation -- only `fetch_and_pin_live_body` is monkeypatched to avoid a
live `gh` dependency; every downstream step, including the real checker
subprocess chain spawned via `reviewer_transport.run_reviewer_transport()`,
runs for real) returns `compact_result.verdict` / `compact_result.next_action`
that correctly route BOTH the approve and needs-fix cases, without going
through the `issue-reviewer` child agent's stdout at any point.

AC2: the canonical `produce` code path never calls `classify_child_stdout()`
or `retry_once_on_transport_failure()` -- proven both behaviorally (patching
both functions to raise AssertionError if invoked, then running the full
real `_cmd_produce()` roundtrip for both verdict branches) and via a static
source-shape check on `_cmd_produce`'s own source text (a regression guard
against a future edit silently reintroducing a call to either function or to
an `issue-reviewer` subprocess invocation).
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import inspect
import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
SCRIPTS_DIR = ROOT / ".claude" / "skills" / "issue-refinement-loop" / "scripts"
PIPELINE_SCRIPT = SCRIPTS_DIR / "run_root_review_pipeline.py"
SKILL_MD = ROOT / ".claude" / "skills" / "issue-refinement-loop" / "SKILL.md"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _load_pipeline_module():
    spec = importlib.util.spec_from_file_location("run_root_review_pipeline_canonical_delivery", PIPELINE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("run_root_review_pipeline_canonical_delivery", module)
    spec.loader.exec_module(module)
    return module


_PIPELINE = _load_pipeline_module()
_REPO = "squne121/loop-protocol"

_APPROVE_BODY = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: research
parent_issue: none
goal_ref: "Issue #2380 canonical delivery fixture (approve branch)"
change_kind: research
```

## Outcome

Fixture body for a genuine end-to-end `produce` CLI regression test proving
canonical Step 2 directly consumes `compact_result.verdict` (Issue #2380).

## Acceptance Criteria

- [ ] AC1: fixture body is well-formed enough for check_issue_contract.py to
      synthesize a complete REVIEW_ISSUE_RESULT_V1 with verdict approve.

## Verification Commands

```bash
# AC1
# baseline-expect: pass
$ true
```

## Allowed Paths

- fixture/e2e_produce_canonical_delivery_approve.md
"""

# C3 (`check_c3_ac_checkbox_format`) requires every AC line to use the
# `- [ ]` checkbox prefix; this fixture intentionally omits it so the REAL
# checker chain (not a hand-written fixture) produces a genuine
# `verdict: needs-fix` / `NEXT_ACTION: request_changes` result.
_NEEDS_FIX_BODY = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: research
parent_issue: none
goal_ref: "Issue #2380 canonical delivery fixture (needs-fix branch)"
change_kind: research
```

## Outcome

Fixture body for a genuine end-to-end `produce` CLI regression test proving
canonical Step 2 directly consumes `compact_result.verdict` (Issue #2380).

## Acceptance Criteria

AC1: fixture body intentionally omits the `- [ ]` checkbox prefix so the
real checker chain's C3 check fails, producing a genuine needs-fix verdict.

## Verification Commands

```bash
# AC1
# baseline-expect: pass
$ true
```

## Allowed Paths

- fixture/e2e_produce_canonical_delivery_needs_fix.md
"""


def _run_real_produce(tmp_path: Path, monkeypatch, *, body: str, issue_number: int) -> dict:
    """Run a REAL `_cmd_produce()` invocation (real checker subprocess chain
    via `reviewer_transport.run_reviewer_transport()`); only the live GitHub
    body fetch is replaced with a pinned fixture, matching the existing
    `test_root_review_pipeline_readback_v2_ssot.py` E2E pattern."""
    body_sha256 = _PIPELINE.sha256_of(body)
    monkeypatch.setattr(_PIPELINE, "_REPO_ROOT", tmp_path)

    def _fake_fetch(issue_number_, repo, timeout_seconds=15):
        return body, body_sha256, None

    monkeypatch.setattr(_PIPELINE, "fetch_and_pin_live_body", _fake_fetch)

    args = argparse.Namespace(issue_number=issue_number, repo=_REPO)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _PIPELINE._cmd_produce(args)
    out = json.loads(buf.getvalue())
    assert rc == 0, out
    assert out["status"] == "ok", out
    return out


# ---------------------------------------------------------------------------
# AC1: canonical Step 2 directly consumes `compact_result.verdict` /
# `compact_result.next_action` for BOTH approve and needs-fix, without the
# `issue-reviewer` child agent's stdout ever entering the picture.
# ---------------------------------------------------------------------------


def test_given_real_produce_approve_body_when_run_then_compact_result_routes_approve_directly(
    tmp_path: Path, monkeypatch
):
    out = _run_real_produce(tmp_path, monkeypatch, body=_APPROVE_BODY, issue_number=2380001)

    compact_result = out["compact_result"]
    assert compact_result["verdict"] == "approve"
    assert compact_result["next_action"] == "proceed"
    assert out["merged_review_result"]["verdict"] == "approve"

    # `verified_transport_artifact` (REVIEWER_COMPACT_ARTIFACT_V2) is the
    # canonical artifact reference canonical Step 2 uses -- it must be
    # self-consistent with `compact_result.artifact_path`.
    vta = out["verified_transport_artifact"]
    assert vta["schema"] == "REVIEWER_COMPACT_ARTIFACT_V2"
    assert str(Path(vta["root"]) / vta["relative_path"]) == compact_result["artifact_path"]


def test_given_real_produce_needs_fix_body_when_run_then_compact_result_routes_needs_fix_directly(
    tmp_path: Path, monkeypatch
):
    out = _run_real_produce(tmp_path, monkeypatch, body=_NEEDS_FIX_BODY, issue_number=2380002)

    compact_result = out["compact_result"]
    assert compact_result["verdict"] == "needs-fix"
    assert compact_result["next_action"] == "request_changes"
    assert out["merged_review_result"]["verdict"] == "needs-fix"
    assert out["merged_review_result"]["blocking_issues"], "expected at least one blocking issue"


# ---------------------------------------------------------------------------
# AC2: the canonical `produce` code path never calls `classify_child_stdout()`
# / `retry_once_on_transport_failure()`, and never invokes the
# `issue-reviewer` child agent.
# ---------------------------------------------------------------------------


def _forbid(name: str):
    def _raise(*args, **kwargs):
        raise AssertionError(f"canonical Step 2 delivery path must not call {name}()")

    return _raise


@pytest.mark.parametrize(
    "body,issue_number",
    [
        (_APPROVE_BODY, 2380003),
        (_NEEDS_FIX_BODY, 2380004),
    ],
    ids=["approve", "needs-fix"],
)
def test_given_real_produce_when_run_then_classify_child_stdout_and_retry_are_never_called(
    tmp_path: Path, monkeypatch, body: str, issue_number: int
):
    monkeypatch.setattr(_PIPELINE, "classify_child_stdout", _forbid("classify_child_stdout"))
    monkeypatch.setattr(_PIPELINE, "retry_once_on_transport_failure", _forbid("retry_once_on_transport_failure"))

    # If either forbidden function were reached, `_run_real_produce()` would
    # raise AssertionError instead of returning `status: ok`.
    out = _run_real_produce(tmp_path, monkeypatch, body=body, issue_number=issue_number)
    assert out["status"] == "ok"


def test_cmd_produce_source_never_references_classify_child_stdout_retry_or_issue_reviewer_agent():
    """Static regression guard (Issue #2380 AC2): `_cmd_produce()`\'s own
    source text must not reference `classify_child_stdout`,
    `retry_once_on_transport_failure`, or spawn the `issue-reviewer` agent --
    a future edit that silently reintroduces the legacy relay/retry/child
    invocation into the canonical `produce` code path is caught here even if
    it happened to be behaviorally unreachable in the tests above."""
    source = inspect.getsource(_PIPELINE._cmd_produce)
    assert "classify_child_stdout" not in source
    assert "retry_once_on_transport_failure" not in source
    assert "issue-reviewer" not in source
    assert "issue_reviewer" not in source


def test_cmd_produce_result_is_self_sufficient_for_canonical_step2_routing(tmp_path: Path, monkeypatch):
    """AC1: `produce`\'s JSON output alone (no further child-agent round trip)
    must carry everything canonical Step 2 needs to route: `verdict`,
    `next_action`, and the verified artifact reference."""
    out = _run_real_produce(tmp_path, monkeypatch, body=_APPROVE_BODY, issue_number=2380005)
    compact_result = out["compact_result"]
    for key in ("verdict", "next_action", "artifact_path", "reviewed_body_sha256", "attempt_id"):
        assert key in compact_result, f"canonical Step 2 routing needs compact_result.{key}"
    for key in ("root", "relative_path", "sha256", "schema", "invocation_id", "attempt"):
        assert key in out["verified_transport_artifact"], f"canonical Step 2 routing needs verified_transport_artifact.{key}"


# ---------------------------------------------------------------------------
# SKILL.md static consistency (Issue #2380 AC6-adjacent): canonical Step 2\'s
# routing table text must key off `compact_result.verdict` directly and must
# state issue-reviewer is not invoked for canonical routing.
# ---------------------------------------------------------------------------


def test_skill_md_step2_routing_table_reads_compact_result_verdict_directly():
    text = SKILL_MD.read_text(encoding="utf-8")
    start = text.index("### Step 2: レビュー")
    end = text.index("### Step 2.5")
    step2_text = text[start:end]

    assert "compact_result.verdict: approve" in step2_text
    assert "compact_result.verdict: needs-fix" in step2_text


def test_skill_md_step2_states_issue_reviewer_not_invoked_for_canonical_routing():
    text = SKILL_MD.read_text(encoding="utf-8")
    start = text.index("### Step 2: レビュー")
    end = text.index("### Step 2.5")
    step2_text = text[start:end]

    assert "canonical Step 2 では起動されない" in step2_text
    assert "classify_child_stdout()" in step2_text
    assert "retry_once_on_transport_failure()" in step2_text
