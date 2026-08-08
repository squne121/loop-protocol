---
name: "speckit-taskstoissues"
description: "既存タスクを、利用可能な設計成果物に基づく実行可能かつ依存順のGitHub Issueへ変換する。"
argument-hint: "GitHub Issue用の任意フィルターまたはラベル"
compatibility: ".specify/ ディレクトリを持つ spec-kit プロジェクトが必要"
metadata:
  author: "github-spec-kit"
  source: "templates/commands/taskstoissues.md"
user-invocable: true
disable-model-invocation: false
---

> **LOOP_PROTOCOL NOTE（2026-05-23）**: このスキルを LOOP_PROTOCOL の Issue 起票に直接使うことは禁止されています。
> Issue 起票は必ず新規起票専用の `issue-creator` / `create-issue` skill 経由で行うこと（body quality gate・triage policy 適用のため）。既存 Issue の更新は `issue-editor` / `edit-issue` route を使う。

## ユーザー入力

```text
$ARGUMENTS
```

ユーザー入力が空でない場合は、処理を進める前に**必ず**内容を考慮する。

## 実行前チェック

**拡張hookの確認（tasks-to-issues変換前）**:
- プロジェクトルートに `.specify/extensions.yml` があるか確認する。
- 存在する場合は読み込み、`hooks.before_taskstoissues` キー配下のエントリを確認する。
- YAMLを解析できない、または不正な場合は、hook確認を静かに省略して通常どおり続行する。
- `enabled` が明示的に `false` のhookを除外する。`enabled` フィールドがないhookは既定で有効として扱う。
- 残った各hookでは、hookの `condition` 式を解釈または評価してはならない。
  - `condition` フィールドがない、null、または空の場合は、そのhookを実行可能として扱う。
  - 空でない `condition` が定義されている場合は、そのhookを省略し、条件評価はHookExecutor実装に委ねる。
- hookコマンド名からスラッシュコマンドを構成するときは、ドット（`.`）をハイフン（`-`）へ置換する。例: `speckit.git.commit` → `/speckit-git-commit`。
- 実行可能な各hookでは、`optional` フラグに応じて次を出力する。
  - **任意hook**（`optional: true`）:
    ```
    ## 拡張hook

    **任意の事前hook**: {extension}
    コマンド: `/{command}`
    説明: {description}

    入力: {prompt}
    実行: `/{command}`
    ```
  - **必須hook**（`optional: false`）:
    ```
    ## 拡張hook

    **自動の事前hook**: {extension}
    実行中: `/{command}`
    EXECUTE_COMMAND: {command}

    hookコマンドの結果を待ってから、作業手順へ進む。
    ```
- hookが登録されていない、または `.specify/extensions.yml` が存在しない場合は静かに省略する。

## 作業手順

1. リポジトリルートで `.specify/scripts/bash/check-prerequisites.sh --json --require-tasks --include-tasks` を実行し、FEATURE_DIRとAVAILABLE_DOCSの一覧を解析する。すべてのパスは絶対パスでなければならない。`I'm Groot` のように引数に単一引用符がある場合は、`'I'\''m Groot'` のようにエスケープする（可能なら二重引用符の `"I'm Groot"` を使う）。
1. 実行したスクリプトの結果から **tasks** のパスを取得する。
1. 次を実行してGit remoteを取得する:

```bash
git config --get remote.origin.url
```

> [!CAUTION]
> remote がGitHub URLである場合に限り、次の手順へ進む。

1. 一覧内の各タスクについて、GitHub MCP serverを使い、Git remoteに対応するリポジトリへ新しいIssueを作成する。

> [!CAUTION]
> remote URLと一致しないリポジトリには、いかなる場合もIssueを作成してはならない。

## 実行後チェック

**拡張hookの確認（tasks-to-issues変換後）**:
プロジェクトルートに `.specify/extensions.yml` があるか確認する。
- 存在する場合は読み込み、`hooks.after_taskstoissues` キー配下のエントリを確認する。
- YAMLを解析できない、または不正な場合は、hook確認を静かに省略して通常どおり続行する。
- `enabled` が明示的に `false` のhookを除外する。`enabled` フィールドがないhookは既定で有効として扱う。
- 残った各hookでは、hookの `condition` 式を解釈または評価してはならない。
  - `condition` フィールドがない、null、または空の場合は、そのhookを実行可能として扱う。
  - 空でない `condition` が定義されている場合は、そのhookを省略し、条件評価はHookExecutor実装に委ねる。
- hookコマンド名からスラッシュコマンドを構成するときは、ドット（`.`）をハイフン（`-`）へ置換する。例: `speckit.git.commit` → `/speckit-git-commit`。
- 実行可能な各hookでは、`optional` フラグに応じて次を出力する。
  - **任意hook**（`optional: true`）:
    ```
    ## 拡張hook

    **任意hook**: {extension}
    コマンド: `/{command}`
    説明: {description}

    入力: {prompt}
    実行: `/{command}`
    ```
  - **必須hook**（`optional: false`）:
    ```
    ## 拡張hook

    **自動hook**: {extension}
    実行中: `/{command}`
    EXECUTE_COMMAND: {command}
    ```
- hookが登録されていない、または `.specify/extensions.yml` が存在しない場合は静かに省略する。

## 出力制約 (OUTPUT_BUDGET_V1)

`docs/dev/agent-skill-boundaries.md#OUTPUT_BUDGET_V1` の制約に従う。routing-critical な機械可読フィールドは削らず、人間向け説明・証跡・diff 再掲のみを削減する。
