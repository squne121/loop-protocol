# Web Research Routing

## Trigger

`REFINEMENT_LOOP_PLAN_V1.decisions.web_research_policy.required == true` のときだけ `web-researcher` を起動する。条件外は `skip_reason: no_critical_external_claim` を記録してスキップする。

## Consumer boundary

`WEB_RESEARCH_RESULT_V1` は `.claude/agents/web-researcher.md` と `gemini-cli-headless-delegation` 側の schema を SSOT とする。issue-refinement-loop は以下の consumer field だけを読む。

- `status`
- `failure_class`
- `verification_route`
- `claims`
- `unresolved_risks`

`LOOP_STATE.web_research` に共通反映する項目:

- `status`
- `failure_class`
- `verification_route`
- `result`

## Routing rules

- `status: ok` → Step 2 へ進む
- `status: insufficient_context` かつ critical claim あり → human escalation
- `status: inconclusive` かつ critical claim が inconclusive → human escalation
- `status: failed` かつ critical claim あり → human escalation
- 上記以外は unresolved risk を記録して Step 2 へ進む

## Must not store

- `retry_count`
- `fallback_query`
- `raw_grounding_state`

retry / fallback / attempt log の設計変更は `#394` 以降の責務。
