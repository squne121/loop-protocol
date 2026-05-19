---
name: pr-review-judge
description: implementation child issue に紐づく PR をレビューするときに使う。linked issue の contract（AC / Allowed Paths / Verification Commands）と PR 本文 / diff / 検証証跡を照合し APPROVE / REQUEST_CHANGES を判定する。self-authored PR は `gh pr review --comment` で verdict を記録する（`--approve` / `--request-changes` は使わない）。LOOP_VERDICT YAML を verdict コメントに含めて impl-review-loop の自動判定に使えるようにする。
---

# PR Review Judge

PR の差分・evidence と linked issue contract を照合し、verdict を決定する手順。

## Input

- `PR番号` または `PR URL`（必須）
- `reviewed_head_sha`（任意。orchestrator から渡された場合は LOOP_VERDICT YAML に転記する）

## Procedure

### 0. Self-authored PR ガード

PR author と実行アカウントが同一の場合は **`gh pr review --comment` のみ** を使う（`--approve` / `--request-changes` は使わない）。GitHub の制約で自分の PR を自分で approve できないため。

```bash
PR_AUTHOR=$(gh pr view <PR番号> --json author --jq '.author.login')
ACTOR=$(gh api user --jq '.login')
SELF_AUTHORED=$([ "$PR_AUTHOR" = "$ACTOR" ] && echo true || echo false)
```

### 1. Linked Issue を特定

```bash
gh pr view <PR番号> --json body --jq '.body' | grep -E "(Closes|Fixes|Resolves) #[0-9]+"
```

`Closes #N` がない PR は判定せず「linked issue を `Closes #N` で明示してください」と返して終了。

linked issue の以下を取得:
```bash
gh issue view <linked_issue> --json title,body,labels
```
- `## Outcome`
- `## Acceptance Criteria`
- `## Allowed Paths`
- `## Verification Commands`

### 2. Mergeable 状態を確認

test-runner SubAgent が投稿する `<!-- TEST_VERDICT_MACHINE v1 -->` マーカー付きコメントから取得するのを優先する:

```bash
TEST_VERDICT_BODY=$(gh pr view <PR番号> --json comments --jq '
  [.comments[] | select(.body | contains("<!-- TEST_VERDICT_MACHINE v1 -->"))] | last | .body
')
MERGEABLE=$(echo "$TEST_VERDICT_BODY" | grep "mergeable:" | head -n1 | sed -E 's/.*mergeable:[[:space:]]*//; s/[[:space:]]*$//')
MERGE_STATE_STATUS=$(echo "$TEST_VERDICT_BODY" | grep "merge_state_status:" | head -n1 | sed -E 's/.*merge_state_status:[[:space:]]*//; s/[[:space:]]*$//')
```

TEST_VERDICT_MACHINE コメントが見つからない場合のみ、フォールバックで:
```bash
gh pr view <PR番号> --json mergeable,mergeStateStatus
```

判定:
- `mergeable=CONFLICTING` または `mergeStateStatus=DIRTY` → **Conflict blocker**（REQUEST_CHANGES）
- `mergeStateStatus=BLOCKED` → **Merge blocker**（review/protection 待ち等、REQUEST_CHANGES）
- `mergeable=UNKNOWN`（retry 後も） → **Unknown blocker**（REQUEST_CHANGES）
- `mergeable=MERGEABLE` かつ `mergeStateStatus=CLEAN|UNSTABLE` → 次へ進む

### 3. CI 証跡を確認

```bash
gh pr checks <PR番号>
```

判定:
- 全チェックが `pass` / `success` → CI pass
- `fail` / `failure` が存在 → CI fail（blocker として記録、Step 4 へ進む）
- CI チェックが紐づいていない、または `pending` のみ → **CI 証跡なし blocker**（REQUEST_CHANGES、CI 完了後に再レビュー）

GitHub Actions が動いていない場合のフォールバック（test-runner 出力の `verification_commands_pass/fail` 数値で代替）:
```bash
echo "$TEST_VERDICT_BODY" | grep -E "verification_commands_(pass|fail):"
```
test-runner が `verification_commands_fail: 0` を返していれば CI 証跡相当として扱う。

### 4. PR Evidence をレビュー

PR 本文の `## 受け入れ条件の達成状況` / `## 検証コマンド結果` / `## Allowed Paths 遵守` を確認:

```bash
gh pr view <PR番号> --json body --jq '.body'
gh pr diff <PR番号> --name-only
```

判定項目:

