---
name: task
description: Native Claude operator の ACTIVE Task を明示的に強制切り替えする `/task <target>` の仕様。通常の Issue/PR 作業開始 prompt は Task Context が自動で追従する（Issue #2827）ため、`/task` は normal workflow の必須手順ではなく低頻度の explicit forced override（escape hatch）である。Task Context v1（Issue #2564、authority 移管: Issue #2625）が `UserPromptExpansion` command lifecycle 内で deterministic に処理する。「Task を強制的に切り替えたい」「自動で切り替わらなかった」「/task の使い方」のトリガーで参照する。
---

# `/task <target>` — ACTIVE Task 強制切り替え（explicit override / escape hatch）

Task Context v1（Issue #2564、Issue #2625 で authority 移管）が Native Claude
operator 向けに提供する、ACTIVE Task を人間が意図的に強制 supersede/rebind
するための低頻度の explicit forced override（escape hatch）。

## 通常の切り替えは `/task` を必要としない（Issue #2827）

- ordinary な Issue/PR 作業開始 prompt（例: `Issue #123 を対象にレビューして`）が
  ちょうど 1 つの high-confidence primary target（GitHub Issue、または local claim
  済み PR）を明示している場合、Task Context は `UserPromptSubmit` の観測済み
  provenance を根拠に、既存の atomic binder で対象の Task へ**自動で rebind**
  する（`reason_code: user_prompt_primary_target_rebind`）。operator は
  `/task` を先に入力する必要はない。
- 自動 rebind しない場合（曖昧・複数 primary・参考のみ・未 claim PR・別の live
  managed session が保持する Task・内部由来の入力など）でも Claude の通常処理は
  継続し、確認 dialog や `/task` は強制されない。Task が自動では切り替わらなかった
  ときの強制手段が、以下の `/task <target>` である。

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
  REFERENCE_ONLY/AMBIGUOUS/NONE）。ordinary prompt の自動 rebind が適格でない
  ACTIVE な別 Task の mismatch は advisory-only（Issue #2625 AC1/AC2、mutation
  なし）であり、`/task <target>` は常にこの advisory を無条件に supersede して
  rebind できる。別の live managed Binding が保持する Task にも `/task` は
  強制的に rebind できる（自動 rebind は共有しない）。

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

## unbound セッションでの bootstrap（Issue #2790 AC2/AC5）

`/task <target>` を実行した Native Claude session に現在の Task Context
Binding が存在しない場合（典型例: `fork` は Issue #2564 AC15 により親
operator の TabBinding を意図的に継承しない by-design non-inheritance
であり、fork 後のセッションは恒常的に unbound のまま残る）、次の条件を
すべて満たすときに限り、`/task <target>` 自身が明示的 bootstrap authority
として動作し、独立した新規 Binding + ExecutionRun を作成してその target に
bind する（`bootstrap_unbound_session`、
`scripts/task-context/task_context_hook_flows.py`）:

- Herdr-tracked セッションである（`herdr_tab_id` が存在する）
- 有効な Claude session identity（`claude_session_id`）が存在する
- 現在の Binding が存在しない（`get_binding_by_current_session` が
  `NotFoundError`）
- ユーザーが明示的に `/task` を実行し、かつ解決済みの target（GitHub
  ref または ad-hoc title）を伴っている

target が一切指定されていない場合（`/task` のみ）は、bootstrap を
speculative に行わず、従来通り明示的な validation failure
（`no_binding_for_session`）として扱う。この bootstrap は既存の
`create_binding`/`relocate_binding`/`start_execution_run`/
`bind_target_to_binding`/`bind_ad_hoc_task_to_binding` という既存の
typed service layer のみで完結し、新規 daemon・lease table・lock
coordinator は一切追加しない。親 Binding（fork 元）の identity・
session・location は一切変更されない。

`no_binding_for_session` かつ target 未指定の場合のエラーメッセージ
（`.claude/hooks/task_context/hook_entry.py` の
`_task_command_failure_message`）は、target 再入力ではなく Binding
不在が原因であることと、有効な target を指定すれば bootstrap される
ことを明示する（Issue #2790 AC9 -- 修正前は target が完全修飾済みでも
「再入力せよ」と誤誘導していた）。

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
  （実 subprocess 経由の adapter 層）、
  `tests/task-context/test_hook_flows_session_start.py`（fork bootstrap /
  compact identity 不変の regression）、
  `tests/task-context/test_hook_entry_task_command_messages.py`
  （エラーメッセージが原因に一致することの regression）

## Stop Conditions

- 通常の自然言語 prompt や shell 起動引数を escape hatch として扱わない
  （`/task` という文字列で prompt が literal に開始する場合のみ、Claude
  Code 自身の `UserPromptExpansion` command lifecycle 経由で special case
  が発火する）。
- ordinary `UserPromptSubmit` 上の raw `/task` 文字列を state-changing
  authority として再導入しない（Issue #2625 Owner Decision）。
