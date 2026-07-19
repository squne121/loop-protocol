---
doc_id: hook-boundaries
doc_title_ja: フック境界ドキュメント
status: stable
related_issue: 923
schema_version: hook_boundaries_manifest_v1
---

# Hook 境界ドキュメント — Hook Boundaries

このドキュメントは `.claude/settings.json` に設定されている全フックの
**責務分類**・**fail policy**・**exit code contract**・**stdout/stderr contract**・**agent routing** を正本として記述する。

`scripts/check_hook_boundaries.py` がこの YAML manifest と `settings.json` を構造照合し drift を検出する。

> **重要**: Codex CLI および Claude Code の hook は **fail-closed ローカルガードレール** であり、セキュリティ境界ではない。  
> hook failure は non-blocking の場合でも transcript にエラーとして露出し、後続の agent 判断を汚染しうる。  
> セキュリティ上の保護は branch protection / GitHub Actions CI / repository permission で行う。  
> Codex hooks は `.codex/hooks.json` に集約し、`.codex/config.toml` に inline hooks を混在させない。

---

## 1. 分類の定義

| 分類 | 定義 |
|---|---|
| `blocker` | hook が非ゼロ終了した場合、agent はその操作を停止（block）しなければならない |
| `telemetry` | hook が非ゼロ終了しても agent は作業を継続する（best-effort）。成功時のみ artifact を生成する |
| `warning` | hook が非ゼロ終了した場合、agent は警告を記録するが操作は継続する |
| `mode_dependent` | hook の動作（block / pass-through）が環境変数で切り替わる |

---

## 2. hook_boundaries_manifest_v1

機械可読な正本 YAML。`scripts/check_hook_boundaries.py` はこの block を入力として使用する。

照合キーは `(handler_id, event)` の複合キーを使用する。
同一スクリプトが複数 event（Stop / SubagentStop 等）に配置される場合は別エントリとして記述する。

