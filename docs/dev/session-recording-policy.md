---
id: session-recording-policy
status: stable
summary_ja: "この front matter は session-recording-policy SSOT の識別情報を表す。"
related_issue: "#242"
related_issues:
  - "#136"
  - "#241"
  - "#243"
created: "2026-05-24"
---

# session 記録 Kill Switch policy (SSOT)

本文書は `session_recording_policy/v1` YAML と Kill Switch 手順の唯一の正本（SSOT）である。
`secrets_mode` が `none` 以外に遷移した場合の session 記録制御と、Kill Switch の実行手順を定める。

## Codex CLI Hook Boundary（Codex CLI フック境界）

- `.codex/hooks.json` の structural validation は repo-local wiring の確認に限られ、runtime active hook state や trust state の証跡にはならない。
- Codex CLI では `[features].hooks` を canonical key とし、`codex_hooks` は deprecated alias としてのみ扱う。
- project `.codex/` layer は trust 済みでなければ load されない。`--dangerously-bypass-hook-trust` は one-off automation 向けの非既定 escape hatch であり、pilot の通常運用では使わない。
- `PreToolUse` / `PermissionRequest` は予防層であり security boundary ではない。canonical final gate は post-run verifier と private artifact validation に置く。
- Codex hook command は `rtk pnpm exec node ...` を明示 dependency とし、runtime では `node` と repo script のみで deterministic に再実行できるようにする。

### Codex SubagentStop における scope-rollup capture の記録（#1527、Scope Delta (2)）

Codex native agent registry の `scope-rollup-runner` dispatch source は `.codex/agents/scope-rollup-runner.toml` とする。これは named agent identity を capture producer の `agent_type: scope-rollup-runner` と整合させるための registry 定義であり、hook trust / runtime-active state の証跡ではない。generic/default/worker fallback は capture target として認めない。

Codex の `SubagentStop` では、adapter は capture policy の適用と subprocess transport
だけを担当し、canonical artifact の decision authority は既存
`.claude/hooks/capture_scope_rollup_final_response.py` とする。adapter は marker parse、
invocation ID、capture path、sidecar schema、duplicate/stale/SHA256 判定を再実装しない。

#### eligibility / readiness の判定根拠は固定 private location のみとする

source-bound eligibility / readiness の受理経路は、`.claude/scripts/check_session_recording_runtime_safety.py`
（eligibility）と `scripts/session-recording/bootstrap-source-bound-readiness.mjs`（readiness）が
書き込む固定 private location（既定: `.claude/tmp/session-recording/scope-rollup-{eligibility,readiness}.json`、
mode `0600`）**のみ**である。hook payload 内の inline object、任意ファイルパス、`artifacts[]` 配列内
fuzzy-match、`source_bound` / `source_bound_artifacts` キーは、eligibility/readiness の情報源として
一切受理しない（#1527 の初回実装がこの経路を受理していたことが敵対的レビューで確認された不備であり、
Scope Delta (2) で是正した）。

adapter（Node のみ）は producer を起動する前に、両方の固定 location を検証する。missing / invalid /
stale の場合は producer を一切起動せず、固定 reason code で即時 skip する（cold environment での
`uv`/Python 起動そのものを防ぐ）。producer（Python）は、adapter からの `SCOPE_ROLLUP_REQUIRE_SOURCE_BOUND_ELIGIBILITY=1`
env（Codex adapter の trusted process spawn でのみ設定され、hook payload からは設定できない）を受けたときのみ、
同じ固定 location を独立に再検証する（defense in depth）。この env が未設定の Claude
`session_manifest_coordinator.sh` の raw-payload 経路は、#1527 以前と同一の無加工 capture を継続する
（回帰なし）。

eligibility artifact の binding 検証: repo root realpath、policy digest（`docs/dev/session-recording-policy.md`
の sha256）、secret-policy digest（`docs/dev/secret-policy.md` の sha256）、`public_checkpoint_present: false`、
`secrets_mode: none`、`safety_verdict: allow`、mode `0600`、owner、no-symlink、`additionalProperties: false`
（未知キー・欠落キーは両方 fail-closed）。timestamp lifecycle は事前生成を正とする方向で判定する:
`eligibility.generated_at <= hook_received_at`、`eligibility.generated_at <= marker.generated_at`、
`hook_received_at < eligibility.expires_at`、`readiness.generated_at <= hook_received_at`。marker より後に
生成された artifact、または `expires_at` を過ぎた artifact は stale として拒否する。

readiness artifact の binding 検証: repo root realpath、`producer_digest`（capture producer 自身の sha256、
コード変更後の stale readiness を防ぐ）、`prepared: true`、`interpreter_realpath` の存在。hot path は
`readiness.interpreter_realpath` を直接 spawn し、`uv run --locked`（暗黙 sync の可能性がある）は使わない。

sidecar provenance には、検証した eligibility/readiness artifact の digest（`sha256:...`）と検証結果
reason code を記録する（`provenance.eligibility_artifact_digest` / `provenance.eligibility_verification_reason_code`
/ `provenance.readiness_artifact_digest` / `provenance.readiness_verification_reason_code`）。

| 条件 | capture | hook result |
|---|---|---|
| `SubagentStop`、guard allow、`stop_hook_active: false`、固定 location の eligibility/readiness が valid | producer を起動 | 既存 manifest flow を継続 |
| 固定 location の eligibility/readiness が missing/invalid/stale | producer を起動しない（固定 skip reason） | 既存 manifest flow を継続 |
| `secrets_mode != none`、`public_checkpoint_enabled`、`unknown_visibility_mapping`、guard deny | skip | guard の既存 decision を維持 |
| `stop_hook_active: true` または malformed payload | skip | existing fail-open output を維持 |
| non-target agent（eligibility/readiness は valid） | producer を起動 | canonical `.txt` は作らず diagnostic sidecar のみ |

- production invocation は、readiness artifact が指す fixed interpreter を直接 spawn し（`uv run` は使わない）、
  `cwd: repoRoot`、stdin は受信した JSON payload の原文とする。
- timeout（3,500 ms）後は process group に `SIGKILL` を送り、bounded grace（500 ms）で `close` を待ってから
  `kill(-pgid, 0)` 等で process group の liveness 不在を確認する。spawn/nonzero/timeout は fixed reason code
  の bounded diagnostic のみを残す。child stdout/stderr、absolute path、final response、marker 本文、
  例外 message を adapter stdout/stderr に出してはならない。
- `CODEX_SCOPE_ROLLUP_CAPTURE_SCRIPT` は test-only であり、canonicalize 後に `tests/fixtures/hooks/scope-rollup-capture/` 配下の fixture だけを受理する。production は既定 producer 以外を使わない。
- canonical `.txt` の唯一の producer は上記 Python script である。PyYAML unavailable 時の `hook_unavailable` sidecar fallback は Claude coordinator の例外であり、Codex adapter に production fallback を追加しない。
- production readiness は `pnpm bootstrap`（`scripts/session-recording/bootstrap-source-bound-readiness.mjs`）が、
  対応 Python の locked sync、PyYAML import smoke、producer の `py_compile` smoke を実際に完了してから
  readiness artifact を atomic 生成する。いずれかに失敗した場合は nonzero exit とし、虚偽の `prepared: true`
  を書かない。hot path（`package.json` の `session-recording:capture-hot-path-check`）は `uv run --no-sync`
  を用い、environment sync や Python download を行わない。
