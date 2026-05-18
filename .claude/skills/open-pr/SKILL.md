---
name: open-pr
description: 承認済みの implementation issue または refinement issue の PR を起票するときに使う。publish ゲート、idempotency チェック、Closes/Refs 使い分けガイダンス、canonical PR template 生成を担当する独立スキル。
---

# Open PR

承認済み issue の PR を起票する専用スキル。`implement-issue` / `impl-review-loop` から分離可能な再利用可能な形で、PR 作成ロジック・publish ゲート・idempotency チェック・Closes/Refs 使い分けを実装する。

## Use When

- implementation child issue / refinement issue の PR を起票したい
- publish ゲート（人間承認）を確認してから PR を作成したい
- 既存 PR がある場合は重複作成を避けて update したい
- `Closes` / `Refs` の使い分けが必要な場合
- 「PR 起票して」「PR を作成して」などの短文トリガー、または `implement-issue` / `impl-review-loop` からの委譲

## Do Not Use When

- PR の merge / close / reopen を行いたい
- 既存 PR を削除・リセットしたい
- リリース・publishing の自動化
- CI/CD の変更

## Input

**必須パラメータ:**

- `pr_title`: PR 本文の `## Summary` 行が対応。形式: `feat(component): description` など Conventional Commits に従う
- `linked_issue`: linked issue 番号（例: `1766`）。PR 本文の `Closes`/`Refs` に使う
- `publish`: 人間承認ゲート。`yes` が明示されていない場合は PR 作成を中断する

**任意パラメータ:**

- `pr_body`: PR 本文（`.github/PULL_REQUEST_TEMPLATE.md` に従う markdown）
- `pr_body_evidence`: PR Evidence テンプレートに基づく AC・Commands Run・Changed Paths 等のテキスト
- `dry_run`: `true` を指定した場合は、PR 作成ハンドシェイクを表示するが実際には作成しない（debug 用）
- `change_kind`: 変更粒度。`spec_only` / `code` / `mixed` のいずれか。未指定時は `mixed` を想定
- `parent_issue`: 親 issue 番号（任意。指定時は Closes/Refs 判定と優先順位に使用）
- `is_dependent`: `true|false`（任意。`parent_issue` が開いている場合の Refs 判定補助）
- `canonical_pr_url`: 呼び出し元がすでに canonical PR を確定している場合の URL。repair branch 再実行や loop 継続時に同一 issue の canonical PR を一意に保つために使う
- `repair_context`: 既存 PR を置き換える必要がある場合の追加文脈。`reason`、`previous_pr_url`、`mode=create-replacement|reuse-existing` を含め、repair branch での新規 PR 作成可否を明示する
- `superseded_prs`: Superseded mapping の候補を表す PR URL 一覧（JSON 配列文字列）

**caller-facing diagnostics contract:**

- wrapper failure は `ERROR=<code>` を必須で返し、必要に応じて `DIAGNOSTIC_STAGE` / `DIAGNOSTIC_KIND` / `FAILED_COMMAND` / `ERROR_DETAIL` / `COMMAND_STDERR` を追加する
- JSON を期待する `gh ... --json ...` 系の失敗は `DIAGNOSTIC_KIND=json-command-failure` または `json-parse-failure` で返す
- JSON shape が想定と違う contract failure は `DIAGNOSTIC_KIND=json-contract-failure` で返す
- `gh pr create` など非 JSON コマンドの失敗は `DIAGNOSTIC_KIND=non-json-command-failure` で返す
- caller 引数不備は `DIAGNOSTIC_KIND=input-validation-failure`、分類不能な wrapper 内部例外は `DIAGNOSTIC_KIND=unexpected-runtime-failure` で返す
- stderr は human-readable diagnostics 専用とし、stdout の `KEY=VALUE` 群を caller が parse する
- `COMMAND_STDERR` などの diagnostics value は `\n` / `\r` / `\t` / `=` / `\` を escaped surface で返し、1 行 1 key の contract を壊さない

`change_kind` は PR テンプレートの `## Linked Issue` セクションへ次のどれかを記載するための正規属性:
- `spec_only`: `docs_or_rules_only` lane。wrapper 互換のため値は維持しつつ、PR template 上は docs/rules-only lane として扱う
- `code`: 実装コード変更のみ
- `mixed`: 仕様更新 + コード変更

## Dry Run モード

`dry_run: true` を指定した場合、PR 作成を行わずにハンドシェイク（作成予定内容の表示）のみを実行します。この mode は以下の用途に使用します:

### 用途

- **PR テンプレート検証**: PR 本文の必須セクション完成度を確認する（PR 作成前の最終チェック）
- **統合テスト・デバッグ**: `implement-issue` / `impl-review-loop` から `open-pr` への委譲ロジックの動作確認
- **スキル成熟化テスト**: `open-pr` の PR 作成ロジック変更時に副作用がないことを確認する

### 動作仕様

1. **publish ゲートの実行**: `publish: yes` の有無を確認する（通常時と同じ）。
2. **静的 PR テンプレート preflight helper の実行**: `template-required-sections` helper で PR 本文の必須セクション確認を行う（通常時と同じ）。この段階で欠落があれば、既存 PR 検出より前に `E_PR_TEMPLATE_GUARD` を返す。
3. **Closes/Refs 変換プレビューの実行**: linked issue の state を確認して Closes/Refs を決定し、必要な downgrade プレビューを出力する（通常時と同じ）。
4. **Idempotency チェックの実行**: 既存 PR の有無を確認する（通常時と同じ）。dry_run ではテンプレートガードと Closes/Refs 変換プレビューを隠す短絡条件として使わない。
5. **ハンドシェイク表示**: PR 作成予定内容を標準出力に表示する。
6. **PR 非作成**: 実際の `gh pr create` コマンドは実行**しない**。

