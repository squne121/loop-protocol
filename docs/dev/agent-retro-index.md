# Agent Retro Index

`agent_retro_index` は複数のエージェントランにまたがる振り返りデータを集約するインデックスアーティファクトです。
個別ランの `agent_run_report/v1` とは異なり、パターンの横断分析とフォローアップ Issue の追跡を目的とします。

## アーティファクト責務差分

`agent_session_manifest`、`agent_run_report`、`agent_retro_index` の責務差分については
`docs/dev/agent-run-report.md` の「アーティファクト責務差分」セクションを参照してください。

| アーティファクト | 責務 | 生成タイミング |
|---|---|---|
| `agent_session_manifest` | セッション中の内部追跡（読み取りファイル・ツール呼び出し） | セッション中（逐次） |
| `agent_run_report` | 個別ランの公開可能要約と AC 達成状況 | セッション終了後 |
| `agent_retro_index` | 複数ランの横断インデックス・friction パターン・フォローアップ Issue 集約 | ラン完了後またはレトロスペクティブ時 |

## agent_retro_index スキーマ

```json
{
  "schema": "agent_retro_index/v1",
  "generation_verdict": "complete",
  "entries": [
    {
      "report_comment_url": "https://github.com/squne121/loop-protocol/issues/940#issuecomment-4760662331",
      "report_digest": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
      "issue": 940,
      "pr": 1073,
      "merge_sha": "0000000000000000000000000000000000000000",
      "tags": ["docs", "agent-run-report", "handoff"],
      "friction_summary": "PR Body Japanese Check が standalone Closes #940 block で失敗。段落統合で解消。",
      "quality_signals": ["all-ac-pass", "ci-all-pass", "docs-only"],
      "follow_up_issues": [941]
    }
  ],
  "orphan_reports": [],
  "ambiguous_links": []
}
```

スキーマの正本は `docs/schemas/agent-retro-index.schema.json` を参照。
`entries[].follow_up_issues` は Issue 番号の integer array（URL/reason object ではない）。

## follow_up_issues の運用

### 起票する場合

follow-up Issue を起票したら、対応する `entries[].follow_up_issues` に記録する:

1. `gh issue create` で Issue を起票する
2. 取得した Issue 番号（integer）を `follow_up_issues` array に追加する
3. `agent_retro_index` JSON を更新し、以下のスクリプトで再生成する:

```bash
pnpm agent-retro-index:update        # update-retro-index.mjs
pnpm agent-retro-index:verify-digest # update-retro-index.mjs --verify-artifact-json
pnpm agent-retro-index:check         # check-agent-run-reports.mjs
```

### 起票しない場合

スコープ内で解消済み、または起票するほどの重要度がない場合は、
`agent_run_report/v1` の `commands_summary` に理由を留める（起票しない判断の記録）。
詳細は `docs/dev/agent-run-report.md` の「Follow-up Issue Creation」セクションを参照。

## export-chatgpt-context での使用

`agent_retro_index` は `export-chatgpt-context` CLI の `--retro-index-json` 引数に渡し、
`priority_signals` セクション（friction / human intervention / follow-ups）の生成に使用される。

```bash
node scripts/agent-logs/export-chatgpt-context.mjs \
  --retro-index-json artifacts/agent-retro-index.json \
  --source-set-json artifacts/agent-retro-index-source-set.json \
  ...
```

CLI の詳細オプションは `docs/dev/agent-run-report.md` の「ChatGPT Context Bundle Export」セクションを参照。

## Phase Stop Conditions（レトロ更新フェーズ）

`agent_retro_index` を更新する場合の Stop Conditions:

- **`report finalized`**: 対象ランの `agent_run_report/v1` が確定している
- **`public-safe check pass`**: 含める friction 記述に機密情報（local_path・raw output 等）が含まれていない
- **`posting dry-run or upsert done`**: `export-chatgpt-context` dry-run が成功しているか、GitHub への upsert が完了している

詳細な Stop Conditions は `docs/dev/agent-run-report.md` の「Phase Stop Conditions」セクションを参照。

## Hook Boundary Policy

hook は diagnostic/prevention レイヤーであり、カノニカルゲートではない。
post-run verifier が canonical gate である。

詳細は `docs/dev/agent-run-report.md` の「Hook Boundary Policy」セクションおよび
`docs/dev/hook-boundaries.md` を参照。

## operation index との責務境界（#1405）

`agent_operation_session_index/v1`（`docs/schemas/agent-operation-session-index.schema.json`）は
`agent_retro_index` とは異なる責務を持つ、operation 単位の index である。

| アーティファクト | 単位 | 責務 |
|---|---|---|
| `agent_retro_index` | 複数ラン横断 | friction パターン・フォローアップ Issue の集約 |
| `agent_operation_session_index/v1` | 単一 Issue/PR operation | operation（issue_comment / pr_comment 等）と agent run / public artifact（run report・retro index・`CHATGPT_RETRO_CONTEXT_V1` marker）の対応関係を machine-readable に固定する |

`agent_operation_session_index/v1` は `agent_retro_index` を置き換えず、
`agent_retro_index` の `entries[].report_comment_url` が指す run report と
同じ GitHub comment chain を、operation 起点で辿れるようにする補完 index である。

checker: `pnpm agent-operation-session-index:check`（`scripts/check-agent-operation-session-index.mjs`）。
`chatgpt_retro_execution_proof/v1` との関係（#1153 parent closure proof）は
`docs/dev/agent-run-report.md` の「#1405 Parent Closure Proof Contract」セクションを参照。
