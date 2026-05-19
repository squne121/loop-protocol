# GitHub Ops 運用ルール

`gh` CLI を介した GitHub 操作の共通規約。AI エージェント・人間レビュアー双方が参照する。
個別の skill / SubAgent はこのルールに従って Issue / PR / コメントを更新する。

## Body File Guidance（`gh issue edit` / `gh pr edit` / `gh issue comment` 共通）

Issue / PR 本文の長文・多行更新時は **必ず body-file 経由** で操作する。inline `--body "..."` はクォート崩壊・HEREDOC 由来エスケープ混入を招くため使わない。

```bash
mkdir -p tmp
BODY_FILE="tmp/<task>-<番号>-body.md"
# 本文全体を $BODY_FILE に書き出してから:
gh issue edit <番号> --body-file "$BODY_FILE"
gh pr edit <番号> --body-file "$BODY_FILE"
gh issue comment <番号> --body-file "$BODY_FILE"
```

### Pre-edit guard

`--body-file` を渡す直前に以下を実行し、空ファイル・1 byte ファイル・HEREDOC 由来エスケープ混入を検知して停止する:

```bash
wc -c "$BODY_FILE"
if [ "$(wc -c < "$BODY_FILE")" -le 1 ]; then
  echo "body-file が空または 1 byte です: $BODY_FILE" >&2
  exit 1
fi
if grep -Pn '\\(?:\"|\$)' "$BODY_FILE"; then
  echo "HEREDOC 由来エスケープ混入の可能性。該当行を確認して再実行" >&2
  exit 1
fi
```

### HEREDOC 内のコードフェンス

HEREDOC サンプルにコードフェンスを含める場合は ``` ではなく `~~~` を使う（HEREDOC 終端の `EOF` 直前にコードフェンスが解釈されて崩れる事故を防ぐ）:

```markdown
~~~yaml
key: value
~~~
```

## Parent Issue の Machine-Readable Contract

parent Issue の `## Machine-Readable Contract` は以下の closed enum を使い、placeholder のまま確定しない。

`parent_mode`:
- `delivery-rollup`: child implementation の完了を rollup して close する
- `quality-gate`: child 完了と quality decision を分離、Quality Decision Record の確定まで close しない
- `routing-map`: canonical destination の地図を維持する
- `decision-log`: 意思決定記録と next action の固定を主目的にする

`closure_mode`:
- `child-complete` / `measurement-ready` / `quality-validated` / `routing-complete` / `decision-recorded`

`parent_mode` と `closure_mode` の互換:

| parent_mode | 許容される closure_mode |
|---|---|
| `delivery-rollup` | `child-complete` |
| `quality-gate` | `measurement-ready` または `quality-validated` |
| `routing-map` | `routing-complete` |
| `decision-log` | `decision-recorded` |

### Fail-close 条件

- `parent_mode` 欠落 → 自動推定で確定しない（提案までで止める）
- `closure_mode` と `Quality Decision Record.Status` 不一致 → invalid、close 判定不可
- `<required: ...>` placeholder や enum 外値 → missing 扱い

## Issue / PR コメントへの記録プロトコル

オーケストレーター skill / SubAgent が Issue / PR にコメントを残す際の構造化テンプレ。

### イテレーション開始時

```markdown
## <skill 名>: iteration <N> 開始 ($(date -u +%Y-%m-%dT%H:%M:%SZ))

- Inputs: <要約>
- 前 iteration からの差分: <ある場合>
```

### イテレーション完了時

```markdown
## <skill 名>: iteration <N> 完了 ($(date -u +%Y-%m-%dT%H:%M:%SZ))

- 結果サマリ: <verdict / status>
- 次イテレーション: <Yes / No (理由)>
- 詳細: <REVIEW_ISSUE_RESULT_V1 / LOOP_VERDICT 等の構造化出力要約>
```

### ループ終了時

```markdown
## <skill 名>: 完了 ($(date -u +%Y-%m-%dT%H:%M:%SZ))

- 最終 iteration: <N>
- termination_reason: <approved | max_iterations | human_escalation>
- LOOP_STATE 最終値: <主要フィールド>
- 次アクション: <人間レビュー / マージ / 追加 iteration 等>
```

## 認証・リポジトリ指定

- `gh auth status` で認証状態を確認。認証エラー時は人間に再認証を依頼
- スクリプト・skill 内で `gh` を呼ぶ際は `--repo <owner>/<name>` を明示（worktree 内の origin が変わっても安全）
- 対話的にユーザーが叩く想定のコマンドでは `--repo` を省略してよい

## ラベル運用

- 状態: `state/queued` / `state/in-progress` / `state/needs-human` / `state/blocked` / `state/done`
- 種別: `phase/implementation` / `phase/research`
- カテゴリ: `bug` / `enhancement` / `chore` / `docs` / `tracking` 等

詳細は `.github/ISSUE_TEMPLATE/*.yml` の labels 定義を SSOT として参照する。

## scripts 集約による permission 削減パターン

オーケストレーション skill（edit-issue / post-merge-cleanup / create-issue）の inline bash は `.py` / `.sh` script に集約し、`subprocess.run([...])` 配列形式 + 外部入力 allowlist validation を必須とする。Bash allowlist は scripts entrypoint パターン（`Bash(uv run python3 .claude/skills/<name>/scripts/*.py *)`）に絞ることで permission prompt を 1 ループあたり 1 回に削減する。

スクリプト呼び出しは **必ず `uv run python3 ...` 形式** を使う（bare `python3` 直起動は禁止）。`uv` は dependency lock を共有し、再現性のある実行を保証する。Bash allowlist にも `python3` 直起動パターンを置かないこと。

一時ファイル（バックアップ・中間 body・コメント本文等）は **リポジトリルート配下の `tmp/` に作成** する。システム `/tmp/` は使わない（プロジェクト外への副作用回避 / cleanup の追跡性確保のため）。バックアップ等の一時ファイルは `.gitignore` で除外する。

## .claude/settings.local.json 個人用設定例

`.claude/settings.local.json` は **個人ローカル設定** であり、リポジトリには含めない（`.gitignore` で除外済み）。下記を個人用に配置することで Bash 系の追加許可を適用できる。

```json
{
  "permissions": {
    "allow": [
      "Read(//home/<USER>/.gemini/**)",
      "Bash(env)",
      "Bash(gemini --version)"
    ]
  }
}
```

組織全体で許可したいパターンは `.claude/settings.json`（repo-tracked / 共有）に置く。個人ローカル特化（端末固有のパス、テスト用環境変数等）は `.claude/settings.local.json` に置く。