### 実装例

```bash
# dry_run モードで呼び出す（実行可能: `scripts/open-pr`）
scripts/open-pr \
  --pr_title "feat(skills/open-pr): dry_run セクション拡充" \
  --linked_issue 1770 \
  --pr_body "$(cat <<'EOF'
## Linked Issue
Closes #1770

## Summary
- dry_run モードの精細な仕様定義
- implement-issue からの段階的移行計画を記載

## Acceptance Criteria -> Evidence
- AC1: dry_run セクションに実装例・保証事項・テスト用途ガイドラインが追記されている ✓
- AC2: implement-issue の PR 作成セクションに open-pr 委譲オプションが明記されている ✓
- AC3: 移行計画が open-pr または implement-issue SKILL.md に記載されている ✓

## Commands Run
| Command | Exit Code | Scope | Notes |
|---|---:|---|---|
| `bash scripts/sync-agent-skills.sh --check` | 0 | docs_or_rules_only lane | no drifts detected |
| `git diff --check` | 0 | all lanes | format 崩れなし |

## Changed Paths
- .agents/skills/open-pr/SKILL.md
- .agents/skills/implement-issue/SKILL.md

## Risks
なし

## Rollback
docs 変更のみのため、必要に応じて該当コミットを revert する:
git revert <commit>

## Follow-ups Intentionally Deferred
- open-pr スキルの呼び出し側（implement-issue / impl-review-loop）の実際のリファクタリング（移行計画 follow-up Issue で対応予定）

## 類似 Issue 統合方針
PR #1767 の Follow-ups Intentionally Deferred を本 Issue に統合

## Knowledge Harvesting
- dry_run モードの用途を明確化（PR テンプレート検証・統合テスト・スキル成熟化テスト）
- 段階的移行計画の判断基準を明記（呼び出し側のリファクタリングロードマップ）

## Process / Skill / Agent Improvements
- open-pr スキルの publish ゲート・Idempotency チェック・テンプレートガード・Closes/Refs 判定が dry_run モードでも完全に実行される仕様であるため、test 環境での予行演習に有用

## Renumbering / Identifier Migration
なし

## Long-form Evidence
dry_run モードは、実装側（implement-issue）が PR 作成前に「予定内容」を確認するために使用される。以下の観点で PR テンプレートの完成度を検証できる:
1. 必須セクションの欠落確認
2. Closes/Refs の自動判定の適正性確認
3. published Issue とのリンク確認
EOF
)" \
  --publish yes \
  --dry_run true
```

### ハンドシェイク表示例

```
[DRY_RUN] PR 作成プレビュー
================================================
PR Title: feat(skills/open-pr): dry_run セクション拡充
Branch: feat/issue-1770-improve-skills-open-pr-dry-run-implement-issue
Linked Issue: Closes #1770
PR Status: DRAFT

PR Body Preview:
================================================
## Linked Issue
Closes #1770

## Summary
- dry_run モードの精細な仕様定義
...（以下省略）

================================================
[DRY_RUN] 実際の PR 作成は実行されていません。
[DRY_RUN] 内容確認後、publish: yes で再度呼び出してください。
```

### 保証事項

- `dry_run: true` を指定しても、`publish: yes` が未指定の場合は `E_APPROVAL_MISSING` を返す（ゲートは完全に実行）
- `dry_run: true` を指定しても、PR テンプレートガードが失敗すれば `E_PR_TEMPLATE_GUARD` を返す（ガードは完全に実行）
- `dry_run: true` では、PR テンプレートガードと Closes/Refs 変換プレビューを idempotency 短絡より前に必ず実行する
- `dry_run: true` で既存 PR が検出される場合でも、先に検出した `E_PR_TEMPLATE_GUARD` は隠さずに返す
- `dry_run: true` で PR テンプレートガードが成功した場合のみ、既存 PR 検出時に既存 PR URL を出力する（Idempotency チェックは完全に実行）
- 実際には `gh pr create` コマンドは実行**しない**ため、同一ブランチに重複 PR を生成しない

## Publish ゲート

### ゲート条件

- `publish: yes` が明示されていない場合、PR 作成を中断し以下を出力して停止する:

```
[ERROR] E_APPROVAL_MISSING: publish: yes が指定されていません。

人間承認が必要です。以下を実行してから再度呼び出してください:
- 実装結果を確認し、Release Readiness を判定する
- 「publish: yes」を明示的に指示する

参照: https://github.com/..../issues/<linked_issue>#comment-xxxxx
```

- `publish: yes` が確認された場合のみ、以下のステップへ進む。

## Idempotency Check（既存 PR の検出と update 判定）

このセクションは、publish gate / template guard / Closes-Refs state 判定が完了した後に実行する。

### 手順

