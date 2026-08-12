# Web Research Routing

## Trigger

`REFINEMENT_LOOP_PLAN_V1.decisions.web_research_policy.required == true` のときだけ `web-researcher` を起動する。条件外は `skip_reason: no_critical_external_claim` を記録してスキップする。

## Consumer boundary

`WEB_RESEARCH_RESULT_V1` の詳細定義、および AGY-first / native Web fallback / evidence quality gate 判定は、`.claude/agents/web-researcher.md` を SSOT とする。

orchestrator は `WEB_RESEARCH_RESULT_V1` の詳細（attempt log や query mutation 等）を再実装せず、以下の consumer field だけを読んで routing を行う。

- `status`
- `failure_class`
- `verification_route`
- `retry_count`
- `fallback_used`
- `critical_external_claims`
- `unresolved_risks`

## Routing rules

- `status: ok`（`verification_route: agy_grounded_research` または `native_web`）→ Step 2 へ進む
- `status: insufficient_context` かつ `critical_external_claims` に unresolved あり → human escalation
- `status: inconclusive` かつ `critical_external_claims` が inconclusive → human escalation
- `status: failed` かつ `critical_external_claims` あり → human escalation
- 上記以外は unresolved risk を記録して Step 2 へ進む

`web_tool_call_count`、`search_query_count`、provider hook/provenance trace は consumer routing の入力ではない。これらの欠落または zero だけで `inconclusive` / `failed` / human escalation にしてはならない。native fallback は web-researcher 内で完結させ、両 route で critical claim evidence を得られなかった結果だけを escalation に渡す。

## Must not store

retry / fallback / attempt log の内部状態を orchestrator 側（LOOP_STATE）に保存してはならない。これらは SubAgent 側で完結させる。