| 項目 | 確認内容 | fail 時 |
|---|---|---|
| **AC coverage** | linked issue の各 AC が PR 本文の `## 受け入れ条件の達成状況` で `[x]` / `[ ]` + 根拠記載されている | blocker |
| **Allowed Paths 遵守** | `gh pr diff --name-only` の出力がすべて linked issue の `## Allowed Paths` に含まれる | blocker |
| **検証コマンド結果** | linked issue の `## Verification Commands` 各コマンドが PR 本文で結果記録されている（`✅ 通過` 等の具体記述） | blocker（雰囲気で通さない） |
| **scope 混入** | PR diff にスコープ外の修正・refactoring が混入していない | blocker |

placeholder のままの行（例: `[x] AC1: <達成（根拠）>` の `<...>` が残存）は証跡として数えず blocker。

### 5. verdict 決定

- Step 2-4 のいずれかで blocker → `REQUEST_CHANGES`
- blocker なし → `APPROVE`

self-authored PR の場合は **verdict 値に関わらず `--comment` で投稿**（GitHub 制約）。

### 6. verdict コメントを投稿

```bash
# self-authored
gh pr review <PR番号> --comment --body-file /tmp/pr-verdict-<PR番号>.md

# 他者の PR
gh pr review <PR番号> --approve --body-file /tmp/pr-verdict-<PR番号>.md
gh pr review <PR番号> --request-changes --body-file /tmp/pr-verdict-<PR番号>.md
```

## Verdict コメントテンプレート

````markdown
## Verdict: APPROVE | REQUEST_CHANGES

### Mergeability
- mergeable=<MERGEABLE|CONFLICTING|UNKNOWN>, mergeStateStatus=<CLEAN|UNSTABLE|DIRTY|BLOCKED|UNKNOWN>

### Evidence Check
- AC coverage: <○/△/×、根拠>
- Allowed Paths: <遵守 / 逸脱 + 該当ファイル>
- CI Verification: <gh pr checks の結果サマリ>
- 検証コマンド結果: <PR 本文の `## 検証コマンド結果` セクション要約>

### Blockers
<!-- 0 件なら「なし」と書く -->
- なし / <blocker 詳細>

### Non-blockers（任意改善）
- なし / <改善提案>

## LOOP_VERDICT
```yaml
verdict: APPROVE | REQUEST_CHANGES
blockers: []
mergeable: MERGEABLE | CONFLICTING | UNKNOWN
mergeStateStatus: CLEAN | UNSTABLE | DIRTY | BLOCKED | UNKNOWN
reviewed_head_sha: <SHA>
```
````

### LOOP_VERDICT YAML の制約

1. `reviewed_head_sha` は YAML ブロック **内** に記載する（外側は禁止）
2. コメント本文全体で `reviewed_head_sha:` 行は 1 つだけ（複数だと parse が最初の行のみ採用）
3. コードフェンス（` ``` `）は `\` でエスケープしない（heredoc 内でもそのまま書く）

## Stop Conditions

- linked issue が `Closes #N` で特定できない → 判定せず「`Closes #N` を PR 本文に追加してください」と返す
- PR 本文の `## 受け入れ条件の達成状況` / `## 検証コマンド結果` / `## Allowed Paths 遵守` が空欄 → `REQUEST_CHANGES`（雰囲気で通さない）
- linked issue の `## Allowed Paths` が空欄 → 「linked issue 側で Allowed Paths を明示してください」と返す

## Guardrails

- linked issue が不明な PR は判定しない（Stop Conditions）
- Issue contract にない完了条件を勝手に追加しない
- Evidence 不足を「雰囲気」で通さない
- self-authored PR では `gh pr review --approve` / `--request-changes` を使わない（必ず `--comment`）
- 曖昧な場合は APPROVE せず REQUEST_CHANGES（fail-closed）

## Output Contract

GitHub surface:
- self-authored: `gh pr review <番号> --comment --body-file <verdict.md>`
- 他者: `gh pr review <番号> --approve --body-file <verdict.md>` または `--request-changes`

stdout: 実行ログと verdict サマリ。verdict の正本は GitHub コメント側。

## Related

- `.claude/skills/implement-issue/SKILL.md` — PR 起票元の手順
- `.claude/skills/impl-review-loop/SKILL.md` — LOOP_VERDICT を読んで自動判定するオーケストレーター
- `.claude/agents/pr-reviewer.md` — 本 skill を使う SubAgent
- `.claude/agents/test-runner.md` — TEST_VERDICT_MACHINE を投稿する SubAgent
- `.github/PULL_REQUEST_TEMPLATE.md` — PR 本文テンプレ
- [`references/best-practices.md`](references/best-practices.md) — PR レビュー全般のベストプラクティス
- [`references/review-output-contract.md`](references/review-output-contract.md) — Verdict 出力契約の詳細