- adapter subprocess test は adapter path verified の証跡だけであり、`.codex/hooks.json` の runtime-active/trust または live Codex smoke の成功を示さない。

---

## active PreToolUse handlers（#783 追加・有効ハンドラー一覧）

`.codex/hooks.json` に登録されている active PreToolUse handlers とその責務境界。

| matcher | handler | 責務 |
|---|---|---|
| `^Bash$` | `.codex/hooks/local_main_branch_guard.sh` | local root branch policy（fail-closed） |
| `^Bash$` | `.claude/hooks/worktree_scope_guard.py` | worktree cleanup scope policy（fail-closed shared core） |
| `^Bash$` | `scripts/check-codex-agents.mjs --hook-pretool` | rtk bypass guard / Allowed Paths enforcement |
| `^Bash$` | `.codex/hooks/session-recording-composite.mjs --event PreToolUse` | session recording guard（reason_code taxonomy） |
| `^Bash$` | `.codex/hooks/ci_test_performance_advisory.sh` | CI/test lane advisory（fail-open advisory） |
| `^(apply_patch\|Edit\|Write)$` | `scripts/check-codex-agents.mjs --hook-pretool` | Allowed Paths enforcement（write tool） |
| `^(apply_patch\|Edit\|Write)$` | `.codex/hooks/session-recording-composite.mjs --event PreToolUse` | session recording guard（patch/write） |
| `^(apply_patch\|Edit\|Write)$` | `.codex/hooks/ci_test_performance_advisory.sh` | CI/test lane advisory（fail-open advisory） |

Codex CLI は同一 event に matching する複数の command hooks を **concurrently launched** する（公式仕様）。
実行順序・short-circuit・handler 間依存は保証されない。
各 handler は独立して fail-closed な判定を返す必要があり、他の handler が先に deny することに依存してはならない。

### reason_code taxonomy（#783・理由コード分類）

`session-recording-composite.mjs` が返す deny reason の分類。

| reason_code | command_kind | 対象コマンド例 | 意味 |
|---|---|---|---|
| `secret_boundary_violation` | `gh_secret` | `gh secret list` | gh secret コマンドによる Secret 参照 |
| `secret_boundary_violation` | `gh_api_actions_secrets` | `gh api .../secrets` | gh api 経由の GitHub Actions Secret 参照 |
| `secret_boundary_violation` | `printenv` | `printenv` | 環境変数全ダンプ |
| `secret_boundary_violation` | `env_dump` | `env`（standalone） | 環境変数全ダンプ（bare env） |
| `secret_boundary_violation` | `python_os_environ` | `python3 -c '...os.environ...'` | Python 経由の環境変数アクセス |
| `remote_write_requires_approval` | `git_push` | `git push origin main` | remote への git push |
| `readonly_investigation_allowed` | — | `env FOO=bar cmd` | variable prefix 付き read-only 調査（通過） |

`env FOO=bar <cmd>` パターン（変数代入プレフィックス）は secret dump ではなく read-only investigation として通過する（AC2）。

### #360 / #639 との境界

- **#360（destination guard policy）**: remote write の許可 policy 自体の見直しは #360 が担当する。本 #783 は deny reason の分類整理のみ。`remote_write_requires_approval` を自動許可に変更する設計は #360 スコープ。
- **#639（PR body mutation enforcement）**: PR body の mutation 強制実装は #639 が担当する。本 #783 は hook output shape の整形のみ。

### publish lane approval bridge（#1408・#360 との責務差分）

- **#360（destination guard policy）**: main/master への push destination そのものを拒否する汎用 guard。判定対象は push 先ブランチであり、証跡の有無に関わらず適用される。本境界は #1408 実装後も変更しない。
- **publish lane approval bridge（#1408）**: `codex-hook-adapter.mjs` の PreToolUse remote write 判定に限定して追加された、狭い bypass 経路。`rtk git push origin HEAD:refs/heads/<active-branch>` かつ `scripts/agent-guards/git_mutation_command_policy.py` の bounded policy（expected/current/local/verified/declared head 比較、`LOOP_PUBLISH_ALLOWED_PATHS_GATE_STATUS`）が `status: allow` を返した場合のみ、generic `remote_write_requires_approval` deny を経由せず通過する。安全判定ロジック自体は `git_mutation_command_policy.py` の再利用であり、hook adapter 側に別実装の安全判定は追加していない。
- force/tag/all/delete/mirror push、直接 `git push`、`git -C <dir> push`、wrapper bypass は publish lane approval bridge の対象外であり、引き続き deny される（#360 の destination guard および既存 deny reason taxonomy がそのまま適用される）。
- **スコープ縮小（#1408 アドバーサリアルレビュー iteration 2 反映）**: 本 bridge は **既存 remote branch の更新（fast-forward push）のみ**を対象とする。remote 上にブランチが存在しない新規 branch の初回 publish は本 PR のスコープ外であり、別 Issue #1449 に切り出した。`git_mutation_command_policy.py` は remote ref 不在を `_ls_remote_head` の `git ls-remote --exit-code` 非ゼロ終了（returncode 2）から明示的に判別し、`remote_branch_absent_not_supported` として deny する。
- **initial_branch_create レーン（#1449、existing_branch_update レーンとは別の decision path）**: 上記の existing_branch_update レーン（`rtk git push origin HEAD:refs/heads/<branch>`、2 引数）とは別に、`rtk git push --force-with-lease=refs/heads/<branch>: origin HEAD:refs/heads/<branch>`（完全限定 empty-expect lease、3 引数）という別 argv shape の initial_branch_create レーンを追加した。remote branch 状態は同一実行サイクルの live `git ls-remote --refs --exit-code`（`classify_remote_branch_state`）から `present` / `absent` / `probe_error` の排他的 3 状態にのみ分類し、`absent` の場合だけ candidate になる（`present` は `remote_branch_present_route_existing_update` として deny、`probe_error` は `probe_error_fail_closed` として deny — fail-closed）。lease 実行は argv 配列・`shell=False` のみで行い（`execute_initial_branch_create_push`）、push 成功後は同じ ref への live readback を再度行い、readback head と local head が完全一致した場合のみ成功として扱う（不一致は `readback_mismatch_local_head`、readback 失敗は `readback_failed_after_push` として safety stop）。`--force` / `-f` / `+refspec` / 引数なし `--force-with-lease` / expect 値ありの lease / lease ref と push target の不一致 / 複数 lease / 複数 refspec / tag / `--tags` / `--all` / `--mirror` / `--delete` は本レーンでも引き続き deny する（`validate_initial_branch_create_argv` が完全一致以外を一律拒否）。
- **`remote_readback_source` は `ls_remote` のみを認可**する。`github_branch_api` / `fetch_then_show_ref` は実際に remote を再読込みせず環境変数値をそのまま信用する自己申告ラベルだったため、`publish_guard_context_invalid` として deny する。
- push 実行前に `git remote get-url --push --all origin` 相当で push URL を取得し、canonical repository identity（`squne121/loop-protocol`）と一致することを検証する。不一致（別 repository への push URL、`insteadOf` 等での向け替えを含む）は `origin_remote_identity_mismatch` として deny する。
- policy 自身（`classify_rtk_git_mutation`）が push 先ブランチ（解決済み `origin/HEAD`、`LOOP_DEFAULT_BRANCH` を含む default branch 名 `main` / `master` / `trunk`）である場合に `push_target_is_default_branch` で deny する回帰も追加した（#360 の destination guard に独立して重ねる防御）。
- Allowed Paths gate の `status: ok` は issue_number（`LOOP_ISSUE_NUMBER`）/ base_sha（`expected_remote_head`）/ head_sha（`local_head` と一致する `declared_publish_head` / `verified_head`）に bind する。`LOOP_PUBLISH_ALLOWED_PATHS_GATE_ISSUE_NUMBER` / `LOOP_PUBLISH_ALLOWED_PATHS_GATE_BASE_SHA` / `LOOP_PUBLISH_ALLOWED_PATHS_GATE_HEAD_SHA` が実際の issue / expected remote head / local head と一致しない場合は `allowed_paths_gate_binding_mismatch` として deny し、過去の head や別 issue で得た `ok` の再利用を防ぐ。

