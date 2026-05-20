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
BACKUP_JSON=$(uv run python3 .claude/skills/edit-issue/scripts/backup-and-parse-issue.py "$ISSUE_NUMBER")
BACKUP_FILE=$(uv run python3 -c "import json,sys; print(json.loads('$BACKUP_JSON')['backup_file'])")
```

`backup-and-parse-issue.py` は `gh issue view <N> --json body --jq .body` を subprocess 配列形式で実行し、リポジトリルート配下の `tmp/issue_<N>_backup_<ts>.md` に保存したうえで JSON を stdout に返す。このバックアップは後続の全ステップで abort 時の復旧に使う。

### 2. レビューフィードバックの収集

`reviewer_feedback_url` 指定時:
```bash
COMMENT_ID=<id extracted from reviewer_feedback_url by caller>
mkdir -p tmp
gh api "/repos/$REPO/issues/comments/$COMMENT_ID" --jq '.body' > tmp/reviewer_feedback.md
```

`COMMENT_ID` は `reviewer_feedback_url` の末尾 `issuecomment-<id>` 部分を安全にパースして指定すること。

未指定時: Issue コメント一覧から最新の改善提案コメント（`review-issue` などの skill 由来）を `gh issue view <番号> --comments` で取得して使う。

### 3. 改善後の本文を生成

reviewer feedback に基づき以下を満たす形で本文を更新:

- テンプレ構造を維持（`.github/ISSUE_TEMPLATE/{種別}.yml` の必須セクションが残っている）
- AC / VC 番号一致を確認（[`../create-issue/references/body-authoring.md`](../create-issue/references/body-authoring.md) §VC 作成ガイダンス参照）
- Machine-Readable Contract block の YAML key を破壊しない（値のみ更新）
- 削除確認パターン、決定論的 VC の原則を適用

#### 3a. ISSUE_TEMPLATE を読み込んで required ラベルを列挙する

本文を書き換える前に、対象 ISSUE_TEMPLATE の `validations.required: true` ラベルを動的に列挙して、必須セクションの網羅性を確認する。

`guard-issue-body.py` の `load_required_labels()` 関数が `.github/ISSUE_TEMPLATE/{issue_kind}.yml` を `yaml.safe_load()` でパースし、`type: markdown` 要素を除外したうえで `validations.required: true` の `attributes.label` を返す。issue_kind は以下の優先順で解決する:

1. `--issue-kind` CLI 引数（`implementation` / `research` / `parent`）
2. 本文の Machine-Readable Contract fenced yaml 内の `issue_kind` フィールド
3. どちらも解決不能なら `template_guard` を fail（pass にしない）

```bash
# yq を使う場合（利用可能なら優先）
yq '.body[] | select(.type != "markdown") | select(.validations.required == true) | .attributes.label' \
  .github/ISSUE_TEMPLATE/<種別>.yml

# python3 を使う場合（guard-issue-body.py の load_required_labels と同等）
uv run python3 -c "
import yaml
with open('.github/ISSUE_TEMPLATE/<種別>.yml') as f:
    t = yaml.safe_load(f)
required = [
    i['attributes']['label'] for i in t.get('body', [])
    if i.get('type') != 'markdown' and i.get('validations', {}).get('required')
]
print('\n'.join(required))
"
```

列挙した全ラベルが更新後の本文に `## <ラベル>` として存在することを確認すること。

対応する `.yml` が存在しない種別を渡された場合、`guard_template()` は pass ではなく fail を返す。

#### 3b. AC 番号と VC コメント # AC<n> の照合

AC 件数と VC の `# AC<n>` コメント件数が一致しているかを確認する（`guard-issue-body.py` の AC/VC alignment check と同じロジックを手動で事前確認）。

```bash
# 更新後の本文ファイルに対して実行する
AC_COUNT=$(awk '/^## Acceptance Criteria/{flag=1; next} /^## /{flag=0} flag && /- \[ \] AC[0-9]/' "$NEW_BODY" | wc -l)
VC_AC_COUNT=$(rg -c "# AC[0-9]" "$NEW_BODY" || echo 0)
[ "$AC_COUNT" -eq "$VC_AC_COUNT" ] \
  && echo "PASS: AC/VC 番号一致 ($AC_COUNT 件)" \
  || echo "FAIL: AC=$AC_COUNT / VC AC コメント=$VC_AC_COUNT"
```

不一致の場合は本文を修正してから次へ進む。

#### 3c. rg を用いた VC 構築

VC コマンドを作成または更新する場合は `rg` を使う。Perl 互換正規表現が必要な場合は `-P` フラグを付ける（`grep -P` は GNU grep 限定だが `rg -P` は ripgrep 組み込みのため移植性が高い）。

```bash
# 基本形: 特定パターンが存在することを確認
rg -n "<pattern>" <file>

# Perl 互換正規表現が必要な場合
rg -Pn "<perl-compatible-pattern>" <file>

# 見出し配下の内容確認（2 段パイプ）
rg -nA 20 "^## <見出し>" <file> | rg "<content-pattern>"
```

`grep` は GNU 拡張差や Perl 互換構文の扱いが環境によって分かれるため、VC での使用を避ける。

