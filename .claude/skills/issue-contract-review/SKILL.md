---
name: issue-contract-review
description: 人間承認後・実装着手直前に、Issue contract（作業計画・コンテクスト）が指定通りで開発フローに沿って AI Agent が安全に着手できるかを **決定論的に** preflight する skill。VC が baseline で fail することと AC が決定論的に検証可能であることを確認する。Issue 内容・文脈レビューは review-issue / issue-refinement-loop の責務で、本 skill では扱わない。branch / PR / worktree を作る前に必ず通す。
---

# Issue Contract Review

実装着手直前の **preflight skill**。Issue 本文・コメント・VC・AC を読んで「このまま `implement-issue` に渡せるか」を決定論的に判定する。

責務範囲は **「指定通りの作業計画 / コンテクスト / 開発フロー適合性」の事前確認** に絞る。Issue 本文の品質（Outcome 抽象性・AC 検証可能性等の構造的レビュー）は `review-issue`、Issue 改善ループは `issue-refinement-loop` の責務であり、本 skill では再判定しない。

## Input

- `Issue番号` または `Issue URL`（必須）

## Procedure

### 1. Issue contract を取得

```bash
gh issue view <番号> --json title,body,labels,comments
```

### 2. 開発フロー適合性チェック（決定論的）

| 確認項目 | 判定 |
|---|---|
| **テンプレ準拠** | `.github/ISSUE_TEMPLATE/{種別}.yml` の必須セクションがすべて存在 |
| **state ラベル** | `state/queued` または `state/in-progress` であって `state/needs-human` / `state/blocked` でない |
| **Allowed Paths 明示** | `## Allowed Paths` が存在し、空でない |
| **VC 明示** | `## Verification Commands` が存在し、コマンドが 1 つ以上ある |
| **Stop Conditions 明示**（implementation のみ） | `## Stop Conditions` が 6 定型項目で埋まっている |

1 つでも fail なら **BLOCKED**。issue comment に「contract 不備」を投稿し、`issue-refinement-loop` を呼ぶことを提案する。

### 3. VC preflight（決定論的）

**前提**: AC は「実装前 baseline で fail し、実装後に pass する」検証スクリプトとして書かれている。

```bash
# Issue 本文から VC を抽出し、現状（実装前 baseline）で実行
# 各 VC を実行して結果を集計
```

判定:
- **想定どおり fail**: OK（実装着手可）
- **想定外に pass**: VC 設定がおかしい（既に該当機能が存在する / VC が緩すぎる / Issue が既に解決済み）→ BLOCKED
- **実行不可能（コマンド不在 / ファイル不在）**: VC が誤っている → BLOCKED

BLOCKED 時は issue comment に該当 VC と理由を書き、`issue-refinement-loop` の起動を **人間に提案** する（自動起動しない）。

### 4. AC 検証可能性チェック（決定論的）

| 確認項目 | 判定 |
|---|---|
| **AC が `- [ ]` 形式** | チェックボックス形式で書かれている |
| **AC ⇔ VC 番号一致** | `# AC<N>` コメントが VC 内で AC 番号と一致 |
| **検証スクリプト型** | VC が `grep` / `test -f` / `pnpm test` 等の決定論的判定（exit code / 数値比較）を使っている |
| **意味的評価混入なし** | 「適切に動作する」「品質を改善する」等の主観表現が AC / VC にない |

1 つでも fail → BLOCKED。`issue-refinement-loop` を提案。

### 5. Worktree / Branch 命名 preflight

```bash
EXPECTED_WORKTREE=".claude/worktrees/issue-<番号>-<slug>"
EXPECTED_BRANCH="worktree-issue-<番号>-<slug>"
```

- `git worktree list` で既存 worktree と衝突しないこと
- `git branch --list "$EXPECTED_BRANCH"` で既存 branch がないこと
- `.claude/worktrees/` 配下である（外部配置でない）こと

衝突時は人間判断を求めて停止。

### 6. 出力契約（CONTRACT_REVIEW_RESULT_V1）

```yaml
CONTRACT_REVIEW_RESULT_V1:
  status: go | blocked
  generated_at: <ISO 8601>
  generated_by: issue-contract-review
  issue_url: https://github.com/<owner>/<repo>/issues/<番号>
  checks:
    template_compliance: pass | fail
    state_label: pass | fail
    allowed_paths_present: pass | fail
    vc_present: pass | fail
    stop_conditions_complete: pass | fail | n/a
    vc_preflight:
      passed: true | false
      vc_failed_as_expected: <count>
      vc_passed_unexpectedly: <count>
      vc_unrunnable: <count>
    ac_verifiability: pass | fail
    worktree_branch_collision: clear | conflict
  next_action: implement_issue | propose_refinement_loop | human_judgment
  blocked_reasons: []
  warnings: []
```

## Output (GitHub side)

| status | 書くもの | 停止 |
|---|---|---|
| `go` | contract-snapshot コメント（後述）を投稿。続けて `implement-issue` を呼ぶ準備完了 | 人間承認待ち（implement-issue 呼び出しの最終承認）|
| `blocked` | 不足理由 + `issue-refinement-loop` の提案コメントを投稿 | 人間判断待ち |

### contract-snapshot コメントの最小構成

```markdown
## Contract Snapshot ($(date -u +%Y-%m-%dT%H:%M:%SZ))

- Outcome: <1 文>
- Acceptance Criteria: <番号付き>
- Verification Commands: <列挙>
- Allowed Paths: <列挙>
- Worktree: `.claude/worktrees/issue-<番号>-<slug>`
- Branch: `worktree-issue-<番号>-<slug>`
- VC preflight: baseline fail = <count>, 想定外 pass = 0, 実行不可 = 0
```

### blocked コメントの最小構成

```markdown
## Contract Review: BLOCKED ($(date -u +%Y-%m-%dT%H:%M:%SZ))

### 不足項目
- <fail した check と理由>

### 推奨アクション
- `issue-refinement-loop` の起動を推奨します。承認後に実行可能です。
```

## Handoff to implement-issue

`status: go` 時に `implement-issue` へ渡す必須項目:

- Issue 番号
- contract-snapshot comment URL
- Outcome / Acceptance Criteria / Verification Commands / Allowed Paths / Required Skills
- Worktree / Branch 命名（preflight で確定したもの）

## Guardrails

- **Issue 本文の品質判定はしない**（`review-issue` の責務）
- **コード編集を開始しない**（preflight 専用）
- **VC を実装後の動作確認に使わない**（baseline 確認のみ）
- **branch / PR / worktree を本 skill では作らない**（`implement-issue` の責務）
- 不適合検出時は自動修復せず、`issue-refinement-loop` の起動を人間に提案する

## Related

- `docs/dev/dor.md` — Implementation Issue の Definition of Ready (DoR) 基準。本 skill の各チェック項目の正本定義先。
- `.claude/skills/review-issue/SKILL.md` — Issue 本文の構造的品質レビュー（本 skill の前段）
- `.claude/skills/issue-refinement-loop/SKILL.md` — 不適合時に提案する改善ループ
- `.claude/skills/implement-issue/SKILL.md` — `status: go` 時の handoff 先
- `.claude/skills/ssot-discovery/SKILL.md` — Issue 関連 SSOT の探索
- `.github/ISSUE_TEMPLATE/implementation.yml` — 必須セクションの正本
