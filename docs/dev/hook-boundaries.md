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
    matcher: "Bash|Read|Write|Edit|Grep|Glob"
    command: "${CLAUDE_PROJECT_DIR}/.claude/hooks/secret_boundary_guard.sh"
    args: []
    timeout: 10
    classification: blocker
    fail_policy: fail_closed
    exit_codes:
      0: allow
      2: block
    stdout_contract: silent_on_allow
    stderr_contract: minimal_structural_message_on_block
    agent_action:
      on_nonzero: stop_tool_call
      on_zero: proceed
    notes: >
      secret / credential 境界フック。
      jq 不在など自身がエラーになった場合も fail-closed（exit 2）。
      このフックは fail-closed のまま維持し、best-effort 化してはならない（AC3）。

  - handler_id: worktree_scope_guard
    event: PreToolUse
    matcher: "Bash|Write|Edit|MultiEdit"
    command: "${CLAUDE_PROJECT_DIR}/.claude/hooks/worktree_scope_guard.sh"
    args: []
    timeout: 10
    classification: blocker
    fail_policy: fail_closed
    exit_codes:
      0: allow
      2: block
    stdout_contract: silent_on_allow
    stderr_contract: minimal_structural_message_on_block
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
    exit_codes:
      0: allow
      2: block_only_in_enforce_mode
    stdout_contract: silent
    stderr_contract: jsonl_shadow_log_or_block_reason
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
    exit_codes:
      0: always
    stdout_contract: silent
    stderr_contract: jsonl_shadow_log_only
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
    timeout: 180
    classification: telemetry
    fail_policy: fail_open
    exit_codes:
      0: always
    stdout_contract: silent
    stderr_contract: diagnostic_on_failure_max_10_lines
    agent_action:
      on_any: proceed
    notes: >
      Stop イベントで session manifest を生成する。
      guard（session_recording_policy_guard.sh）と
      producer（generate_session_manifest_from_hook.mjs）を逐次実行する coordinator。
      guard failure / producer failure いずれも exit 0（fail-open）。
      task blocker にしてはならない（AC2）。
      hook failure は diagnostic artifact 欠落として記録・報告される（AC10）。

  - handler_id: session_manifest_coordinator
    event: SubagentStop
    matcher: null
    command: "${CLAUDE_PROJECT_DIR}/.claude/hooks/session_manifest_coordinator.sh"
    args: []
    timeout: 180
    classification: telemetry
    fail_policy: fail_open
    exit_codes:
      0: always
    stdout_contract: silent
    stderr_contract: diagnostic_on_failure_max_10_lines
    agent_action:
      on_any: proceed
    notes: >
      SubagentStop イベントでも session_manifest_coordinator と同一スクリプトを使用。
      task blocker にしてはならない（AC2）。
      hook failure は diagnostic artifact 欠落として記録・報告される（AC10）。

  - handler_id: generate_session_manifest_from_hook
    event: PostToolUse
    matcher: "Bash|Edit|Write"
    command: "node"
    args:
      - "${CLAUDE_PROJECT_DIR}/.claude/hooks/generate_session_manifest_from_hook.mjs"
    timeout: 60
    classification: telemetry
    fail_policy: fail_open
    exit_codes:
      0: producer_success_or_skip
      nonzero: producer_failure_ignored
    stdout_contract: silent
    stderr_contract: diagnostic_on_failure
    agent_action:
      on_any: proceed
    notes: >
      PostToolUse hook（Bash|Edit|Write）で session manifest を更新する。
      command が "node" で、実際のスクリプトは args[0] に格納される（AC6）。
      producer failure は exit 0 扱いで agent を block しない。
      task blocker にしてはならない（AC2）。
      hook failure は diagnostic artifact 欠落として記録・報告される（AC10）。

  - handler_id: save_loop_state_before_compaction
    event: PreCompact
    matcher: null
    command: "${CLAUDE_PROJECT_DIR}/.claude/hooks/save_loop_state_before_compaction.sh"
    args: []
    timeout: 30
    classification: telemetry
    fail_policy: fail_open
    exit_codes:
      0: always
    stdout_contract: always_empty
    stderr_contract: diagnostic_on_failure_max_10_lines
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
| `worktree_scope_guard.sh` | blocker | **操作を停止（block）** |
| `guard-japanese-prose.sh` | mode_dependent | shadow モード: 継続（log のみ）/ enforce モード: **停止** |
| `rtk_boundary_shadow_guard.sh` | telemetry | 継続（log のみ） |
| `session_manifest_coordinator.sh`（Stop） | telemetry | 継続 |
| `session_manifest_coordinator.sh`（SubagentStop） | telemetry | 継続 |
| `generate_session_manifest_from_hook.mjs` | telemetry | 継続 |
| `save_loop_state_before_compaction.sh` | telemetry | 継続 |

---

## 4. agent 判断表（Codex CLI）

Codex CLI はこれらのフックを Claude Code フック機構と同一の形では実行しない。
以下は **参考情報** であり、Claude Code の動作との parity を主張するものではない。

| hook | Codex CLI での扱い |
|---|---|
| `secret_boundary_guard.sh` | Codex CLI は Claude Code hooks を直接実行しない。同等の制御は Codex の policy / allowed-tools 設定で実装する必要がある |
| `guard-japanese-prose.sh` | 同上 |
| その他 telemetry hooks | Codex は Claude Code の hooks 機構を持たないため、これらの telemetry は Codex セッションでは生成されない |

---

## 5. fail-open telemetry 設計の説明

AC2 対応: 以下の best-effort telemetry フックは作業 blocker にしない。

- `session_manifest_coordinator.sh`（Stop / SubagentStop）
- `generate_session_manifest_from_hook.mjs`（PostToolUse）
- `save_loop_state_before_compaction.sh`（PreCompact）
- `rtk_boundary_shadow_guard.sh`（PreToolUse）

これらは全て `fail_policy: fail_open` で設計されており、hook failure 時も exit 0 を返す（AC2）。

hook failure が発生した場合は diagnostic artifact が欠落するが、agent の作業継続は妨げられない。
failure は stderr または shadow JSONL に記録されるため、診断として追跡可能である（AC10）。

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
- `generate_session_manifest_from_hook.mjs`: stderr に producer failure を出力し、artifact 欠落を記録
- `save_loop_state_before_compaction.sh`: stderr に保存失敗を出力（最大 10 行）
- `rtk_boundary_shadow_guard.sh`: JSONL shadow log に記録（log write 失敗時は無言で pass）

PR review・session 終了時に artifact 欠落が検出された場合、follow-up issue として記録・追跡する。
