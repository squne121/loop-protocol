# Rule: github-ops-workflow

GitHub 操作（gh CLI）の不変条件。

## 1. gh CLI を経由する

- Issue / PR / リリースの操作はすべて `gh` CLI で行う（Web UI 手作業を AI からは行わない）
- API 呼び出しが必要な場合は `gh api` を使う

## 2. Issue / PR 本文の更新は body-file を経由する

長文 / 多行の本文更新時は、シェル展開やクォートのバグを避けるため `tmp/` 経由の body-file を使う。

```bash
mkdir -p tmp
cat > tmp/issue-<番号>-body.md <<'EOF'
（本文）
EOF
gh issue edit <番号> --body-file tmp/issue-<番号>-body.md
```

- `tmp/` は `.gitignore` 済み（コミット対象外）
- 短文・1 行のコメントは `--body "..."` で問題なし

## 3. Issue / PR コメントは構造化する

- レビュー結果は箇条書きと verdict ラベル（APPROVE / REQUEST_CHANGES / BLOCKED 等）で構造化
- AI が連続コメントしないよう、1 セッションでまとめる

## 4. ラベル運用

- 状態を示すラベル: `state/queued`, `state/in-progress`, `state/needs-human`, `state/done`
- 種別を示すラベル: `phase/research`, `phase/implementation`
- カテゴリ: `bug`, `enhancement`, `chore`, `docs` 等
- 詳細は [`issueops-mode-guard`](issueops-mode-guard.md) / [`issue-uncertainty-policy`](issue-uncertainty-policy.md) を参照

## 5. 認証

- `gh auth status` で認証状態を確認できる
- 認証エラー時は人間に再認証（`gh auth login`）を依頼

## 6. リポジトリ指定

スクリプト・skill 内で `gh` コマンドを呼ぶ際は `--repo <owner>/<name>` を明示する（worktree 内の origin が変わっても安全に動かすため）。

ただし対話的にユーザーが叩く想定のコマンドでは `--repo` を省略してもよい。
