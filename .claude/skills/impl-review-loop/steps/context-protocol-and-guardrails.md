# Context Protocol（作業手順）

設計原則（control-plane / data-plane / LOOP_STATE / Context 効率 / 無限ループ防止 / 冪等性 / ループ内の人間承認）は `docs/dev/agent-skill-boundaries.md` の「オーケストレーター設計原則」セクションを正本とする。本ファイルは loop 実行中の手順詳細だけ書く。

## LOOP_STATE 更新タイミング

**各 Step 完了直後** に LOOP_STATE YAML を会話履歴へ明示記録する。次イテレーション開始時に最新値を読み戻す。

## SubAgent 出力の取扱い

各 SubAgent は構造化フォーマット（YAML / KEY=VALUE）で結果を返す。orchestrator はそれを parse して LOOP_STATE に反映する:

| SubAgent | 出力契約 | 受け取り方 |
|---|---|---|
| `implementation-worker` | `IMPLEMENT_RESULT_V1` YAML | `status` / `pr_url` / `verification` を LOOP_STATE へ |
| `test-runner` | `TEST_VERDICT_MACHINE v1` マーカー付き PR コメント | `gh pr view --json comments` 経由で抽出 |
| `pr-reviewer` | `LOOP_VERDICT` YAML（verdict コメント内） | step-5-mergeability-handling.md の抽出手順を使う |

## 外部仕様調査の判定根拠記録

外部仕様調査（`gemini-cli-headless-delegation`）のスキップ・実施判断は LOOP_STATE.external_research_skip_basis に記録する:

```yaml
external_research_skip_basis: "internal-only change in src/systems; no external spec dependency (iteration 0)"
```