```yaml
hook_boundaries_manifest_v1:
  - handler_id: secret_boundary_guard
    event: PreToolUse
    matcher: "Bash|Read|Write|Edit|Grep|Glob|MultiEdit"
    command: "${CLAUDE_PROJECT_DIR}/.claude/hooks/secret_boundary_guard.sh"
    args: []
    timeout: 10
    classification: blocker
    fail_policy: fail_closed
    script_exit_contract:
      normal: 0
      block: 2
      internal_producer_failure: 2
    claude_event_semantics:
      event: PreToolUse
      exit_2_effect: blocks_tool_call
      other_nonzero_effect: non_blocking_error_or_stderr_visible
    stdout_contract: silent_on_allow
    stderr_contract: minimal_structural_message_on_block
    redaction_contract:
      no_raw_command: true
      no_raw_secret_like_value: true
      no_raw_transcript: true
      no_manifest_body_on_stdout: true
    agent_action:
      on_nonzero: stop_tool_call
      on_zero: proceed
    notes: >
      secret / credential 境界フック。
      jq 不在など自身がエラーになった場合も fail-closed（exit 2）。
      このフックは fail-closed のまま維持し、best-effort 化してはならない（AC3）。

  - handler_id: local_main_branch_guard
    event: PreToolUse
    matcher: "Bash"
    command: "${CLAUDE_PROJECT_DIR}/.claude/hooks/local_main_branch_guard.sh"
    args: []
    timeout: 10
    classification: blocker
    fail_policy: fail_closed
    script_exit_contract:
      normal: 0
      block: 2
      internal_producer_failure: 2
    claude_event_semantics:
      event: PreToolUse
      exit_2_effect: blocks_tool_call
      other_nonzero_effect: non_blocking_error_or_stderr_visible
    stdout_contract: silent_on_allow
    stderr_contract: minimal_structural_message_on_block
    redaction_contract:
      no_raw_command: true
      no_raw_secret_like_value: true
      no_raw_transcript: true
      no_manifest_body_on_stdout: true
    agent_action:
      on_nonzero: stop_tool_call
      on_zero: proceed
    notes: >
      local root checkout のブランチ drift を early-stage でブロックするフック。
      secret_boundary_guard の後、worktree_scope_guard の前に配置する
      （順序: secret → local_main → worktree）。
      local root context（cwd == primary worktree root）かつ default branch 以外への
      branch mutation コマンドを fail-closed で block する。
      linked issue worktree 内では allow し、既存の worktree_scope_guard に委譲する。
      guard script 不在など自身がエラーになった場合も fail-closed（exit 2）。
      このフックは fail-closed のまま維持し、best-effort 化してはならない（Issue #1014）。
      reason_code 一覧（#1089 追加、#1109 で gh_mutation_denied 分離、#1124 で 5 分類追加）:
      readonly_command / branch_safe_maintenance_command / deterministic_checker_command /
      github_remote_ops_command / gh_mutation_denied / unparseable_branch_mutation。
      gh_mutation_denied: gh issue/pr のうち readonly allowlist 外かつ github_remote_ops_command・github_issue_mutation_command 外の mutation 系コマンドを fail-closed した場合に使用（#1109）。
      github_remote_ops_command: post-merge-cleanup 最小集合（gh issue close/comment/reopen, gh pr comment/edit）および github_issue_mutation_command に使用（#1124）。
      unparseable_branch_mutation: compound/wrapper/redirection・/tmp wrapper・python -c・parse failure 等に使用。
      fd-duplication（2>&1 |）はパイプ前のみ正規化して readonly_command として許可。
      gh issue view/list, gh pr view/list/status は readonly_command として許可。
      gh issue/pr mutation コマンド（edit/close/merge 等）は gh_mutation_denied で fail-closed（#1109）。
      /tmp wrapper / python -c は unparseable_branch_mutation で fail-closed。
      deterministic_checker_command は DETERMINISTIC_CHECKER_ALLOWLIST の exact-path のみ許可。
      probe scripts (git_ref_probe.py / git_worktree_probe.py) は DETERMINISTIC_CHECKER_ALLOWLIST に登録済み（Issue #1197）。
      local root default branch 保護（branch drift 防止）は維持しつつ、Issue #1241 以降は shared policy で
      bounded な `rtk git add/commit/push` と `HOOK_COMMAND_REPAIR_HINT_V1` を扱う。

      ## gh CLI コマンド 5 分類（#1124）

      local_main_branch_guard は gh issue/pr コマンドを以下の 5 分類で判定する:

      | 分類 | reason_code | 代表コマンド | 判断基準 |
      |---|---|---|---|
      | display_readonly_command | readonly_command | gh issue view, gh pr view, gh issue list | DISPLAY_READONLY_PATTERNS に合致 |
      | readonly_artifact_export_command | readonly_command | gh issue view <N> ... > tmp/<file> | `>` で tmp/ 先へのリダイレクトのみ |
      | github_issue_mutation_command | github_remote_ops_command | gh issue create | --repo squne121/loop-protocol + --body-file tmp/... 必須 |
      | github_pr_metadata_command | github_remote_ops_command | gh pr comment/edit | is_github_remote_ops_command で判定 |
      | github_destructive_command | gh_mutation_denied | gh pr merge, gh pr checkout | 上記以外の gh issue/pr mutation |

      ### github_issue_mutation_command の allow 条件

      `gh issue create` が以下の条件をすべて満たす場合のみ allow:
      1. `--repo squne121/loop-protocol` が存在する（完全一致）
      2. `--body-file tmp/<path>` が存在する（tmp/ から始まる相対パス、"-" は不可）
      3. interactive フラグ不在: `--editor` / `-e` / `--web` / `-w`
      4. `--title <value>` が存在する

      ### readonly_artifact_export_command の allow 条件

      `gh issue view <N> ... > tmp/<filename>` が以下の条件をすべて満たす場合のみ allow:
      1. `gh issue view <N>` で始まる（view のみ、edit/create は不可）
      2. リダイレクトは `>` のみ（`>>` は不可）
      3. 先は `tmp/` から始まる（src/, docs/, .env, .git は不可）
      4. lhs にシェルメタキャラ（|, ;, &&, ||, backtick, $()）なし
      raw `gh issue edit` / `gh issue comment` は `gh_mutation_denied` で block される。
      `gh api -f body=...` / `gh api graphql -f query='mutation { ... }'` /
      `gh api --method POST ...` のような allowlist 外 `gh api` は
      `github_api_command` (`gh_api_not_allowed`) で block される。

  - handler_id: worktree_scope_guard
    event: PreToolUse
    matcher: "Bash|Write|Edit|MultiEdit"
    command: "${CLAUDE_PROJECT_DIR}/.claude/hooks/worktree_scope_guard.sh"
    args: []
    timeout: 10
    classification: blocker
    fail_policy: fail_closed
    script_exit_contract:
      normal: 0
      block: 2
      internal_producer_failure: 2
    claude_event_semantics:
      event: PreToolUse
      exit_2_effect: blocks_tool_call
      other_nonzero_effect: non_blocking_error_or_stderr_visible
    stdout_contract: silent_on_allow
    stderr_contract: minimal_structural_message_on_block
    redaction_contract:
      no_raw_command: true
      no_raw_secret_like_value: true
      no_raw_transcript: true
      no_manifest_body_on_stdout: true
    agent_action:
      on_nonzero: stop_tool_call
      on_zero: proceed
    notes: >
      worktree スコープ外の mutation を遮断するフック。
      python3 不在など自身がエラーになった場合も fail-closed（exit 2）。
      active issue worktree がある場合でも shared cleanup / skill-runtime exact policy のみを例外とし、
      bootstrap 系の追加 allowlist はこの PR では導入しない。
      Issue #1241 以降は issue worktree publish path の `rtk git add/commit/push` だけを shared bounded
      policy で解釈し、deny 時は `HOOK_COMMAND_REPAIR_HINT_V1` を stderr に返す。
      Issue #1402 以降は strict publish lane 用 env binding
      (`LOOP_PUBLISH_EXPECTED_REMOTE_HEAD`, `LOOP_PUBLISH_CURRENT_REMOTE_HEAD`,
      `LOOP_PUBLISH_DECLARED_PUBLISH_HEAD`, `LOOP_PUBLISH_VERIFIED_HEAD`,
      `LOOP_PUBLISH_ALLOWED_PATHS_GATE_STATUS`, `LOOP_PUBLISH_REMOTE_READBACK_SOURCE`)
      が全て揃った場合だけ allow retry を検討する。欠落・partial・malformed な場合、shared policy は
      `publish_guard_context_missing` / `publish_guard_context_invalid` の `PUBLISH_SAFETY_STOP_REPORT_V1` とともに停止する。

  - handler_id: guard-japanese-prose
    event: PreToolUse
    matcher: "Bash|Write|Edit|MultiEdit"
    command: "${CLAUDE_PROJECT_DIR}/.claude/hooks/guard-japanese-prose.sh"
    args: []
    timeout: 15
    classification: mode_dependent
    fail_policy: shadow_by_default
    script_exit_contract:
      normal: 0
      block_only_in_enforce_mode: 2
    claude_event_semantics:
      event: PreToolUse
      exit_2_effect: blocks_tool_call
      other_nonzero_effect: non_blocking_error_or_stderr_visible
    stdout_contract: silent
    stderr_contract: jsonl_shadow_log_or_block_reason
    redaction_contract:
      no_raw_command: true
      no_raw_secret_like_value: true
      no_raw_transcript: true
      no_manifest_body_on_stdout: true
    agent_action:
      on_nonzero_shadow: proceed_and_log
      on_nonzero_enforce: stop_tool_call
      on_zero: proceed
    mode_env: GUARD_JAPANESE_PROSE_MODE
    mode_values:
      unset_or_shadow: "exit 0（shadow モード: block せず JSONL に記録のみ）"
      enforce: "exit 2（enforce モード: 日本語比率不足でブロック）"
      invalid: "shadow として動作 + invalid_mode を JSONL に記録"
    notes: >
      default は shadow モード（exit 0）。
      GUARD_JAPANESE_PROSE_MODE=enforce 設定時のみ exit 2 でブロックする（AC8）。
      handler_id はスクリプトファイル名（guard-japanese-prose）と一致する。

  - handler_id: rtk_boundary_shadow_guard
    event: PreToolUse
    matcher: "Bash"
    command: "${CLAUDE_PROJECT_DIR}/.claude/hooks/rtk_boundary_shadow_guard.sh"
    args: []
    timeout: 10
    classification: telemetry
    fail_policy: fail_open
    script_exit_contract:
      normal: 0
      internal_producer_failure: 0
    claude_event_semantics:
      event: PreToolUse
      exit_2_effect: blocks_tool_call
      other_nonzero_effect: non_blocking_error_or_stderr_visible
    stdout_contract: silent
    stderr_contract: jsonl_shadow_log_only
    redaction_contract:
      no_raw_command: true
      no_raw_secret_like_value: true
      no_raw_transcript: true
      no_manifest_body_on_stdout: true
    agent_action:
      on_any: proceed
    notes: >
      rtk trust boundary の direct bypass を shadow モードで記録するのみ。
      block は一切行わず、常に exit 0。
      task blocker にしてはならない（AC2）。
      hook failure は diagnostic artifact 欠落として記録・報告される（AC10）。

  - handler_id: session_manifest_coordinator
    event: Stop
    matcher: null
    command: "${CLAUDE_PROJECT_DIR}/.claude/hooks/session_manifest_coordinator.sh"
    args: []
    timeout: 55
    classification: telemetry
    fail_policy: fail_open
    script_exit_contract:
      normal: 0
      internal_producer_failure: 0
    claude_event_semantics:
      event: Stop
      exit_2_effect: prevents_stop
      other_nonzero_effect: non_blocking_error_or_stderr_visible
    stdout_contract: silent
    stderr_contract: machine_readable_timeout_reason_max_10_lines
    redaction_contract:
      no_raw_command: true
      no_raw_secret_like_value: true
      no_raw_transcript: true
      no_manifest_body_on_stdout: true
    agent_action:
      on_any: proceed
    notes: >
      Stop イベントで pending debounce state を flush したうえで session manifest を生成する。
      guard（session_recording_policy_guard.sh）と
      producer（generate_session_manifest_from_hook.mjs）を逐次実行する coordinator。
      debounce flush / guard / producer は個別 timeout を持ち、
      hang 時も 60 秒未満で exit 0 のまま machine-readable timeout reason を返す。
      guard failure / producer failure いずれも exit 0（fail-open）。
      task blocker にしてはならない（AC2）。
      hook failure は diagnostic artifact 欠落として記録・報告される（AC10）。

  - handler_id: session_manifest_coordinator
    event: SubagentStop
    matcher: null
    command: "${CLAUDE_PROJECT_DIR}/.claude/hooks/session_manifest_coordinator.sh"
    args: []
    timeout: 55
    classification: telemetry
    fail_policy: fail_open
    script_exit_contract:
      normal: 0
      internal_producer_failure: 0
    claude_event_semantics:
      event: SubagentStop
      exit_2_effect: prevents_subagent_stop
      other_nonzero_effect: non_blocking_error_or_stderr_visible
    stdout_contract: silent
    stderr_contract: machine_readable_timeout_reason_max_10_lines
    redaction_contract:
      no_raw_command: true
      no_raw_secret_like_value: true
      no_raw_transcript: true
      no_manifest_body_on_stdout: true
    agent_action:
      on_any: proceed
    notes: >
      SubagentStop イベントでも session_manifest_coordinator と同一スクリプトを使用。
      pending debounce state を flush してから guard / producer を実行する。
      task blocker にしてはならない（AC2）。
      hook failure は diagnostic artifact 欠落として記録・報告される（AC10）。

  - handler_id: session_manifest_debounce
    event: PostToolUse
    matcher: "Bash|Edit|Write"
    command: "node"
    args:
      - "${CLAUDE_PROJECT_DIR}/.claude/hooks/session_manifest_debounce.mjs"
    timeout: 10
    classification: telemetry
    fail_policy: fail_open
    script_exit_contract:
      normal: 0
      internal_producer_failure: 0
    claude_event_semantics:
      event: PostToolUse
      exit_2_effect: cannot_block_completed_tool_call
      other_nonzero_effect: non_blocking_error_or_stderr_visible
    stdout_contract: silent
    stderr_contract: silent_or_machine_readable_summary_max_10_lines
    redaction_contract:
      no_raw_command: true
      no_raw_secret_like_value: true
      no_raw_transcript: true
      no_manifest_body_on_stdout: true
    agent_action:
      on_any: proceed
    notes: >
      PostToolUse hook（Bash|Edit|Write）は Claude Code native async で
      session_manifest_debounce.mjs を front gate として起動する。
      read-only Bash は skip し、mutation Bash / Edit / Write のみ debounce queue へ入れる。
      command が "node" で、実際のスクリプトは args[0] に格納される。
      producer invocation は detached worker で集約され、PostToolUse path 自体は stdout を出さない。
      task blocker にしてはならない（AC2）。
      hook failure は diagnostic artifact 欠落として記録・報告される（AC10）。

  - handler_id: ci_test_performance_advisory
    event: PreToolUse
    matcher: "Bash|Write|Edit|MultiEdit"
    command: "${CLAUDE_PROJECT_DIR}/.claude/hooks/ci_test_performance_advisory.sh"
    args: []
    timeout: 10
    classification: warning
    fail_policy: fail_open
    script_exit_contract:
      normal: 0
      internal_producer_failure: 0
    claude_event_semantics:
      event: PreToolUse
      exit_2_effect: blocks_tool_call
      other_nonzero_effect: non_blocking_error_or_stderr_visible
    stdout_contract: hookSpecificOutput_additionalContext_on_match_silent_otherwise
    stderr_contract: silent_or_minimal_on_failure
    redaction_contract:
      no_raw_command: true
      no_raw_secret_like_value: true
      no_raw_transcript: true
      no_manifest_body_on_stdout: true
    agent_action:
      on_match: emit_advisory_and_proceed
      on_no_match: proceed_silently
      on_any_failure: proceed
    notes: >
      CI/test-lane 関連 path（.github/workflows/、pyproject.toml、uv.lock 等）を
      検出した場合に CI_TEST_PERFORMANCE_ADVISORY_V1 を stdout へ出力する non-blocking advisory。
      stdout 形式は hookSpecificOutput.additionalContext ラッパー（Claude Code / Codex CLI 公式 PreToolUse 出力契約）に準拠する:
      { "hookSpecificOutput": { "hookEventName": "PreToolUse", "additionalContext": "CI_TEST_PERFORMANCE_ADVISORY_V1 {inner_json}" } }
      inner payload スキーマ: schemas/ci_test_performance_advisory_v1.schema.json
      block: false であり、通常 tool call を一切 block しない。
      失敗時（jq 不在・JSON parse error 等）も exit 0（fail-open）で継続する。
      block してはならない（AC2）。fail_open を維持する。

  - handler_id: root_temporary_residue_advisory
    event: PreToolUse
    matcher: "Bash|Write|Edit|MultiEdit"
    command: "${CLAUDE_PROJECT_DIR}/.claude/hooks/root_temporary_residue_advisory.sh"
    args: []
    timeout: 10
    classification: warning
    fail_policy: fail_open
    script_exit_contract:
      normal: 0
      internal_producer_failure: 0
    claude_event_semantics:
      event: PreToolUse
      exit_2_effect: blocks_tool_call
      other_nonzero_effect: non_blocking_error_or_stderr_visible
    stdout_contract: hookSpecificOutput_additionalContext_on_match_silent_otherwise
    stderr_contract: silent_or_minimal_on_failure
    redaction_contract:
      no_raw_command: true
      no_raw_secret_like_value: true
      no_raw_transcript: true
      no_manifest_body_on_stdout: true
    agent_action:
      on_match: emit_advisory_and_proceed
      on_no_match: proceed_silently
      on_any_failure: proceed
    notes: >
      repo root の `.tmp/`、`.temp/`、`.tmp-*` を検出した場合に
      REPO_TEMP_FOLDER_ADVICE_V1 を stdout に出力する non-blocking advisory。
      stdout 形式は hookSpecificOutput.additionalContext ラッパーに準拠する:
      { "hookSpecificOutput": { "hookEventName": "PreToolUse", "additionalContext": "REPO_TEMP_FOLDER_ADVICE_V1 {inner_json}" } }
      inner payload スキーマ: schemas/repo_temp_folder_advice_v1.schema.json
      block: false を固定し、tool call を止めずに `tmp/` または `.claude/tmp/` への移行を案内する。
      local_main_branch_guard の classification: blocker は維持し、この hook に deny logic を混在させない。

  - handler_id: save_loop_state_before_compaction
    event: PreCompact
    matcher: null
    command: "${CLAUDE_PROJECT_DIR}/.claude/hooks/save_loop_state_before_compaction.sh"
    args: []
    timeout: 30
    classification: telemetry
    fail_policy: fail_open
    script_exit_contract:
      normal: 0
      internal_producer_failure: 0
    claude_event_semantics:
      event: PreCompact
      exit_2_effect: blocks_compaction
      other_nonzero_effect: non_blocking_error_or_stderr_visible
    stdout_contract: always_empty
    stderr_contract: diagnostic_on_failure_max_10_lines
    redaction_contract:
      no_raw_command: true
      no_raw_secret_like_value: true
      no_raw_transcript: true
      no_manifest_body_on_stdout: true
    agent_action:
      on_any: proceed
    notes: >
      PreCompact hook。compaction をブロックしてはならない設計（常に exit 0）。
      save 失敗時は stderr に記録し exit 0 で継続。
      task blocker にしてはならない（AC2）。
      hook failure は diagnostic artifact 欠落として記録・報告される（AC10）。
```

