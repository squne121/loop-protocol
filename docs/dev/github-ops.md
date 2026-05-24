# GitHub Ops 運用ルール

`gh` CLI を介した GitHub 操作の共通規約。AI エージェント・人間レビュアー双方が参照する。
個別の skill / SubAgent はこのルールに従って Issue / PR / コメントを更新する。

> **GitHub Milestone 操作の正本**: GitHub Milestone の作成・割当・close・rollup に関する運用規約は
> `docs/dev/milestone-ops.md` が正本である。本ドキュメントは `gh` CLI の共通規約を扱い、
> Milestone 固有の判断基準・命名規則・AI 操作フローは `docs/dev/milestone-ops.md` を参照すること。

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

- 状態: `state/in-progress` / `state/needs-human` / `state/blocked` / `state/done` / ~~`state/queued`~~（deprecated / legacy — 詳細は「state ラベル意味論定義」参照）
- 種別: `phase/implementation` / `phase/research`
- カテゴリ: `bug` / `enhancement` / `chore` / `docs` / `tracking` 等
- triage: `triage-required`（後述）

詳細は `.github/ISSUE_TEMPLATE/*.yml` の labels 定義を SSOT として参照する。

### triage-required ラベル

**目的**: AI エージェントが自動起票した follow-up Issue を人間または AI が triage するまでの間、未評価であることを明示する。

**付与タイミング**: `impl-review-loop` Step 5 / `post-merge-cleanup` の main thread が follow-up Issue を自動起票する際に **必ず付与** する。手動起票の場合は任意。

**解除タイミング**: triage 完了後（`state/needs-human` / close のいずれかに移行した時点、または単に `triage-required` を除去して `phase/` ラベルを付与した時点）。

**state ラベルとの関係**: `triage-required` は state ラベルと競合しない補助ラベル。triage 完了時に `state/queued` は付与しない（`state/queued` は deprecated / legacy 扱い — AI 着手可否・VC・contract-review では参照禁止）。

**運用フロー**:

```
自動起票（triage-required 付与）
        ↓
triage セッション（人間または AI エージェント）
        ↓
  有効 → triage-required 除去 + phase/ ラベル
  重複 → duplicate ラベル + close
  不要 → not planned で close
  保留 → state/needs-human
```

**注意**: `triage-required` は state ラベル体系（`state/queued` 等）とは独立した補助ラベルであり、AI 着手可否判定の primary signal として使わない（着手可否は blocker / dependency の close 状態で判断する）。

### state ラベル意味論定義

| ラベル | 意味 |
|---|---|
| `state/queued` | **deprecated / legacy** — AI 着手可否・VC・contract-review では参照禁止。着手可否の source of truth は blocker/dependency の close 状態である。triage フローでの自動付与も廃止（#211）。 |
| `state/in-progress` | 担当者（AI エージェントまたは人間）が現在作業中。 |
| `state/blocked` | blocker / dependency のうち少なくとも 1 つが open のため、着手できない状態。**補助・派生表示**であり、AI 着手可否の primary signal ではない（後述）。 |
| `state/needs-human` | 仕様判断・ライセンス確認・インフラ操作など、AI が単独で進められない判断を人間に求めている状態。 |
| `state/done` | Issue が完了し close された状態。 |

### AI 着手可能性の source of truth

**AI が Issue に着手してよいかどうかの source of truth は、GitHub native dependency（`depends on` リンク）がすべて close されているかどうかである。native dependency が利用できない場合は Issue 本文の `Depends on #N` fallback で補完する。**

- `state/blocked` ラベルは補助・派生表示にすぎず、AI 着手可否の primary signal ではない。
  - `state/blocked` が残存しているだけで BLOCKED 判定しない（ラベルの更新遅れ・付け忘れが生じうるため）。
- `state/queued` ラベルは deprecated / legacy 扱いであり、AI 着手可否・VC・contract-review での参照は禁止する（legacy cleanup 目的の削除のみ許容）。blocker / dependency がすべて close されていれば着手可であり、`state/queued` の有無は判定に関与しない。
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

## Codex project-local permissions 方針

Codex 側では `.claude/settings.json` と同型の allow / ask / deny を持たないため、project-local の境界は以下 3 面に分解して管理する。

