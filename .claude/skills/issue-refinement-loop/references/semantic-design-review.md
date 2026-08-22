# Step 2.5: Semantic Design Review（Issue #2296）

deterministic checker（Step 2, `ISSUE_REVIEW_RESULT_COMPACT_V2`）が `VERDICT: approve` を
返した直後にのみ評価する追加レーン。決定論的に解けない領域（AC の設計意図との整合性、
schema/protocol/orchestration の architecture 判断、workflow contract の一貫性）を
`issue-design-reviewer` SubAgent（既定 `model: sonnet` / `effort: high`、frontmatter 固定。
複雑時のみ per-invocation で `model: opus` へ昇格）に read-only で評価させる。
`decide_next_loop_action.py` はこのレーンの追加によって一切変更されない
（semantic gate は完全に `join_review_results.py` に閉じる）。

## 1. Applicability 判定

```bash
uv run --locked python3 .claude/skills/issue-refinement-loop/scripts/semantic_review_trigger.py \
  --input-json '{"checker_gap_count": 2, "heuristic_concern_count": 0, "user_requested": false, "semantic_rewrite_requested": false, "severity_tagged_anchor_findings": [], "owner_decision_conflict": false, "cross_contract_change": {"schema": false, "protocol": false, "orchestration": false}}'
```

入力 JSON は手書きの ad-hoc payload ではなく、`semantic_review_trigger.build_semantic_review_trigger_input()`
（#2296 fix_delta iteration 6, P1-3）が既存の trusted artifact（Step 2 deterministic checker
の gap 一覧・heuristic concern 一覧・anchor comment body 群）から機械的に組み立てる。
`anchor_comment_bodies` を渡すと `scope_signal_delta.extract_severity_tags()`
（`extract_directive_markers()` とは独立した関数、P1-4）が severity-tagged 見出しを抽出し
`severity_tagged_anchor_findings` へ反映する。

`semantic_review_applicable: false` の場合は本レーンをスキップし、直接 Step 4.5 へ進む。

`semantic_review_applicable` は before/after 比較ではなく、明示シグナルのみに基づく
**適用可否分類**である（materiality の呼称は使わない。P0-2）。

## 2. Bundle の pin と起動

```bash
uv run --locked python3 .claude/skills/issue-refinement-loop/scripts/semantic_review_transport.py \
  pin-bundle \
  --issue-number <N> \
  --body-file <pinned_body_file> \
  --prompt-version v1 \
  --requested-model sonnet \
  --anchor-feedback-file <anchor_feedback_file (任意)> \
  --deterministic-findings-file <deterministic_findings_file (任意)>
```

`pin-bundle` は `body_sha256` / `prompt_version` / `requested_model` の組から決定論的に
導出される `invocation_id` を返し、`<invocation_dir>/bundle.json` と `<invocation_dir>/body.md`
（pinned body の実テキスト）の両方を書き込む（P0-1）。**クロス invocation の結果キャッシュ/再利用は
一切ない**（#2296 fix_delta iteration 6, P1-2）: 同じ `(body_sha256, prompt_version,
requested_model)` の組み合わせで再度 `pin-bundle` を呼んでも、過去の成功結果を再利用せず、
必ず fresh な起動を前提とする。

orchestrator（main/root session）は Agent tool を使って `issue-design-reviewer` を sibling
SubAgent として起動し、完了を待ってから次のステップへ進む（**completion join barrier**。
background/foreground の区別を本レーンは前提にしない。Claude Code はその区別を構造的に
保証する公開契約を持たないため、この文書もそれを主張しない、P0-2）。

起動時のタスクプロンプトは以下を必ず含める（P0-1、`.claude/agents/issue-design-reviewer.md`
frontmatter 直下の説明と同一の要旨）:

> `<invocation_dir>/bundle.json` を読み、そこに記録された `body_file`
> （既定 `body.md`）が指すファイルを読め。まさにその pinned body だけをレビューせよ。
> 生の semantic review schema に準拠する JSON オブジェクトを 1 つだけ返せ。
> 別の Issue 本文を fetch したり、それで代替したりしてはならない。

SubAgent が返した raw JSON（`assessment`/`findings` のみ）をファイルへ保存する。

## 3. 結果の検証・保存

```bash
uv run --locked python3 .claude/skills/issue-refinement-loop/scripts/semantic_review_transport.py \
  record-result \
  --invocation-dir <pin-bundle が返した invocation_dir> \
  --result-file <SubAgent 出力を保存したファイル> \
  --completed-at <ISO8601, agent 完了時刻> \
  --current-body-sha256 <必須。stale 判定用の再チェック body_sha256>
