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
| **state ラベル** | `state/needs-human` が付いていれば BLOCKED。それ以外の state ラベル（`state/blocked` 等）の有無は着手判定に影響しない |
| **Allowed Paths 明示** | `## Allowed Paths` が存在し、空でない |
| **VC 明示** | `## Verification Commands` が存在し、コマンドが 1 つ以上ある |
| **Stop Conditions 明示**（implementation のみ） | `## Stop Conditions` が 6 定型項目で埋まっている |

1 つでも fail なら **BLOCKED**。issue comment に「contract 不備」を投稿し、`issue-refinement-loop` を呼ぶことを提案する。

注: state ラベルチェックは `state/needs-human` 付与の有無のみを着手判定に用いる。`state/needs-human` が付いていない場合、その他の state ラベル（`state/blocked` 等）の有無は BLOCKED 判定に影響しない。着手可否の source of truth は Step 3 の open blocker / dependency 確認（GitHub native dependency / `Depends on #N` fallback）である。

### 3. blocker / dependency 全 close 確認（決定論的）

```bash
bash .claude/skills/issue-contract-review/scripts/check_blockers.sh <issue_number> <owner>/<repo>
```

| 結果 | 判定 |
|---|---|
| exit 0 / blocker なし | OK — 次ステップへ |
| exit 0 / 全 blocker closed | OK — 次ステップへ |
| exit 1 / blocker open あり | **BLOCKED** — human_escalation |
| exit 1 / native と `Depends on #N` 不一致 | **BLOCKED** — human_escalation |

- native dependency API (`/issues/<N>/dependencies/blocked_by`) を primary として使用する。
- native API が取得不可の場合のみ、Issue 本文の `Depends on #N` を fallback として使用する。
- native と `Depends on #N` が不一致の場合は自動判断せず human escalation とする。
- BLOCKED 時は issue comment に open blocker 番号・不一致内容を記載し、人間による確認を依頼する。go 判定は出さない。

Fallback `Depends on #N` parsing は専用セクション（例: `## Depends On`）の line-anchored 宣言文のみを対象とする:
`^- Depends on #<number>` または `^Depends on #<number>` の形式。
Delivery Rule の条件文・歴史的注記・コメント・Conditional examples は blocker と見なさない。
（実績: #262 で Delivery Rule 内の条件文「コンフリクトリスクがある場合は `Depends on #14 merge`」を fallback blocker と誤検出した事例がある。）

### 4. VC preflight（決定論的）

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

#### VC failure root cause 分類

各 VC が fail した場合、以下の 5 分類で root cause を判定する（正規表現マッチ + exit code の組み合わせ）:

| 分類 | 条件 | 判定 |
|---|---|---|
| `file_not_found_expected` | `test -f` / `test -d` 等の存在確認コマンド + exit 1 + stderr 空 | 期待通り baseline fail → GO |
| `file_not_found_unrunnable` | `No such file or directory` を含む + 実行対象ファイル/コマンドが missing | 想定外 → BLOCKED |
| `env_missing_dep` | stderr に `No module named`/`ModuleNotFoundError`/`ImportError`/`command not found`/`not found`/`ERR_MODULE_NOT_FOUND`/`Permission denied`/`is not executable` を含む、または exit code 126/127 | 想定外（env 不備）→ BLOCKED |
| `expected_baseline_fail` | grep/rg 系コマンドの exit 1 + no match、count assertion が threshold 未満 | 期待通り baseline fail → GO |
| `unknown` | 上記いずれにも該当しない | `human_judgment`（自動 GO しない）|

分類例:
- `uv run python -m pytest tests/` が `ModuleNotFoundError: No module named 'yaml'` で exit 1 → `env_missing_dep` → BLOCKED
- `rg -n "pattern" file` が exit 1 + no output → `expected_baseline_fail` → GO
- `python missing_script.py` が `No such file or directory` で exit 1 → `file_not_found_unrunnable` → BLOCKED
- `test -f new_feature.py` が exit 1 → `file_not_found_expected` → GO

`unknown` 判定時は自動 GO しない。`human_judgment` として扱い、人間に確認を求める。

- `env_missing_dep` 分類時は **BLOCKED** 判定とする（env_missing_dep → BLOCKED）。
- `file_not_found_unrunnable` 分類時は **BLOCKED** 判定とする（file_not_found_unrunnable → BLOCKED）。
- `unknown` 分類時は `human_judgment`（自動 GO しない）として人間の判断を求める（unknown → human_judgment）。