### command structure classification（コマンド構造分類）と #1408 publish lane authorization（公開レーン認可）の責務分離（#1428）

`codex-hook-adapter.mjs` の `classifyRemoteWrite(command)` は、raw command 文字列全体への
正規表現 substring match ではなく、`scripts/agent-guards/shell_command_analysis.py`
（`SHELL_COMMAND_ANALYSIS_V1`）が返す **command structure classification** を基準に
`git push` / `rtk git push` を判定する（#1428）。

- **本 responsibility（command structure classification, #1428）**: シェル上で実際に実行される
  simple command を、quoted argument・検索キーワード・heredoc data 等の非実行データと区別し、
  `command_kind`（`git_push` / `rtk_git_push`）・`execution_context`（`top_level` /
  `list` / `pipeline` / `command_substitution` / `execution_carrier` 等）を機械可読な enum
  として返す。静的に literal と確定できない command word / subcommand（dynamic executable、
  `find -exec` / `xargs` 等の未対応 execution carrier を含む）は `status: indeterminate` として
  fail-closed に扱い、remote write classifier は allow に倒さない。
- **#1408 の responsibility（publish lane authorization）**: `rtk git push` と判定された command
  について、実際に push を許可するかどうかの最終判断（`scripts/agent-guards/git_mutation_command_policy.py`
  の `classify_rtk_git_mutation` / publish guard context 検証）は `#1408` が担当する。本 #1428 は
  publish lane の allow / deny 条件そのものを変更しない。
- `git_mutation_command_policy.py` の外部 API（`classify_rtk_git_mutation` シグネチャ）は #1428 の
  スコープでは変更しない。同 module は raw command 文字列を独自に `shlex.split` で再 tokenize し
  続けるが、両 module は互いに独立した trust boundary 内で raw command の再解析を行うのみであり、
  #1428 の analyzer 出力を #1408 の policy へ直接受け渡す配線変更は本 Issue のスコープ外
  （split-brain regression は `scripts/agent-guards/tests/test_shell_command_analysis.py` で固定）。

---

## codex exec live smoke（diagnostic-only、#783 追加・診断限定）

`codex exec` を使った live smoke test は **diagnostic-only 手順**とする。CI required gate には含めない。

### 背景

