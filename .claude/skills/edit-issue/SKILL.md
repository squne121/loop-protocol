---
name: edit-issue
description: 既存 GitHub Issue 本文の更新手順。reviewer フィードバックや人間判断結果を反映して `gh issue edit` で本文を書き戻すまでの一連の手順を提供する。issue-author SubAgent や main session が「Issue ◯◯ の本文を修正して」「Issue 本文を更新して」「edit issue」などのトリガーで使う。`create-issue`（新規起票）に対する **既存 Issue 修正版**で、Template Guard / Outcome Quality Guard / 必須セクション保持を起票と同じ基準で適用する。
---

# Edit Issue

既存 Issue 本文を `gh issue edit` で安全に書き戻す手順。
`create-issue`（新規起票手順）と対をなし、共通参照 [`../create-issue/references/body-authoring.md`](../create-issue/references/body-authoring.md) の guideline を踏襲する。

## Inputs

- `issue_number`（必須）
- `reviewer_feedback_url` または `reviewer_feedback_text`（任意。なければ最新コメントから抽出）

## Procedure

### 1. バックアップ取得（必須）

```bash
ISSUE_NUMBER=<issue_number>
REPO=$(git remote get-url origin | sed 's/.*github.com[:/]//' | sed 's/\.git$//')
BACKUP_FILE="/tmp/issue_${ISSUE_NUMBER}_backup_$(date +%s).md"
gh issue view "$ISSUE_NUMBER" --repo "$REPO" --json body --jq .body > "$BACKUP_FILE"
test -s "$BACKUP_FILE" || { echo "BACKUP FAILED" >&2; exit 1; }
```

このバックアップは後続の全ステップで abort 時の復旧に使う。

### 2. レビューフィードバックの収集

`reviewer_feedback_url` 指定時:
```bash
COMMENT_ID=$(echo "$REVIEWER_FEEDBACK_URL" | sed -E 's|.*issuecomment-([0-9]+).*|\1|')
gh api "/repos/$REPO/issues/comments/$COMMENT_ID" --jq '.body' > /tmp/reviewer_feedback.md
```

未指定時: Issue コメント一覧から最新の改善提案コメント（`review-issue` などの skill 由来）を `gh issue view <番号> --comments` で取得して使う。

### 3. 改善後の本文を生成

reviewer feedback に基づき以下を満たす形で本文を更新:

- テンプレ構造を維持（`.github/ISSUE_TEMPLATE/{種別}.yml` の必須セクションが残っている）
- AC / VC 番号一致を確認（[`../create-issue/references/body-authoring.md`](../create-issue/references/body-authoring.md) §VC 作成ガイダンス参照）
- Machine-Readable Contract block の YAML key を破壊しない（値のみ更新）
- 削除確認パターン、決定論的 VC の原則を適用

更新後の本文全体を tmp ファイルに保存:
```bash
NEW_BODY="/tmp/issue_${ISSUE_NUMBER}_new_$(date +%s).md"
# <更新後の本文全体を $NEW_BODY に書き出す>
```

### 4. Guard を適用（create-issue と同じ基準）

以下を順に通過させる。1 つでも fail なら abort してバックアップから復旧。

#### 4a. Template Guard

```bash
TEMPLATE_KIND=$(grep -oE '^title:.*"[^"]+"' "$BACKUP_FILE" | sed -E 's/.*"(.*)".*/\1/' | grep -oE '実装|調査|導入' || echo unknown)
# テンプレ種別判定 → 必須セクションを確認
# 例 implementation:
for section in "## Outcome" "## In Scope" "## Out of Scope" "## Acceptance Criteria" "## Verification Commands" "## Allowed Paths" "## Stop Conditions"; do
  grep -qF "$section" "$NEW_BODY" || { echo "[Template Guard] Missing: $section"; exit 2; }
done
```

#### 4b. Outcome Quality Guard

```bash
# Outcome が成果物形式と完了条件を含むか軽量検証
# 不適合パターン（動作状態のみ）を検出
awk '/^## Outcome$/{flag=1;next} /^## /{flag=0} flag' "$NEW_BODY" \
  | grep -qE "決定される$|整理される$|完了する$|検討する$|改善する$" \
  && { echo "[Outcome Quality Guard] Outcome に成果物形式が不足"; exit 2; }
```

#### 4c. 差分閾値監視（削減率 50% 超で abort）