```

`--current-body-sha256` は必須引数（#2296 fix_delta iteration 6, P1-2: freshness の再チェックを
省略可能にしない）。

`record-result` は以下を fail-closed で検証する:

- `result-file` が存在し空でないこと
- `completed-at` が bundle の `pinned_at` より後であること（**completion join barrier** の検証。
  これは「foreground 実行の証明」ではなく、単に呼び出し元が結果を待ってから呼んだことの検証）
- `result-file` の mtime が `pinned_at` より後であること（stale な canned result を弾く弱い
  ヒューリスティックであり、genuine な agent 実行の証明ではない。P0-2）
- raw JSON が strict（重複キー拒否）であること
- モデル出力が `assessment`/`findings` のみであること（`owner_disposition` を含む場合は拒否、P0-3）
- 各 finding の `severity` が `blocker|high|medium|low` のいずれかであること
- `assessment: clear` なのに `findings` が非空、または `assessment: findings` なのに `findings`
  が空、という相関違反を拒否する（P0-3、`schemas/semantic_review_result_v1.schema.json` の
  `allOf`/`if`/`then` 制約と、`record_result()` 自身の明示チェックの二重防御）
- 組み立てた artifact 全体を `schemas/semantic_review_result_v1.schema.json` に対し
  `jsonschema` で検証する（P0-3）

検証を通過した場合のみ `SEMANTIC_REVIEW_RESULT_V1` sidecar artifact を
`.claude/artifacts/issue-refinement-loop/<issue>/<invocation_id>/semantic_review_result.json`
へ保存し、`transport_status: ok`（または stale 時 `stale`）を返す。

## 4. Join（結果の統合）

```bash
uv run --locked python3 .claude/skills/issue-refinement-loop/scripts/join_review_results.py \
  --input-json '{"deterministic_verdict": "approve", "semantic_assessment": "findings", "transport_status": "ok", "findings": [{"severity": "high", "summary": "example"}], "transport_policy": "best_effort", "finding_policy": "route_high_open_to_rewrite", "retry_already_attempted": false, "source_artifact": "<record-result が保存した semantic_review_result.json への path>", "checked_body_sha256": "<pinned body_sha256>"}'
```

`effective_verdict`（`approve` | `needs-fix` | `retry` | `human_judgment_required`）を返す。
`retry` は本 Step 2.5 の内部ループでのみ消費され、`decide_next_loop_action.py` には一切渡さない:

- `approve` → Step 4.5 へ
- `needs-fix` → Step 4（rewrite）へ。この verdict が deterministic ではなく semantic finding
  由来の場合、結果には追加で `rewrite_lane: "semantic"` と `semantic_rewrite_constraints`
  （`SEMANTIC_REWRITE_CONSTRAINTS_V1`、`.claude/agents/issue-editor.md` 参照）が含まれる。
  Step 4 はこの payload を **再構築せずそのまま** `issue-editor` へ渡す（P0-4）
- `retry` → transport（Step 2/3）を **一度だけ** 再実行し、`retry_already_attempted: true` で
  本 join を再実行する。二度目も transport が失敗した場合は `best_effort` ポリシーの下で
  `approve` + `semantic_review_unavailable: true` + `SEMANTIC_REVIEW_UNAVAILABLE` 警告
  （terminal/final report まで運ぶ、intermediate JSON だけに留めない。P0-3/P1-5）に収束する
- `human_judgment_required` → Step 5（human escalation）へ

`join_review_results()` の decision は `semantic_assessment` ラベルではなく `findings` の内容を
最優先で評価する（P0-3）: `assessment: clear` を自称していても open な blocker/high finding が
含まれていれば `needs-fix` になる。

## Policy 値

- `transport_policy`: `best_effort`（既定。transport 失敗時は 1 回だけ自動再実行し、
  それでも不能なら approve + `SEMANTIC_REVIEW_UNAVAILABLE` warning で継続） | `required`
  （明示指定時のみ。transport 失敗時は `human_judgment_required`）
- `finding_policy`: `route_high_open_to_rewrite`（常時有効な唯一の値。`severity: blocker|high`
  かつ有効な `owner_disposition` が未記録の finding のみ rewrite へルーティングする）

## Owner Disposition の記録経路

`owner_disposition`（`status: accepted|deferred|rejected` / `reason`（非空文字列、必須） /
`recorded_by: owner`）はモデル出力に含まれない別フィールドであり、Owner または orchestrator が
Issue コメント等の記録を経て `semantic_review_result.json` の該当 finding へ追記する形で運用する
（`issue-design-reviewer` 自身は書き込めない）。`join_review_results.py` は
`recorded_by == "owner"` かつ `status` が `accepted`/`deferred` かつ `reason` が非空文字列で
ある場合にのみ、その disposition を有効な降格根拠として扱う（#2296 fix_delta iteration 6, P1-1:
`recorded_by` や `reason` を欠いた偽装/不完全な disposition は blocker/high finding を
無効化しない）。