1. **既存 PR の検出**:
   ```bash
   BRANCH=$(git branch --show-current)
   EXISTING_PR=$(gh pr list --head "$BRANCH" --state open --json number,title,url,state,headRefName,updatedAt | jq '.[] | select(.state == "OPEN")')
   SAME_ISSUE_OPEN_PRS=$(gh pr list --state open \
     --search "\"#${linked_issue}\" in:body" \
     --json number,title,url,state,headRefName,updatedAt,isDraft)
   ```
   - 同一ブランチに既存の `OPEN` PR がある場合、PR 番号を記録する。
   - `state` を `--json` に含めることで、`jq '.state == "OPEN"'` フィルタで既存 PR 分岐へ到達できる。
   - `SAME_ISSUE_OPEN_PRS` は `Closes #<issue>` / `Refs #<issue>` のどちらでもヒットする issue 単位の候補一覧として扱う。
   - repair branch で branch 名が変わっても、issue 本文リンクで同一 issue の既存 PR を検出できることを優先する。

2. **update / create の判定**:
   - **既存 PR がある場合**:
     - PR の merge status を確認する（`gh pr view <PR> --json mergeStateStatus`）。
     - `mergeStateStatus` が `DIRTY` / `BLOCKED` の場合、PR は既にレビュー待ち・修正待ちの状態であり、重複を避けて **update しない**。代わり、既存 PR URL を出力して終了する。
     - `mergeStateStatus` が `CLEAN` / `UNSTABLE` / `UNKNOWN` の場合、重複作成を避けるため既存 PR を参照して **update しない** のが原則だが、本 skill では「重複 PR の作成」を避けるのが主目的であるため、既存 PR URL を出力して終了する。
   - **repair branch で replacement PR が必要な場合**:
     - `repair_context.mode=create-replacement` と `repair_context.previous_pr_url` の両方が指定されている場合は、この判定を same-issue reuse より優先する。
     - `repair_context.previous_pr_url` が唯一の `SAME_ISSUE_OPEN_PRS` と一致するなら、新規 PR 作成へ進んでよい。
     - この場合は作成後に `SUPERSEDED_PR_URL=<repair_context.previous_pr_url>` を出力し、post-merge-cleanup または caller が destination mapping と close を引き継ぐ。
   - **同一 issue の open PR が 1 件だけ存在し、`canonical_pr_url` が未指定の場合**:
     - その PR を canonical PR とみなし、branch が異なっていても新規 PR は作成しない。
     - `PR_URL=<url> (existing)` と `CANONICAL_PR_URL=<url>` を出力して終了する。
     - これは repair branch の再実行で `#1945` と `#1946` のような重複 PR を再発させないための default である。
   - **`canonical_pr_url` が指定されている場合**:
     - その URL が `SAME_ISSUE_OPEN_PRS` の候補一覧に含まれるか、または `repair_context.mode=create-replacement` で置換対象 `previous_pr_url` と対になる canonical PR であることを検証する。
     - linked issue が一致しない、候補一覧に存在しない、または close 済みの別 issue PR を指している場合は `E_CANONICAL_PR_INVALID` を返して停止する。
   - **同一 issue の open PR が複数ある場合**:
     - `canonical_pr_url` が指定されていれば、その URL を canonical として採用する。
     - `canonical_pr_url` が未指定なら `E_CANONICAL_PR_AMBIGUOUS` を返して停止する。呼び出し元は LOOP_STATE / issue comment に canonical PR を先に記録してから再試行すること。
   - **上記いずれにも当てはまらない場合**: 新規 PR を create する（以下 Step 3）。

   **出力例**:
   ```
   [IDEMPOTENT] 既存 PR を検出しました:
   PR #1766: feat(skills): PR 起票専用エージェントスキル（open-pr）を新規追加する
   URL: https://github.com/squne121/KindleAudiobookMakeSystem/pull/1766
   PR_URL=https://github.com/squne121/KindleAudiobookMakeSystem/pull/1766 (existing)
   CANONICAL_PR_URL=https://github.com/squne121/KindleAudiobookMakeSystem/pull/1766
   CANONICAL_PR_SOURCE=same-issue-open-pr
   LINKED_ISSUE_ACTION=<computed: Closes|Refs>
   EXISTING_PR_BODY_UPDATED=false

   PR 本文を更新する場合は、以下を実行してください:
   gh pr edit 1766 --body "..."
   ```

   linked issue が CLOSED で downgrade が発生した場合は、既存 PR 分岐でも以下を併記する:
   - `WARN_DOWNGRADE=Closes->Refs`
   - `LINKED_ISSUE_ACTION=Refs`

### エラーコード

- `E_DUPLICATE_PR`: 同一ブランチに既存 PR がありかつ update 不可の場合（本スキルではこのエラーを返さず、既存 PR を出力して停止）
- `E_CANONICAL_PR_AMBIGUOUS`: 同一 issue の open PR が複数あり、canonical PR を一意に決められない
- `E_CANONICAL_PR_INVALID`: `canonical_pr_url` が対象 issue の PR 候補と一致せず、誤った canonical 判定になる

## Closes / Refs 使い分け

### 判定ロジック

Linked issue の状態により、`Closes` か `Refs` か を自動判定する:

```bash
# linked_issue の状態を確認
ISSUE_STATE=$(gh issue view "$linked_issue" --json state --jq '.state')

case "$ISSUE_STATE" in
  OPEN)
    KEYWORD="Closes"  # Issue が open → Closes で close する
    ;;
  CLOSED)
    KEYWORD="Refs"    # Issue が既に close 済み → Refs で参照のみ
    ;;
  *)
    echo "[ERROR] E_LINKED_ISSUE_STATE_UNKNOWN: Issue #$linked_issue の状態が不明です（$ISSUE_STATE）"
    exit 1
    ;;
esac
```

