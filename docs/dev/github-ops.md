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

### state ラベル意味論定義

| ラベル | 意味 |
|---|---|
| `state/queued` | 着手可能な状態で待機中。blocker / dependency がすべて close されており、担当者が割り当てられていないか未着手。 |
| `state/in-progress` | 担当者（AI エージェントまたは人間）が現在作業中。 |
| `state/blocked` | blocker / dependency のうち少なくとも 1 つが open のため、着手できない状態。**補助・派生表示**であり、AI 着手可否の primary signal ではない（後述）。 |
| `state/needs-human` | 仕様判断・ライセンス確認・インフラ操作など、AI が単独で進められない判断を人間に求めている状態。 |
| `state/done` | Issue が完了し close された状態。 |

### AI 着手可能性の source of truth

**AI が Issue に着手してよいかどうかの source of truth は、blocker / dependency がすべて close されているかどうかである。**

- `state/blocked` ラベルは補助・派生表示にすぎず、AI 着手可否判定の primary signal として使わない。
  - ラベルは手動更新の遅れや付け忘れが生じうるため、ラベルのみに依存しない。
- `state/ready` ラベルは採用しない（判断根拠は後述）。

### state/ready 不採用の根拠

`state/ready` ラベルを追加しても「ready かどうか」を判断するには結局 blocker / dependency の close 状態を確認する必要がある。ラベルはその確認結果の複製にすぎず、二重管理によるズレ（ラベルは ready だが blocker が open のまま等）を招く。したがって `state/ready` は採用しない。着手可否は blocker / dependency の close 状態を直接参照することで判断する。

### blocker / dependency の本文表現規約

Issue 本文で依存関係を表現する場合は、以下の優先順位に従う。

1. **GitHub native dependency（primary）**: GitHub の "Add a dependency" 機能（`depends on` リンク）を使う。GitHub UI および API から依存関係を直接参照できる。
2. **`Depends on #N` テキスト表現（fallback）**: native dependency が利用できない場合、または補足的に明示する場合は Issue 本文に `Depends on #N` の形式で記載する。

### blocker 判定の優先順位

AI エージェントが着手可否を判定する際の blocker 確認順序:

1. GitHub native dependency API でリンクされた dependency Issue がすべて close か確認する（primary）。
2. Issue 本文に `Depends on #N` パターンが存在する場合、該当 Issue `#N` がすべて close か確認する（fallback / 補完）。

### native dependency と `Depends on #N` が不一致の場合

native dependency が示す依存関係と、本文の `Depends on #N` 記述が矛盾または不一致（例: native では依存なし、本文では `Depends on #N` が open を指している）の場合は、**自動判断せず human escalation とする**。不一致を Issue コメントに記録し、人間による確認・修正を依頼する。

## scripts 集約による permission 削減パターン

オーケストレーション skill（edit-issue / post-merge-cleanup / create-issue）の inline bash は `.py` / `.sh` script に集約し、`subprocess.run([...])` 配列形式 + 外部入力 allowlist validation を必須とする。Bash allowlist は scripts entrypoint パターン（`Bash(uv run python3 .claude/skills/<name>/scripts/*.py *)`）に絞ることで permission prompt を 1 ループあたり 1 回に削減する。

スクリプト呼び出しは **必ず `uv run python3 ...` 形式** を使う（bare `python3` 直起動は禁止）。`uv` は dependency lock を共有し、再現性のある実行を保証する。Bash allowlist にも `python3` 直起動パターンを置かないこと。

一時ファイル（バックアップ・中間 body・コメント本文等）は **リポジトリルート配下の `tmp/` に作成** する。システム `/tmp/` は使わない（プロジェクト外への副作用回避 / cleanup の追跡性確保のため）。バックアップ等の一時ファイルは `.gitignore` で除外する。

## .claude/settings.json permissions 方針

Claude Code の `.claude/settings.json` は `permissions.allow` / `permissions.ask` / `permissions.deny` の 3 セクションで構成される。
公式ドキュメント: https://docs.anthropic.com/claude-code/permissions（確認日: 2026-05-21）

### permissions.ask の動作

`permissions.ask` に列挙されたコマンドは、Claude Code がそのコマンドを実行しようとする際に **実行前確認プロンプトを表示する**。
人間が「許可（Allow）」を選ぶと実行される。「拒否（Deny）」を選ぶとキャンセルされる。

> 参照: https://docs.anthropic.com/claude-code/permissions#permission-modes

### GitHub 操作（Issue / PR）→ allow 方針

**方針**: GitHub Issue / PR 操作はすべて `allow` に置く。

AI エージェントと人間のコミュニケーションは **Issue / PR をインターフェース** として行う設計のため、
`gh issue *` / `gh pr *` / `gh api *` コマンドを `ask`（確認プロンプト）にすると
エージェントの自律的なワークフロー（issue コメント投稿、PR 起票、ラベル更新等）が毎回中断される。

Issue / PR はそれ自体が人間可視の監査ログであり、エージェントの行動は GitHub 上で追跡・取消可能なため、
追加の確認プロンプトは不要と判断した。（Issue #48 調査・Issue #114 実装、方針確定 2026-05-21）

### git commit / push → allow 方針

**方針**: `git commit *` / `git push *` も `allow` に置く。

`main` ブランチへの直接 push は **GitHub の branch protection rules**（require PR reviews / restrict pushes）で防ぐ。
Claude Code の `ask` プロンプトで防ぐのではなく、インフラ層のガードレールを使う（workaround 最小化方針）。

branch protection が設定されている限り、エージェントが `git push` を実行しても
`main` への直接マージは不可能なため、`ask` を置く必要はない。

### git worktree / checkout / stash → allow 方針

**方針**: `git worktree *` / `git checkout *` / `git stash *` も `allow` に置く。

`implement-issue` skill および SubAgent が worktree ベースで実装を進めるため、
これらのコマンドが毎回確認プロンプトを要求すると worktree 作成フローが中断される。
副作用リスクは低く（ローカル git 操作のみ）、`allow` で問題ない。

## main ブランチ branch protection 設定状況

確認日時: 2026-05-21

確認コマンド:

```bash
gh api repos/squne121/loop-protocol/branches/main/protection
```

設定済み項目:

| 項目 | 値 |
|---|---|
| `required_pull_request_reviews.dismiss_stale_reviews` | `true` |
| `required_pull_request_reviews.required_approving_review_count` | `0` |
| `allow_force_pushes.enabled` | `false` |
| `required_status_checks` | `typecheck` / `lint` / `test` / `build`（strict: true） |
| `allow_deletions.enabled` | `false` |

`git push --force` は GitHub 側でブロックされる。これは `.claude/settings.json` の `git push *` を allow 設定しても、GitHub リモートが force push を拒否することを意味する。AI エージェントが誤って force push を試みた場合でも、リポジトリ保護が維持される。

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
