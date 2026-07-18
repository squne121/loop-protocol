---
pilot_date: "2026-05-30"
recording_method: hook-based-metadata-ledger
parent_issue: "#246"
status: in-progress
---

# Session Recording Pilot Smoke Test

## Scope Boundary

このファイルは #522 の smoke test 記録用ワークシートであり、#246 の Acceptance Criteria 達成証跡そのものではない。
#246 の達成判定は、Issue / PR コメントに保存された `agent_session_manifest/v1`、Kill Switch 検証結果、Secret scan、metrics、採否判断をもって行う。

## 概要

#246 (research/pilot: AI 駆動 session 記録 pilot smoke test) のパイロット実走記録。
採用方式: **Hook-based Metadata Ledger**（#245 手順書の Adopt 判定）。EntireCLI は使用しない。

## Evidence Storage Boundary

- `artifacts/session-recording-pilot/` は local-only / private artifact 用であり、raw transcript、local absolute path、API key、`.env`、agent-local settings を GitHub に投稿・commit・push してはならない。
- Codex CLI adapter が生成する session manifest（Issue #1546 以降は canonical per-user state root `$XDG_STATE_HOME/loop-protocol/session-manifests/v1/<repo_key>/codex/**`、既定 `$HOME/.local/state` 配下。レガシー repo-local `tmp/session-manifests/codex/**` へは新規 write されない）と `tmp/codex-pilot-metrics.json` も private/local artifact とし、runtime active hook / trust state の public evidence に流用しない。
- public GitHub comment に置けるのは `agent_session_manifest/v1` の metadata のみ（`source_kind: public_github_comment`）。
- `entire/checkpoints/v1` または同等の checkpoint branch が public remote に存在しないことを Kill Switch 検証で確認する。
- raw transcript は `public_github_comment` surface への出力禁止（#242 policy）。

## Claude Code Hooks の既知の制限

- `Stop` は「turn 完了」単位で発火するため、session 完了証跡として単独利用しない。
- API error 時は `StopFailure` を確認する（`Stop` は発火しない）。
- ユーザー interrupt 時は `Stop` / `StopFailure` ともに発火しないため、interrupt 時の manifest 未記録リスクを Known Limitation として扱う。
- `PostToolUse` は既実行操作を取り消せないため、ブロック用途は `PreToolUse` に限定する。

## Required Evidence Links

#246 の各 AC に対応する証跡（実行後に記入）:

| 証跡 | comment_url | manifest_id | verification.overall |
|---|---|---|---|
| Preflight manifest | https://github.com/squne121/loop-protocol/issues/246#issuecomment-4583023247 | asm-778b814c-dd9e-40de-9f6e-7a2dffa976f1 | pass |
| Implementation manifest | https://github.com/squne121/loop-protocol/issues/246#issuecomment-4583035545 | asm-abc08254-6aee-4aa8-8e31-3a876e1655a2 | pass |
| PR review manifest | https://github.com/squne121/loop-protocol/issues/246#issuecomment-4583040005 | (pr_review phase) | pass |
| Kill Switch verification log | | | |
| Secret exposure scan | | | |
| Metrics report | | | |
| Recommendation | | | |

## 実行フェーズ記録

### Phase 1: Preflight

- [x] agent_session_manifest posted
  - comment_url: https://github.com/squne121/loop-protocol/issues/246#issuecomment-4583023247
  - manifest_id: asm-778b814c-dd9e-40de-9f6e-7a2dffa976f1
  - verification.overall: pass
  - redaction.raw_transcript_included: false
  - redaction.local_paths_included: false

### Phase 2: Implementation

- [x] implementation manifest posted
  - comment_url: https://github.com/squne121/loop-protocol/issues/246#issuecomment-4583035545
  - manifest_id: asm-abc08254-6aee-4aa8-8e31-3a876e1655a2
  - verification.overall: pass
  - redaction.raw_transcript_included: false
  - redaction.local_paths_included: false

### Phase 3: PR Review

- [x] pr_review manifest posted
  - comment_url: https://github.com/squne121/loop-protocol/issues/246#issuecomment-4583040005
  - manifest_id: (pr_review phase)
  - verification.overall: pass
  - redaction.raw_transcript_included: false
  - redaction.local_paths_included: false

### Phase 4: Kill Switch Verification

- [ ] kill_switch_runtime_smoke PASS
  - log_ref:
  - positive_case: pass
  - negative_case_public_checkpoint: blocked_exit_2
  - negative_case_unknown_mapping: blocked_exit_2

### Phase 5: Secret Exposure Scan

- [ ] local_worktree_scan: pass
- [ ] git_history_scan: pass
- [ ] entire_metadata_scan: pass
- [ ] github_secret_scanning: pass / unavailable (reason: )
- [ ] synthetic_canary: pass (real_secret_used: false)

### Phase 6: Metrics

- token_usage: unavailable (ai_self_reported: forbidden)
- latency_ms:
- human_intervention_count:

### Phase 7: Recommendation

- [ ] verdict posted
  - recommendation: continue_with_metadata_only / continue_after_followups / stop_and_do_not_adopt
  - reason:
