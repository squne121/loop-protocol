---
name: task
description: Native Claude operator の ACTIVE Task を明示的に切り替える `/task <target>` escape hatch の仕様。Task Context v1（Issue #2564、advisory-only 化と authority 移管: Issue #2625）が `UserPromptExpansion` command lifecycle 内で deterministic に処理する唯一の手動 rebind 経路。「Task を切り替えたい」「別の Issue に移りたい」「/task の使い方」のトリガーで参照する。
---

# `/task <target>` — ACTIVE Task 明示切り替え escape hatch

Task Context v1（Issue #2564、Issue #2625 で authority 移管）が Native Claude
operator 向けに提供する、ACTIVE Task を人間が意図的に supersede/rebind する
ための唯一の明示的経路。

## 位置づけ

- これは Claude Code の custom slash command（`.claude/commands/*.md`）では
  ない。`/task <target>` の state-changing authority は、Claude Code の
  `UserPromptExpansion` command lifecycle（`command_name == "task"`、
  user-typed slash/Skill command が展開される時にのみ発火する専用イベント）
  上でのみ処理される。`.claude/hooks/task_context/hook_entry.py`（event:
  `UserPromptExpansion`, matcher: `task`）が `command_args` を
  `classifier.parse_slash_task_target` で構造化 target に解決し、
  `task_context_hook_flows.on_user_prompt_expansion` が atomic rebind を
  適用する。
- **Issue #2625（Owner Decision）**: 通常の `UserPromptSubmit`（ordinary
  natural-language prompt）は、raw 文字列 `/task ...` の特別扱いを
  state-changing authority として一切使用しない。`UserPromptSubmit` 上で
  `/task` らしい raw text を classifier が観測しても（
  `classification_kind == "SLASH_TASK"`）、Task/Activity/Binding の
  mutation は一切行われない non-mutating no-op として扱われる
  （`reason_code: slash_task_raw_text_no_state_authority`）。これは
  「hook input だけから physical human submit と internal completion/
  injected turn を完全には区別できない」という前提と、ordinary prompt に
  だけ raw 文字列不信任を適用し `/task` にだけ適用しない非対称な扱いを
  解消するための変更である。
- 判定の precedence は常に最優先: `UserPromptExpansion` 上の `/task`
  authority > 通常の primary-target classifier（EXPLICIT/INFERRED/
  REFERENCE_ONLY/AMBIGUOUS/NONE）。ACTIVE な別 Task の mismatch は
  advisory-only（Issue #2625 AC1/AC2）であり、`/task <target>` は常に
  この advisory を無条件に supersede して rebind できる。

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

- classifier（target 解析ロジック。文字列パースのみ、authority ではない）:
  `.claude/hooks/task_context/classifier.py`
  （`_SLASH_TASK_RE` / `parse_slash_task_target`）
- adapter entry point（`UserPromptExpansion`, matcher: `task`）:
  `.claude/hooks/task_context/hook_entry.py`
  （`_apply_user_prompt_expansion_fields`）
- server 側 rebind flow（sole state-changing authority）:
  `scripts/task-context/task_context_hook_flows.py` の
  `on_user_prompt_expansion`
- hook 登録: `.claude/settings.json`（`UserPromptExpansion`, matcher: `task`）
- 検証: `tests/task-context/test_hook_flows_user_prompt_expansion.py`
  （service 層）、`tests/task-context/test_hook_entry_subprocess.py`
  （実 subprocess 経由の adapter 層）

## Stop Conditions

- 通常の自然言語 prompt や shell 起動引数を escape hatch として扱わない
  （`/task` という文字列で prompt が literal に開始する場合のみ、Claude
  Code 自身の `UserPromptExpansion` command lifecycle 経由で special case
  が発火する）。
- ordinary `UserPromptSubmit` 上の raw `/task` 文字列を state-changing
  authority として再導入しない（Issue #2625 Owner Decision）。