## HOOK_COMMAND_REPAIR_HINT_V1（Hook コマンド修復ヒント）

repair hint は agent steering 用の bounded diagnostics であり、authorization の代替ではない。

```yaml
HOOK_COMMAND_REPAIR_HINT_V1:
  blocked_command_class: "rtk_git_add"
  boundary_layer: "worktree_scope_guard_denied"
  reason_code: "git_add_requires_explicit_pathspec"
  safe_action: "broad pathspec をやめて 1 file 単位の pathspec を使う"
  suggested_command: "rtk git add <allowed-path-file>"
  forbidden_alternatives: ["git add .", "git add -A", "git push --force"]
  verification_command: "git diff --name-only"
  stop_condition: "safe な single command に直せない場合は人間判断"
```

### reason_code の分岐

| reason_code | safe_action の要点 | suggested / verification の例 |
|---|---|---|
| `git_add_requires_explicit_pathspec` | broad pathspec をやめて 1 file に絞る | `rtk git add <allowed-path-file>` / `git diff --name-only` |
| `git_add_outside_allowed_paths` | Issue contract の Allowed Paths に戻す | `rtk git add <allowed-path-file>` / `git diff --name-only` |
| `allowed_paths_missing_for_git_mutation` | runtime に Allowed Paths binding がある状態へ戻す | `git diff --cached --name-only` / `git diff --cached --name-only` |
| `commit_staged_changes_outside_allowed_paths` | staged diff を Allowed Paths subset に戻す | `rtk git commit -m "issue-1241 update"` / `git diff --cached --name-only` |
| `push_refspec_requires_active_branch` | `PUBLISH_LANE_DECISION_V1 status=allow_retry` の allowed command だけを使う | suggestion なし / `git ls-remote --refs --exit-code origin refs/heads/<active-branch>` |
| `publish_guard_context_missing` / `publish_guard_context_invalid` | publish lane の decision inputs を live readback 証跡付きで揃える | suggestion なし / `git ls-remote --refs --exit-code origin refs/heads/<branch>` |
| `allowed_paths_gate_not_ok` | Allowed Paths gate を `ok` にできる current-head 証跡を取得する | suggestion なし / `allowed_paths_review_gate.py status == ok` |
| `issue_context_required` | issue 未解決の root / unrelated cwd では mutation しない | `git worktree list` / `git branch --show-current` |
| `target_dir_outside_worktree` | active issue worktree 配下へ戻る | `git status --short` / `git branch --show-current` |
| `no_matching_worktree` / `ambiguous_worktree` | worktree catalog を 1 件に特定する | `git worktree list` / `git branch --show-current` |
| `rtk_unknown_inner` | wrapper を剥がさず direct な `rtk git add/commit/push` へ揃える | `rtk git add <allowed-path-file>` / `git branch --show-current` |
| `git_add_requires_controlled_executor` / `git_commit_requires_controlled_executor` | raw `git add`/`git commit`/`rtk git add`/`rtk git commit` は agent lane では常に deny -- controlled executor 経由に切り替える（Issue #1611 AC9） | `uv run --locked python3 scripts/agent-guards/controlled_git_change_exec.py --help` / `git diff --cached --name-status -M -z` |

### 運用ガイド

- `HOOK_COMMAND_REPAIR_HINT_V1` は direct `rtk git ...` の exact / bounded 形だけを示し、`bash -lc`、`env FOO=... rtk git ...`、`command rtk git ...` の wrapper 展開は suggestion に使わない。
- `suggested_command` は authorization を付与しない。rules / hooks / post-run verifier が独立に reject できる。
- `allowed_paths_missing_for_git_mutation` は fail-closed 理由であり、Issue contract の Allowed Paths binding が runtime に見えていない状態を示す。
- branch publish failure では `boundary_layer` と `reason_code` を分離し、`expected_remote_head` / `current_remote_head` / `local_head` / `verified_head` の比較が崩れたら `PUBLISH_SAFETY_STOP_REPORT_V1` に倒す。
- Issue #1611 以降、staging/commit の唯一の認可経路は `scripts/agent-guards/controlled_git_change_exec.py`
  （`execute_controlled_change`）である。`git_mutation_command_policy.py` の
  `classify_agent_lane_add_commit` は raw `git add`/`git commit`/`rtk git add`/`rtk git commit`
  シェルコマンド文字列を、そのコマンド自身が controlled executor でない限り常に deny する（既存の
  `classify_rtk_git_mutation` の add/commit 分岐は後方互換のため変更していない -- 実際の agent lane
  はこの新しい deny-first 判定を優先する）。Codex 側の静的 rule
  (`.codex/rules/default.rules`) も `rtk git add` / `rtk git commit -m` を `forbidden` に narrowing
  し、controlled executor 本体の exact invocation prefix
  (`uv run --locked python3 scripts/agent-guards/controlled_git_change_exec.py`) のみ `allow` にして
  いる（AC14、`scripts/ci/codex_execpolicy_matrix.py` の `execpolicy_case_definitions()` に静的ケース
  として記録）。

---

## 3. agent 判断表（Claude Code / Claude Code 向け）

| hook | 分類 | hook failure 時の agent 動作 |
|---|---|---|
| `secret_boundary_guard.sh` | blocker | **操作を停止（block）** |
| `local_main_branch_guard.sh` | blocker | **操作を停止（block）** |
| `worktree_scope_guard.sh` | blocker | **操作を停止（block）** |
| `guard-japanese-prose.sh` | mode_dependent | shadow モード: 継続（log のみ）/ enforce モード: **停止** |
| `rtk_boundary_shadow_guard.sh` | telemetry | 継続（log のみ） |
| `ci_test_performance_advisory.sh` | warning / fail_open | 継続（advisory 出力のみ、block なし） |
| `session_manifest_coordinator.sh`（Stop） | telemetry | 継続 |
| `session_manifest_coordinator.sh`（SubagentStop） | telemetry | 継続 |
| `session_manifest_debounce.mjs` | telemetry | 継続 |
| `save_loop_state_before_compaction.sh` | telemetry | 継続 |

