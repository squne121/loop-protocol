# Step 2.5: Semantic Design Review（Issue #2296）

deterministic checker（Step 2, `ISSUE_REVIEW_RESULT_COMPACT_V2`）が `VERDICT: approve` を
返した直後にのみ評価する追加レーン。決定論的に解けない領域（AC の設計意図との整合性、
schema/protocol/orchestration の architecture 判断、workflow contract の一貫性）を
`issue-design-reviewer` SubAgent（Sonnet、per-invocation `effort: high`）に read-only で
評価させる。`decide_next_loop_action.py` はこのレーンの追加によって一切変更されない
（semantic gate は完全に `join_review_results.py` に閉じる）。

## 1. Applicability 判定

```bash
uv run --locked python3 .claude/skills/issue-refinement-loop/scripts/semantic_review_trigger.py \
  --input-json '{"checker_gap_count": 0, "heuristic_concern_count": 0, ...}'
```

`semantic_review_applicable: false` の場合は本レーンをスキップし、直接 Step 4.5 へ進む。

`semantic_review_applicable` は before/after 比較ではなく、明示シグナルのみに基づく
**適用可否分類**である（materiality の呼称は使わない。P0-2）。

## 2. Bundle の pin と foreground 起動

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

`pin-bundle` は `body_sha256` / `prompt_version` / `requested_model` の組から
決定論的に導出される `invocation_id` を返す。同一 invocation 内で同じ組み合わせの
成功結果がすでに存在する場合（`cache_hit: true`）は再起動せず既存結果を再利用する。

`cache_hit: false` の場合、orchestrator（main/root session）は **Agent tool を使って
`issue-design-reviewer` を sibling SubAgent として foreground で起動し、完了を待つ**
（nested 起動ではない。バックグラウンドのまま次の処理へ進めることは禁止する）。
起動時 model override は `model: sonnet` / `effort: high`（複雑時のみ `model: opus`）。

SubAgent が返した raw JSON（`assessment`/`findings` のみ）をファイルへ保存する。

## 3. 結果の検証・保存

```bash
uv run --locked python3 .claude/skills/issue-refinement-loop/scripts/semantic_review_transport.py \
  record-result \
  --invocation-dir <pin-bundle が返した invocation_dir> \
  --result-file <SubAgent 出力を保存したファイル> \
  --completed-at <ISO8601, agent 起動完了時刻> \
  --current-body-sha256 <re-check 用、任意>
```

`record-result` は以下を fail-closed で検証する:

- `result-file` が存在し空でないこと（foreground 実行の完了証跡）
- `completed-at` が bundle の `pinned_at` より後であること
- `result-file` の mtime が `pinned_at` より後であること（stale な canned result の再利用を拒否する）
- raw JSON が strict（重複キー拒否）であること
- モデル出力が `assessment`/`findings` のみであること（`owner_disposition` を含む場合は拒否、P0-3）
- 各 finding の `severity` が `blocker|high|medium|low` のいずれかであること

検証を通過した場合のみ `SEMANTIC_REVIEW_RESULT_V1` sidecar artifact を
`.claude/artifacts/issue-refinement-loop/<issue>/<invocation_id>/semantic_review_result.json`
へ保存し、`transport_status: ok` を返す。

## 4. Join

```bash
uv run --locked python3 .claude/skills/issue-refinement-loop/scripts/join_review_results.py \
  --input-json '{"deterministic_verdict": "approve", "semantic_assessment": "findings", "transport_status": "ok", "findings": [...], "transport_policy": "best_effort", "finding_policy": "route_high_open_to_rewrite"}'
```

`effective_verdict`（`approve` | `needs-fix` | `human_judgment_required`）を、既存の
Step 2 routing table にそのまま通常の VERDICT として渡す:

- `approve` → Step 4.5 へ
- `needs-fix` → Step 4（rewrite）へ（`decide_next_loop_action.py` は rewrite 後の
  next-action 決定タイミングでのみ従来通り呼ばれる。呼び出し規約は変更しない）
- `human_judgment_required` → Step 5（human escalation）へ

## Policy 値

- `transport_policy`: `best_effort`（既定。transport 失敗時は 1 回だけ自動再実行し、
  それでも不能なら approve + warning で継続） | `required`（明示指定時のみ。transport
  失敗時は `human_judgment_required`）
- `finding_policy`: `route_high_open_to_rewrite`（常時有効な唯一の値。`severity: blocker|high`
  かつ `owner_disposition` が未記録の finding のみ rewrite へルーティングする）

## Owner Disposition の記録経路

`owner_disposition`（`status: accepted|deferred|rejected` / `reason` / `recorded_by: owner`）は
モデル出力に含まれない別フィールドであり、Owner または orchestrator が Issue コメント等の
記録を経て `semantic_review_result.json` の該当 finding へ追記する形で運用する
（`issue-design-reviewer` 自身は書き込めない）。