### State Conflict 検出

**`Closes` に close 済み Issue を指定した場合**:
- Issue の state を確認してから `Closes` / `Refs` の使い分けを行う。
- 呼び出し元（`implement-issue` 等）が誤って `Closes` を指定した場合、本スキルが自動的に `Refs` に downgrade する。
- downgrade を行った場合は以下を output して続行する（エラー扱いにしない）:

```
[WARN] Closes → Refs に downgrade しました:
  - Issue #1766 は既に CLOSED です
  - PR 本文の `Closes #1766` を `Refs #1766` に変更しました
  - 理由: 既存 PR が先に close した、または別理由で close 済みの場合、重複 close を避けるため Refs を使用します
WARN_DOWNGRADE=Closes->Refs
LINKED_ISSUE_ACTION=Refs
```

- downgrade ケースはエラーコード `E_LINKED_ISSUE_STATE_CONFLICT` を返す**のではなく**、自動修正して続行する。

### Multi-issue supersede 統合ケース

本スキルの自動判定（前掲）は **single-linked_issue** の保守的 downgrade を行う。複数 issue を supersede 統合する PR では、以下のとおり `Closes` を明示的に複数 listing してよい:

- 条件: 本 PR が当該 issue のスコープを統合・吸収している（PR 本文の `## Background` / `## 類似 Issue 統合方針` で supersede 関係を明示）
- 動作: linked issue が CLOSED でも `Closes #N` を PR 本文に複数 listing する。GitHub API は no-op で扱うため重複 close の害はなく、IssueOps timeline 上に「supersede 統合の trail signal」が残る
- 注意: spec-status / IssueOps カウント系ツールが「この PR は N issue を close する」と誤集計する可能性がある。PR 本文の「類似 Issue 統合方針」表で supersede 関係を明示することで誤集計を読み解けるようにする
- 例: PR #2208 が `Closes #2142, Closes #1460, Closes #1821` を使い、#1460 / #1821 は既に CLOSED だった

判断基準の詳細 decision tree は `.agents/rules/github-ops-workflow.md` の **KH-N8: Closes / Refs 使い分け decision tree** を参照。

### Skill 経路 / Orchestrator 直接記述経路の責任分担（F1 解消）

`open-pr` skill が input 引数 `linked_issue` を受け取って実行する自動判定（前掲の bash ロジック）は、**single-linked_issue 入力に対する保守的 safety net** として動作する。CLOSED issue は無条件に Refs に downgrade される。

multi-issue supersede 統合ケースで `Closes #N` を多重 listing する場合、呼び出し元（`implement-issue` / orchestrator）は **`open-pr` skill の自動判定を経由せず**、`gh pr create --body` で PR 本文を直接構築する。

| 経路 | 入力 | CLOSED issue 処理 |
|---|---|---|
| `open-pr` skill 自動判定 | `linked_issue=<N>` 単一 | 無条件 Refs downgrade（既存挙動） |
| Orchestrator 直接記述 | PR 本文に複数 `Closes #N` | KH-N8 supersede 判定に従い Closes 維持 |

この境界が崩れる（orchestrator が `open-pr` skill を経由して multi-supersede を試みる）と、自動 downgrade が KH-N8 のガイダンスを上書きしてしまう。multi-supersede ケースは skill bypass を前提とする。

将来 `open-pr` skill に `supersede_issues: [<N>, ...]` のような optional 入力を追加して downgrade を抑制する拡張を行う場合は、別 Issue で扱う（本 PR では Out of Scope）。

### ガイダンス例

**AC4: Closes/Refs state conflict の処理確認（expected behavior）**:

```bash
# OPEN Issue に対して Closes を指定した場合
input_linked_issue=1766 input_keyword="Closes"  # Issue #1766 は OPEN
# → PR 本文に「Closes #1766」を記載して create / update

# CLOSED Issue に対して Closes を指定した場合
input_linked_issue=1765 input_keyword="Closes"  # Issue #1765 は CLOSED
# → PR 本文に「Refs #1765」に downgrade して記載、WARN を出力して続行
```

## PR 作成手順

### Step 1: PR テンプレートの canonical 形式を確認

`.github/PULL_REQUEST_TEMPLATE.md` の必須セクションをすべて満たしているか確認する。

親 Issue を持つ implementation PR では、Issue 本文の `## Parent Goal Ref` / `## Current Validated Scope` / `## Remaining Parent Gaps` を PR 本文へ引き継ぐ。`Desired Destination` が親側にだけ存在する場合でも、PR では `## Parent Goal Ref` に参照を残し、validated されていない将来 destination を `## Current Validated Scope` へ混ぜない。

**必須セクション preflight（fail-closed）**:
- `## Linked Issue` — `Closes #N` または `Refs #N` を含む
- `## Parent Goal Ref`
- `## Summary`
- `## Current Validated Scope`
- `## Remaining Parent Gaps`
- `## Acceptance Criteria -> Evidence`
- `## Normalized Findings`
- `## Commands Run`
  - `change_kind=spec_only`（docs_or_rules_only lane）でも表形式で command / exit code / scope / notes を残す
  - `just check` を省略する場合は `Scope` または `Notes` に targeted check か `対象外` の理由を明記する
- `## Changed Paths`
- `## Risks`
- `## Rollback`
- `## Follow-ups Intentionally Deferred`
- `## 類似 Issue 統合方針`
- `## Knowledge Harvesting`
- `## Process / Skill / Agent Improvements`（implementation issue の場合）
- `## Renumbering / Identifier Migration`
- `## Long-form Evidence`

