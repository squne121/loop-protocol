---
id: agent-observation-capability
status: stable
related_issue: "#1221"
parent_issue: "#1153"
created: "2026-06-28" # 本書はエージェント観測能力マトリクスの正本であり初版の作成日を示す
---

# エージェント観測能力マトリクス（SSOT）

本書は `agent_observation_capability/v1` の capture capability verdict マトリクスの SSOT である。Claude Code / Codex CLI / Google Antigravity の 3 つの surface について、verdict を SYNTHETIC evidence（synthetic fixture と read-only host inventory）のみで確定する。real telemetry も real Latitude pilot も real trace export も一切実行しない。

`unsupported` と `unverified` は失敗ではない。これらは Child C0（observation source / provenance / safety schema の admission）と Child C1（adapter）が参照する input availability シグナルとして扱う。

この契約を強制する機械検査は `check_session_recording_runtime_safety.py --capability-fixture <path>` であり、`tests/session_recording_runtime_safety.test.ts` を通じて `pnpm test` から駆動する。

## 契約

下記の YAML が機械可読な契約の正本である。

```yaml
agent_observation_capability/v1:
  schema: agent_observation_capability/v1
  evidence_mode: synthetic_only
  real_runtime_evidence: blocked_until_pilot_exception_approve_timeboxed_real_pilot
  verdict_enum: [supported, partial, unsupported, unverified]
  supported_predicate:
    runtime_event_observed: true
    capture_artifact_observed: true
    raw_values_emitted: false
  non_failure_verdicts: [unsupported, unverified]
  non_failure_meaning: child_c0_c1_input_availability
```

`supported` は `runtime_event_observed == true` かつ `capture_artifact_observed == true` かつ `raw_values_emitted == false` のときに限り成立する。`evidence_mode: synthetic_only` では信頼できる provenance は `synthetic_fixture` のみであり、`real_pilot_verified` は #1220 の `LATITUDE_PILOT_EXCEPTION_V1` gate が `approve_timeboxed_real_pilot` へ遷移し activation fields が machine-verified になるまで blocked のままとする。本書は #1220 の A1 decision gate 既定（`approve_synthetic_only` / `blocked_until_activation`）を変更しない。`docs/dev/secret-policy.md` も変更しない。

## hook 共存の PASS 契約

hook 共存（Latitude の async Stop hook と既存 coordinator hook の併存）に依存する surface は、以下の closed contract を満たすときに限り `supported` へ到達できる。async hook は gate ではなく、canonical gate は post-run verifier であり、hook の exit 0 は authoritative ではない。

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

## public-safety admission 契約

マトリクスへ projection する全ての evidence artifact は、以下の admission を満たす必要がある。

```yaml
public_safety:
  raw_values_emitted: false
  forbidden_field_scan: pass
  prompt_excerpt_present: false
  tool_io_excerpt_present: false
  local_absolute_path_present: false
  credential_value_present: false
  digest_is_over_public_projection_only: true
```

## surface 一覧

surface はちょうど 3 つであり、各 surface は closed enum から verdict をちょうど 1 つだけ持つ。

### Claude Code（クロードコードの surface）

Claude Code では user / project / local / managed_policy / plugin / skill / agent_frontmatter の各層を checked surface として棚卸しする。async Stop hook は診断・予防の層であって gate ではない。`supported` は runtime event と capture artifact が相関し、`hook_coexistence_pass_requires` を満たし、raw values を出力しないときに限り認める。synthetic な既定 verdict は、述語を満たす synthetic fixture が現れるまで `unverified` とする。

```yaml
surface: claude_code
verdict: unverified
checked_surfaces:
  - user
  - project
  - local
  - managed_policy
  - plugin
  - skill
  - agent_frontmatter
gate_model:
  async_hook_is_gate: false
  canonical_gate: post_run_verifier
  pass_requires: hook_coexistence_pass_requires
```

### Codex CLI（コーデックスの surface）

Codex CLI では canonical な feature key を `[features].hooks` とし、`codex_hooks` は legacy alias としてのみ扱う。`.codex/hooks.json` と `validate-codex-hooks.mjs` が drift している間、非 canonical key の間、project 層が untrusted の間は、Codex を `supported` にしない。

```yaml
surface: codex_cli
verdict: unsupported
canonical_feature_key: "[features].hooks"
legacy_alias: codex_hooks
supported_blocked_while:
  - codex_hooks_json_validator_drift
  - non_canonical_hook_key
  - project_layer_untrusted
```

### Google Antigravity（グーグル アンチグラビティの surface）

Google Antigravity では MCP 接続・IDE 起動・Artifacts 生成・browser recording のいずれも capture 証拠として数えない。capture artifact と runtime event の両方が観測され相関しない限り、Antigravity は `unverified` で固定する。

```yaml
surface: google_antigravity
verdict: unverified
non_capture_signals:
  - mcp_connection
  - ide_launch
  - artifacts_generation
  - browser_recording
supported_requires:
  capture_artifact_observed: true
  runtime_event_observed: true
```

## negative control 一覧

以下の synthetic な negative-control fixture は、unsafe な状態を `supported` へ昇格させてはならない。checker はこれらに対して `decision: deny` または `fail_closed` を返す。

- Claude の Stop hook が user 層と project 層で二重に発火する場合（duplicate finalization と duplicate upload）。
- Claude の async Latitude Stop hook が finalizer より後に完了する場合（async hook を gate として使用）。
- Claude の hook が trace artifact なしで exit 0 を返す場合（hook の exit 0 は authoritative ではない）。
- Codex の現行 hooks が validator と drift している場合。
- Codex が legacy の `codex_hooks` のみを持ち canonical key が不在の場合。
- Codex の project 層が untrusted の場合。
- Antigravity が MCP 接続のみで capture artifact を持たない場合。
- `supported` を主張するが runtime event が欠落している場合。
- `supported` を主張するが capture artifact が欠落している場合。
- evidence が raw values を出力している場合。
- latitude の floating な npx package で version が未 pin の場合。
- latitude の provenance が unknown の場合。

## 関連文書

- `docs/dev/session-recording-policy.md` — session 記録 Kill Switch policy と hook 境界の正本。
- `docs/dev/agent-run-report.md` — agent_run_report/v1 と Hook Boundary Policy の記述。
- `docs/dev/secret-policy.md` — Secret Inventory（本マトリクスでは変更せず projection のみ）。
- `.claude/scripts/check_session_recording_runtime_safety.py` — runtime safety と capability の checker。
- Issue #1153 — 親となる pilot tracker。
- Issue #1220 — LATITUDE_PILOT_EXCEPTION_V1 の decision gate。