- `.codex/config.toml`: filesystem / network の permission profile を定義する
- `.codex/rules/default.rules`: sandbox 外実行の allow / prompt / forbidden を定義する
- `AGENTS.md`: repo 内で守る実行方針と `rtk` 前提を定義する

公式ドキュメント（確認日: 2026-05-24）:

- Config basics: https://developers.openai.com/codex/config-basic
- Permissions: https://developers.openai.com/codex/permissions
- Rules: https://developers.openai.com/codex/rules
- AGENTS.md: https://developers.openai.com/codex/guides/agents-md

### 有効化前提

- Codex がこの repository を trusted project として扱っていること
- untrusted project では `.codex/config.toml` / `.codex/rules/*.rules` は読み込まれない
- user-global config や CLI flag で `sandbox_mode` が有効な場合、permission profile より旧 sandbox 設定が優先される
- 実作業前に active permission profile、active rules、読み込まれた instruction surface を確認する

### sandbox_mode 競合リスク

`~/.codex/config.toml` や Codex CLI flags で旧 `sandbox_mode` / `sandbox_workspace_write` が有効な場合、`.codex/config.toml` の `default_permissions` profile ではなく、旧 sandbox 設定が優先されます。

対策:
1. `codex status` で active sandbox_mode を確認
2. この repository 作業時は旧 sandbox 設定を無効化 (`--disable sandbox_mode` など)
3. または Permission profile ベースの設定に全面移行するまで、global config の `sandbox_mode` を disabled に設定

### bootstrap profile 使用例

`loop-protocol-bootstrap` profile は npm registry access が必要な bootstrap / dependency 更新作業に使用します。使用例:

```bash
# 依存関係の更新が必要な場合のみ以下を実行
codex sandbox linux --permissions-profile loop-protocol-bootstrap -C . -- rtk pnpm install
codex sandbox linux --permissions-profile loop-protocol-bootstrap -C . -- rtk pnpm add package-name

# 通常の開発・検証は loop-protocol-rtk を使用
codex sandbox linux --permissions-profile loop-protocol-rtk -C . -- rtk pnpm test
```

### `.codex/config.toml` の責務

- `approval_policy = "on-request"` を project-local の既定とする
- `default_permissions` で Codex の custom profile を選択する
- workspace root は write、`assets/` と `LICENSES/` は read-only に固定する
- `loop-protocol-rtk` では GitHub 操作に必要な最小 network allowlist だけを持たせる
- npm registry が必要な bootstrap / dependency 導入は `loop-protocol-bootstrap` profile に分離する
- permission profile は beta であり、`sandbox_mode` と併用しない

### `.codex/rules/default.rules` の責務

- `rtk` は documented subcommand だけを allow し、Codex の shell 実行入口を project harness に寄せる
- direct `pnpm`、direct `gh`、mutating `git` は forbidden にする
- read-only git inspection は最小限だけ allow する
- `match` サンプルを付けて rule load 時に自己検証できる形を保つ

### `AGENTS.md` の責務

- Codex が repo ルートで読む project-local instruction surface として使う
- `rtk` 経由実行、保護領域、検証コマンド対応を短く固定する
- 既存の `CLAUDE.md` / SSOT を置き換えず、Codex が project-local guidance を確実に読める薄い入口として保つ

### Instruction surface / rules surface の実効確認

Codex が project-local config / rules / instruction を正しく読み込んでいることを確認する手順:

```bash
# 1. Instruction surface 確認
# （Codex に project-local AGENTS.md を読んでいるかを確認させる）
codex --ask-for-approval never "List the active instruction sources and summarize loaded AGENTS.md guidance."

# 2. Rules surface 確認
# （project-local rules が active profile に反映されているかを確認）
codex execpolicy check --pretty --rules .codex/rules/default.rules -- rtk gh issue view 1

# 3. Permission profile 実効確認
# （現在のコンテキストで active permission profile と sandbox_mode が何かを確認）
# Note: コマンド名は Codex バージョンで異なる可能性があるため、`codex --help` で確認
codex status
```

失敗原因が user-global config や CLI flags による上書きなら、PR コメントに「repo-local 設定は user-global / CLI flags に上書きされ得るため、merge 後の有効化は各環境で確認が必要」と明記すること。