---

## 4. agent 判断表（Codex CLI / Codex CLI 向け）

この manifest は Claude Code の `.claude/settings.json` hook topology を対象とする。Codex CLI については、この manifest から Claude Code と同等の event / output / exit-code parity を主張しない。Codex 側の制御は sandbox、approval、network policy、telemetry、および Codex 固有 hook 実装の検証で別途扱う。

| hook | Codex CLI での扱い |
|---|---|
| `secret_boundary_guard.sh` | この manifest の対象外。同等の制御は Codex 固有の policy / allowed-tools 設定で別途実装する必要がある |
| `local_main_branch_guard.sh` | `.codex/hooks.json` の PreToolUse・PermissionRequest 経由で Codex parity を実現。startup preflight `check_local_main_branch_state.py` も必須（Codex PreToolUse は完全な interception boundary ではないため） |
| `guard-japanese-prose.sh` | この manifest の対象外（Claude Code のみ） |
| その他 telemetry hooks | この manifest の対象外。Codex セッションでの telemetry 収集は Codex 固有実装に依存する |

---

## 5. fail-open telemetry 設計の説明

AC2 対応: 以下の best-effort telemetry フックは作業 blocker にしない。

- `session_manifest_coordinator.sh`（Stop / SubagentStop）: 停止時コーディネータ
- `session_manifest_debounce.mjs`（PostToolUse front gate）: 事前集約ゲート
- `save_loop_state_before_compaction.sh`（PreCompact）: 圧縮前保存
- `rtk_boundary_shadow_guard.sh`（PreToolUse）: shadow 記録ガード

これらは全て `fail_policy: fail_open` で設計されており、hook failure 時も exit 0 を返す（AC2）。

hook failure が発生した場合は diagnostic artifact が欠落するが、agent の作業継続は妨げられない。
failure は stderr または shadow JSONL に記録されるため、診断として追跡可能である（AC10）。
`generate_session_manifest_from_hook.mjs` は Stop/SubagentStop producer / debounce worker downstream として best-effort 実行される。

---

## 6. secret_boundary_guard.sh の fail-closed 維持方針

AC3 対応: `secret_boundary_guard.sh` は fail-closed 設計を維持する。

- `jq` 不在など環境エラーでも exit 2（fail-closed）
- stdin が不正な PreToolUse JSON の場合も exit 2（fail-closed）
- この hook を best-effort 化（fail-open に変更）することは禁止する

---

## 7. mode_dependent: guard-japanese-prose.sh（モード依存）

AC8 対応: `guard-japanese-prose.sh` の default 動作は **shadow モード（exit 0）**。

| `GUARD_JAPANESE_PROSE_MODE` | 動作 |
|---|---|
| 未設定 / `shadow` | exit 0（shadow モード: block せず JSONL に記録のみ） |
| `enforce` | exit 2（enforce モード: 日本語比率不足でブロック） |
| 不正値 | shadow として動作 + `invalid_mode` を JSONL に記録 |

この分離により、CI や強制モードでのみ block が発動し、通常開発フローの妨げを最小化する。

---

## 8. telemetry failure の報告方針

AC10 対応: telemetry hook failure は **task blocker ではない** が、以下の形で記録・報告される。

- `session_manifest_coordinator.sh`: stderr に diagnostic を出力（最大 10 行）し、artifact 欠落を記録
- `session_manifest_debounce.mjs`: stderr に front gate / flush / producer timeout の machine-readable summary を出力し、artifact 欠落を記録
- `generate_session_manifest_from_hook.mjs`: downstream producer failure を redacted stderr に出力し、artifact 欠落を記録
- `save_loop_state_before_compaction.sh`: stderr に保存失敗を出力（最大 10 行）
- `rtk_boundary_shadow_guard.sh`: JSONL shadow log に記録（log write 失敗時は無言で pass）

PR review・session 終了時に artifact 欠落が検出された場合、follow-up issue として記録・追跡する。

---

## 9. local_main_branch_guard.sh の fail-closed 維持方針（Issue #1014）

`local_main_branch_guard.sh` は fail-closed 設計を維持する。

- guard script 不在など環境エラーでも exit 2（fail-closed）
- stdin が不正な PreToolUse JSON の場合も exit 2（fail-closed）
- compound / unparseable コマンドも fail-closed（`unparseable_branch_mutation`）
- inline env override（コマンド文字列内の `LOOP_ALLOW_LOCAL_ROOT_BRANCH_CHANGE=1`）は block する
- hook process env の両変数（`LOOP_ALLOW_LOCAL_ROOT_BRANCH_CHANGE` + `LOOP_LOCAL_ROOT_BRANCH_CHANGE_REASON`）が設定された場合のみ manual override を許可する
- linked issue worktree 内では allow し、既存の `worktree_scope_guard` に委譲する

startup preflight: `uv run python3 scripts/check_local_main_branch_state.py --json`
Codex セッション開始前に必須実行。非ゼロ終了時は実装作業を開始しない。

### PreToolUse hook 実行順序（Claude Code）

`secret_boundary_guard` → `local_main_branch_guard` → `worktree_scope_guard` → その他

理由: branch drift は worktree scope violation より前に、より明確な reason code で止めるべき。

---

## 10. pretool_fastpath_classifier.py による fast-path 分類（Issue #1289）

`scripts/agent-guards/pretool_fastpath_classifier.py` は **独立した PreToolUse hook として登録しない共有ライブラリ** である。`local_main_branch_guard.py`（および Bash ラッパー経由で `.codex/hooks.json` の `local_main_branch_guard.sh` エントリ）から内部的に import され、既存の allow/block 判定を変更せずに telemetry を bounded な fast-path 分類で補強する。`.claude/settings.json` と `.codex/hooks.json` の PreToolUse hook トポロジ（hook 数）は本変更前後で変わらない（`check_hook_boundaries.py` で検証可能）。`.codex/hooks.json` は Codex runtime config であり、root key は `hooks` のみを許可する。fast-path contract metadata は下記 `fastpath_contract_v1` を正本とし、runtime config root へ再混入させない。

```yaml
fastpath_contract_v1:
  module: scripts/agent-guards/pretool_fastpath_classifier.py
  classification:
    - readonly_display
    - exact_controlled_executor_authorized
    - mutation_or_unknown
  fail_policy: fail_open
  stdout_contract: silent
  stderr_contract: silent
  registered_as_independent_hook: false
  runtime_config_root_key_allowed: false
```

### classification 語彙

| classification | 定義 |
|---|---|
| `readonly_display` | `local_main_branch_guard.is_readonly_command` と `worktree_scope_guard.classify_bash`（read_only）の **交差集合** のみ。片方でも unknown/mutating/cleanup/metadata mutation なら `mutation_or_unknown` |
| `exact_controlled_executor_authorized` | `controlled_skill_mutation_exec.py` / `skill_runtime_exec.py` への exact 呼び出しで、かつ command_id が registry の `ALL_COMMAND_IDS` に属し、repo binding・input namespace shape・stable policy_hash が計算可能である場合のみ |
| `exact_controlled_executor_shape`（内部・非公開終端） | canonical executor path + exact argv shape のみに一致し authorized 条件を満たさない。`classify()` の戻り値としては常に `mutation_or_unknown` に畳み込まれる（外部から直接観測されない） |
| `mutation_or_unknown` | 上記いずれにも一致しないデフォルト。既存 fail-closed hook chain を無変更で通過する |

### fail policy（失敗時方針）

fastpath classifier 自体は telemetry 専用であり **fail-open**（`fail_policy: fail_open`、`classification: telemetry`）。分類計算が例外を送出しても `local_main_branch_guard.evaluate()` の返す `status` / `reason_code` には一切影響しない（`fastpath` フィールドが `None` になるのみ）。既存 guard の block 判定は本変更の影響を受けない（AC2）。

### stdout/stderr contract（出力契約）

classifier は stdout/stderr に何も書き込まない（純粋な Python 関数）。呼び出し元 guard（`local_main_branch_guard.py`）の `_result()` 返却 dict に `fastpath` キーとして bounded な telemetry dict（`classification` / `command_id` / `policy_hash` / `display_summary` のみ）を付加する。raw command body・input-file 中の secret-like value は telemetry に含めない（AC3）。

### `gh api` token-level 検証

`gh api` は method・field flag・endpoint を token-level で検証し、`-X`/`--method` が POST/PATCH/PUT/DELETE、または `-f`/`-F`/`--field`/`--raw-field`/`--input` のような field flag が存在する場合は `mutation_or_unknown` とする。`gh issue`/`gh pr` の `--web`/`-w` は browser side effect を持つため `readonly_display` から除外する。

---

## 11. SESSION_MANIFEST_LEGACY_STATE_V1 診断契約（Issue #1430）

`session_manifest_debounce.mjs`（PostToolUse front gate）と `generate_session_manifest_from_hook.mjs`（Stop/SubagentStop producer）は、起動のたびに PR #1426 hard-cutover 以前の旧 layout の runtime state 残存を検出する。検出は read-only（`lstatSync`/`readdirSync`）であり、hook を fail-close させない best-effort telemetry である（分類は telemetry のまま変更しない）。

