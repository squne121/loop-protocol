"""#1516: weak path / ordinal AC signals must not create false collision routes."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[4]
HELPER = REPO_ROOT / ".claude/skills/implement-issue/scripts/check_implementation_overlap.py"
DEFAULT_REPO = "squne121/loop-protocol"
CREATE_ISSUE_SCRIPTS = REPO_ROOT / ".claude/skills/create-issue/scripts"
IMPLEMENT_ISSUE_SCRIPTS = REPO_ROOT / ".claude/skills/implement-issue/scripts"
sys.path.insert(0, str(CREATE_ISSUE_SCRIPTS))
from check_issue_overlap import (  # noqa: E402
    PathScopeKind,
    SOURCE_OK,
    classify_path_scope_kind,
    gh_search_candidates,
    paths_conflict,
)

sys.path.insert(0, str(IMPLEMENT_ISSUE_SCRIPTS))
import check_implementation_overlap  # noqa: E402


def _sha(body: str) -> str:
    return f"sha256:{hashlib.sha256(body.encode()).hexdigest()}"


def _body(*, outcome: str, scope: str, allowed_paths: list[str], ac: str = "AC1", schema: str = "") -> str:
    schema_line = f"\n{schema}" if schema else ""
    return (
        "## Machine-Readable Contract\n\n```yaml\ncontract_schema_version: v1\n"
        "issue_kind: implementation\nchange_kind: code\n```\n\n"
        f"## Outcome\n\n{outcome}{schema_line}\n\n## In Scope\n\n{scope}{schema_line}\n\n"
        f"## Acceptance Criteria\n\n- [ ] {ac}: behavior\n\n## Allowed Paths\n\n"
        + "\n".join(f"- {path}" for path in allowed_paths)
        + "\n"
    )


def _record(number: int, body: str) -> dict[str, object]:
    return {
        "number": number,
        "title": f"implementation {number}",
        "body": body,
        "body_sha256": _sha(body),
        "labels": [{"name": "phase/implementation"}],
        "updatedAt": "2026-07-14T00:00:00Z",
        "url": f"https://github.com/squne121/loop-protocol/issues/{number}",
        "state": "OPEN",
    }


def _run(tmp_path: Path, current_body: str, candidate_body: str) -> dict[str, object]:
    current = _record(1283, current_body)
    candidate = _record(1284, candidate_body)
    current_file = tmp_path / "current.json"
    candidates_file = tmp_path / "candidates.json"
    current_file.write_text(json.dumps(current), encoding="utf-8")
    candidates_file.write_text(json.dumps([candidate]), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "--issue-number",
            "1283",
            "--dry-run",
            "--repo",
            DEFAULT_REPO,
            "--current-file",
            str(current_file),
            "--candidates-file",
            str(candidates_file),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


# GitHub readback on 2026-07-15.  These are deliberately literal test inputs:
# the body digest assertions below must fail if the fixture drifts.
ISSUE_1283_CURRENT = (
    """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: \"#1176\"
goal_ref: \"M4 Upgrade Loop の runtime / E2E evidence を追加する\"
change_kind: mixed
```

## Outcome

"""
    "Playwright E2E と必要最小限の observability hook が追加され、upgrade 購入後の `weaponPower` 永続化と next-sortie "
    "projectile damage 反映、および preview / E2E storage namespace 分離が証跡付きで再現できる状態。"
    """

## In Scope

- `tests/e2e/m4-upgrade-loop.spec.ts` の追加
- `tests/e2e/m4-preview-namespace.spec.ts` の追加
- `playwright.config.ts`
- `package.json`
- E2E typecheck

## Acceptance Criteria

- [ ] AC1: E2E 専用 key を seed できる
- [ ] AC13: full `pnpm test:e2e` が PASS する

## Allowed Paths

- tests/e2e/m4-upgrade-loop.spec.ts
- tests/e2e/m4-preview-namespace.spec.ts
- playwright.config.ts
- package.json
"""
)

ISSUE_198_CANDIDATE = (
    """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: \"#171\"
goal_ref: \"SSOT registry を generated.yml に移行する\"
change_kind: code
```

## Outcome

"""
    "各 SSOT 文書に YAML Frontmatter が追加され、`generate-ssot-registry.sh` の実行により "
    "`docs/ssot-registry.generated.yml` が生成される状態。"
    """

## In Scope

- `.claude/skills/ssot-discovery/scripts/generate-ssot-registry.sh`
- `package.json` への `ssot:generate` / `ssot:check` npm script 追加

## Acceptance Criteria