**preflight helper / 検査コマンド（fail-closed）**:

```bash
# PR テンプレートの見出し一覧を取得
TEMPLATE_HEADERS=$(grep "^## " .github/PULL_REQUEST_TEMPLATE.md | sed 's/^## //')

# PR 本文から見出しを取得
PR_BODY_HEADERS=$(echo "$pr_body" | grep "^## " | sed 's/^## //')

# 不足セクションをチェック
MISSING_HEADERS=()
while IFS= read -r header; do
  if ! echo "$PR_BODY_HEADERS" | grep -q "^$header$"; then
    MISSING_HEADERS+=("$header")
  fi
done <<< "$TEMPLATE_HEADERS"

if [ ${#MISSING_HEADERS[@]} -gt 0 ]; then
  echo "[ERROR] PR Template Guard: Missing sections: ${MISSING_HEADERS[@]}"
  echo "ERROR=E_PR_TEMPLATE_GUARD"
  echo "DIAGNOSTIC_STAGE=pr-template-preflight"
  echo "DIAGNOSTIC_KIND=template-preflight-failure"
  echo "PREFLIGHT_CHECK=template-required-sections"
  echo "MISSING_SECTIONS=<section1,section2,...>"
  exit 1
fi

# PR Evidence mirror の構造ドリフト検知（template-driven fail-closed）
python3 scripts/sync-pr-evidence-template.py --check
```

### Step 2: Linked Issue に `Closes` / `Refs` を設定

#### 2-1: `change_kind` / リンク先種別決定

`linked_issue` の state と依存関係からリンク種別を決定する。優先順位は以下:

1. `parent_issue` が指定され、当該 parent が `CLOSED` の場合: `parent CLOSED -> Refs`
2. `parent_issue` が指定され、`is_dependent: true` の場合: `parent OPEN and dependent -> Refs`
3. それ以外は `linked_issue` の state による従来判定 (`OPEN -> Closes`, `CLOSED -> Refs`)

`change_kind` が指定されていない場合はデフォルト `mixed` とし、`pr_body` の `## Linked Issue` セクションに `change_kind: mixed` を追記する。`spec_only` は wrapper 上の値を維持しつつ、PR template では `docs_or_rules_only` lane として解釈する。

1. `parent_issue`/`is_dependent` の情報で `KEYWORD` を先に決定する（上記優先順位）
2. 決まらない場合は `linked_issue` の state から従来判定する
3. `KEYWORD` と `change_kind` を `pr_body` の `## Linked Issue` セクションへ反映する
4. 必要に応じて downgrade / dependency downgrade 警告を出す

```bash
if [ -n "${parent_issue:-}" ]; then
  ISSUE_STATE=$(gh issue view "$parent_issue" --json state --jq '.state')
  if [ "$ISSUE_STATE" = "CLOSED" ]; then
    KEYWORD="Refs"
  elif [ "${is_dependent:-false}" = "true" ]; then
    KEYWORD="Refs"
  fi
fi

if [ -z "${KEYWORD:-}" ]; then
  ISSUE_STATE=$(gh issue view "$linked_issue" --json state --jq '.state')
  case "$ISSUE_STATE" in
    OPEN) KEYWORD="Closes" ;;
    CLOSED) KEYWORD="Refs" ;;
    *) echo "[ERROR] E_LINKED_ISSUE_STATE_UNKNOWN: Issue #$linked_issue の状態が不明です（$ISSUE_STATE）"; exit 1 ;;
  esac
fi

if [ "$KEYWORD" = "Closes" ] && [ "${is_dependent:-false}" = "true" ]; then
  echo "[WARN] is_dependent=true を明示した場合は Refs を優先します。"
  KEYWORD="Refs"
fi

CHANGE_KIND=${change_kind:-mixed}
case "$CHANGE_KIND" in
  spec_only|code|mixed) ;;
  *) echo "[ERROR] E_OPEN_PR_CHANGE_KIND_INVALID: change_kind は spec_only|code|mixed のみ許可です（$CHANGE_KIND）"; exit 1 ;;
esac
```

