---
name: session-recording-policy
description: >
  session 記録 / Kill Switch / EntireCLI / checkpoint / secret-policy 関連作業時に、
  Claude Code が参照できる操作手順 Skill。
  policy 確認・checker 実行・SESSION_RECORDING_POLICY_VERDICT 記録を標準化する。
  本 Skill は policy 手順を提供するものであり、
  session 記録ツールの導入完了を意味しない。
disable-model-invocation: true
allowed-tools: Bash Read
---

# session-recording-policy Skill

session 記録 / Kill Switch / EntireCLI / checkpoint / secret-policy 関連作業を行う際に、
Claude Code が参照できる操作手順 Skill。

## 本 Skill の位置づけ

本 Skill は **policy 手順を提供するものであり、session 記録ツールの導入完了を意味しない**。

- session 記録ツール本体の導入（EntireCLI 等）は別スコープ
- session 記録の仕組みの構築（deterministic producer など）は別 Issue (#377 等)
- GitHub 経由の Main Loop session 振り返り可能化は別スコープ
- secret 流出の絶対保証ではない（policy 宣言に過ぎない）

pilot 開始には以下が必要であり、それらが main に merge されるまで pilot を開始しない：

- **#246**: Kill Switch runtime smoke test
- **#325**: Claude hook 統合（Stop/SubagentStop での自動実行）— 実装済み
- **#324**: CI 連動（pnpm policy:check 等） — 実装済み
- **deterministic manifest producer** (#377)
- **manifest schema validation gate** (#378)
- **no-push / private checkpoint / local-only verifier** (#379)
- **secret exposure scanner** (#380)

## 必須参考文書

以下の 3 つは session recording 関連作業時に **必ず読む**：

1. `docs/dev/session-recording-policy.md` （Kill Switch policy / SSOT）
2. `docs/dev/secret-policy.md` （Secret Inventory / no-secret 運用境界）
3. `docs/schemas/agent-session-manifest.schema.json` （manifest schema）

---

## 手順

### Step 1: policy 文書を読む

session 記録 / Kill Switch / checkpoint / EntireCLI / secret-policy に関わる作業を開始する前に、以下の 3 文書を読む。

```bash
# 1. session recording / Kill Switch policy SSOT
cat docs/dev/session-recording-policy.md

# 2. Secret Inventory / no-secret 境界 SSOT
cat docs/dev/secret-policy.md

# 3. session manifest schema
cat docs/schemas/agent-session-manifest.schema.json
```

### Step 2: policy checker を実行する

policy の structural integrity を検証する。

```bash
python3 .claude/scripts/check_session_recording_policy.py docs/dev/session-recording-policy.md
```

**期待値**: exit 0 で all checks passed。

**失敗時**: 11 項目の check result を確認し、違反を修正する。

### Step 3: SESSION_RECORDING_POLICY_VERDICT を記録する

以下の YAML を GitHub Issue コメント / PR 本文に記録する。
checker 実行結果に基づいて値を埋める。

```yaml
SESSION_RECORDING_POLICY_VERDICT:
  checker: pass | fail
  secret_policy_consistent: true | false
  manifest_policy_consistent: true | false
  kill_switch_triggered: false | true
  kill_switch_required_end_state_recorded: true | false
  human_review_required: true | false
```

**Kill Switch を実行した場合は additional metadata を追加**：

```yaml
SESSION_RECORDING_POLICY_VERDICT:
  checker: pass | fail
  secret_policy_consistent: true | false
  manifest_policy_consistent: true | false
  kill_switch_triggered: true
  kill_switch_required_end_state_recorded: true
  required_end_state:
    session_recording_tool_enabled: false
    git_hooks_recording_enabled: false
    public_checkpoint_branch_present: false
    auto_push_sessions_allowed: false
    full_transcript_remote_visibility: none
    leaked_credentials_rotated_or_revoked: true
  human_review_required: true
```

---

## checklist（作業フロー）

session 記録 / Kill Switch / checkpoint / EntireCLI / secret-policy に関わる PR / Issue コメントを書く際の確認チェックリスト：

- [ ] `docs/dev/session-recording-policy.md` を読んだ
- [ ] `docs/dev/secret-policy.md` を読んだ
- [ ] `docs/schemas/agent-session-manifest.schema.json` を読んだ
- [ ] `python3 .claude/scripts/check_session_recording_policy.py docs/dev/session-recording-policy.md` を実行した
- [ ] checker が exit 0 で pass した
- [ ] `SESSION_RECORDING_POLICY_VERDICT` を GitHub コメント / PR 本文に記録した
- [ ] Kill Switch が必要な場合は `required_end_state` の達成状況を記録した
- [ ] 人間レビューが必要な場合は Issue / PR に明記した

---

## Claude Code 専用・Codex 対応は別 Issue

本 Skill は **Claude Code 専用** (`.claude/skills/...`) です。

Codex CLI (`.agents/skills/...`) 対応は本 Issue の Out of Scope であり、
別 Issue #381 で扱います。

---

## 関連リンク

- `docs/dev/session-recording-policy.md` — Kill Switch policy / SSOT
- `docs/dev/secret-policy.md` — Secret Inventory / no-secret 運用境界
- `docs/schemas/agent-session-manifest.schema.json` — manifest schema
- `.claude/scripts/check_session_recording_policy.py` — policy structural checker
- Issue #136 — session 記録ツール導入判断（parent）
- Issue #242 — session recording Kill Switch policy 完成（#323）
- Issue #243 — `agent_session_manifest/v1` schema SSOT 化
- Issue #324 — policy checker CI 連動 — 実装済み (#355)
- Issue #325 — Claude hook 統合（Stop/SubagentStop） — 実装済み
- Issue #326 — Skill 導線標準化（本 Issue）
- Issue #377 — deterministic manifest producer（follow-up）
- Issue #378 — manifest validation gate（follow-up）
- Issue #379 — no-push/private/local-only verifier（follow-up）
- Issue #380 — secret exposure scanner（follow-up）
- Issue #381 — Codex CLI 対応（follow-up）
- Issue #246 — pilot smoke test（follow-up）

## 出力制約 (OUTPUT_BUDGET_V1)

`docs/dev/agent-skill-boundaries.md#OUTPUT_BUDGET_V1` の制約に従う。