### 4.5. 動作検証 AC の実行環境前提チェック

Issue 本文の `## Runtime Verification Applicability` セクションを確認する。

- `decision: not_applicable` が明示されている → 本チェックをスキップ（次へ）
- セクション自体が存在しない:
  - implementation issue の場合 → **BLOCKED** または `human_judgment`（fail-closed）
  - non-implementation / legacy issue の場合 → warning（non-blocking）
- `decision: deferred` → 本チェックをスキップ（後続 Issue/フェーズで確認）
- `decision: immediate` → 以下の実行環境前提チェックを実施する

#### decision: immediate の場合の必須確認項目

contract-snapshot に以下がすべて明示されているかを確認する:

| 確認項目 | 判定 |
|---|---|
| 動作検証の対象 AC と VC が `applicable_acs` に明示されている | 不在 → BLOCKED |
| 動作検証に必要な実行環境（CLI ツール名・認証方法・ネットワーク要件等）が記載されている | 不在 → BLOCKED |
| 実行環境が整っていない場合の停止条件（exit 77 等の SKIP 規約）が記載されている | 不在 → BLOCKED |
| フォールバック経由の成功を PASS としない旨が明示されている | 不在 → BLOCKED（`_*_fallback: true` は PASS と見なさない原則の確認） |
| 証跡要件（artifact 出力先・ファイル名パターン等）が記載されている | 不在 → BLOCKED |

上記のいずれかが不足している場合は **BLOCKED** とし、issue comment に不足項目を列挙して `issue-refinement-loop` の起動を人間に提案する。

```bash
# Issue 本文の Runtime Verification Applicability セクションを確認
gh issue view <番号> --json body --jq '.body' | grep -A 20 "Runtime Verification Applicability"
```

> 動作検証の適用判定（immediate / deferred / not_applicable）の詳細規約は `docs/dev/runtime-verification-policy.md` を参照する。本 skill は「contract snapshot に前提が明示されているか」を決定論的に確認するのみ。実環境での動作確認は `implementation-worker` / `test-runner` の責務。

### 5. AC 検証可能性チェック（決定論的）

| 確認項目 | 判定 |
|---|---|
| **AC が `- [ ]` 形式** | チェックボックス形式で書かれている |
| **AC ⇔ VC 番号一致** | `# AC<N>` コメントが VC 内で AC 番号と一致 |
| **検証スクリプト型** | VC が `grep` / `test -f` / `pnpm test` 等の決定論的判定（exit code / 数値比較）を使っている |
| **意味的評価混入なし** | 「適切に動作する」「品質を改善する」等の主観表現が AC / VC にない |

1 つでも fail → BLOCKED。`issue-refinement-loop` を提案。

### 6. Worktree / Branch 命名 preflight

```bash
EXPECTED_WORKTREE=".claude/worktrees/issue-<番号>-<slug>"
EXPECTED_BRANCH="worktree-issue-<番号>-<slug>"
```

- `git worktree list` で既存 worktree と衝突しないこと
- `git branch --list "$EXPECTED_BRANCH"` で既存 branch がないこと
- `.claude/worktrees/` 配下である（外部配置でない）こと

衝突時は人間判断を求めて停止。

### 7. 出力契約（CONTRACT_REVIEW_RESULT_V1）

```yaml
CONTRACT_REVIEW_RESULT_V1:
  status: go | blocked
  generated_at: <ISO 8601>
  generated_by: issue-contract-review
  issue_url: https://github.com/<owner>/<repo>/issues/<番号>
  checks:
    template_compliance: pass | fail
    # state_label: ok | blocked
    # ok = state/needs-human ラベルなし（着手可能）
    # blocked = state/needs-human ラベルあり（着手不可）
    state_label: ok | blocked
    allowed_paths_present: pass | fail
    vc_present: pass | fail
    stop_conditions_complete: pass | fail | n/a
    vc_preflight:
      passed: true | false
      vc_failed_as_expected: <count>
      vc_passed_unexpectedly: <count>
      vc_unrunnable: <count>
      classifications:
        - ac: <AC番号>
          command: "<実行したコマンド>"
          exit_code: <int>
          category: file_not_found_expected | file_not_found_unrunnable | env_missing_dep | expected_baseline_fail | unknown
          confidence: high | medium | low
          evidence:
            stdout_excerpt: "<stdout の抜粋>"
            stderr_excerpt: "<stderr の抜粋>"
          decision: go | blocked | human_judgment
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
