# Issue #394 Migration Rationale: Absorbed & Superseded Issues

Issue #394 の SubAgent-owned contract migration により、以下の Issue の責務が本アーキテクチャに吸収または置換（superseded）されたことを記録する。

## #203 (transient auth_error retry in orchestrator)

**判定: SUPERSEDED**

- **理由**: `web-researcher` が自律的に `retry_count` と `failure_class: auth_error` を管理する責務を持ったため、orchestrator 側で retry 判定を行う必要がなくなった。
- **吸収先**: `.claude/agents/web-researcher.md` の `Execution: Grounding Quality & Fallback Logic`

## #247 (WEB_RESEARCH_RESULT_V1 & retry/fallback boundary)

**判定: ABSORBED**

- **理由**: `WEB_RESEARCH_RESULT_V1` schema が `web-researcher.md` で正式に定義され、`retry_count`, `fallback_used`, `attempt_log` (attempts), `critical_external_claims` の責務境界が確定した。
- **吸収先**: `.claude/agents/web-researcher.md` および `.claude/skills/issue-refinement-loop/references/web-research-routing.md`

## #248 (REPO_EVIDENCE_REF_V1 integration)

**判定: ABSORBED**

- **理由**: `codebase-investigator` が `REPO_EVIDENCE_REF_V1` を canonical evidence schema として参照し、独自 schema の作成を禁止したことで、検証メタデータ（commit SHA / excerpt hash）の SSOT が統一された。
- **吸収先**: `.claude/agents/codebase-investigator.md` の `Result: CODEBASE_INVESTIGATION_RESULT_V1`

## #391 Phase 3 (SubAgent-owned contract migration)

**判定: COMPLETED**

- **理由**: `issue-refinement-loop` から SubAgent 内部判断 prose を完全に排除し、機械可読な I/O contract による routing 責務への限定を達成した。