- [ ] AC1: SSOT 文書に YAML Frontmatter が存在する

## Allowed Paths

- .claude/skills/ssot-discovery/scripts/generate-ssot-registry.sh
- package.json
"""
)

ISSUE_1326_CANDIDATE = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: \"#1260\"
goal_ref: \"cloud_pilot_success_contract_v1 を fail-closed に検証する\"
change_kind: code
```

## Outcome

cloud_pilot_success_contract_v1 checker・schema・negative fixture を実装し、pnpm test が全件 PASS する状態。

## In Scope

- `scripts/check-cloud-pilot-success-contract.mjs`
- `package.json` に checker 実行用の script エントリを追加する

## Acceptance Criteria

- [ ] AC1: checker が存在する

## Allowed Paths

- scripts/check-cloud-pilot-success-contract.mjs
- package.json
"""

ISSUE_1283_GOLDEN_SHA256 = "sha256:91161f3b8271a8830848e75c219ec0a400c6e038464ca41bf5503f2a4f3903ab"
ISSUE_198_GOLDEN_SHA256 = "sha256:450a9523e45ab948a83f792729fb9e09014bd2bb36f5dfea1c4fbaefd9b2ce77"
ISSUE_1326_GOLDEN_SHA256 = "sha256:4b5a1388e96731c3bc4ab354b34a919e364a7bcc27a895e2ed15eed2757c24da"


def test_exact_file_kind(tmp_path: Path) -> None:
    assert [classify_path_scope_kind(name) for name in ("README", "LICENSE", "Dockerfile", "Makefile")] == [
        PathScopeKind.EXACT
    ] * 4
    current = _body(outcome="current", scope="current", allowed_paths=["README", "LICENSE", "Dockerfile", "Makefile"])
    payload = _run(tmp_path, current, _body(outcome="other", scope="other", allowed_paths=["README"]))
    assert payload["route"] == "proceed_with_collision_evidence"


def test_package_json_weak(tmp_path: Path) -> None:
    payload = _run(
        tmp_path,
        ISSUE_198_CANDIDATE,
        ISSUE_1326_CANDIDATE,
    )
    candidate = payload["candidates"][0]
    assert payload["route"] == "proceed_with_collision_evidence"
    assert candidate["heading_overlap"] is False
    assert candidate["structural_signals"]["has_structural_collision"] is False
    assert candidate["reasons"] == ["readback_confirmed_disjoint", "low_specificity_path_only", "ordinal_ac_id_only"]


def test_package_json_strong(tmp_path: Path) -> None:
    payload = _run(
        tmp_path,
        _body(outcome="alpha", scope="`package.json#scripts.foo`", allowed_paths=["package.json"]),
        _body(outcome="beta", scope="`package.json#scripts.foo`", allowed_paths=["package.json"]),
    )
    candidate = payload["candidates"][0]
    assert payload["route"] in {"human_review_required", "duplicate"}
    assert candidate["structural_signals"]["has_structural_collision"] is True
    assert "independent_strong_signal_detected" in candidate["reasons"]


def test_package_json_dependency_keys_are_compared_structurally(tmp_path: Path) -> None:
    same = _run(
        tmp_path,
        _body(outcome="alpha", scope="`package.json#dependencies.react`", allowed_paths=["package.json"]),
        _body(outcome="beta", scope="`package.json#dependencies.react`", allowed_paths=["package.json"]),
    )
    different = _run(
        tmp_path,
        _body(outcome="alpha", scope="`package.json#dependencies.react`", allowed_paths=["package.json"]),
        _body(outcome="beta", scope="`package.json#dependencies.zod`", allowed_paths=["package.json"]),
    )
    assert same["route"] in {"human_review_required", "duplicate"}
    assert (
        "package.json#dependencies.react" in same["candidates"][0]["structural_signals"]["shared_specific_edit_targets"]
    )
    assert different["route"] == "proceed_with_collision_evidence"


def test_broad_prefix_weak(tmp_path: Path) -> None:
    payload = _run(
        tmp_path,
        _body(outcome="alpha", scope="alpha", allowed_paths=["tests/e2e/foo.spec.ts"]),
        _body(outcome="beta", scope="beta", allowed_paths=["tests/"]),
    )
    assert payload["route"] == "proceed_with_collision_evidence"
    assert "broad_prefix_only" in payload["candidates"][0]["reasons"]
    assert payload["candidates"][0]["heading_overlap"] is False
    assert payload["candidates"][0]["structural_signals"]["has_structural_collision"] is False