2026-06-10 時点で upstream issue [`openai/codex#26452`](https://github.com/openai/codex/issues/26452) が OPEN であり、
`codex exec` の hook dispatch 動作が不安定なことが確認されている。
live smoke の成功/失敗はこの Issue の merge gate に含めない（`decision: not_applicable`）。

### diagnostic 手順（人間が手動確認する場合）

```bash
# codex exec が利用可能か確認（binary チェック）
which codex || echo "codex not found"
codex --version 2>/dev/null || echo "codex version unavailable"

# PreToolUse hook が発火するか確認（diagnostic-only — 成功は保証しない）
# upstream openai/codex#26452 が open のため hook dispatch が不安定
codex exec --dangerously-bypass-hook-trust false -- bash -c 'echo test' 2>&1 || true

# hook output を手動確認する場合
echo '{"tool_name":"Bash","tool_input":{"command":"git push origin main"}}' \
  | node .codex/hooks/session-recording-composite.mjs --event PreToolUse
# 期待: {"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"..."}}
```

上記は **diagnostic-only**。CI ゲートや merge 条件には含めない。

---

## 機械可読メタデータ (session_recording_policy/v1)

```yaml
schema: session_recording_policy/v1
source_of_truth:
  secret_policy: docs/dev/secret-policy.md
  manifest_schema: docs/schemas/agent-session-manifest.schema.json
derived_from_secret_policy:
  current_secrets_mode: none
  fail_closed_on_unknown_mapping: true
taxonomy_mapping:
  current:
    description: "現時点で repo に存在する Secret の状態"
    secrets_mode_when_absent: none
    secrets_mode_when_present: unknown
    public_full_transcript_allowed: false
    session_recording_allowed: true
    checkpoint_push_allowed: false
    rationale: "Secret なし = none。Secret 発生時は unknown に分類して本文書を再確認する"
  publish_secret:
    description: "publish/deploy 用 Secret（itch.io butler, Cloudflare Pages 等）"
    secrets_mode: publish_secret
    public_full_transcript_allowed: false
    session_recording_allowed: false
    checkpoint_push_allowed: false
    rationale: "deploy 認証情報が session transcript に混入するリスク。recording 停止を要求"
  app_runtime_secret:
    description: "アプリ実行時に必要な API key 等（外部 API 呼び出し等）"
    secrets_mode: app_secret
    public_full_transcript_allowed: false
    session_recording_allowed: false
    checkpoint_push_allowed: false
    rationale: "runtime secret が transcript に露出しうるため recording 禁止"
  agent_local_secret:
    description: "AI Agent ローカル設定（.claude/settings.local.json 等）"
    secrets_mode: app_secret
    public_full_transcript_allowed: false
    session_recording_allowed: false
    checkpoint_push_allowed: false
    rationale: "local override ファイルに API key が含まれうるため recording 禁止"
  checkpoint_token:
    description: "session 記録ツール用 token（EntireCLI 等）"
    secrets_mode: app_secret
    public_full_transcript_allowed: false
    session_recording_allowed: false
    checkpoint_push_allowed: false
    rationale: "recording credential 自体が Secret のため、public transcript は絶対禁止"
# 注意: 全 mode で public_full_transcript_allowed は false。
# none 時でも public transcript は禁止。session 記録ツール未導入時も同様。
public_surfaces:
  github_issue_or_pr_comment:
    agent_session_manifest:
      body_allowed: false
      opaque_ref_allowed: true
      allowed_refs:
        - artifact_id
        - artifact_url
        - artifact_digest
        - schema_ref
        - validation_verdict
      forbidden_content:
        - raw_transcript
        - transcript_excerpt
        - local_absolute_path
        - secret_value
        - full_command_output
        - full_prompt
      rationale: >
        manifest body は Issue / PR comment に出さない。公開コメントでは
        artifact_digest や validation_verdict などの opaque ref のみを許可する。
    agent_run_report:
      body_allowed: conditional
      conditions:
        - issue_935_schema_and_redaction_validator_merged
        - issue_937_exact_marker_upsert_guard_merged
        - exact_marker_upsert_guard_passed
        - redaction_validator_clean
        - max_body_bytes_passed
      live_public_posting: forbidden_until_dependencies_merge
      rationale: >
        redacted report は conditional public comment の対象だが、#934 merge 時点では
        dry-run 設計のみを許可し、live public posting は禁止する。
    agent_retro_index:
      body_allowed: conditional
      conditions:
        - issue_935_schema_and_redaction_validator_merged
        - issue_937_exact_marker_upsert_guard_merged
        - exact_marker_upsert_guard_passed
        - redaction_validator_clean
        - max_body_bytes_passed
      live_public_posting: forbidden_until_dependencies_merge
      rationale: >
        retro index も conditional public comment の対象だが、#934 merge 時点では
        dry-run のみで、live public posting は #935 / #937 完了後に限る。
  github_issue_comment:
    compatibility_alias_of: github_issue_or_pr_comment
    raw_transcript_allowed: false
    source_kind_prohibited:
      - transcript
      - local_file
    compatibility_note: >
      legacy checker / policy consumer 互換のため `github_issue_comment` キーを残す。
      current policy の正本は `github_issue_or_pr_comment` であり、
      新規 contract は alias 側へ増設しない。
github_public_checkpoint_branch_allowed: false
checkpoint_remote:
  allowed_visibility:
    - private_verified
  fail_closed_on_unknown_visibility: true
  visibility_check_unknown_action: fail_closed
  verification_method:
    github_remote: "gh repo view <owner/repo> --json visibility --jq '.visibility'"
    required_result: "PRIVATE"
auto_push_sessions_allowed: false
manual_review_required_before_push: true
kill_switch:
  trigger_conditions:
    - secrets_mode != none
    - checkpoint_token_present
    - public_checkpoint_branch_detected
    - raw_transcript_public_surface_detected
    - secret_scan_status == flagged
    - session_recording_tool_enabled and push_remote_visibility == public
  required_end_state:
    session_recording_tool_enabled: false
    git_hooks_recording_enabled: false
    public_checkpoint_branch_present: false
    auto_push_sessions_allowed: false
    full_transcript_remote_visibility: none
    leaked_credentials_rotated_or_revoked: true
  verification_required: true

# Latitude telemetry state definitions (#1157)
# export_state と capture_state は独立した状態として定義される。
# LATITUDE_CLAUDE_CODE_ENABLED=0 は export を停止するが capture を停止する証明にならない。
latitude:
  pilot_state: BLOCKED
  real_development_session_allowed: false
  export_state:
    description: "Latitude export hook の有効/無効状態 (LATITUDE_CLAUDE_CODE_ENABLED)"
    values:
      enabled: "export フックが有効（LATITUDE_CLAUDE_CODE_ENABLED が 1 または未設定）"
      disabled: "export フックが無効（LATITUDE_CLAUDE_CODE_ENABLED=0）"
      unknown: "状態が確認不能"
    control_variable: LATITUDE_CLAUDE_CODE_ENABLED
    stop_export_only: true
    note: "export 停止は capture 停止の証明にならない（preload が active な場合）"
  capture_state:
    description: "BUN_OPTIONS preload による capture の active/inactive 状態"
    values:
      active: "preload が settings/systemd/shell/process environment に設定されている"
      inactive: "preload が設定されていない"
      unknown: "状態が確認不能"
    capture_independent_from_export: true
    note: "export が disabled でも preload が active なら capture は active のまま"
  uninstall_postcondition:
    quiescent_two_stage_required: true
    stages:
      - "1. 新規起動経路の BUN_OPTIONS / hook を停止"
      - "2. Claude CLI / Desktop / IDE extension host / 関連 Bun process を停止"
      - "3. active process inheritance を再検査"
      - "4. settings / backup / state / spool / preload を検査・除去"
      - "5. 静止後に 2 回目の read-only scan"
      - "6. 2 回の結果が同一かつ対象 process が存在しない場合のみ contained"
    note: "official uninstall を信用せず postcondition を再検査する"
  real_session_start_gate:
    # A1 decision (#1220) は docs/dev/secret-policy.md の LATITUDE_PILOT_EXCEPTION_V1 を正本とする。
    a1_decision_ref: "docs/dev/secret-policy.md#LATITUDE_PILOT_EXCEPTION_V1"
    a1_decision_marker: LATITUDE_PILOT_EXCEPTION_V1
    a1_parent_issue: "#1153"
    a1_issue: "#1220"
    required_conditions:
      - latitude_containment_complete (別 Issue の人間 Decision)
      - secret_inventory_consistent
      - export_state_confirmed_disabled_or_never_enabled
      - capture_state_confirmed_inactive
      - uninstall_postcondition_verified
      - a1_decision_is_approve_timeboxed_real_pilot_with_all_required_fields
    # activation gate の正本は Stop hook ではなく pre-session host verifier JSON とする。
    activation_gate_authority: pre_session_host_verifier
    activation_gate_command: ".claude/scripts/check_session_recording_runtime_safety.py --json --execution-profile host"
    stop_hook_role: diagnostic_layer_only
    blocked_until: "LATITUDE_PILOT_EXCEPTION_V1 が approve_timeboxed_real_pilot かつ必須 field 充足"
    # A1 decision に応じた activation state:
    activation_state_by_decision:
      absent_or_multiple_marker: blocked_until_activation
      decision_defer: blocked_until_activation
      decision_approve_synthetic_only: blocked_until_activation
      decision_reject_and_uninstall: deny
      decision_approve_timeboxed_real_pilot_incomplete: blocked_until_activation
      decision_approve_timeboxed_real_pilot_complete: allow
    blocked_state_when:
      - a1_decision_absent
      - a1_decision_defer
      - a1_decision_approve_synthetic_only
      - a1_decision_reject_and_uninstall          # deny
      - required_fields_missing
      - remote_cleanup_state_unknown
      - argv_exposure_possible_or_unknown
    synthetic_only_scope:
      allowed:
        - synthetic_fixture_validation
        - policy_validation
      forbidden:
        - real_prompt
        - real_trace_export
        - real_cloud_pilot
```

## Latitude real pilot activation gate (#1220・実運用開始ゲート)

real session 開始可否（`real_session_start_gate`）の正本は、`docs/dev/secret-policy.md` の
`LATITUDE_PILOT_EXCEPTION_V1` decision（A1 decision）である。`a1_decision_ref` がこの境界を指す。

- decision 不在、`defer`、`approve_synthetic_only`、`reject_and_uninstall`、required fields 不足、
  `remote_cleanup_state` が unknown、`argv_exposure_state` が possible / unknown のいずれかなら、
  real session start gate は `BLOCKED`（host verifier 上は `blocked_until_activation`、
  `reject_and_uninstall` は `deny`）を維持する。
- `approve_synthetic_only` は synthetic fixture / policy validation のみ許可し、
  real prompt / real trace export / real Cloud pilot は許可しない。
- activation gate の正本は Stop hook ではなく、session 開始前の host verifier JSON
  （`check_session_recording_runtime_safety.py --json --execution-profile host`）とする。
  Claude Code hook は matching する全 hook が並列実行されるため、hook の通過を activation 証明にしない。
- `latitude:real-pilot:preflight` は上記 host verifier JSON を strict mode で再評価する
  sole real-pilot pre-session gate であり、`decision: allow` 単独や fixture PASS を allow 根拠にしない。
- `latitude:real-pilot:preflight`（#1261 実装後、PR #1352 fix_delta 反映）は
  `components.latitude.distribution.state` の summary field だけでなく、`resolution_source`
  （closed enum・`unknown` 不可）、`package_spec`（exact semver 必須。`npx` prefix を正規化した上で
  `<name>@x.y.z` 形式を強制）、`dist_integrity`（SRI 形式必須）、`resolved_registry_origin`
  （approved registry origin と一致必須）、`lockfile_digest`、`tarball_sha256`、
  `installed_entrypoint_sha256`、`preload_sha256`、`hook_command_sha256`（すべて `sha256:<64hex>`）、
  `npx_invocation`（closed enum。`floating` は常に block）、
  `components.latitude.argv_exposure_state`（`absent_verified` 必須）、
  `components.latitude.remote_cleanup_state`（`machine_verified` 必須、`human_attested` は代替不可）を
  直接 assert する。
- `npx_invocation` の判定は Claude settings hook command の read-only parsing を最優先とする。
  floating `npx -y @latitude-data/claude-code-telemetry` のような version 未固定の npx 起動は、
  lockfile / node_modules / npm cache / npm global list による分類より優先して
  `resolution_source: npx_only` + `npx_invocation: floating` として検出し、
  `latitude_npx_invocation_floating` reason_code とともに real pilot preflight を block する。
- `argv_exposure_state` の host mode 検査は、checker 自身の argv（`/proc/self/cmdline`）ではなく、
  Claude Code / node / bun / npx / npm / Latitude telemetry package を含む command line を持つ
  関連プロセス群への presence-only scan とする。shell history / terminal scrollback は読まない。
  scan が完了できない（`/proc` 読み取り不可・関連プロセス特定不可・権限不足）場合は
  `unknown`（fail-closed）とし、`absent_verified` は positive scan が完了した場合にのみ返す。
  legacy override `SRRS_LAT_ARGV_CREDENTIAL` も、`present` 以外の値（`absent` を含む）は
  `unknown` に寄せ、単一の boolean override から `absent_verified` を断定しない。
- host mode の real npm registry signature / provenance attestation 検証と
  installed entrypoint / preload / hook command の実 sha256 計算、provider-side retention の
  real machine verification は follow-up Issue（#1351）の対象であり、それまでは host 実行時に
  これらの evidence field が `unknown` / `None` のまま fail-closed になる（false-green にはならない）。
- #1261 は本 PR（#1352）の fix_delta 反映後も、`pnpm run security:session-recording:host` /
  `pnpm run policy:check` の host 実行ログが未添付である限り open のままとし、この PR 単体では
  close しない。host 実行証跡が添付された時点で改めて #1261 の close 判断を行う。
- `security:session-recording` は CI / policy / smoke bundle であり、host readiness proof ではない。
- `security:session-recording:host` は pre-activation local preflight 専用であり、
  `SRRS_*` override を reject する。generic CI required gate には組み込まない。
  deterministic な CI gate には `security:session-recording:fixture` を使う。
  `security:session-recording:runtime` は fixture alias 互換面であり、host proof として扱わない。
- host preflight の blocked / deny は CI required gate failure ではなく、
  real pilot blocked evidence として扱う。

```yaml
real_pilot_preflight_exit_semantics:
  blocked:
    exit_code: 1
    meaning: required_activation_evidence_or_cleanup_still_missing
  fail_closed:
    exit_code: 2
    meaning: malformed_unknown_fixture_or_non_host_input
```

### Parent #1153 Child の prerequisite 参照（前提条件の参照）

parent #1153 の以下の Child は、`LATITUDE_PILOT_EXCEPTION_V1`（A1 decision marker）を
prerequisite として参照する。本 Issue（#1220）は A1 decision gate の admission までを担当し、
#1221〜#1224（Child B / C1 / E など）の実装は行わない。

| Child | 責務 | A1 decision への依存 |
|---|---|---|
| Child B | capture capability verdict の確定 | real pilot 評価は A1 が `approve_timeboxed_real_pilot` のときのみ |
| Child C1 | private observation source の adapter 接続 | real trace export は A1 activation 後に限定 |
| Child E | Cloud pilot metrics と adopt/withdraw decision | real Cloud pilot は A1 activation gate 通過が前提 |

## Public comment boundary（公開コメント境界）

- `agent_session_manifest/v1` の manifest 本文は Issue / PR comment に出さない。公開コメントでは `artifact_digest`、`artifact_url`、`schema_ref`、`validation_verdict` などの opaque ref のみ許可する。
- `agent_run_report/v1` / `agent_retro_index/v1` は #935 schema/redaction validator と #937 exact marker upsert guard を通過した後のみ conditional public comment 可とする。
- #934 は docs-only boundary cleanup であり、#936 の lifecycle 導線も含めて live public posting はまだ禁止する。merge 時点で許可されるのは dry-run 設計と public-safe 境界の固定のみ。
- public comment posting は trusted context のみで行う。`pull_request_target` では実行せず、top-level read-only baseline を維持したまま posting job に限って `issues: write` / `pull-requests: write` を付与する。
- hook は diagnostic / prevention layer であり security boundary ではない。report/index の public-safe 判定正本は post-run validator と posting guard に置く。
- `artifact_url` は opaque ref ではあるが canonical evidence ではない。retention-limited / auth-dependent / non-canonical locator として扱い、永続的な識別は `artifact_digest`、`schema_ref`、`validation_verdict`、workflow run URL、comment marker に寄せる。
- 過去の manifest comment template や pilot wording は historical / non-current / not live public posting として扱い、current live public posting 許可の根拠にしない。

---

## Kill Switch 手順

`trigger_conditions` のいずれかに該当した場合、以下の順序で Kill Switch を実行する。

### ステップ 1: session 記録ツールの即時停止

session 記録ツール（EntireCLI 等）が起動していれば、即座に停止する。
auto-start 設定がある場合はそれを無効化する。

```bash
# EntireCLI が動作中か確認
ps aux | grep -i entire | grep -v grep

# または session recording に関連するプロセスを確認
ps aux | grep -E "(checkpoint|session-record|entire)" | grep -v grep
```

### ステップ 2: Git hook の確認と無効化

session 記録が Git hook 経由で実行されている場合、hook を停止する。

```bash
# hook ファイルの存在確認
ls -la .git/hooks/ | grep -E "pre-commit|pre-push|post-commit"

# hook の内容を確認
cat .git/hooks/pre-push 2>/dev/null || echo "pre-push hook: なし"
cat .git/hooks/pre-commit 2>/dev/null || echo "pre-commit hook: なし"

# session recording に関連する hook を無効化（chmod で実行権限を除去）
# 例: chmod -x .git/hooks/pre-push
```

### ステップ 3: push remote の visibility 確認

checkpoint が push される remote の visibility を検証する。

```bash
# remote の一覧確認
git remote -v

# pushRemote の確認
git config --get remote.origin.pushurl 2>/dev/null || echo "pushRemote: 未設定（origin を使用）"

# insteadOf / pushInsteadOf の確認
git config --list | grep -E "url\.|insteadOf|pushInsteadOf"

# GitHub リポジトリの visibility を確認
gh repo view <owner/repo> --json visibility --jq '.visibility'
# 期待値: PRIVATE
# PRIVATE 以外の場合は Kill Switch を継続する
```

### ステップ 4: public checkpoint branch の削除

`entire/checkpoints/v1` 等の checkpoint branch が public リポジトリに存在する場合、削除する。

```bash
# remote の checkpoint branch を確認
git ls-remote origin | grep -E "entire|checkpoint|session"

# public checkpoint branch が存在する場合は削除
# git push origin --delete entire/checkpoints/v1
# ※ 削除前に必ず人間が確認すること
```

### ステップ 5: public surface への raw transcript 混入確認

GitHub issue comment / PR body に raw transcript が混入していないか確認する。

```bash
# 直近の GitHub issue comment を確認（要人間確認）
gh issue list --repo <owner/repo> --state all --json number,title --limit 20

# PR body / comment に transcript / local_file が含まれていないか
# 自動削除は行わず、人間が確認・編集する
```

### ステップ 6: Secret の revoke / rotate

session transcript から Secret が漏洩した可能性がある場合、対象 Secret を即時 revoke する。

```bash
# 漏洩の可能性がある Secret を特定
# docs/dev/secret-policy.md の各区分の「漏洩時手順」を参照

# checkpoint_token の場合:
# 1. 対象サービス（EntireCLI 等）で token revoke
# 2. 新 token に rotate
# 3. session データの公開範囲を確認・非公開化

# 漏洩確認が取れたら required_end_state の leaked_credentials_rotated_or_revoked: true を記録
```

### Kill Switch 完了確認

全ステップ完了後、以下の検証コマンドで `required_end_state` を確認する（「検証コマンド」セクション参照）。

---

## 検証コマンド

session 記録に関するリスクを確認するための検証コマンド一覧。

### 1. remote の branch 一覧確認（checkpoint branch 検出）

```bash
git ls-remote origin | grep -E "entire|checkpoint|session" || echo "checkpoint branch: なし"
```

### 2. Git hook 残存確認

```bash
ls -la .git/hooks/ 2>/dev/null | grep -E "pre-commit|pre-push|post-commit" || echo "recording hooks: なし"
# hook が存在する場合は内容を確認する
cat .git/hooks/pre-push 2>/dev/null | grep -i -E "entire|checkpoint|session|recording" || echo "recording in pre-push: なし"
```

### 3. remote config / pushRemote / insteadOf 確認（リモート設定確認）

```bash
git remote -v
git config --get remote.origin.pushurl 2>/dev/null || echo "pushRemote: 未設定"
git config --list | grep -E "url\.|insteadOf|pushInsteadOf" || echo "insteadOf: なし"
```

### 4. GitHub comment surface 確認（GitHub コメント面確認）

public GitHub comment に session transcript / local_file が混入していないかを確認する。

```bash
# 最新の GitHub issue comment を確認（要人間確認）
gh issue list --repo squne121/loop-protocol --state open --json number,title --limit 10
# PR comment の確認
gh pr list --repo squne121/loop-protocol --state open --json number,title --limit 10
```

---

## secrets_mode 遷移時の対応フロー

`docs/dev/secret-policy.md` の Decision Gate を通過した後、以下のフローで対応する。

```
secrets_mode 変化（none → 非 none）
  |
  v
[RP-1] 本文書の session_recording_policy/v1 YAML を更新
  - derived_from_secret_policy.current_secrets_mode を更新
  - 対応 taxonomy_mapping の session_recording_allowed / checkpoint_push_allowed を確認
  |
  v
[RP-2] Kill Switch trigger_conditions を確認
  - checkpoint_token_present が true になった場合は Kill Switch を発動
  - session 記録ツールが未導入であれば trigger 不要
  |
  v
[RP-3] 検証コマンドを実行
  - git ls-remote / hook 確認 / remote-config 確認 / comment surface 確認
  |
  v
[RP-4] required_end_state を GitHub Issue コメントに記録
  - 人間が承認してから session 記録を再開（auto_push_sessions_allowed は常に false）
```

---

## 運用導線

### いつ checker を実行するか

以下のファイルを変更する PR では、必ず checker を実行して結果を PR 本文に記録する。

- `docs/dev/session-recording-policy.md`（本文書）
- `docs/dev/secret-policy.md`
- `docs/schemas/agent-session-manifest.schema.json`
- `.claude/scripts/check_session_recording_policy.py`
- session 記録 / checkpoint / EntireCLI / Claude hook / Secret 関連設定

```bash
uv run --locked python3 .claude/scripts/check_session_recording_policy.py docs/dev/session-recording-policy.md
```

### どこに記録するか

PR 本文または GitHub Issue コメントに以下の YAML を記録する。

```yaml
SESSION_RECORDING_POLICY_VERDICT:
  checker: pass | fail
  secret_policy_consistent: true | false
  manifest_policy_consistent: true | false
  kill_switch_triggered: true | false
  kill_switch_required_end_state_recorded: true | false  # Kill Switch 発動時のみ
  human_review_required: true | false
```

Kill Switch を実行した場合は `required_end_state` の達成状況も GitHub Issue コメントに記録する。

### 運用導線の実装状況

本文書（`session_recording_policy/v1`）は policy 宣言と checker スクリプトまで完成しており、
以下の導線の実装状況を示す。

| 導線 | 状態 | 担当 Issue |
|---|---|---|
| CI 連動（`pnpm policy:check` / python-test workflow）| 実装済み | #324 |
| Claude hook（Stop/SubagentStop での自動実行）| 実装済み | #325 |
| Skill（操作手順の標準化・手動呼び出し）| 実装済み | #326 |
| 人間導入手順書（onboarding）| 実装済み | #245 |
| manifest producer hook wiring（Stop/SubagentStop/PostToolUse）| 実装済み | #402 |
| GitHub Actions artifact workflow（private artifact のみ）| 実装済み | #402 |
| coordinator hook（guard → producer 直列化、blocked attempt 抑止）| 実装済み | #651 |
| pilot smoke test（Kill Switch 動作確認）| 未実装 | #246 |

> **重要**: deterministic manifest producer、manifest schema validation path、
> no-push / private checkpoint / local-only verifier、Kill Switch runtime smoke test、
> Skill 手順 (#326)、pilot smoke test (#246) が完了するまで、
> full transcript を生成する session 記録ツールの pilot / 本番運用を開始しないこと。

---

## manifest producer lifecycle（Hook + CI・生成ライフサイクル）

manifest producer（`scripts/generate-session-manifest.mjs`）は以下の自動 lifecycle で呼び出される。

### Claude Code hook lifecycle（Claude Code フックの流れ）

`.claude/settings.json` の hooks セクションで以下のイベントが wiring されている。

> **設計原則（#651）**: Stop / SubagentStop には `session_manifest_coordinator.sh` のみを wiring する（single coordinator only）。
> `session_recording_policy_guard.sh` や `generate_session_manifest_from_hook.mjs` を Stop / SubagentStop の sibling hooks として直接追加してはならない。
> **session manifest hook は best-effort telemetry であり、AI agent 作業をブロックしない**（coordinator は常に exit 0 で終了する）。

| イベント | hooks |
|---|---|
| Stop | `session_manifest_coordinator.sh`（single coordinator only） |
| SubagentStop | `session_manifest_coordinator.sh`（single coordinator only） |
| PostToolUse | `generate_session_manifest_from_hook.mjs`（matcher で対象 tool を限定） |
| SessionStart | 対象外（context 混入リスクが高いため除外） |

coordinator（`session_manifest_coordinator.sh`）の動作:
- stdin を一度だけ読み取り、guard と producer の両方に同一 payload を渡す
- guard（`session_recording_policy_guard.sh`）を producer より先に実行する（順序固定）
- guard が exit 0 の場合のみ producer を実行する（guard failure → 標準 manifest を生成しない）
- guard failure / producer failure のいずれでも exit 0 で終了し、Stop / SubagentStop をブロックしない
- `stop_hook_active: true` の場合は producer を呼ばず即時 exit 0（short-circuit）
- 失敗理由は stderr にのみ出力する（stdout は空）

hook wrapper（`generate_session_manifest_from_hook.mjs`）の動作:
- stdin の hook JSON を読み取り、producer CLI 引数へ変換する（hook_event_name / session_id / tool_name / tool_use_id / agent_id を抽出）
- stdout は完全に沈黙させる（manifest JSON を stdout に出さない）
- `transcript_path` / `cwd` の絶対パスを public output に含めない
- artifact file へ atomic write（temp + rename）を行う
- 同一 stable key（`hookEventName:toolName:ledgerPhase`）の artifact が既にあれば duplicate skip する
- **best-effort artifact generation**: producer 失敗 / artifact 書き込み失敗時は `exit 0` でセッションをブロックしない（stderr にログを出力）

> **注意（#412 境界）**: artifact に Secret が混入しない保証は `#412` 完了まで **保留**。
> 現状は `secrets_mode: none` 前提で運用する。
> `private artifact` は legacy visibility enum 名であり、secret-safe を意味しない。ここでいう `private artifact` は
> 「Issue / PR comment ではない retention-limited non-comment surface」を指す。
> **public repo では artifact は REST API 経由で公開アクセス可能**。manifest / report / index の content は public-safe contract
> （絶対パスなし、token なし、transcript 本文なし、full command output なし）を満たすこと。

### GitHub Actions CI lifecycle（GitHub Actions CI の流れと実行順）

`.github/workflows/session-manifest.yml` が `push` / `pull_request` / `merge_group` trigger で実行される。

| 設定項目 | 値 |
|---|---|
| trigger | `push` + `pull_request` + `merge_group`（`pull_request_target` は不使用） |
| permissions | `contents: read`（read-only、write 権限なし） |
| persist-credentials | `false` |
| artifact upload | `actions/upload-artifact@v6`、`retention-days: 7`、`if-no-files-found: error` |
| artifact name prefix | `agent-session-manifest` |

### required check operational contract (#432・運用契約)

manifest validation gate を main の merge blocker にする場合、required check の exact context は
`agent-session-manifest / validate-generated-artifact` とする。

| 項目 | 値 |
|---|---|
| workflow name | `agent-session-manifest` |
| job_id | `validate_generated_artifact` |
| job name | `validate-generated-artifact` |
| required check context | `agent-session-manifest / validate-generated-artifact` |

`phase_instance_id` は current schema で以下の 2 形式を受け付ける。

1. `issue-<N>:<main_loop_phase>:<seq>`
   - local / issue-bound / AI-driven run 用
   - 例: `issue-934:impl:001`
2. `ci:<producer_slug>:<run_id>:<run_attempt>`
   - GitHub Actions CI run 用
   - `producer_slug` は workflow name そのものではなく schema-safe fixed slug を使う
   - 例: `ci:session-manifest:123456789:1`

CI producer は `ci:session-manifest:<run_id>:<run_attempt>` を current schema reality として使う。`issue-432:impl:<seq>` への固定変換は current policy ではない。

required check の SSOT は **branch protection** とする（ruleset PATCH API が 404 を返すため）。
ruleset が利用可能になった場合は ruleset 側に required checks を移行し、branch protection fallback を削除する。

<!-- verification-anchor: branch protection|ruleset 日本語検証アンカー -->

### required check 設定と確認

1. `agent-session-manifest / validate-generated-artifact` が CI で一度成功してから required check に登録する。
   GitHub は過去 7 日以内に成功していない check context を required check 候補として扱えないことがある。
2. branch protection の `required_status_checks` を SSOT として required check を登録する（ruleset の PATCH API が 404 を返すため branch protection を fallback とする）。

確認コマンド:

```bash
# ruleset 側の確認（admin 権限がある場合）
gh api repos/squne121/loop-protocol/rulesets

# branch protection の確認（本 repo の SSOT）
gh api repos/squne121/loop-protocol/branches/main/protection/required_status_checks \
  --jq '.checks[]?.context // .contexts[]?'
# 期待値: agent-session-manifest / validate-generated-artifact が含まれること
```

### required check 登録済み証跡（2026-05-31）

branch protection の required_status_checks に `agent-session-manifest / validate-generated-artifact` を登録済み。

```
# gh api repos/squne121/loop-protocol/branches/main/protection/required_status_checks --jq '.checks[]?.context // .contexts[]?'
typecheck
lint
test
build
python-test
agent-session-manifest / validate-generated-artifact
```

ruleset PATCH API（id: 16796903）は 404 を返したため、branch protection を SSOT として登録した。
ruleset と branch protection の required checks が diverge する場合は人間が調整する。

<!-- verification-anchor: branch protection|ruleset 日本語検証アンカー -->

### admin stop condition（管理者停止条件）

ruleset / branch protection の参照または更新に必要な admin 権限がない場合は、required check enforcement の実設定を進めず stop condition とする。
その場合は docs と workflow だけを更新し、`gh api .../rulesets` または
`gh api .../required_status_checks` を実行できなかった事実を Issue / PR に記録して人間へ引き継ぐ。

<!-- verification-anchor: required_status_checks|admin stop condition|stop condition admin 日本語検証アンカー -->

---

## artifact channel と public-safe content contract（成果物チャネルと公開安全契約）

manifest の出力先は **retention-limited GitHub Actions artifact** とする。
「private artifact channel」は「Issue / PR comment ではない非コメント面（artifact として保持期間付きで管理）」を指す。

**重要**: public repo（`squne121/loop-protocol`）では、GitHub Actions artifact は
`actions/artifacts` REST API 経由で誰でもダウンロード可能である。
「private artifact」という語は "Secret が含まれない" ことを保証するものではなく、
「公開コメント・git history ではない管理された配布面」であることを示す用語である。
`artifact_url` は artifact / workflow run / repository retention に依存する auth-dependent な locator であり、
canonical な永続証跡ではない。永続 identity は `artifact_digest` と comment marker / workflow run URL に寄せる。

### manifest content の public-safe contract（manifest 公開安全契約）

manifest JSON が GitHub Actions artifact として公開リポジトリに格納される以上、
**manifest content は public-safe でなければならない**。以下を contract として定める。

| 項目 | 要件 | 理由 |
|---|---|---|
| 絶対パス（`/home/`, `/Users/`, `C:\` 等） | 含めない | 開発環境のパスが漏洩する |
| token-like string（40 文字以上の hex/base64） | 含めない | API key / git token が漏洩する |
| transcript 本文（会話内容） | 含めない | session 内容が公開される |
| Secret 値（API key, password, token） | 含めない（#412 担当） | Secret 漏洩 |

manifest JSON に含まれる情報（例: repository, phase, actor-type, evidence-source-ref, timestamp）は
public-safe な構造化メタデータであり、上記の禁止項目は producer CLI が生成しない設計になっている。

Secret 境界の完全な保護は `#412` が担当し、本スコープ（#402）では保証しない。

以下のチャネルへの manifest 本文の出力は **禁止**。

| チャネル | 禁止理由 |
|---|---|
| workflow log（`echo` / `cat` 等） | public log に manifest content が混入する |
| Issue / PR comment（`gh issue comment` / `gh pr comment`） | public surface に manifest が露出する |
| git commit（commit message / blob） | git history に manifest が残る |
| stdout（hook wrapper 経由） | hook stdout は Claude Code に取り込まれる可能性がある |

manifest を参照する必要がある場合は、GitHub Actions の artifact download を経由する。

---

## #412 との境界

本文書のスコープ（#402）は **hook / CI wiring と private artifact channel の設計**に限定する。

upstream security boundary として `#412` が担当する範囲は以下のとおり。

| 境界 | 担当 |
|---|---|
| Secret 値を manifest producer pipeline に到達させない upstream 統制 | `#412` |
| Secret scan / token rotation / rotate-on-leak 手順 | `#412` + `docs/dev/secret-policy.md` |
| manifest validation gate を CI required check に昇格する enforcement | 別 follow-up |

`#412` が完了するまでは、manifest には Secret を含まない前提で運用する（`secrets_mode: none`）。
`#412` 完了まで **Secret 混入は保証外** であり、Safety Claim Matrix の "Not controlled" 列に該当する。

---

---

## GitHub Secret 境界ポリシー（#412 追加）

`#412` の実装により、measurement pipeline（Claude Code hooks / GitHub Actions workflow）は
**GitHub Secret は値を取得しない**。presence boolean と metadata のみを扱う。

### 基本原則

- **Secret 値は取得しない**: `printenv`、`env`、`gh secret`、`export -p`、`.env` ファイル読み取りは
  `secret_boundary_guard.sh`（PreToolUse hook）によって block される。
- **presence / metadata のみ扱う**: Secret が存在するかどうか（boolean）と、
  kind / category などの非機密 metadata のみを pipeline に通す。
- **value を pipeline に渡さない**: manifest / log / artifact に Secret 値・そのハッシュ・
  base64 / hex エンコード形式のいずれも含めない。
- **fail-closed**: guard が malformed stdin を受け取った場合は exit 2（block）で fail closed する。

### 実装（#412 完了後）

| 境界 | 実装手段 |
|---|---|
| Claude Code PreToolUse hook | `.claude/hooks/secret_boundary_guard.sh` — 高リスク command / sensitive path を exit 2 で block |
| `.claude/settings.json` deny | `.env`、`secrets/**`、credential path、`gh secret`、`printenv` 等への deny エントリ |
| CI workflow | `session-manifest.yml` に `secrets.` 参照なし、`pull_request_target` なし、`contents: read` のみ |
| manifest schema | `secret_policy.value_exposed: false` フィールドで状態を機械的に記録 |

### 関連テスト

- `.claude/hooks/tests/test_secret_boundary_contract.py` — sentinel fixture・structural・guard 動作検証


## 関連文書

- `docs/dev/secret-policy.md` — Secret Inventory と no-secret 運用境界（`secret_policy/v1` SSOT）
- `docs/schemas/agent-session-manifest.schema.json` — `agent_session_manifest/v1` JSON Schema SSOT
- `docs/dev/agent-skill-boundaries.md` — SubAgent / Skill 責務境界（Hook-based Ledger 設計含む）
- `docs/dev/runtime-verification-policy.md` — 動作検証 AC 運用ポリシー
- `CLAUDE.md` — プロジェクト入口
- `.claude/rules/project-constitution.md` — 運用ルールの正本
- `.claude/scripts/check_session_recording_policy.py` — policy 構造検証スクリプト（11 項目）
- `.claude/hooks/generate_session_manifest_from_hook.mjs` — hook wrapper（producer 呼び出し）
- `.github/workflows/session-manifest.yml` — CI artifact workflow
- Issue #136 — session 記録ツール導入判断（親 Issue）
- Issue #241 — `secret_policy/v1` SSOT 化（PR #317 完了）
- Issue #243 — `agent_session_manifest/v1` schema SSOT 化（PR #314 完了）
- Issue #245 — session 記録ツール人間導入手順書（実装済み / PR #347）
- Issue #246 — pilot smoke test（実装予定）
- Issue #402 — hook + CI wiring 実装（本 Issue）
- Issue #412 — upstream security boundary（Secret 管理）

## #1221 agent observation capability boundary（agent 観測能力境界）

`agent_observation_capability/v1` の capture capability verdict は synthetic evidence のみで固定する正本を
`docs/dev/agent-observation-capability.md` に置く。本節はその hook coexistence / canonical gate 境界を要約する。

- Hook（async Stop hook を含む）は diagnostic / prevention レイヤーであり canonical gate ではない。
  canonical gate は post-run verifier である。
- async Stop hook の存在・hook exit 0・hook presence のいずれも PASS の証明にはならない。
- hook 共存の PASS 条件は以下の closed contract を満たすこととする:

```yaml
hook_coexistence_pass_requires:
  expected_handlers_fired_once: true
  duplicate_finalization_absent: true
  duplicate_upload_absent: true
  async_hook_not_used_as_gate: true
  post_run_verifier_observed_final_state: true
  runtime_event_and_capture_artifact_correlated: true
  hook_exit_zero_not_authoritative: true
  raw_values_emitted: false
```

- #1220 の `LATITUDE_PILOT_EXCEPTION_V1` A1 decision gate（既定 `approve_synthetic_only` /
  `blocked_until_activation`）は本節で変更しない。
- `docs/dev/secret-policy.md` は変更しない。
- real prompt / real trace export / real Cloud pilot は引き続き禁止であり、real runtime evidence は
  pilot exception が `approve_timeboxed_real_pilot` になるまで blocked とする。
