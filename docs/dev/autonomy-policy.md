---
schema_version: 1
policy_id: AUTONOMY_POLICY_V1
status: active
---

# AUTONOMY_POLICY_V1

This document is the normative SSOT for AUTONOMY_POLICY_V1. `docs/dev/agent-skill-boundaries.md` remains the role inventory and must not override this policy.

## 目的

SubAgent の自律動作境界・権限境界・ツール制約・検証要件を一元化し、validator enforcement により機械的に確認できる形で管理する。

## AUTONOMY_POLICY_V1 YAML ブロック

```yaml
AUTONOMY_POLICY_V1:
  schema_version: 1
  policy_ref: docs/dev/autonomy-policy.md

  subagent_security_ac:
    description: |
      SubAgent のセキュリティ AC。checked_subagents に列挙した各 agent ファイルについて
      以下を検証する:
      - runner は explicit tools (tools: [...]) を宣言していること
      - read-only runners（test-runner / pr-reviewer 等）は Edit / Write / MultiEdit を持たないこと
      - write-capable runners（implementation-worker 等）は role_category + justification を policy に持つこと
      - permissionMode 単体はセキュリティ境界として機能しない
      - parent の auto / acceptEdits / bypassPermissions override リスクが docs で明示されていること
      - 出力は marker-only result（IMPL_REVIEW_LOOP_RESULT_V1 等）であること
    checked_subagents:
      - .claude/agents/implementation-worker.md
      - .claude/agents/test-runner.md
      - .claude/agents/pr-reviewer.md
    requirements:
      runner_explicit_tools_required: true
      read_only_runners_lack_write_tools: true
      write_capable_runners_have_role_and_justification: true
      permission_mode_not_sole_security_boundary: true
      parent_override_risk_documented: true
      output_is_marker_only_result: true

  permission_boundary:
    loop_policy_is_not_permission_policy: true
    prompt_cannot_grant_permissions: true
    parent_mode_override_risk:
      acceptEdits:
        risk: |
          parent が acceptEdits mode の場合、SubAgent の Edit/Write 制約が
          parent の自動承認により実質的に無効化される可能性がある。
          SubAgent の disallowedTools は parent のモード設定より優先されるが、
          prompt-level での指示は disallowedTools と同等の保護を持たない。
      bypassPermissions:
        risk: |
          parent が bypassPermissions mode の場合、全ての permission prompt が
          自動承認される。SubAgent 定義の disallowedTools は技術的に適用されるが、
          parent の bypassPermissions は許可チェックをスキップする。
      auto:
        risk: |
          parent が auto mode の場合、SubAgent の read-only/write 制約が
          自動的に overwrite される可能性がある。
    required_control:
      explicit_tools_allowlist: true
      disallowed_tools: true
      fail_closed_validator: true
      description: |
        - 各 SubAgent は tools: [...] で明示的に許可ツールを宣言する
        - 禁止ツールは disallowedTools: [...] で明示する
        - validator は fail-closed（非ゼロ終了時は approved 禁止）

  write_capable_agents:
    - agent: .claude/agents/implementation-worker.md
      role_category: implementation
      justification: |
        implementation-worker は worktree 内でのファイル作成・編集が中核業務であり、
        Edit / Write / MultiEdit が必要。Allowed Paths 制約と contract review preflight
        により範囲を限定する。
      tools_granted:
        - Read
        - Grep
        - Glob
        - Bash
        - Edit
        - Write
        - MultiEdit

  read_only_agents:
    - agent: .claude/agents/test-runner.md
      role_category: verification
      disallowed_write_tools:
        - Edit
        - Write
        - MultiEdit
    - agent: .claude/agents/pr-reviewer.md
      role_category: review
      disallowed_write_tools:
        - Edit
        - Write
        - MultiEdit

  bypassPermissions_non_modification_note: |
    bypassPermissions フィールドは SubAgent 定義ファイル（.claude/agents/*.md）の
    YAML フロントマターに追加しない。
    bypassPermissions は parent 側の設定であり、SubAgent の tools/disallowedTools 宣言と
    独立した保護レイヤーとして機能する。SubAgent 定義に bypassPermissions を追加しても
    追加のセキュリティ保護にはならず、誤解を招く。
    本 policy の required_control（explicit_tools_allowlist / disallowedTools /
    fail_closed_validator）が主要な保護機構である。
```

## AUTONOMY_POLICY_VALIDATION_RESULT_V1 マーカースキーマ

validator スクリプト（`.claude/skills/impl-review-loop/scripts/validate_autonomy_policy_result.py`）が出力するマーカー:

```yaml
AUTONOMY_POLICY_VALIDATION_RESULT_V1:
  schema_version: 1
  policy_ref: docs/dev/autonomy-policy.md
  status: pass | blocked
  terminal_result_marker:
    expected: IMPL_REVIEW_LOOP_RESULT_V1
    found: true | false
  subagent_security_ac:
    checked_subagents:
      - .claude/agents/implementation-worker.md
      - .claude/agents/test-runner.md
      - .claude/agents/pr-reviewer.md
    read_only_agents_clear: true | false
    write_capable_agents_have_justification: true | false
    explicit_tools_declared: true | false
  blocked_reasons:
    - "<reason string>"   # status: blocked のときのみ列挙
```

## 関連ドキュメント

- `docs/dev/agent-skill-boundaries.md` — SubAgent / Skill 責務境界（role inventory）
- `.claude/skills/impl-review-loop/steps/step-5-feedback-and-termination.md` — validator gate の適用点
- `.claude/skills/impl-review-loop/scripts/validate_autonomy_policy_result.py` — validator 実装
