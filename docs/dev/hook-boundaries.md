---
doc_id: hook-boundaries
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

---

## 3. agent 判断表（Claude Code）

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

## 4. agent 判断表（Codex CLI）

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

- `session_manifest_coordinator.sh`（Stop / SubagentStop）
- `session_manifest_debounce.mjs`（PostToolUse front gate）
- `save_loop_state_before_compaction.sh`（PreCompact）
- `rtk_boundary_shadow_guard.sh`（PreToolUse）

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

## 7. mode_dependent: guard-japanese-prose.sh

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