### 検出対象の旧 layout

| legacy_kind | 検出元スクリプト | 旧 path |
|---|---|---|
| `debounce_events_dir` | `session_manifest_debounce.mjs` | `artifacts/session-manifest-debounce/events/` |
| `debounce_worker_lock` | `session_manifest_debounce.mjs` | `artifacts/session-manifest-debounce/worker.lock` |
| `producer_lock_tmp` | `generate_session_manifest_from_hook.mjs`、および front gate 自身（AC14） | `artifacts/.lock-<32hex>` / `artifacts/.tmp-<uuid>[.failed]` |
| `producer_root_manifest` | `generate_session_manifest_from_hook.mjs`、および front gate 自身（AC14） | `artifacts/private-agent-session-manifest-<hook>-<timestamp>-<32hex>.json` |

AC14: `session_manifest_debounce.mjs` は PostToolUse の実運用経路（`spawn(..., {detached: true, stdio: 'ignore'})` で起動される `--worker` 子プロセス）の stdio が破棄されるため、producer 側の旧 layout（`producer_lock_tmp` / `producer_root_manifest`）も front gate 自身の同期呼び出し内で検出し、`stdio: 'ignore'` の子プロセスに依存せず観測可能にする。

### SESSION_MANIFEST_LEGACY_STATE_V1（診断スキーマ）

```yaml
SESSION_MANIFEST_LEGACY_STATE_V1:
  status: legacy_state_detected  # 固定値。検出時のみこの行自体が出力される
  legacy_kind: debounce_events_dir | debounce_worker_lock | producer_lock_tmp | producer_root_manifest
  paths: []          # 検出した legacy path の一覧（repo-relative）。先頭 20 件に bound（AC9）
  detected_at: <ISO 8601>
  total_count: <int> # 検出総数（bound 前）。AC9 で追加
  truncated: true | false  # total_count が bound（20 件）を超える場合のみ true。AC9 で追加
```

必須フィールド（`status` / `legacy_kind` / `paths` / `detected_at`）は Issue #1430 契約で固定されている。`total_count` / `truncated` は追加フィールドとして許容される（Issue #1430 Notes for Reviewer 参照）。

### SESSION_MANIFEST_LEGACY_SCAN_V1（scan 失敗診断、AC12）

fs アクセスエラー（`EACCES` 等）は「旧 layout なし」と同一視せず、別スキーマの診断行で区別する。この行も best-effort であり hook を fail-close させない。

```yaml
SESSION_MANIFEST_LEGACY_SCAN_V1:
  status: scan_failed
  legacy_kind: debounce_events_dir | debounce_worker_lock | producer_lock_tmp
  path: <string>     # scan に失敗した対象パス（repo-relative）
  reason: <string>   # Node.js の fs エラーコード（例: EACCES）。取得できない場合は unknown_error
  detected_at: <ISO 8601>
```

`.lock-*` / `.tmp-*` / `events` / `worker.lock` は、ディレクトリ/symlink がそのパス名を占有していても legacy 判定に含めない（`lstatSync`/`readdirSync(..., {withFileTypes: true})` によるファイル種別の区別。AC12）。

### 出力順序（AC8）

`SESSION_MANIFEST_LEGACY_STATE_V1` / `SESSION_MANIFEST_LEGACY_SCAN_V1` は、両スクリプトが同一呼び出しで出力する他の既存 result 行（例: `SESSION_MANIFEST_DEBOUNCE_RESULT_V1`）よりも **必ず後** に出力される（内部的にバッファし、`finally` で呼び出し終盤にまとめて flush する）。`session_manifest_coordinator.sh` の `sanitize_stderr()` は各ステップの stderr の先頭行のみを `detail=` に採用するため、この順序により既存の result 行が legacy diagnostic に隠されることを防ぐ。

### 重複抑制（AC13）

同一 `legacy_kind` + 同一 `paths` セットの診断は、現行 layout の runtime subtree（`artifacts/session-manifest-runtime/legacy-state-markers/`）配下の one-shot marker ファイルにより、2 回目以降の起動では再発火しない。marker の書き込み自体が失敗した場合は fail-open（抑制せず診断を出力する）。

### producer 失敗時の queued event 保持（AC10）

`session_manifest_debounce.mjs` の flush loop は、producer 呼び出しが失敗（非 0 exit または timeout）した場合、対象の queued event ファイルを削除しない。次回の flush 試行で再試行される。

---

## 12a. pr_review.publish の位置づけ（Issue #1536）

`scripts/agent-guards/controlled_skill_mutation_exec.py` の `CONTROLLED_SKILL_MUTATION_COMMAND_POLICY` に `pr_review.publish` command id を追加した（Option C: controlled review publisher）。`local_main_branch_guard.sh` / `worktree_scope_guard.sh` は既存の `REASON_CONTROLLED_SKILL_MUTATION_EXECUTOR` 判定（`is_controlled_skill_mutation_exec_command()`、`ALL_COMMAND_IDS` メンバーシップに基づく exact command class allow）をそのまま適用するため、この2フック自体の変更は不要だった（`termination_report.publish` / `issue_body.update` / `issue_comment.publish` / `contract_snapshot.publish` と同一の authorization lane）。

`pr_review.publish` は `pr-reviewer` SubAgent（read-only、`gh pr review` / worktree bootstrap を一切行わない）の判定結果（`PR_REVIEW_PUBLISH_REQUEST_V1`）を受け取り、`event: COMMENT` 固定・`commit_id` 拘束・idempotency marker 付きで GitHub PR review を投稿する。生の `gh pr review` 呼び出しは `local_main_branch_guard.sh` で引き続き `gh_mutation_denied` として block される（本 Issue で変更しない）。

Codex 側 `.codex/rules/default.rules` は `gh pr review` を引き続き明示的に forbidden とし（`gh` サブコマンド prefix rule）、かつ `controlled_skill_mutation_exec.py` 自体への allow エントリを持たないため、本変更は Claude-only のまま split-brain を生じない（確認のみ、rule 変更なし）。

**Issue #1633 更新（Codex/Claude parity 解消）**: 上記の「Codex 側は allow エントリを持たない」記述は Issue #1633 時点でもはや正確ではない。`.codex/rules/default.rules` に `uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py` の exact prefix allow エントリを追加し、`.claude/settings.json` の `Bash(uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py *)` と同じ 共有 authorization lane に Codex 側も明示的に乗るようにした（`codex execpolicy check` で `decision: allow` を確認済み）。ランタイム hook 層（`is_controlled_skill_mutation_exec_command()`）は元々 Claude/Codex 共通実装であり split-brain は生じていなかったが、静的 `codex execpolicy` layer には対応する allow ルールが欠けていたため、本 Issue でその欠落を埋めた。

## 12b. pr_review.publish の追加ハードニング（Issue #1539 fix_delta）

OWNER レビュー（PR #1539、squne121）で以下の構造的欠陥が指摘され、修正した:

- **trusted bridge の欠如（Blocker 1）**: `pr-reviewer` SubAgent は `Edit`/`Write`/`MultiEdit` を持たず Bash 経由のファイル書き込みも禁止のため、当初の SKILL 文面が要求していた「`PR_REVIEW_PUBLISH_REQUEST_V1` を自ら組み立てて `--input-file` に渡す」経路は実際には SubAgent に実行不能だった。修正: `controlled_skill_mutation_exec.py` に render mode（`--render-body-file` / `--verdict` / `--reviewed-head-sha` / `--expected-head-sha` / `--merge-ready`）を追加。trusted orchestrator（Write ツールを持つ control-plane）が verdict 本文テキストのみを artifact パスへ書き込み、executor 自身が `body_sha256` / `idempotency_key` を再計算し `producer_role` / `event` を自ら固定する（入力からは受け取らない）。
- **host/environment binding の欠如（Blocker 2）**: `_verify_git_remote_origin()` が owner/repo の正規表現抽出のみで host/scheme を無視していたため、`https://attacker.example/<owner>/<repo>.git` 等が trusted と誤認され得た。また `GH_HOST`/`GH_REPO`/`GH_CONFIG_DIR`/`GH_DEBUG`/`DEBUG` が sanitize されず、`gh` subprocess へ `env=` が渡っていなかった。修正: `urlsplit` による構造的 host/scheme/port/userinfo 検査（github.com の HTTPS/SSH canonical form のみ許可）と、全 `gh` subprocess への sanitized env（上記5キー除去）+ `--hostname github.com` 明示。
- **idempotent retry が postcondition を迂回（Blocker 3）**: 既存 marker が1件見つかった retry 経路が `state`/`commit_id` のみ確認して即座に成功を返し、body hash・marker 一意性/位置・現在 PR head・author identity・tracked changes を再検証していなかった。修正: retry も fresh-post と同一の共通 postcondition validator（`_validate_pr_review_postcondition`）を通す。marker 検索も substring match から「末尾に厳密一致」判定に変更。
- **TOCTOU（High 1）**: commit_id 拘束は「A に結び付ける」保証であって「POST 時点でも A が current head」の atomic precondition ではない。修正: POST/readback 後に current head を再取得し、移動していれば `published_but_stale` として fail-closed（review は残るが成功報告はしない）。
- **producer provenance の自己申告（High 2）**: `producer_role` が入力 JSON の自己申告フィールドで、schema も exact-key ではなかった。修正: render mode では `producer_role`/`event` を executor が自ら固定（入力に存在しても無視ではなく、そもそも render mode の入力スキーマに含まれない）。`--input-file` 経路も exact-key schema + body size bound を追加。

