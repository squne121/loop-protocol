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
| `push_refspec_requires_active_branch` | active branch と一致する refspec だけを使う | `rtk git push origin HEAD:refs/heads/<active-branch>` / `git branch --show-current` |
| `issue_context_required` | issue 未解決の root / unrelated cwd では mutation しない | `git worktree list` / `git branch --show-current` |
| `target_dir_outside_worktree` | active issue worktree 配下へ戻る | `git status --short` / `git branch --show-current` |
| `no_matching_worktree` / `ambiguous_worktree` | worktree catalog を 1 件に特定する | `git worktree list` / `git branch --show-current` |
| `rtk_unknown_inner` | wrapper を剥がさず direct な `rtk git add/commit/push` へ揃える | `rtk git add <allowed-path-file>` / `git branch --show-current` |

### 運用ガイド

- `HOOK_COMMAND_REPAIR_HINT_V1` は direct `rtk git ...` の exact / bounded 形だけを示し、`bash -lc`、`env FOO=... rtk git ...`、`command rtk git ...` の wrapper 展開は suggestion に使わない。
- `suggested_command` は authorization を付与しない。rules / hooks / post-run verifier が独立に reject できる。
- `allowed_paths_missing_for_git_mutation` は fail-closed 理由であり、Issue contract の Allowed Paths binding が runtime に見えていない状態を示す。

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