def test_broad_prefix_strong(tmp_path: Path) -> None:
    payload = _run(
        tmp_path,
        _body(
            outcome="alpha", scope="same test target", allowed_paths=["tests/e2e/foo.spec.ts"], schema="TEST_RESULT_V1"
        ),
        _body(outcome="beta", scope="same test target", allowed_paths=["tests/"], schema="TEST_RESULT_V1"),
    )
    assert payload["route"] == "human_review_required"
    assert "independent_strong_signal_detected" in payload["candidates"][0]["reasons"]


def test_ordinal_ac_id_only(tmp_path: Path) -> None:
    payload = _run(
        tmp_path,
        _body(outcome="alpha", scope="alpha", allowed_paths=["package.json"], ac="AC1"),
        _body(outcome="beta", scope="beta", allowed_paths=["package.json"], ac="AC1"),
    )
    candidate = payload["candidates"][0]
    assert payload["route"] == "proceed_with_collision_evidence"
    assert candidate["heading_overlap"] is False
    assert candidate["structural_signals"]["has_structural_collision"] is False
    assert "ordinal_ac_id_only" in candidate["reasons"]


def test_ac_id_with_output_schema(tmp_path: Path) -> None:
    payload = _run(
        tmp_path,
        _body(outcome="alpha", scope="alpha", allowed_paths=["package.json"], ac="AC1", schema="OUTPUT_RESULT_V1"),
        _body(outcome="beta", scope="beta", allowed_paths=["package.json"], ac="AC1", schema="OUTPUT_RESULT_V1"),
    )
    assert payload["route"] in {"human_review_required", "duplicate"}
    assert payload["candidates"][0]["structural_signals"]["has_structural_collision"] is True


def test_issue_1283_golden_fixture(tmp_path: Path) -> None:
    """固定された #1283 / #198 / #1326 readback の exact evidence を検証する。"""
    assert _sha(ISSUE_1283_CURRENT) == ISSUE_1283_GOLDEN_SHA256
    assert _sha(ISSUE_198_CANDIDATE) == ISSUE_198_GOLDEN_SHA256
    assert _sha(ISSUE_1326_CANDIDATE) == ISSUE_1326_GOLDEN_SHA256
    payload = _run(tmp_path, ISSUE_1283_CURRENT, ISSUE_198_CANDIDATE)
    candidate = payload["candidates"][0]
    assert payload["route"] == "proceed_with_collision_evidence"
    assert candidate["issue_number"] == 1284
    assert candidate["heading_overlap"] is False
    assert candidate["readback_complete"] is True
    assert candidate["structural_signals"]["has_structural_collision"] is False
    assert candidate["reasons"] == [
        "readback_confirmed_disjoint",
        "low_specificity_path_only",
        "ordinal_ac_id_only",
    ]


def test_single_level_glob_does_not_overlap_nested_path(tmp_path: Path) -> None:
    assert classify_path_scope_kind("tests/*") is PathScopeKind.UNKNOWN
    assert not paths_conflict("tests/*", "tests/unit/deep/test_x.py")
    payload = _run(
        tmp_path,
        _body(outcome="alpha", scope="alpha", allowed_paths=["tests/*"]),
        _body(outcome="beta", scope="beta", allowed_paths=["tests/unit/deep/test_x.py"]),
    )
    assert payload["route"] == "proceed"


def test_open_issue_source_uses_paginated_api_without_saturation(monkeypatch: object) -> None:
    """GIVEN two REST pages exceeding the former default limit
    WHEN the online source collects candidates
    THEN gh api --paginate is used and source status remains complete.
    """
    pages = [
        [
            {
                "number": number,
                "title": f"issue {number}",
                "body": "body",
                "labels": [],
                "state": "open",
                "html_url": f"https://example.test/{number}",
            }
            for number in range(1, 101)
        ],
        [
            {
                "number": 101,
                "title": "issue 101",
                "body": "body",
                "labels": [],
                "state": "open",
                "html_url": "https://example.test/101",
            },
            {"number": 102, "title": "pull request", "body": "body", "labels": [], "state": "open", "pull_request": {}},
        ],
    ]
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(pages))

    monkeypatch.setattr("check_issue_overlap.subprocess.run", fake_run)  # type: ignore[attr-defined]

    candidates, status = gh_search_candidates(DEFAULT_REPO, ("overlap",))

    assert calls == [
        [
            "gh",
            "api",
            "--paginate",
            "--slurp",
            "repos/squne121/loop-protocol/issues?state=open&per_page=100",
        ]
    ]
    assert len(candidates) == 101
    assert candidates[-1].number == 101
    assert status.issue_search == SOURCE_OK
    assert status.issue_readback == SOURCE_OK