#### 3d. baseline で全 VC が fail することを確認

本文を書き戻す前に、実装前の状態（baseline）で VC が fail することを確認する。fail しない VC は「実装によって変化しない」ため VC として意味がない。

```bash
# 各 VC コマンドを実行して exit code を確認する
# exit code 非ゼロ（fail）であれば baseline check 通過
<VC コマンド>; echo "Exit: $?"

# rg を使う VC の場合、マッチなし = exit 1 = fail = baseline 確認 OK
rg -n "<pattern>" <file>; echo "Exit: $?"  # 0 なら実装済みの可能性
```

baseline で pass する VC が存在する場合は、VC のコンテキスト見出し固定が不足している可能性がある（ex. 別セクションの既存記述に誤マッチ）。VC パターンを tighten してから proceed すること。

更新後の本文全体を tmp ファイルに保存:
```bash
mkdir -p tmp
NEW_BODY="tmp/issue_${ISSUE_NUMBER}_new_$(date +%s).md"
# <更新後の本文全体を $NEW_BODY に Write ツールで書き出す>
```

### 4. Guard を適用（create-issue と同じ基準）

以下のスクリプト呼び出し 1 回で全ガードを適用する。1 つでも fail なら abort してバックアップから復旧。

`--issue-kind` 引数で issue_kind を明示する。指定しない場合は本文の Machine-Readable Contract の `issue_kind` フィールドから自動取得する。

```bash
uv run python3 .claude/skills/edit-issue/scripts/guard-issue-body.py "$NEW_BODY" \
  --orig-file "$BACKUP_FILE" \
  --issue-kind <implementation|research|parent> \
  --format yaml
```

`guard-issue-body.py` は以下を順に検証する:

- **Template Guard**: `.github/ISSUE_TEMPLATE/{issue_kind}.yml` の `validations.required: true` ラベルを動的に取得し、`## <ラベル>` 形式で本文に存在するか確認する。`type: markdown` 要素は除外する。対応 `.yml` が存在しない種別は pass ではなく fail。
- **Outcome Quality Guard**: Outcome が成果物形式と完了条件を含むか（動作状態のみパターンを検出）
- **差分閾値（削減率 50% 超で abort）**: `--orig-file` 指定時のみ適用
- **AC/VC 番号一致**: AC 件数と VC の `# AC<N>` コメント件数が一致するか。ただし issue_kind が `Verification Commands` を必須セクションに持たない種別（例: `parent`）では自動的に `skipped: true` を返して pass 扱いにする（ハードコードではなく ISSUE_TEMPLATE の required ラベルで動的判定）。

スクリプトが exit 2 を返した場合は abort し、ステップ 7 の復旧処理へ進む。

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
mkdir -p tmp
gh issue comment "$ISSUE_NUMBER" --repo "$REPO" --body-file tmp/edit_issue_comment.md
```

コメント本文（`tmp/edit_issue_comment.md`）には変更セクション一覧・変更理由・Guard 結果サマリを含める。

### 7. abort 時の復旧

ステップ 4, 5 のいずれかで fail した場合:

```bash
gh issue edit "$ISSUE_NUMBER" --repo "$REPO" --body-file "$BACKUP_FILE" 2>/dev/null \
  || echo "[CRITICAL] バックアップ復旧失敗。手動対応必要。バックアップ: $BACKUP_FILE" >&2
mkdir -p tmp
gh issue comment "$ISSUE_NUMBER" --repo "$REPO" --body-file tmp/edit_issue_error.md
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
  backup_file: tmp/issue_<番号>_backup_<epoch>.md
  warnings: []
  errors: []
```

## Constraints

- **body-file 経由必須**: `--body "<inline>"` は使わない（クォート崩壊・HEREDOC 由来エスケープのリスク）
- **バックアップ必須**: ステップ 1 を省略しない
- **abort 時自動復旧**: 4, 5 の fail で必ずバックアップから書き戻し試行
- **Machine-Readable Contract block 保持**: YAML key を破壊しない（値のみ更新）

## Guardrails

- **scripts entrypoint 経由統一**: バックアップ取得・Guard 判定は必ず `.claude/skills/edit-issue/scripts/` のスクリプト経由で実行する
- **inline `gh` / `jq` / `grep` / `awk` / heredoc 使用禁止**: Step 1（バックアップ）および Step 4（Guard）での inline bash パイプラインは使用しない。`gh issue edit --body-file` 等の編集系コマンドは引き続き inline で使用してよい
- **スクリプトは `subprocess.run([...])` 配列形式のみ**: `shell=True` 禁止
- **外部入力の validation**: issue_number は `^\d+$`、ファイルパスは `^[A-Za-z0-9._/-]+$` で検証済み

## Related

- `.claude/skills/create-issue/SKILL.md` — 新規起票手順（対）
- [`.claude/skills/create-issue/references/body-authoring.md`](../create-issue/references/body-authoring.md) — VC 作成 / Anchor Verification / Contract block 等の共通ガイドライン
- `.claude/agents/issue-author.md` — 本 skill を使う「Issue 起票・修正の役割」SubAgent
- `.claude/skills/review-issue/SKILL.md` — `needs-fix` 結果を本 skill で本文へ反映
