# Context Protocol（作業手順）

## LOOP_STATE 更新タイミング

**各 Step 完了直後** に LOOP_STATE YAML を会話履歴へ明示記録する。次イテレーション開始時に最新値を読み戻す。

## SubAgent 出力の取扱い

各 SubAgent は構造化フォーマット（YAML / KEY=VALUE）で結果を返す。orchestrator はそれを parse して LOOP_STATE に反映する:

| SubAgent | 出力契約 | 受け取り方 |
|---|---|---|
| `implementation-worker` | `IMPLEMENT_RESULT_V1` YAML | `status` / `pr_url` / `verification` を LOOP_STATE へ |
| `test-runner` | `TEST_VERDICT_MACHINE v2` マーカー付き read-only report（呼び出し元への直接返却。test-runner は PR へコメントを投稿しない、Issue #1648, #88） | `spawn_agent` / `list_agents` の final result から直接受け取る（`gh pr view --json comments` からのTEST_VERDICT 抽出は normal routing として扱わない）。current-head binding tuple の照合結果である`VC_ADJUDICATION_RESULT_V1` を LOOP_STATE へ反映し、Step 4 起動可否の gate に使う（diagnostics-only の TEST_VERDICT_MACHINE 自体は APPROVE/REQUEST_CHANGES 判定の必須 blocking inputとしては扱わない。authoritative evidence は `CI_CHECK_RUN_SCOPED` と束縛済み独立実行 Issue VC。`.claude/skills/pr-review-judge/references/evidence-policy.md` 参照） |
| `pr-reviewer` | `LOOP_VERDICT` YAML（verdict コメント内） | step-5-mergeability-handling.md の抽出手順を使う |

## 外部仕様調査の判定根拠記録

外部仕様調査（`gemini-cli-headless-delegation`）のスキップ・実施判断は LOOP_STATE.external_research_skip_basis に記録する:

```yaml
external_research_skip_basis: "internal-only change in src/systems; no external spec dependency (iteration 0)"
```