```bash
ORIG_LINES=$(wc -l < "$BACKUP_FILE")
NEW_LINES=$(wc -l < "$NEW_BODY")
DIFF_LINES=$((ORIG_LINES - NEW_LINES))
THRESHOLD=$((ORIG_LINES / 2))
if [ "$DIFF_LINES" -gt "$THRESHOLD" ]; then
  echo "[ABORT] 削減率が 50% を超えています（元: ${ORIG_LINES}行 → 新: ${NEW_LINES}行、削減: ${DIFF_LINES}行）" >&2
  exit 2
fi
```

#### 4d. AC/VC 番号一致

```bash
AC_COUNT=$(grep -cE "^- \[.\] AC[0-9]+" "$NEW_BODY" 2>/dev/null || echo 0)
VC_AC_COUNT=$(grep -cE "# AC[0-9]+" "$NEW_BODY" 2>/dev/null || echo 0)
if [ "$AC_COUNT" -gt 0 ] && [ "$AC_COUNT" -ne "$VC_AC_COUNT" ]; then
  echo "[ABORT] AC 件数 (${AC_COUNT}) と VC の # AC<N> コメント件数 (${VC_AC_COUNT}) が一致しません" >&2
  exit 2
fi
```

### 5. body-file 経由で `gh issue edit` を実行

```bash
gh issue edit "$ISSUE_NUMBER" --repo "$REPO" --body-file "$NEW_BODY"
EDIT_EXIT=$?
if [ "$EDIT_EXIT" -ne 0 ]; then
  echo "[ERROR] gh issue edit 失敗。バックアップから自動復元します。" >&2
  gh issue edit "$ISSUE_NUMBER" --repo "$REPO" --body-file "$BACKUP_FILE"
  exit "$EDIT_EXIT"
fi
```

### 6. 変更経緯コメントを投稿

```bash
gh issue comment "$ISSUE_NUMBER" --repo "$REPO" --body "## edit-issue: 本文更新ログ ($(date -u +%Y-%m-%dT%H:%M:%SZ))

### 変更されたセクション
<セクション名と要約>

### 変更理由
<reviewer feedback URL またはトリガー>

### 検証結果
- Template Guard: PASS
- Outcome Quality Guard: PASS
- 差分閾値: ORIG=${ORIG_LINES} 行 / NEW=${NEW_LINES} 行
- AC/VC 番号一致: PASS"
```

### 7. abort 時の復旧

ステップ 4a-4d, 5 のいずれかで fail した場合:

```bash
gh issue edit "$ISSUE_NUMBER" --repo "$REPO" --body-file "$BACKUP_FILE" 2>/dev/null \
  || echo "[CRITICAL] バックアップ復旧失敗。手動対応必要。バックアップ: $BACKUP_FILE" >&2
gh issue comment "$ISSUE_NUMBER" --repo "$REPO" --body "## [ERROR] edit-issue: abort
<エラー内容>
バックアップ: \`$BACKUP_FILE\`（ローカル）"
```

## Output (ISSUE_EDIT_RESULT_V1)

```yaml
ISSUE_EDIT_RESULT_V1:
  status: ok | failed
  generated_at: <ISO 8601>
  generated_by: edit-issue
  issue_url: https://github.com/<owner>/<repo>/issues/<番号>
  guards:
    template_guard: pass | fail
    outcome_quality_guard: pass | fail
    diff_threshold_check:
      passed: true | false
      orig_lines: <int>
      new_lines: <int>
    ac_vc_alignment:
      passed: true | false
      ac_count: <int>
      vc_ac_comment_count: <int>
  update_applied: true | false
  backup_file: /tmp/issue_<番号>_backup_<epoch>.md
  warnings: []
  errors: []
```

## Constraints

- **body-file 経由必須**: `--body "<inline>"` は使わない（クォート崩壊・HEREDOC 由来エスケープのリスク）
- **バックアップ必須**: ステップ 1 を省略しない
- **abort 時自動復旧**: 4a-4d, 5 の fail で必ずバックアップから書き戻し試行
- **Machine-Readable Contract block 保持**: YAML key を破壊しない（値のみ更新）

## Related

- `.claude/skills/create-issue/SKILL.md` — 新規起票手順（対）
- [`.claude/skills/create-issue/references/body-authoring.md`](../create-issue/references/body-authoring.md) — VC 作成 / Anchor Verification / Contract block 等の共通ガイドライン
- `.claude/agents/issue-author.md` — 本 skill を使う「Issue 起票・修正の役割」SubAgent
- `.claude/skills/review-issue/SKILL.md` — `needs-fix` 結果を本 skill で本文へ反映