### Step 2.X: Linked Issue セクション正規化（追記補助）
# 旧ロジックは `## Linked Issue` セクション再構築で上書き正規化する。
```bash
normalize_linked_issue_section() {
  local body="$1"
  local in_section=0
  local section_found=0
  local seen_keyword=0
  local seen_kind=0
  local -a out_lines=()
  local linked_line="$KEYWORD #$linked_issue"
  local kind_line="change_kind: $CHANGE_KIND"

  while IFS= read -r line; do
    if [[ "$line" == "## Linked Issue" ]]; then
      in_section=1
      section_found=1
      seen_keyword=0
      seen_kind=0
      out_lines+=("$line")
      continue
    fi

    if [[ "$in_section" == "1" && "$line" == "## "* ]]; then
      if [[ "$seen_keyword" == "0" ]]; then
        out_lines+=("$linked_line")
        seen_keyword=1
      fi
      if [[ "$seen_kind" == "0" ]]; then
        out_lines+=("$kind_line")
        seen_kind=1
      fi
      out_lines+=("$line")
      in_section=0
      continue
    fi

    if [[ "$in_section" == "1" ]]; then
      if [[ "$line" =~ ^(Closes|Refs)\ #[0-9]+$ ]]; then
        if [[ "$seen_keyword" == "0" ]]; then
          out_lines+=("$linked_line")
          seen_keyword=1
        fi
        continue
      fi
      if [[ "$line" =~ ^change_kind: ]]; then
        if [[ "$seen_kind" == "0" ]]; then
          out_lines+=("$kind_line")
          seen_kind=1
        fi
        continue
      fi
      out_lines+=("$line")
      continue
    fi

    out_lines+=("$line")
  done <<< "$body"

  if [[ "$in_section" == "1" ]]; then
    if [[ "$seen_keyword" == "0" ]]; then
      out_lines+=("$linked_line")
    fi
    if [[ "$seen_kind" == "0" ]]; then
      out_lines+=("$kind_line")
    fi
  fi

  if [[ "$section_found" == "0" ]]; then
    out_lines+=("")
    out_lines+=("## Linked Issue")
    out_lines+=("$linked_line")
    out_lines+=("$kind_line")
  fi

  printf "%s\n" "${out_lines[@]}"
}

pr_body="$(normalize_linked_issue_section "$pr_body")"

# 簡易確認（実行例）
# - 既存に `## Linked Issue` / `Closes #N` / `change_kind:` がある場合:
KEYWORD=Refs
CHANGE_KIND=code
linked_issue=1765
pr_body=$'## Linked Issue\nCloses #1765\nchange_kind: code\n## Notes'
normalize_linked_issue_section "$pr_body" | sed -n l
# → `Linked Issue` / `change_kind:` が実改行で保持され、literal `\\n` が含まれないことを確認

# - 既存の `## Linked Issue` が無い場合:
KEYWORD=Closes
CHANGE_KIND=mixed
linked_issue=1770
pr_body=$'## Summary\n- update'
normalize_linked_issue_section "$pr_body" | sed -n l
# → `## Linked Issue` の行が末尾で追記されることを確認
# 既存の `## Linked Issue` があるが `change_kind:` がない本文:
KEYWORD=Refs
CHANGE_KIND=code
linked_issue=1765
case_body=$'## Linked Issue\nCloses #1765\n## Notes'
normalize_linked_issue_section "$case_body" | sed -n l
printf "linked_count=%s\\n" "$(normalize_linked_issue_section "$case_body" | grep -c '^Closes\|^Refs')"
printf "kind_count=%s\\n" "$(normalize_linked_issue_section "$case_body" | grep -c '^change_kind:')"

# `## Linked Issue` が無い本文:
case_body=$'## Summary\n- update'
normalize_linked_issue_section "$case_body" | sed -n l
printf "linked_count=%s\\n" "$(normalize_linked_issue_section "$case_body" | grep -c '^Closes\|^Refs')"
printf "kind_count=%s\\n" "$(normalize_linked_issue_section "$case_body" | grep -c '^change_kind:')"


```
### Step 3: PR を create または update する

**create の場合**:

```bash
gh pr create --draft \
  --title "$pr_title" \
  --body "$pr_body" \
  --head "$(git branch --show-current)"