AC8（実 PreToolUse hook chain）テストは `secret_boundary_guard` / `local_main_branch_guard` / `worktree_scope_guard` / `guard-japanese-prose` / `rtk_boundary_shadow_guard` / `ci_test_performance_advisory` / `root_temporary_residue_advisory` の 7 hook すべてを `.claude/settings.json` 記載順に実行し、aggregate decision（deny/ask が無いこと）を検証する形に拡張した（`.claude/hooks/tests/hookchain_harness.py`）。

## 12c. scope_rollup.run の位置づけ（Issue #1547）

`scope_rollup.run` は canonical root（worktree 作成前）context からのみ許可される、独立した exact command class である。`local_main_branch_guard.py` / `skill_runtime_command_policy.py` に登録されており、既存の `preflight.run`（`skill_runtime_exec.py` 経由）とは別レーン -- `skill_runtime_exec.py` は `run_refinement_preflight.py` 呼び出しに固定されているため、`scope_rollup.run` は代わりに新規 `scripts/agent-guards/run_scope_rollup_preflight.py` を直接 exact-match する。

- **command**: `uv run python3 scripts/agent-guards/run_scope_rollup_preflight.py --issue-number <N> --repo <owner/repo> --invocation-id <id> --requested-at <ISO8601>`（12 token 完全一致。`--invocation-id` / `--requested-at` は PR #1560 fix_delta で追加 -- caller (scope-rollup-runner) が生成した値をそのまま渡し、executor は独自に UUID / timestamp を生成しない。`--flag=value` 形・追加 flag・wrapper・shell metacharacter はすべて `unparseable_branch_mutation` で fail-closed）
- **destination**: caller は出力先を指定しない。GitHub 生入力（`issues.json` / `prs.json`）は executor-owned private invocation directory（`tempfile.mkdtemp()`, mode `0700`）内に `O_CREAT | O_EXCL | O_NOFOLLOW` + mode `0600` で `.part` ファイルを排他生成し、同一 directory 内 `os.link()` ベースの排他 finalize（`os.rename` は使わない -- 既存 destination を静かに置換しうるため）+ flush + `fsync` で確定する。planner の実行結果（`plan_result.json` 相当）はファイル化せず、bounded streaming で in-memory capture した stdout を直接 JSON parse し、verifier も in-process の `verify_payload()` で検証する（PR #1560 P0-2）。success / failure / timeout の全経路で private directory を `finally` で cleanup し、cleanup 失敗自体も `cleanup_failed` として transaction failure に変換する（P1-3）。`SCOPE_ROLLUP_RUN_RESULT_V1` JSON のみを stdout へ返す（永続 artifact は残さない）
- **safety boundary**: canonical root cwd・default branch・trusted repo（`squne121/loop-protocol`）を実行前後で拘束。`gh` は固定 trusted search dirs（`/usr/bin`, `/usr/local/bin`, `/opt/homebrew/bin`, `/bin`）から解決し、realpath・owner 権限・ancestor directory の world/group-writable 状態（sticky bit なし）まで検証する（PATH shadowing 対策、P1-1）。`GH_HOST` / `GH_REPO` / `GH_FORCE_TTY` / `GH_PAGER` / `PAGER` / `GH_CONFIG_DIR` / `GH_DEBUG` / `GH_PATH` / `GH_PROMPT_DISABLED` は sanitize（呼び出し元環境から継承しない）
- raw `gh issue view / list` / `gh pr list` の shell redirect（`gh ... > /tmp/...`）は引き続き `local_main_branch_guard` の compound/metachar 判定で block される。`scope_rollup.run` の exact invocation だけが許可レーンであり、redirect の部分許可は導入していない
- pagination は `--limit <MAX_ITEMS_PER_KIND + 1>` を要求し、実際の件数が上限を超えた場合は `truncated: true` として transaction 全体を fail-close する（`hasNextPage` を直接確認できないため、上限超過を「完走を証明できない」と同一視する設計）。加えて `gh api graphql` で server-side `totalCount` を独立取得し、フェッチ件数と突き合わせる（P1-2）。マニフェストの `page_count` は `ceil(item_count / 100)` の実測値を記録する
- PR の `files` connection は `files(first: 100)` に固定されるため、`changedFiles` がフェッチ済み `files` 件数を上回る PR については `gh api graphql` でその PR の files connection のみを `hasNextPage` が false になるまで cursor pagination し、完走できなければ `pr_files_pagination_incomplete` として fail-close する（101 件目以降のファイルで overlap を見逃す false negative の防止、P0-3）
- linked issue worktree context では、`is_local_root_context()` の context routing が classifier より先に評価されるため、`scope_rollup.run` classifier 自体には到達しない（`linked_issue_worktree_context` / `not_local_root` が先に allow を返す）
- **既知の follow-up（本 fix_delta ではスコープ外）**: `skill_runtime_exec.py` を単一 registry SSOT として `scope_rollup.run` を統合すること（P1-5）。この executor は元々 `skill_runtime_exec.py` の Allowed Paths 外として独立レーンに設計されており（上記参照）、統合には `skill_runtime_exec.py`（直近で 500 行超の別 PR 変更が入ったばかり）への大規模な変更と Allowed Paths のさらなる拡張が必要になるため、本 PR のスコープには含めない。

## 12d. verified fast-forward merge lane（検証済み fast-forward merge レーン, Issue #1589）

`scripts/agent-guards/git_mutation_command_policy.py` の `classify_rtk_git_mutation` は、`ALLOWED_RTK_GIT_SUBCOMMANDS` に `merge` を追加し、exact `rtk git merge --ff-only <40-hex-target-sha>` を独立の command class（`COMMAND_CLASS_RTK_GIT_MERGE_FF_ONLY` = `rtk_git_merge_ff_only`）として認識する。linked issue worktree の active branch を live-verified remote head へ安全に fast-forward するための lane であり、`execute_initial_branch_create_transaction`（Issue #1449 / PR #1479）と同じ「単一 trusted execution boundary が verify・probe・実際の mutation・postcondition-readback を全て内包し、呼び出し元の raw command は常に deny として返す」設計パターンを踏襲する。

### trusted transaction の内容

`execute_verified_ff_merge_transaction(cwd, target_sha)` は、以下を **全て** 満たす場合のみ `git merge --ff-only <target_sha>`（argv list、`shell=False`）を実行する。いずれか一つでも欠ければ merge は一切実行されない（`git merge` 呼び出し前に fail-closed で deny）。

1. `target_sha` が exact lowercase 40-hex SHA である。
2. attached HEAD が非 detached であり、`DEFAULT_BRANCH_NAMES`（main/master/trunk + `origin/HEAD` 解決値 + `LOOP_DEFAULT_BRANCH`）に含まれない、かつ `_ISSUE_WORKTREE_BRANCH_RE`（`worktree-issue-<N>-<slug>` の canonical 命名形状、`docs/dev/workflow.md#Worktree 配置規約` 準拠）に一致する。
3. `git status --porcelain=v1 --ignore-submodules=none` が空（tracked/untracked/submodule すべて clean）であり、`MERGE_HEAD` / `CHERRY_PICK_HEAD` / `REVERT_HEAD` / `BISECT_LOG` / `rebase-merge` / `rebase-apply` のいずれも存在しない(進行中の git operation なし)。
4. `origin` の全 push URL が `LOOP_CANONICAL_REPO_URL_PATTERN`（既定は `squne121/loop-protocol` の GitHub canonical URL）に一致する。
5. 対象 active branch に対する live `git ls-remote --refs --exit-code origin refs/heads/<active-branch>` の結果が `target_sha` と完全一致する（`absent` / `probe_error` / mismatch はいずれも deny — `classify_remote_branch_state` の 3-state 語彙をそのまま使用）。
6. `target_sha` が local commit object であり（`git cat-file -t`）、local HEAD がその ancestor である（`git merge-base --is-ancestor`）。
7. merge 実行の直前に branch/HEAD が変化していないことを再確認する（verify-to-merge race window を狭める。ゼロにはしない）。

merge 実行後は、active branch 不変・`HEAD == target_sha`・`git status` clean・operation residue なしを無条件に確認する。`post-merge` hook が working tree を変更した場合（clean でなくなる）は `postcondition_violation` として **成功扱いにしない**。`git merge --ff-only` 自体が非ゼロ終了した場合は `merge_rejected_non_fast_forward` として区別する。

### exact allow 条件（旧設計。Issue #1609 fix_delta により下記のとおり変更済み）

