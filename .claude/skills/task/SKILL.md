---
name: task
description: Native Claude operator の ACTIVE Task を明示的に切り替える `/task <target>` escape hatch の仕様。Task Context v1（Issue #2564）が UserPromptSubmit hook 内で deterministic に特別扱いする唯一の手動 rebind 経路。「Task を切り替えたい」「別の Issue に移りたい」「/task の使い方」のトリガーで参照する。
---

# `/task <target>` — ACTIVE Task 明示切り替え escape hatch

Task Context v1（Issue #2564）が Native Claude operator 向けに提供する、
ACTIVE Task を人間が意図的に supersede/rebind するための唯一の明示的経路。

## 位置づけ

- これは Claude Code の custom slash command（`.claude/commands/*.md`）では
  ない。`UserPromptSubmit` hook adapter（`.claude/hooks/task_context/
  hook_entry.py` + `classifier.py`）が、slash command expansion より前に
  raw prompt 文字列として渡された `/task ...` を deterministic に
  special-case 判定する。
- 判定の precedence は常に最優先: `/task` special-case > 通常の
  primary-target classifier（EXPLICIT/INFERRED/REFERENCE_ONLY/AMBIGUOUS/
  NONE）。ACTIVE な別 Task の guard が働いている状態でも `/task <target>`
  は誤って block されない。

## 使い方

```
/task #123
/task owner/repo#123
/task https://github.com/owner/repo/issues/123
/task issue 123
/task pr 123
/task <自由記述のラベル>
```

- GitHub Issue/PR 参照として解釈できる場合（`#N`、`owner/repo#N`、GitHub
  URL、`issue N`/`pr N`）は、その ref を live claim している既存 Task へ
  rebind するか、無ければ新規 Task を作成してその ref を claim する。
- GitHub 参照として解釈できない場合、残りの文字列をそのまま ad-hoc Task の
  title として新規 Task を作成する（raw 文字列は `tasks.title` にのみ
  保存され、`events.metadata_json` の allowlist 制約（AC7）は経由しない）。
- `/task`（target なし）は明示的な validation failure として扱われ、
  rebind を装わない。

## 適用対象外

- ACTIVE Task が `task_refs == 0`（GitHub ref を一切持たない
  provisional/absorbent な ad-hoc Task）の状態で、最初の high-confidence
  primary GitHub target が来た場合の absorb は、この escape hatch の対象
  **ではない**（通常の `UserPromptSubmit` classifier 経路で自動的に
  absorb される。`/task` を使う必要はない）。
- worktree/cwd の変更（`CwdChanged`）は Task/Activity/Binding を変更しない
  （別の独立した RuntimeLocation observation のみ更新する）。

## 実装

- classifier: `.claude/hooks/task_context/classifier.py`
  （`_SLASH_TASK_RE` / `parse_slash_task_target`）
- server 側 rebind flow: `scripts/task-context/task_context_hook_flows.py`
  の `_apply_slash_task_rebind`
- 検証: `tests/task-context/test_user_prompt_submit_flow.py`

## Stop Conditions

- 通常の自然言語 prompt や shell 起動引数を escape hatch として扱わない
  （`/task` という文字列で prompt が literal に開始する場合のみ special
  case が発火する）。
