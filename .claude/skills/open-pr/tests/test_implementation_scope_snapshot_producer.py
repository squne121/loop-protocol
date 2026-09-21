"""Production-facing durable scope snapshot tests for #2699 AC1/AC2."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
EVIDENCE = ROOT / ".claude/skills/impl-review-loop/scripts/implementation_landed_evidence.py"
OPEN_PR = ROOT / ".claude/skills/open-pr/scripts/open_pr.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _issue_body(*, reversed_lists: bool = False, operational: str = "progress one") -> str:
    in_scope = "- build intake\n- publish marker" if not reversed_lists else "- publish marker\n- build intake"
    acs = (
        "- [ ] AC2: marker persists\n- [x] AC1: scope normalizes"
        if not reversed_lists
        else "- [x] AC1: scope normalizes\n- [ ] AC2: marker persists"
    )
    paths = "- `.claude/a.py`\n- `.claude/b.py`" if not reversed_lists else "- `.claude/b.py`\n- `.claude/a.py`"
    return f"""## Machine-Readable Contract
```yaml
goal_ref: marker goal
change_kind: workflow
```
## In Scope
{in_scope}
## Acceptance Criteria
{acs}
## Allowed Paths
{paths}
## Remaining Parent Gaps
- {operational}
## Runtime Evidence
- changing this does not alter semantic scope
"""


def test_open_pr_embeds_implementation_scope_coverage_marker(monkeypatch):
    """AC1: publication builds a durable marker from the live Issue body."""
    open_pr = _load(OPEN_PR, "open_pr_scope_snapshot")
    monkeypatch.setattr(open_pr, "get_linked_issue_body", lambda _repo, _issue: _issue_body())
    monkeypatch.setattr(open_pr, "resolve_head_sha", lambda: "a" * 40)
    body = open_pr.append_implementation_scope_coverage(
        "## Summary\n日本語の説明", repo="squne121/loop-protocol", linked_issue=2699
    )
    assert body is not None
    evidence = _load(EVIDENCE, "scope_snapshot_consumer")
    coverage = evidence.coverage_from_pr_body(pr_body=body, issue_number=2699, live_issue_body=_issue_body())
    assert coverage["exact_coverage"] is True
    marker = coverage["marker"]
    assert marker["issue_number"] == 2699
    assert marker["pr_head_sha"] == "a" * 40


def test_scope_normalizer_excludes_operational_prose_and_progress_state():
    """AC2: checkbox/order/progress-only changes do not alter the digest."""
    evidence = _load(EVIDENCE, "scope_snapshot_normalizer")
    left = evidence.build_scope_manifest(_issue_body(reversed_lists=False, operational="first update"))
    right = evidence.build_scope_manifest(_issue_body(reversed_lists=True, operational="later update"))
    assert evidence._digest(left) == evidence._digest(right)