```

**既存 PR が検出された場合**:
- 重複 PR 作成を避けるため、既存 PR URL を出力して終了する。update は本スキルでは行わない。
- 呼び出し側向けに `EXISTING_PR_BODY_UPDATED=false` を出力し、既存 PR 本文が未更新であることを明示する。
- `repair_context.mode=create-replacement` で replacement PR を作成した場合は、呼び出し側向けに `SUPERSEDED_PR_URL=<old_pr_url>` と `CANONICAL_PR_SOURCE=repair-replacement` を出力する。

### Step 4: PR URL を取得して返す

```bash
# `gh pr create` は URL を stdout に直接返す
PR_URL=$(gh pr create ... )
echo "[SUCCESS] PR が作成されました: $PR_URL"
echo "PR_URL=$PR_URL"
echo "CANONICAL_PR_URL=$PR_URL"
echo "CANONICAL_PR_SOURCE=new-pr"
echo "LINKED_ISSUE_ACTION=$KEYWORD"
```

## Error Codes

| コード | 説明 | 呼び出し元の対応 |
|---|---|---|
| `E_APPROVAL_MISSING` | `publish: yes` が未指定 | 人間承認を受け、再度呼び出す |
| `E_LINKED_ISSUE_STATE_UNKNOWN` | linked issue の state が不明（API error など） | Issue を確認してから retry |
| `E_OPEN_PR_CHANGE_KIND_INVALID` | `change_kind` が `spec_only|code|mixed` 以外 | 値を修正して再実行 |
| `E_PR_TEMPLATE_GUARD` | PR テンプレートの必須セクション欠落 | PR 本文に不足セクションを追加 |
| `E_BRANCH_NOT_FOUND` | 現在の branch が remote に存在しない | push してから再度呼び出す |
| `E_GH_PR_CREATE_FAILED` | `gh pr create` が失敗 | `gh` 認証・ブランチ存在確認・PR 権限を確認 |
| `E_CANONICAL_PR_AMBIGUOUS` | 同一 issue の open PR が複数あり canonical PR を一意に決められない | caller が `canonical_pr_url` を確定して再試行 |
| `E_CANONICAL_PR_INVALID` | `canonical_pr_url` が同一 issue の候補 PR と一致しない | caller が canonical PR の URL を修正して再試行 |

## Output Contract（呼び出し側互換）

`implement-issue` / `impl-review-loop` は、本スキルの出力を以下のキーで取り込む。

- `PR_URL=<url>`: PR 作成成功時の正規 URL（Step 4 の取得結果）
- `PR_URL=<url> (existing)`: 既存 PR 検出時の URL（idempotency）
- `CANONICAL_PR_URL=<url>`: 同一 issue に対する canonical PR の URL
- `CANONICAL_PR_SOURCE=exact-branch|same-issue-open-pr|new-pr|repair-replacement|caller-specified`: canonical 判定の根拠
- `LINKED_ISSUE_ACTION=Closes|Refs`: linked issue state 判定の最終結果
- `SUPERSEDED_PRS=none|url1,url2,...`: 呼び出し元が持つ destination mapping 対象 PR 一覧
- `change_kind=spec_only|code|mixed`: 反映された change_kind
- `WARN_DOWNGRADE=Closes->Refs`: close 済み issue に対して downgrade した場合のみ出力
- `EXISTING_PR_BODY_UPDATED=false`: 既存 PR 検出により PR 本文を update していないことを示す
- `SUPERSEDED_PR_URL=<url>`: replacement PR 作成時に superseded として close 対象へ渡す旧 PR
- `ERROR=E_PR_TEMPLATE_GUARD`: PR テンプレート必須セクション不足。PR は未作成
- `DIAGNOSTIC_STAGE=<stage>`: `pr-template-preflight` / `linked-issue-state` / `same-issue-open-pr-discovery` / `pr-create` などの caller-facing failure stage
- `DIAGNOSTIC_KIND=<kind>`: `template-preflight-failure` / `json-command-failure` / `json-parse-failure` / `json-contract-failure` / `non-json-command-failure` / `input-validation-failure` / `canonical-pr-resolution-failure` / `unexpected-runtime-failure`
- `FAILED_COMMAND=<preview>`: 失敗した `gh` コマンドの短縮 preview
- `ERROR_DETAIL=<detail>`: caller が分岐に使える細粒度理由
- `COMMAND_STDERR=<stderr>`: escaped 済み `gh` stderr の machine-readable surface（`\n` / `\r` / `\t` / `=` / `\` を escape）
- `ERROR=E_OPEN_PR_ARGUMENT_INVALID`: argparse / required args / bool parse failure など、wrapper 起動直後の入力失敗
- `PREFLIGHT_CHECK=template-required-sections`: required section preflight helper 名

### 出力パターン

1. 作成成功:
   - `PR_URL=<url>`
   - `CANONICAL_PR_URL=<url>`
   - `CANONICAL_PR_SOURCE=new-pr`
   - `LINKED_ISSUE_ACTION=Closes` または `LINKED_ISSUE_ACTION=Refs`
2. 既存 PR 検出（Idempotency）:
   - `PR_URL=<url> (existing)`
   - `CANONICAL_PR_URL=<url>`
   - `CANONICAL_PR_SOURCE=exact-branch|same-issue-open-pr|caller-specified`
   - `LINKED_ISSUE_ACTION=Closes` または `LINKED_ISSUE_ACTION=Refs`
   - `EXISTING_PR_BODY_UPDATED=false`
3. Closes/Refs downgrade:
   - `PR_URL=<url>` または `PR_URL=<url> (existing)`
   - `LINKED_ISSUE_ACTION=Refs`
   - `WARN_DOWNGRADE=Closes->Refs`
4. replacement PR 作成:
   - `PR_URL=<new_url>`
   - `CANONICAL_PR_URL=<new_url>`
   - `CANONICAL_PR_SOURCE=repair-replacement`
   - `SUPERSEDED_PR_URL=<old_url>`
5. template guard failure:
   - `ERROR=E_PR_TEMPLATE_GUARD`
   - `DIAGNOSTIC_STAGE=pr-template-preflight`
   - `DIAGNOSTIC_KIND=template-preflight-failure`
   - `PREFLIGHT_CHECK=template-required-sections`
   - `MISSING_SECTIONS=<section1,section2,...>`
   - `PR_URL` は出力しない

## Guardrails

- `publish: yes` が未指定の場合は必ず `E_APPROVAL_MISSING` を返し停止する。勝手に PR 作成を進めない。
- PR template guard で不足セクションを検出した場合は PR 作成を中断し、セクション一覧を出力する。
- 既存 PR がある場合は重複作成を避け、既存 PR URL を出力して終了する。update は行わない（update は別 skill）。
- 同一 issue の open PR が複数あるのに `canonical_pr_url` が無い場合は fail-close する。曖昧なまま新規 PR を作ってはならない。
- `canonical_pr_url` を caller が指定した場合でも、その URL が対象 issue の PR 候補かを必ず検証する。別 issue の PR を canonical として採用してはならない。
- repair branch での新規 PR 作成は `repair_context.mode=create-replacement` と `repair_context.previous_pr_url` がそろった場合に限定し、same-issue reuse より先に判定する。
- `repair_context.mode=create-replacement` でも、`previous_pr_url` 以外に strong match の canonical 候補が存在する場合は `E_CANONICAL_PR_AMBIGUOUS` で停止する。replacement を理由に別 canonical PR を増やしてはならない。
- `same-issue` strong match は title/body の `#<issue>` 判定と、`headRefName` の `issue-<issue>` / `issue_<issue>` / `/issue-<issue>-...` 系 branch convention 判定を分けて扱う。
- `gh pr create` の前に現在 branch の remote 公開有無を確認し、未 push の場合は `E_BRANCH_NOT_FOUND` を返して caller が push retry できるようにする。
- Closes/Refs の downgrade は自動実行するが、output には必ず WARN を記載する。
- `dry_run: true` を指定した場合は PR ハンドシェイクを表示するが実際には `gh pr create` を実行しない。