# ---------------------------------------------------------------------------
# PR #1530 review fix_delta (Blocker 1): fetch_implementation_candidates() is
# a SEPARATE function from check_issue_overlap.gh_search_candidates() tested
# above. It intentionally keeps the bounded `gh issue list --limit N` source
# (Issue #1516 Out of Scope explicitly excludes changing this function; only
# check_issue_overlap.py's create-issue source was required to fully
# paginate, per AC13). This dedicated regression test closes the audit gap:
# without it, test_open_issue_source_uses_paginated_api_without_saturation
# above could be mistaken for coverage of THIS module's source.limit
# contract, when it only covers the sibling create-issue module.
# ---------------------------------------------------------------------------


def _graphql_page_response(nodes: "list[dict]", *, has_next_page: bool, end_cursor: "str | None") -> str:
    return json.dumps(
        {
            "data": {
                "repository": {
                    "issues": {
                        "nodes": nodes,
                        "pageInfo": {"hasNextPage": has_next_page, "endCursor": end_cursor},
                    }
                }
            }
        }
    )


def test_fetch_implementation_candidates_respects_requested_limit(monkeypatch: object) -> None:
    """GIVEN a caller-specified --limit (safety cap) smaller than the total
    result count
    WHEN fetch_implementation_candidates() paginates via GraphQL
    THEN pagination stops at the safety cap and reports saturated=True /
    complete=False (#1493: cursor pagination replaces the single `gh issue
    list --limit` call this test previously asserted against).
    """
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> "subprocess.CompletedProcess[str]":
        calls.append(command)
        nodes = [
            {
                "number": n,
                "title": f"issue {n}",
                "body": "body",
                "updatedAt": "2026-01-01T00:00:00Z",
                "url": "u",
            }
            for n in range(1, 6)
        ]
        return subprocess.CompletedProcess(
            command, 0, stdout=_graphql_page_response(nodes, has_next_page=True, end_cursor="cursor-1")
        )

    monkeypatch.setattr("check_implementation_overlap.subprocess.run", fake_run)  # type: ignore[attr-defined]

    candidates, source_metadata = check_implementation_overlap.fetch_implementation_candidates(DEFAULT_REPO, 5)

    assert calls, "expected at least one gh api graphql call"
    assert calls[0][:3] == ["gh", "api", "graphql"]
    assert len(candidates) == 5
    assert source_metadata["saturated"] is True
    assert source_metadata["complete"] is False
    assert source_metadata["collection_mode"] == "exhaustive_cursor_pagination"


def test_fetch_implementation_candidates_not_saturated_below_limit(monkeypatch: object) -> None:
    """GIVEN fewer results than --limit (safety cap) and hasNextPage=false
    WHEN fetch_implementation_candidates() runs
    THEN saturated is False and complete is True (#1493: completeness is
    proven by GraphQL pageInfo.hasNextPage, not by comparing count to limit).
    """

    def fake_run(command: list[str], **_kwargs: object) -> "subprocess.CompletedProcess[str]":
        nodes = [
            {
                "number": n,
                "title": f"issue {n}",
                "body": "body",
                "updatedAt": "2026-01-01T00:00:00Z",
                "url": "u",
            }
            for n in range(1, 4)
        ]
        return subprocess.CompletedProcess(
            command, 0, stdout=_graphql_page_response(nodes, has_next_page=False, end_cursor=None)
        )

    monkeypatch.setattr("check_implementation_overlap.subprocess.run", fake_run)  # type: ignore[attr-defined]

    candidates, source_metadata = check_implementation_overlap.fetch_implementation_candidates(DEFAULT_REPO, 100)

    assert len(candidates) == 3
    assert source_metadata["saturated"] is False
    assert source_metadata["complete"] is True


def test_issue_198_and_1326_do_not_force_human_review_via_package_json_alone(tmp_path: Path) -> None:
    """Direct regression for PR #1530 review Blocker 2: current #198 and
    #1326 bodies (In Scope both mention `package.json` as the only shared
    edit target) must not force human_review_required purely from that
    low-specificity shared path.
    """
    payload = _run(tmp_path, ISSUE_198_CANDIDATE, ISSUE_1326_CANDIDATE)
    candidate = payload["candidates"][0]
    assert payload["route"] == "proceed_with_collision_evidence"
    assert candidate["heading_overlap"] is False
    assert candidate["structural_signals"]["has_structural_collision"] is False
    assert "low_specificity_path_only" in candidate["reasons"]