`classify_rtk_git_mutation` は、上記 transaction の実行結果に関わらず（成功・拒否・precondition failure のいずれでも）常に `status: "deny"` を返す。transaction の outcome（`verified_ff_merge_completed` / `merge_ff_only_rejected` / `postcondition_check_failed` / 各種 precondition deny 理由）は `reason_code` にそのまま格納され、呼び出し元の raw `rtk git merge` コマンドが別途再実行されることはない。shape が exact 2-token `--ff-only <40-hex-sha>` でない場合（短縮/非hex SHA、flag 順序変更、追加 option、`--no-ff`、bare branch name 等）は transaction を呼び出す前に `merge_shape_requires_exact_ff_only_sha` で deny する。

### Codex allow rule（Codex 許可ルール。旧設計。Issue #1609 fix_delta により下記のとおり変更済み）

`.codex/rules/default.rules` は exact `rtk git merge --ff-only` prefix のみを、既存の generic `rtk git merge`（`--ff-only` を伴わない形状は引き続き prompt）バケットより前に allow として追加する。Codex 側での不要な人間確認を避けるためであり、実際の安全性強制は引き続き `git_mutation_command_policy.py` の trusted transaction が担う。

### deny 境界

- root checkout（default branch）・非 issue-worktree 命名の branch・detached HEAD はいずれも `git merge` 呼び出し前に deny される（`local_main_branch_guard.py` / Codex 両 flavor で回帰確認済み）。
- `git reset --hard` / `rtk git push --force ...` 等の既存 destructive command deny は本 lane 追加後も維持される。
- raw `git merge`（`rtk` prefix なし）は本 policy の対象外（`classify_rtk_git_mutation` は `no_match` を返し、他レイヤーの一般的な mutating command 判定に委ねる）。
- `.claude/worktrees/issue-1589-linked-issue-worktree-verified-fast-forw` の worktree_scope_guard 統合は、既存の `classify_rtk_git_mutation` 汎用ディスパッチ（resolved active Issue の clean linked worktree にのみ command class を通す既存経路）をそのまま利用し、本 Issue で `worktree_scope_guard.py` 自体の分岐追加は不要だった（既存 230 テスト回帰確認済み）。



### Issue #1609 fix_delta（P0 / P1 / P2 追加修正）

OWNER の敵対的レビュー（PR #1609）により、上記の初回実装には authorization より先に merge が実行される P0 Blocker と、複数の P1 Blocker が指摘され、以下のとおり修正した。

- P0 Blocker: `classify_rtk_git_mutation` の merge レーン（`_classify_rtk_git_merge`）は、副作用のない PURE shape classifier に戻した（exact shape なら `status: allow` を `target_sha` 付きで返すのみで、`execute_verified_ff_merge_transaction` は一切呼び出さない）。実際の transaction 実行は `.claude/hooks/worktree_scope_guard.py` の `_decide_rtk_git_merge` が、active Issue 解決済み・matching worktree が一意・`cwd == expected worktree`・当該 worktree が linked worktree（root checkout でない）の全てを認可した後にのみ行う。
- P1 Blocker: `execute_verified_ff_merge_transaction` は `expected_worktree_realpath` / `active_issue_number` を必須キーワード引数として受け取り、cwd がその realpath と一致すること、当該 worktree が linked worktree であること（`git rev-parse --git-dir` が `--git-common-dir` と異なること）、branch 名に埋め込まれた Issue 番号が `active_issue_number` と一致することを、呼び出し元の申告を信用せず自ら再検証する。
- P1 Blocker: operation residue（`MERGE_HEAD` / `CHERRY_PICK_HEAD` / `REVERT_HEAD` / `BISECT_LOG` / `rebase-merge` / `rebase-apply`）は `git rev-parse --git-path` で対象名ごとに per-worktree の GIT_DIR 配下を解決してから検査する（旧実装の `--git-common-dir` 解決は linked worktree では誤ったディレクトリを見ていた）。
- P1 Blocker: canonical remote 検証は `git remote get-url origin`（push url でなく fetch url）を単一解決し、その同じ URL を `classify_remote_branch_state` の live probe にも使う。push url だけ canonical にして fetch url を別リポジトリに向ける迂回を防ぐ。
- P1 Blocker: `git merge` 実行中の `OSError` / `TimeoutExpired` は無条件 deny に畳み込まず、`execution_not_started`（spawn 前の OSError）・`transport_error_but_merged_and_verified`・`transport_error_no_merge_observed`・`transport_error_state_ambiguous` の 4 状態に分類する。timeout 後も無条件で postcondition readback（branch/HEAD/clean/operation residue）を行ってから分類する。
- P2: `_SHA_RE` 照合は小文字化前の raw 入力に対して行い、uppercase SHA は shape 不一致として deny する。
- Codex allow rule: exact `rtk git merge --ff-only` prefix を allow にする design は、Codex execpolicy がマッチした全 rule のうち最も厳しい decision（forbidden 優先 prompt 優先 allow の順）を採用するため、既存の generic `rtk git merge` prompt rule にも一致してしまい exact shape が prompt のまま解決されるバグがあった。現在は専用 executor `scripts/agent-ops/verified_ff_merge_exec.py` の exact invocation shape（`uv run --locked --no-sync python3 scripts/agent-ops/verified_ff_merge_exec.py --target-sha SHA`）のみを allow とし、`rtk git merge`（`--ff-only` の有無を問わず）は引き続き prompt のままにしている。この executor は Codex 環境向けの独立した authorization（`LOOP_ISSUE_NUMBER` からの active Issue 解決・cwd の branch 形状照合）を行ってから `execute_verified_ff_merge_transaction` を呼び出す。


---

---

## 12e. default-branch fast-forward sync lane（既定ブランチ fast-forward 同期レーン, Issue #1603）

`scripts/agent-guards/git_mutation_command_policy.py` の `classify_rtk_git_mutation` は、12d 節の `COMMAND_CLASS_RTK_GIT_MERGE_FF_ONLY`（Issue #1589）とは別の command class `COMMAND_CLASS_RTK_GIT_MERGE_DEFAULT_BRANCH_FF_ONLY`（`rtk_git_merge_default_branch_ff_only`）として、exact `rtk git merge --ff-only origin/<candidate>` を認識する。target identity が異なる: #1589 は active branch 自身の live remote head（呼び出し元が渡す 40-hex SHA）を検証するのに対し、本レーンは canonical default branch の LIVE identity を `git ls-remote --symref` で検証し、object-only fetch してから merge する。同一の trusted execution boundary パターン（verify・probe・実際の mutation・postcondition-readback を全て内包し、呼び出し元の raw command は常に deny として返す）を踏襲する。

### trusted transaction の内容

`execute_verified_default_branch_ff_merge_transaction(cwd, candidate_default_branch)` は、以下を **全て** 満たす場合のみ `git merge --ff-only <live-oid>`（argv list、`shell=False`）を実行する。いずれか一つでも欠ければ merge は一切実行されない。

1. `cwd` が `expected_worktree_realpath` と同一 realpath であり、LINKED worktree（root checkout でない）である。
2. `candidate_default_branch` が `git check-ref-format --branch` と形状 regex の両方を満たす。
3. attached HEAD が `DEFAULT_BRANCH_NAMES` に含まれない、かつ `_ISSUE_WORKTREE_BRANCH_RE`（`worktree-issue-<N>-<slug>`）に一致し、`<N>` が `active_issue_number` と一致する。
4. worktree/index/submodule が clean であり、進行中の git operation がない。
5. `origin` の解決済み FETCH url が canonical repository identity に一致する（同じ url を以下の probe / fetch に使う）。
6. live 識別 probe #1: `git ls-remote --symref --exit-code <fetch-url> HEAD refs/heads/<candidate>` を実行し、`HEAD` が exact `ref: refs/heads/<candidate>` を返し、`HEAD` と `refs/heads/<candidate>` の OID が一致する一意な小文字 40-hex SHA であることを確認する（`origin/HEAD` のローカルキャッシュや `LOOP_DEFAULT_BRANCH` は認可根拠にしない）。
7. object-only, destination-less な `git fetch <fetch-url> <live-oid>` を実行する（ローカル ref は一切更新しない）。
8. live 識別 probe #2（#6 の再実行）で default branch identity と OID が fetch 前後で不変であることを確認する。
9. fetch した OID が local commit object であり、local HEAD がその ancestor である。
10. merge 実行の直前に branch/HEAD が変化していないことを再確認する。

merge 実行後は、active branch 不変・`HEAD == live-oid`・`git status` clean・operation residue なしを無条件に確認する。`postcondition_violation` / `merge_rejected_non_fast_forward` / timeout 分類（`execution_not_started` / `transport_error_but_merged_and_verified` / `transport_error_no_merge_observed` / `transport_error_state_ambiguous`）は 12d 節の `execute_verified_ff_merge_transaction` と同じ語彙・分類方針を踏襲する。

### classify_rtk_git_mutation の振り分け（routing）

`classify_rtk_git_mutation` の `merge` 分岐は、shape が exact 2-token `--ff-only origin/<branch-name-shaped-token>` の場合のみ `_classify_rtk_git_merge_default_branch`（PURE shape classifier、副作用なし）へルーティングし、それ以外（40-hex SHA を含む既存の 12d 節の形状等）は従来どおり `_classify_rtk_git_merge` へルーティングする。トランザクションの実行認可は `.claude/hooks/worktree_scope_guard.py` の `_decide_rtk_git_merge_default_branch`（`_decide_rtk_git_merge` と同じ authorize-before-execute パターン）が、active Issue 解決済み・matching worktree が一意・`cwd == expected worktree`・当該 worktree が linked worktree であることを認可した後にのみ行う。