## 段階的移行計画（implement-issue / impl-review-loop からの委譲）

本スキルは `implement-issue` / `impl-review-loop` から段階的に PR 作成責務を受け取る再利用可能なスキルとして設計されています。以下は段階的な統合・移行計画です。

### 背景

- `implement-issue` SKILL.md は元々 Step 7 で PR 作成・テンプレートガード・Idempotency チェック・Closes/Refs 判定を内部実装していた
- PR #1767 で `open-pr` スキルが独立し、publish ゲート・Idempotency チェック・Closes/Refs 自動判定を専責機能として実装
- 本 Issue #1770 では `open-pr` の精細な仕様（dry_run mode 等）を定義し、`implement-issue` との段階的統合計画を明示

### 移行フェーズ

**Phase 1: 同期統合テスト**

- `implement-issue` が PR 作成を継続（status quo）
- `open-pr` スキルは draft pull の用途でテスト利用
- dry_run モードで `open-pr` の PR テンプレートガード・Closes/Refs 判定ロジックを検証
- **確認項目**: Closes/Refs downgrade が stale issue state を誤判定していないか

**Phase 2: 部分委譲**

- 新規実装 issue から `implement-issue` が `open-pr` へ PR 作成を委譲開始
- `impl-review-loop` の PR review / verdict は従来通り
- **条件**: 以下をすべて満たす
  - `open-pr` の PR テンプレートガード・Idempotency チェックが 5 件以上の実運用で確認済み
  - `implement-issue` の PR 作成コード（Step 7）が `open-pr` への委譲パターンで書き換え可能と判定
  - `open-pr` と `impl-review-loop` の連携テストが完了

**Phase 3: 完全委譲**

- `implement-issue` の Step 7 を完全に `open-pr` に委譲
- `implement-issue` は verify まで実装（Step 1-6）、PR 作成は `open-pr` に out-source
- `impl-review-loop` は `open-pr` との統合を正式化（`open-pr` の出力形式に適応）

### 互換確認手順

移行の各フェーズで以下を確認します。

**テンプレートガード互換確認（Phase 1）:**

```bash
# scripts/open-pr の PR テンプレートガード検証（`scripts/open-pr` invocation）
scripts/open-pr \
  --pr_title "test: テンプレートガード互換確認" \
  --linked_issue 1770 \
  --pr_body "## Summary\n（欠陥: 他必須セクション欠落）" \
  --publish yes \
  --dry_run true

# 期待: E_PR_TEMPLATE_GUARD が返り、欠落セクション一覧が出力される
```

**Idempotency 互換確認（Phase 2）:**

```bash
# 同一ブランチでの重複 PR 検出確認（skill invocation の擬似表記）
scripts/open-pr --pr_title "feat: test" --linked_issue 1770 --pr_body "..." --publish yes
# → 新規 PR を作成

scripts/open-pr --pr_title "feat: test (修正版)" --linked_issue 1770 --pr_body "..." --publish yes
# 期待: 既存 PR を検出、重複作成しない
```

**Closes/Refs 自動判定互換確認（Phase 2-3）:**

```bash
# OPEN issue に対する Closes 指定（skill invocation の擬似表記）
ISSUE=1770  # OPEN
scripts/open-pr --pr_title "feat: test" --linked_issue $ISSUE --pr_body "..." --publish yes
# 期待: PR 本文に「Closes #1770」を記載

# CLOSED issue に対する Closes 指定（downgrade 確認）
ISSUE=1768  # CLOSED
scripts/open-pr --pr_title "feat: follow-up" --linked_issue $ISSUE --pr_body "..." --publish yes
# 期待: WARN 出力 + PR 本文に「Refs #1768」を記載
```

### 移行判断基準

以下の基準に基づいて各フェーズへの進行を判定します。

| フェーズ | 進行条件 | 判定者 |
|---|---|---|
| Phase 1 完了 | dry_run テスト 10 件以上実行、ガード・downgrade ロジック確認完了 | `issue-contract-review` / human decision |
| Phase 2 開始 | Phase 1 テスト完了、実運用テスト 5 件以上完了 | `impl-review-loop` / human decision |
| Phase 3 開始 | Phase 2 で downgrade / Idempotency バグ報告なし、互換テスト完了 | `issue-contract-review` / human decision |

時間軸（例: Week 1-2）は計画例として扱い、フェーズ進行の判定には使いません。進行可否は上表の件数ベース・観測ベースの条件で判定します。

### 後続 Follow-up Issues

- 未起票（本 PR マージ後に起票予定）: `impl-review-loop` への `open-pr` 統合（Phase 3）
- 未起票（本 PR マージ後に起票予定）: `implement-issue` の Step 7 削除・リファクタリング（Phase 3）
- 未起票（本 PR マージ後に起票予定）: dry_run モードの CI/CD テスト活用ガイドライン文書化

## Related

- skill: `.agents/skills/implement-issue/SKILL.md`
- skill: `.agents/skills/impl-review-loop/SKILL.md`
- rule: `.agents/rules/github-ops-workflow.md` — Closes/Refs 使い分けの参照先（Issue #1754）
- template: `.github/PULL_REQUEST_TEMPLATE.md` — canonical PR template
- template: `templates/github-ops/pr-evidence.md` — PR Evidence template
