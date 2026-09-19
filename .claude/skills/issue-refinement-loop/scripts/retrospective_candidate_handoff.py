#!/usr/bin/env python3
"""Retrospective follow-up candidate -> controlled Issue materialization handoff.

Issue #2602 (#1939 Workstream 4): normalizes a human-authorized retrospective
follow-up candidate (one of two existing, distinct producer schemas -- see
``adapt_chatgpt_candidate`` / ``adapt_agent_improvement_candidate`` below) into
the existing ``FOLLOW_UP_ISSUE_REQUEST_V1``-shaped materialization input, then
hands off to the existing ``create-issue`` writer (``create_issue_txn.py``)
using the existing caller-side dedupe_key search convention
(``plan_child_materialization.py::_search_dedupe_candidates()``).

Design Constraints (Issue #2602, do not violate without a fresh contract
review):

- **Reuse, do not clone**: this module does not reimplement
  ``issue-refinement-loop`` / ``impl-review-loop`` stage logic, and does not
  reimplement ``plan_child_materialization.py``'s dedupe_key search -- it
  imports and calls it directly.
- **Extend, do not replace, the existing Issue writer**: issue materialization
  is delegated to ``create_issue_txn.run_transaction()`` (extended in this
  same Issue with an opt-in ``skip_internal_title_dedupe`` flag), never a new
  independent Issue creator.
- **Single implementation launch owner**: this module's responsibility STOPS
  at producing/normalizing a materialization request and handing off to the
  existing create-issue -> issue-refinement-loop chain. It never invokes
  ``impl-review-loop`` or ``issue-refinement-loop``'s internal state machine
  (root_entry_router.py's root-owned entry-transition function, its Step-1
  invocation callback, or issue-refinement-loop's next-action/preflight
  decision functions) directly -- root_entry_router.py's root-owned
  entry-transition function remains the sole owner of that transition. (Do
  not add a call to any of those functions here -- a static test asserts
  their identifier names never appear in this module's source; see
  ``test_retrospective_candidate_handoff.py``.)
- **Human-triggered**: ``materialize_candidate()`` requires an explicit
  ``human_authorized=True`` argument; retrospective completion alone is never
  treated as authorization.
- **No false completion**: this module's terminals describe Issue
  materialization only (``created`` / ``reused_open`` / ``reused_closed`` /
  ``human_escalation`` / ``failed`` / ``unauthorized``). It never claims
  ``implemented`` or ``draft_pr_ready`` -- those are ``impl-review-loop``
  terminals, propagated verbatim by ``propagate_canonical_terminal()``.
- **No hidden fallback**: ``propagate_canonical_terminal()`` returns whatever
  terminal ``issue-refinement-loop`` / ``impl-review-loop`` produced,
  unmodified. It never remaps or upgrades a terminal to a different meaning.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

# ---------------------------------------------------------------------------
# Reuse existing production modules (do not clone their logic).
# ---------------------------------------------------------------------------

_CREATE_ISSUE_SCRIPTS_DIR = (Path(__file__).resolve().parent.parent.parent / "create-issue" / "scripts")
if str(_CREATE_ISSUE_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_CREATE_ISSUE_SCRIPTS_DIR))

import create_issue_txn  # noqa: E402
import plan_child_materialization as _plan_child_materialization  # noqa: E402

# Reused as-is (Issue #2602 Design Constraints: "Reuse the existing
# caller-side dedupe convention"). This module does not reimplement the
# gh issue list --state all --search "<dedupe_key>" search.
search_dedupe_candidates = _plan_child_materialization._search_dedupe_candidates


# ---------------------------------------------------------------------------
# Canonical terminal vocabulary (AC5/AC7): never remapped, never upgraded.
# ---------------------------------------------------------------------------

CANONICAL_LOOP_TERMINALS: frozenset[str] = frozenset(
    {
        "already_satisfied",
        "no_change_required",
        "blocked",
        "human_escalation",
        "max_iterations",
        "draft_pr_ready",
    }
)


def propagate_canonical_terminal(loop_result: dict[str, Any]) -> dict[str, Any]:
    """Return ``loop_result`` unchanged (AC5/AC7 identity passthrough).

    ``issue-refinement-loop`` / ``impl-review-loop`` are the sole owners of
    what a terminal means. This function must never mutate, remap, or
    "upgrade" (e.g. a worker-local ``human_review_required`` warning must
    never be promoted to a hard ``blocked``) whatever they returned -- it
    exists only so callers have a single, auditable seam to route through,
    and so a regression that starts mutating the dict is caught by an
    equality assertion in tests rather than silently shipped.
    """
    return dict(loop_result)


# ---------------------------------------------------------------------------
# Producer-specific adapters (## Candidate Input Contract)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NormalizedCandidate:
    """FOLLOW_UP_ISSUE_REQUEST_V1-shaped normalization of a producer-specific
    retrospective follow-up candidate. Neither producer schema is modified or
    extended with a ``dedupe_key`` field (both are ``additionalProperties:
    false``) -- the dedupe key is derived here, outside those schemas."""

    title: str
    body: str
    dedupe_key: str
    source_reference: dict[str, Any]
    issue_kind: str = "implementation"
    labels: tuple[str, ...] = ("triage-required",)
    blocked_by: tuple[str, ...] = ()


def _normalize_title_for_key(title: str) -> str:
    """Deterministic title normalization for dedupe key derivation: collapse
    whitespace and lowercase. Never uses run-specific values (timestamps,
    digests) as input."""
    return re.sub(r"\s+", " ", title.strip()).lower()


def derive_chatgpt_dedupe_key(target: dict[str, Any], candidate: dict[str, Any]) -> str:
    """AC2/AC11: derive a deterministic dedupe key for a
    ``chatgpt_retrospective_result/v1`` follow_up_issue_candidates[] entry
    from ``target.repo`` + ``target.type`` + ``target.number`` + normalized
    ``title`` only. Never uses ``input_marker_digest`` or any timestamp."""
    norm_title = _normalize_title_for_key(candidate["title"])
    return f"chatgpt-candidate:v1:{target['repo']}:{target['type']}:{target['number']}:{norm_title}"


def adapt_chatgpt_candidate(result: dict[str, Any], candidate: dict[str, Any]) -> NormalizedCandidate:
    """Adapt a single ``chatgpt_retrospective_result/v1`` entry from
    ``follow_up_issue_candidates[]`` (schema:
    docs/schemas/chatgpt-retrospective-result.schema.json) into a
    NormalizedCandidate. ``result`` is the enclosing envelope (its
    top-level ``target`` applies to every candidate in the array)."""
    target = result["target"]
    dedupe_key = derive_chatgpt_dedupe_key(target, candidate)
    return NormalizedCandidate(
        title=candidate["title"],
        body=candidate["body"],
        dedupe_key=dedupe_key,
        source_reference={
            "producer_schema": "chatgpt_retrospective_result/v1",
            "target_repo": target["repo"],
            "target_type": target["type"],
            "target_number": target["number"],
        },
        blocked_by=tuple(candidate.get("blocked_by", ())),
    )


def derive_agent_candidate_dedupe_key(candidate: dict[str, Any]) -> str:
    """AC2/AC11: derive a deterministic dedupe key for an
    ``agent_improvement_candidate/v1`` entry (schema:
    .claude/skills/agent-retrospective/schemas/agent_improvement_candidate_v1.schema.json).

    When ``finding_contract`` is present, its ``identity.value`` (a stable,
    cross-run FINDING_IDENTITY_V1 hash that deliberately excludes
    ``source_run_ref``/``base_sha``/timestamps/evidence fingerprints -- see
    ``compute_finding_identity()`` in agent-retrospective's
    validate_retrospective_schema.py) is the dedupe key source. When absent
    (legacy candidate, ``delta_capability: legacy_unavailable``), the
    candidate's own ``candidate_id`` (stable within this single candidate's
    lifecycle) is used instead. Run-specific values are never used either
    way."""
    finding_contract = candidate.get("finding_contract")
    if finding_contract is not None:
        identity_value = finding_contract["identity"]["value"]
        return f"agent-candidate:v1:identity:{identity_value}"
    return f"agent-candidate-legacy:v1:{candidate['candidate_id']}"


def adapt_agent_improvement_candidate(candidate: dict[str, Any]) -> NormalizedCandidate:
    """Adapt a single ``agent_improvement_candidate/v1`` record into a
    NormalizedCandidate."""
    dedupe_key = derive_agent_candidate_dedupe_key(candidate)
    finding_contract = candidate.get("finding_contract")
    source_reference: dict[str, Any] = {
        "producer_schema": "agent_improvement_candidate/v1",
        "candidate_id": candidate["candidate_id"],
        "source_run_ref": dict(candidate.get("source_run_ref", {})),
    }
    if finding_contract is not None:
        source_reference["finding_identity_value"] = finding_contract["identity"]["value"]
    else:
        source_reference["delta_capability"] = "legacy_unavailable"
    return NormalizedCandidate(
        title=candidate["title"],
        body=candidate["description"],
        dedupe_key=dedupe_key,
        source_reference=source_reference,
    )


# ---------------------------------------------------------------------------
# Dedupe / overlap readback (AC2) -- reuses the existing caller-side
# dedupe_key search convention, then confirms exact-key match against issue
# body (the reused search itself is a GitHub full-text --search query, which
# can match on substrings unrelated to an exact dedupe_key; this
# confirmation step is what distinguishes AC2(a) same-key/different-title
# from AC2(b) different-key/same-title).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DedupeMatch:
    number: int
    title: str
    state: str  # OPEN | CLOSED
    state_reason: str | None
    url: str


@dataclass(frozen=True)
class DedupeDecision:
    action: Literal["create", "reuse_open", "reuse_closed", "human_escalation"]
    match: DedupeMatch | None = None
    reason: str | None = None
    search_truncated: bool = False


def _fetch_issue_detail(repo: str, number: int, gh_bin: str) -> dict[str, Any]:
    """Default (production) issue detail fetch: title/state/stateReason/url/body.
    Tests inject ``detail_fn`` instead of exercising this subprocess path."""
    args = [
        gh_bin,
        "issue",
        "view",
        str(number),
        "--repo",
        repo,
        "--json",
        "number,title,state,stateReason,url,body",
    ]
    completed = subprocess.run(args, capture_output=True, text=True)
    if completed.returncode != 0:
        return {}
    try:
        return json.loads(completed.stdout.strip() or "{}")
    except json.JSONDecodeError:
        return {}


def readback_dedupe_matches(
    repo: str,
    dedupe_key: str,
    *,
    gh_bin: str = "gh",
    search_fn: Callable[[str, str, str], list[dict[str, Any]]] | None = None,
    detail_fn: Callable[[str, int, str], dict[str, Any]] | None = None,
) -> DedupeDecision:
    search = search_fn or search_dedupe_candidates
    detail = detail_fn or _fetch_issue_detail

    raw_candidates = search(repo, dedupe_key, gh_bin)
    # _search_dedupe_candidates() (reused, unmodified) hard-codes --limit 10;
    # mirror that bound here so a saturated result is flagged rather than
    # silently treated as complete (AC2(e)).
    search_truncated = len(raw_candidates) >= 10

    confirmed: list[DedupeMatch] = []
    for item in raw_candidates:
        number = int(item["number"])
        detail_payload = detail(repo, number, gh_bin)
        body_text = detail_payload.get("body") or ""
        if dedupe_key not in body_text:
            # AC2(b): a full-text search hit whose body does not contain the
            # exact dedupe_key is NOT treated as a duplicate by title alone.
            continue
        confirmed.append(
            DedupeMatch(
                number=number,
                title=detail_payload.get("title", item.get("title", "")),
                state=str(detail_payload.get("state", item.get("state", ""))).upper(),
                state_reason=detail_payload.get("stateReason"),
                url=detail_payload.get("url", item.get("url", "")),
            )
        )

    if not confirmed:
        return DedupeDecision(action="create", search_truncated=search_truncated)
    if len(confirmed) > 1:
        return DedupeDecision(
            action="human_escalation",
            reason="ambiguous_multiple_exact_key_matches",
            search_truncated=search_truncated,
        )
    match = confirmed[0]
    if match.state == "OPEN":
        return DedupeDecision(action="reuse_open", match=match, search_truncated=search_truncated)
    # AC2(d)/B4: CLOSED match is reused (not reopened); disposition preserved.
    return DedupeDecision(action="reuse_closed", match=match, search_truncated=search_truncated)


# ---------------------------------------------------------------------------
# Materialization orchestrator (AC1/AC2/AC6)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MaterializationResult:
    status: Literal[
        "unauthorized",
        "created",
        "reused_open",
        "reused_closed",
        "human_escalation",
        "failed",
    ]
    issue_number: int | None
    issue_url: str | None
    disposition: str | None
    dedupe_key: str
    search_truncated: bool
    source_reference: dict[str, Any]
    next_action: dict[str, Any] | None
    errors: list[str] = field(default_factory=list)


_IMPLEMENTATION_TITLE_PREFIXES = ("実装:", "implement:")

# PR #2673 review iteration 2 blocker: materialize_candidate()'s create path
# handed a body to create_issue_txn.run_transaction() that failed its own
# in-repo validate_issue_body.py --kind implementation gate (LP001 missing
# sections, LP002 missing MRC fields, LP031 title prefix) before any GitHub
# mutation was even attempted. Per this Issue's Design Constraints
# ("retrospective orchestration が独自の ready 判定を実装しない" /
# "Reuse, do not clone"), this module does not synthesize genuine AC/VC
# content -- it renders clearly-marked placeholder sections that structurally
# satisfy the validator and explicitly point to issue-refinement-loop as the
# mandatory next action to refine them.
_PLACEHOLDER_NOTICE = (
    "この Issue は retrospective follow-up candidate の自動 handoff（Issue #2602）により、"
    "issue-refinement-loop の refine 前提となる構造ゲート（LP001/LP002/LP031）を通すための"
    "プレースホルダーとして生成された。具体的な内容は issue-refinement-loop がこの Issue を"
    "精査して確定する必要がある（本モジュールは独自の ready 判定を実装しないスコープ制約のため）。"
)


def render_materialization_title(candidate: NormalizedCandidate) -> str:
    """Render the Issue title for a freshly materialized candidate.

    LP031: an ``implementation``-kind Issue title must start with '実装:' or
    'implement:'. Derived from the candidate's own title (never a hardcoded,
    unrelated title) so the created Issue remains traceable to its source
    candidate."""
    stripped_title = candidate.title.strip()
    if stripped_title.startswith(_IMPLEMENTATION_TITLE_PREFIXES):
        return stripped_title
    return f"実装: {stripped_title}"


def render_materialization_body(candidate: NormalizedCandidate) -> str:
    """Render the Issue body for a freshly materialized candidate.

    The dedupe_key is embedded verbatim so a later rerun's dedupe readback
    (AC2(f): downstream-failure rerun reuses the same Issue) can confirm an
    exact match via ``readback_dedupe_matches()`` against the created
    Issue's own body.

    This body must structurally pass ``validate_issue_body.py --kind
    implementation`` (LP001 required sections, LP002 required Machine-
    Readable Contract fields, LP031 title prefix) because
    ``create_issue_txn.run_transaction()`` calls that validator internally
    before any GitHub mutation (Blocker 2.5). Sections whose real content is
    not this module's responsibility to author are clearly-marked
    placeholders pointing at issue-refinement-loop, not synthesized AC/VC
    content (Design Constraints: "Reuse, do not clone" /
    "retrospective orchestration が独自の ready 判定を実装しない")."""
    source_ref_lines = "\n".join(f"  {k}: {v!r}" for k, v in sorted(candidate.source_reference.items()))
    return (
        "## Machine-Readable Contract\n\n"
        "```yaml\n"
        "contract_schema_version: v1\n"
        f"issue_kind: {candidate.issue_kind}\n"
        f'dedupe_key: "{candidate.dedupe_key}"\n'
        "```\n\n"
        "## Parent Issue\n\n"
        "none\n\n"
        "## Parent Goal Ref\n\n"
        f"- Goal: {_PLACEHOLDER_NOTICE}\n"
        "- Desired Destination: issue-refinement-loop が本 Issue を refine し、具体的な"
        "Goal と Desired Destination を確定する。\n\n"
        "## Current Validated Scope\n\n"
        f"- {_PLACEHOLDER_NOTICE}\n\n"
        "## Remaining Parent Gaps\n\n"
        "- なし（retrospective follow-up candidate に直接の親 Issue はない）\n\n"
        "## Outcome\n\n"
        f"{_PLACEHOLDER_NOTICE}\n\n"
        "## Runtime Verification Applicability\n\n"
        "- decision: not_applicable\n"
        "- reason: retrospective handoff によるプレースホルダー生成のため、"
        "issue-refinement-loop が具体的な動作検証要否を確定するまでは適用判定できない。\n\n"
        "## In Scope\n\n"
        f"- {_PLACEHOLDER_NOTICE}\n\n"
        "## Out of Scope\n\n"
        "- retrospective orchestration が独自の ready 判定を実装すること"
        "（Issue #2602 Design Constraints）\n\n"
        "## Acceptance Criteria\n\n"
        f"- {_PLACEHOLDER_NOTICE}\n\n"
        "## Verification Commands\n\n"
        "```bash\n"
        '$ echo "placeholder: issue-refinement-loop が具体的な Verification Commands を確定する"\n'
        "```\n\n"
        "## Allowed Paths\n\n"
        f"- {_PLACEHOLDER_NOTICE}\n\n"
        "## Stop Conditions\n\n"
        "- Allowed Paths 外の変更が必要と判明した場合\n"
        "- In Scope の固定契約（キー集合・スキーマ・型定義）の変更が必要になった場合\n"
        "- 新規 Issue の起票が必要と判断した場合（スコープ分割が発生する場合）\n"
        "- 後続 Phase / 別スコープへの波及が判明した場合\n"
        "- nested SubAgent delegation が必要になった場合\n"
        "- 外部サービス利用・権限昇格・既存テスト大規模改変が必要になった場合\n\n"
        "## Required Skills\n\n"
        "なし\n\n"
        "## Required Design References\n\n"
        "- docs/dev/agent-skill-boundaries.md#FOLLOW_UP_ISSUE_REQUEST_V1\n\n"
        "## Source Reference\n\n"
        "```yaml\n"
        f"{source_ref_lines}\n"
        "```\n\n"
        "## Description\n\n"
        f"{candidate.body}\n"
    )


def _default_create_fn(**kwargs: Any) -> Any:
    return create_issue_txn.run_transaction(**kwargs)


def materialize_candidate(
    candidate: NormalizedCandidate,
    *,
    human_authorized: bool,
    repo: str,
    gh_bin: str = "gh",
    search_fn: Callable[[str, str, str], list[dict[str, Any]]] | None = None,
    detail_fn: Callable[[str, int, str], dict[str, Any]] | None = None,
    create_fn: Callable[..., Any] | None = None,
) -> MaterializationResult:
    """AC1: human_authorized must be explicitly True -- retrospective
    completion alone never authorizes materialization. AC2: dedupe/overlap
    readback runs before any create call, and a confirmed match is reused
    (never reopened if CLOSED) rather than re-created."""
    if not human_authorized:
        return MaterializationResult(
            status="unauthorized",
            issue_number=None,
            issue_url=None,
            disposition=None,
            dedupe_key=candidate.dedupe_key,
            search_truncated=False,
            source_reference=candidate.source_reference,
            next_action=None,
        )

    decision = readback_dedupe_matches(
        repo, candidate.dedupe_key, gh_bin=gh_bin, search_fn=search_fn, detail_fn=detail_fn
    )

    if decision.action == "human_escalation":
        return MaterializationResult(
            status="human_escalation",
            issue_number=None,
            issue_url=None,
            disposition=None,
            dedupe_key=candidate.dedupe_key,
            search_truncated=decision.search_truncated,
            source_reference=candidate.source_reference,
            next_action=None,
            errors=[decision.reason or "ambiguous dedupe matches"],
        )

    if decision.action == "reuse_open":
        match = decision.match
        assert match is not None
        return MaterializationResult(
            status="reused_open",
            issue_number=match.number,
            issue_url=match.url,
            disposition=None,
            dedupe_key=candidate.dedupe_key,
            search_truncated=decision.search_truncated,
            source_reference=candidate.source_reference,
            next_action={"kind": "issue_refinement_loop", "issue_number": match.number},
        )

    if decision.action == "reuse_closed":
        match = decision.match
        assert match is not None
        # Not reopened; no refinement handoff for a CLOSED issue (its
        # disposition -- state_reason -- is preserved and returned as-is).
        return MaterializationResult(
            status="reused_closed",
            issue_number=match.number,
            issue_url=match.url,
            disposition=match.state_reason,
            dedupe_key=candidate.dedupe_key,
            search_truncated=decision.search_truncated,
            source_reference=candidate.source_reference,
            next_action=None,
        )

    # decision.action == "create": the outer dedupe_key search found no
    # exact-key match, so the internal title-only OPEN-issue dedupe inside
    # create_issue_txn.run_transaction() is explicitly bypassed (AC2(g)) --
    # otherwise it could re-match a different, merely-same-titled issue and
    # silently override this already-confirmed "create" decision.
    creator = create_fn or _default_create_fn
    title = render_materialization_title(candidate)
    body = render_materialization_body(candidate)
    txn_result = creator(
        repo=repo,
        title=title,
        body=body,
        body_file="",
        labels=list(candidate.labels),
        issue_kind=candidate.issue_kind,
        parent_issue_number=0,
        dependency_issue_numbers=[],
        gh_bin=gh_bin,
        skip_internal_title_dedupe=True,
    )
    if txn_result.status not in {"success", "dedupe"} or txn_result.issue_number is None:
        return MaterializationResult(
            status="failed",
            issue_number=None,
            issue_url=None,
            disposition=None,
            dedupe_key=candidate.dedupe_key,
            search_truncated=decision.search_truncated,
            source_reference=candidate.source_reference,
            next_action=None,
            errors=[txn_result.failure_message or "issue creation failed"],
        )
    return MaterializationResult(
        status="created",
        issue_number=txn_result.issue_number,
        issue_url=txn_result.issue_url,
        disposition=None,
        dedupe_key=candidate.dedupe_key,
        search_truncated=decision.search_truncated,
        source_reference=candidate.source_reference,
        next_action={"kind": "issue_refinement_loop", "issue_number": txn_result.issue_number},
    )