### Codex allow rule（Codex 許可ルール）

`.codex/rules/default.rules` は、専用 executor `scripts/agent-ops/verified_default_branch_ff_merge_exec.py` の exact invocation shape（`uv run --locked --no-sync python3 scripts/agent-ops/verified_default_branch_ff_merge_exec.py --candidate-branch NAME`）のみを allow とし、`rtk git merge --ff-only origin/<candidate>`（12d 節と同じ execpolicy most-severe-decision-wins の理由により）は引き続き既存の generic `rtk git merge` prompt rule に委ねる。この executor は `LOOP_ISSUE_NUMBER` からの active Issue 解決・cwd の branch 形状照合という独立した authorization を行ってから `execute_verified_default_branch_ff_merge_transaction` を呼び出す。

### deny 境界

- root checkout（default branch）・非 issue-worktree 命名の branch・detached HEAD はいずれも `git merge` 呼び出し前に deny される。
- default branch 以外の ref、short SHA、`--flag=value`、flag 順序変更、追加 option、wrapper、raw git、dirty state、live/fetch mismatch、non-fast-forward、probe/fetch error、検証後の branch/HEAD change はいずれも merge 前に fail-closed で拒否される。
- 人間による一時回避は、人間が clean state と live remote SHA を確認した上で linked worktree から通常の `git merge --ff-only origin/main` を行うことに限定される。guard/hook の無効化、force push、reset、stash、root checkout 操作は回避策に含めない。
- raw `git merge`（`rtk` prefix なし）は本 policy の対象外（`classify_rtk_git_mutation` は `no_match` を返す）。
- Issue #1589 の active branch remote-head lane（12d 節）とは独立した command class・独立したトランザクションであり、一方の contract を他方の contract で置換しない（#1603 の Scope Collision Preflight で確認済み）。

### Issue #1603 iteration-2 追加修正（OWNER 敵対的レビュー、permission/sandbox boundary 観点）

OWNER の敵対的レビュー（PR #1634 iteration 2）により、初回実装には以下の permission/sandbox boundary 上の指摘があり、以下のとおり修正した。

- **P1-1（Codex executor の cwd binding）**: `verified_default_branch_ff_merge_exec.py` は、`cwd` を自明に `expected_worktree_realpath` として渡す恒真条件ではなく、リポジトリ共有 SSOT `scripts/agent-ops/worktree_catalog.py` の `select_issue_worktrees()` を使って、`git worktree list --porcelain -z` の実 catalog から対象 Issue に一致する linked worktree が一件だけであること・その realpath が `cwd` と一致することを検証する。0 件（`zero_matching_worktrees`）・複数件（`multiple_matching_worktrees`）・root checkout（`expected_worktree_is_root_checkout`）・cwd 不一致（`cwd_not_expected_worktree`）はそれぞれ別 reason code で拒否する。
- **P1-2（trusted Git execution context）**: `_establish_trusted_git_context()` が、`GIT_DIR` / `GIT_WORK_TREE` / `GIT_INDEX_FILE` / `GIT_OBJECT_DIRECTORY` / `GIT_ALTERNATE_OBJECT_DIRECTORIES` / `GIT_CONFIG_COUNT` / `GIT_CONFIG_KEY_*` / `GIT_CONFIG_VALUE_*` を取り除いた sanitized 環境と、一度だけ解決した絶対パスの `git` バイナリを使い、`--show-toplevel` / `--git-dir` / `--git-common-dir` を再解決する。`refs/replace/*` が存在する場合、または legacy `info/grafts` ファイルが非空の場合は fail-closed で deny する（`replace_refs_present` / `legacy_grafts_present`）。この trusted context の確立は、branch/clean/operation-state チェックより **前** に行う（replace-ref はそれらの読み取り結果を透過的に書き換えうるため）。live probe（`git ls-remote`）・fetch・ancestry（`git merge-base --is-ancestor`）・local-commit-object 確認（`git cat-file -t`）・merge 実行は、全てこの trusted context 経由で実行され、`--no-replace-objects` を付与する。
- **P1-3（object-only fetch の副作用境界）**: fetch は `git --no-replace-objects fetch --no-tags --no-recurse-submodules --no-write-fetch-head --no-auto-maintenance --no-write-commit-graph <fetch-url> <live-oid>` に変更し、FETCH_HEAD 書き込み・submodule recursion・auto maintenance・commit-graph 書き込みを明示的に無効化する。fetch の失敗/timeout は `fetch_execution_not_started`（spawn 前の OSError）・`fetch_failed_object_not_observed` / `fetch_failed_object_observed`（clean non-zero exit、fetch 対象 object のローカル存在有無で区別）・`fetch_state_ambiguous`（timeout、exit 意味論を信用できないため常に ambiguous 扱い）に分類する。object database / pack file への書き込みそのものは fetch の不可避な副作用であり、本 transaction が制御する範囲外である（PR の Safety Claim Matrix `Not controlled` 列に明記）。
- **P1-4（非零 merge exit の postcondition 分類）**: `git merge --ff-only` が非零終了した場合、無条件に `merge_rejected_non_fast_forward` とせず、実際の branch/HEAD/clean/operation state を読み直してから分類する。読み直した状態が live target OID と一致すれば `execution_error_but_merged_and_verified`（merge は実際には成功していた）、元の local head のまま unchanged であれば通常の reject（`merge_execution_error_no_merge_observed`）、どちらにも一致しなければ `execution_error_state_ambiguous`（merge 中に別プロセスが branch/HEAD を動かした可能性等）に分類する。
- **P1-High-5（per-worktree execution lock）**: `_WorktreeExecutionLock`（`fcntl.flock`、exclusive・non-blocking・bounded retry）が、trusted context 確立から postcondition readback までの transaction 全区間を、`git rev-parse --git-path loop-default-branch-ff.lock` で解決した worktree 専用 lock file 上で保持する。同一 worktree に対する同時実行は `worktree_execution_lock_contended` で拒否される（`git worktree lock` は prune/move 用であり transaction mutex ではないため別の仕組みとして実装した）。
- **P2-6（正当な hierarchical default branch 名の許可）**: 候補 branch 名の shape チェックは `^(?!-)(?!.*\.\.)[!-~]+$`（非空・空白/制御文字なし・先頭 `-` 不可・`..` を含まない）という permissive な pre-filter に変更し、`release/2026` のような `/` を含む正当な branch や 64 文字超の branch を拒否しない。文法上の唯一の authority は引き続き `git check-ref-format --branch`（trusted transaction 内で実行）であり、classifier 側のこの pre-filter は shape の粗い事前チェックに過ぎない。
- **P2-7（Codex prefix rule の限界と executor 側 exact 検証）**: `.codex/rules/default.rules` の `verified_default_branch_ff_merge_exec.py` allow rule は PREFIX rule であり、Codex execpolicy には exact-shape / no-trailing-token の primitive がないため、duplicate `--candidate-branch` flag・`--flag=value` form・追加 positional・trailing token も prefix 一致自体は成立しうる。実際の enforcement は executor 自身の `_validate_exact_invocation_argv()`（exact `["--candidate-branch", <value>]` の 2-token 検証、argparse 実行前に行う）であり、rule の justification もこの分担を明記するよう修正した。
- **P2-8（live probe の OID 厳密性）**: `_probe_default_branch_identity()` は、`ls-remote --symref` の各行の OID を lowercase に正規化する前に、raw 値のまま `_SHA_RE`（lowercase 40-hex のみ）で検証する（uppercase は REJECT、silent normalize しない）。また `HEAD` / `refs/heads/<candidate>` それぞれについて OID 行が厳密に一件であることを要求し、同一 ref に対する複数行（重複行）は `malformed_output` として fail-closed にする。

### 実行環境ガード（本節の対象外）

`worktree_scope_guard.py`（Claude Code hook）は、既存の `resolve_expected_worktree()`（`scripts/agent-ops/worktree_catalog.py` SSOT を使用）で active Issue worktree を authorize してから `execute_verified_default_branch_ff_merge_transaction` を呼び出しており、本節の P1-1 は Codex executor 独自の authorization 経路（Claude hook を経由しない）にのみ適用される修正である。

## 12. publish lane authorization trust root（歴史的経緯・historical note）

Issue #1454（Phase A, PR #1457 MERGED）で `scripts/trust-root` 一式（`trusted_hook_launcher.py` / `manifest_schema.py` / `install_trust_root.sh`）が external trust root として導入されたが、これを `.codex/hooks.json` へ実際に配線する Issue #1450（Phase B）と、追加ハードニングを扱う Issue #1468 がいずれも個人開発の脅威モデルに対して過剰と判断され not planned でクローズされた。配線先を失った `scripts/trust-root` は不使用コードとなったため、Issue #1469 でコード一式・CI 登録・本節の bootstrap/rotation/managed hook registration 手順を削除した。現行の publish lane 保護は Issue #1408（PR #1442 MERGED、Issue branch 限定 push 許可・force/tag/delete/mirror 拒否）と main branch protection（Issue #360）のみで構成される。
