# Context Protocol / Guardrails

## Context Protocol

### LOOP_STATE の更新タイミング

LOOP_STATE は **各 Step 完了直後** に必ず会話履歴へ明示記録する。次イテレーション開始時に最新値を読み戻す前提のため、口頭でのサマリでは置き換えない。

### SubAgent 出力の取扱い

各 SubAgent は構造化フォーマット（YAML / KEY=VALUE）で結果を返す:

| SubAgent | 出力契約 |
|---|---|
| `implementation-worker` | `IMPLEMENT_RESULT_V1` YAML |
| `test-runner` | `TEST_VERDICT_MACHINE v1` マーカー付きコメント + YAML |
| `pr-reviewer` | `LOOP_VERDICT` YAML（verdict コメント内） |

orchestrator はこれらを parse して LOOP_STATE に反映する。**散文サマリで上書きしない**（Context Rot 防止）。

### 外部仕様調査の判定根拠

外部仕様調査（gemini-cli-headless-delegation）のスキップ・実施判断は LOOP_STATE.external_research_skip_basis に記録する。後続の pr-reviewer 等がスキップ判断の妥当性を評価できる。

```yaml
external_research_skip_basis: "internal-only change in src/systems; no external spec dependency (iteration 0)"
```

## Guardrails

### control-plane / data-plane の分離

- orchestrator（本 skill）は **control-plane**（state tracking + routing）のみ
- data-plane 操作（push / `gh pr edit` / マージ / Issue 本文編集等）は対応する SubAgent に委譲する
- orchestrator が直接 `git push` や `gh pr create` を呼ばない

### 無限ループ防止

- `max_iterations` 超過時は必ず fail-close（デフォルト 5）
- 連続 conflict は 2 回までで自動 escalation
- SubAgent から `human_review_required: true` を受けたら即停止

### 冪等性

- 同一 PR に対して同じ Step を複数回呼んでも壊れないこと（test-runner は最新コメントを採用、pr-reviewer は head SHA で整合チェック）
- LOOP_STATE.iteration を厳密に追跡し、退行（roll back）しない

### Context 効率

- SubAgent 呼び出し前に必要最小限の context を準備（fix_delta は前イテレーションの blockers のみ）
- 全 SubAgent ログを orchestrator が抱え込まない（要約 + 構造化結果のみ保持）
- adversarial review は採用しないため Step 3 関連の context は持ち越さない
