---
id: secret-policy
status: stable
related_issue: "#241"
related_issues:
  - "#136"
  - "#242"
created: "2026-05-24"
---

# Secret Inventory と no-secret 運用境界 (SSOT)

本文書はこのプロジェクトで管理しうる Secret を 5 区分に分類し、no-secret 前提の運用境界を定める。
AI Agent が Secret 関連の判断を都度推論せずに参照できる唯一の正本（SSOT）である。

session 記録ツールの Kill Switch 手順や `secrets_mode` 遷移時の session 記録可否については
`docs/dev/session-recording-policy.md`（#242）を参照する。

---

## 機械可読メタデータ (secret_policy/v1)

```yaml
secret_policy:
  schema: secret_policy/v1
  repository: squne121/loop-protocol
  checked_ref: "25925201417c827bc55d10c30d57af484fe769b1"
  current_secrets_mode: none
  current_project_secret_present: false
  publish_secret_present: false
  app_runtime_secret_present: false
  agent_local_secret_present: false
  checkpoint_token_present: false
  vite_sensitive_env_allowed: false
  public_full_transcript_allowed: false
  generated_at: "2026-05-24"
  related_issues:
    - "#241"
    - "#242"
```

---

## Secret Inventory（5 区分）

### 1. `current` — 現時点で repo に存在する Secret

| 項目 | 内容 |
|------|------|
| **現状** | **なし**。CI は `permissions: contents: read` のみ。deploy job・GitHub Secrets 利用なし |
| **発生条件** | GitHub Actions に deploy ステップを追加した瞬間、または外部サービス連携を追加した時点 |
| **取り扱いルール** | 現在このカテゴリに実 Secret は存在しない。発生次第、以下の区分に分類して本文書を更新する |
| **漏洩時手順** | （現在 Secret なし）将来発生した場合は、漏洩区分に応じた手順（後述）を実施する |

---

### 2. `publish_secret` — publish/deploy 用 Secret（将来発生しうる）

publish_secret は **release integrity secret** として取り扱う。
itch.io butler (`BUTLER_API_KEY`)、Cloudflare Pages (`CLOUDFLARE_API_TOKEN`)、
Vercel、Netlify 等が該当する。
GitHub id-token (OIDC) は stored secret を持たないため `deploy_credential_boundary` として別管理する（後述）。

```yaml
publish_secret_examples:
  - BUTLER_API_KEY
  - CLOUDFLARE_API_TOKEN
  - VERCEL_TOKEN
  - NETLIFY_AUTH_TOKEN

deploy_credential_boundaries:
  github_pages_oidc:
    stored_secret: false
    required_permissions:
      - "pages: write"
      - "id-token: write"
    release_integrity_sensitive: true
```

| 項目 | 内容 |
|------|------|
| **現状** | **なし**。CI の deploy job は存在しない |
| **発生条件** | ゲームを web/store へ公開する deploy pipeline を追加した時点 |
| **取り扱いルール** | GitHub Actions Secrets にのみ保存し、ローカルファイルや `.env` には置かない。CI 導入時は**人間承認必須**。Secret の scope を最小化（リポジトリ単位、環境ごとに分離）する |
| **漏洩時手順** | 1. 対象サービスで即時 revoke（API key 無効化） 2. 新しい Secret で rotate（再発行・CI 更新） 3. GitHub Actions Secrets から旧 Secret を削除 4. コミット履歴に混入した場合は `git filter-repo` で除去 5. **release channel を一時 freeze する** 6. **直近 deploy artifact / build hash / 配布先ページを検証する** 7. **last known good build に rollback 可能か確認する** 8. **CI run log / artifact / deployment history に secret 値が出ていないか確認する** 9. **影響が否定できるまで新規 publish を禁止する** 10. 影響範囲（公開リソースへの不正アクセス等）を調査・報告 |

---

### 3. `app_runtime_secret` — アプリ実行時に必要な API key 等

| 項目 | 内容 |
|------|------|
| **現状** | **現状想定なし**。ゲームはローカル実行の静的フロントエンドであり、外部 API 呼び出しを行わない |
| **発生条件** | ゲームプレイ中に外部 API（スコアボード、課金、認証等）を呼び出す機能を追加した時点 |
| **取り扱いルール** | `VITE_*` 環境変数は **client bundle に露出する（Vite 仕様）ため、sensitive な値を絶対に設定しない**。詳細は「VITE_ 環境変数の取り扱い」セクションを参照。runtime secret が必要になった場合は、バックエンド proxy またはサーバーサイド処理を経由する設計にする |
| **漏洩時手順** | 1. 対象 API サービスで即時 revoke 2. rotate（新 Secret 発行・アプリ更新） 3. client bundle に混入している場合は再ビルド・再デプロイ 4. 影響範囲調査 |

---

### 4. `agent_local_secret` — AI Agent ローカル設定

| 項目 | 内容 |
|------|------|
| **現状** | `.claude/settings.local.json`、`*.local` ファイルが `.gitignore` で除外済み。Latitude API key は `#1153` が「追加済み」と記述するが、実 Secret 存在確認・policy 整合・runtime 安全検証は `#1157` で実施する |
| **発生条件** | ローカル AI Agent（Claude Code 等）の設定ファイルや、API key が記述された local override ファイルが生成された時点。Latitude telemetry 統合（`LATITUDE_API_KEY`）はこのカテゴリに分類される |
| **取り扱いルール** | `.gitignore` の除外パターン（`*.local`、`.claude/settings.local.json`）を維持する。これらのファイルを `git add` しない。共有が必要な設定は `.json.example` 等のテンプレート経由で行う。**Latitude API key を本 repo のどのファイルにも書き込まない** |
| **漏洩時手順** | 1. コミット履歴から `git filter-repo` で除去 2. 対応する API key を revoke / rotate 3. `.gitignore` の設定を再確認 |

#### Latitude API key の分類根拠 (#1157)

Latitude API key は `agent_local_secret` に分類される（taxonomy_mapping により `secrets_mode: app_secret`）。

```yaml
latitude_api_key_classification:
  secret_category: agent_local_secret
  secrets_mode_when_present: app_secret
  real_development_session_allowed: false
  policy_consistent: true
  containment_state: pending_verification
  note: >
    Latitude credential の runtime 存在確認・containment 完了・real session 許可は
    別 Child Issue の人間 Decision を要する（docs/dev/session-recording-policy.md 参照）。
    本 Inventory の current_secrets_mode: none は Latitude API key が
    本 repo の tracked file・git history に存在しないことを前提とする。
```

---

### 5. `checkpoint_token` — session 記録ツール用 token

| 項目 | 内容 |
|------|------|
| **現状** | **なし**。session 記録ツール（EntireCLI 等）は未導入 |
| **発生条件** | EntireCLI 等の session 記録ツールを導入し、checkpoint を remote に push する設定にした時点（`ENTIRE_CHECKPOINT_TOKEN` 等） |
| **取り扱いルール** | ローカル環境変数または `*.local` ファイルに限定して保持する。remote push 先は必ず private リポジトリにする。`secrets_mode` が `none` 以外に遷移する場合、`docs/dev/session-recording-policy.md` の Kill Switch 手順を先行して確認する |
| **漏洩時手順** | 1. 対象サービスで token revoke 2. 新 token に rotate 3. session データの公開範囲を確認・非公開化 4. `docs/dev/session-recording-policy.md` の手順に従って記録設定を見直す |

---

## Latitude real pilot 例外 Decision — LATITUDE\_PILOT\_EXCEPTION\_V1 (#1220)

Child A0（#1157）の containment / runtime inventory / Kill Switch 完了後に、
Latitude credential を保持した real pilot 例外を認めるかを **人間 Decision** として固定する。
この Decision の machine-readable 正本は、本 repo policy YAML（下記 decision marker ブロック）である。
parent #1153 と issue #1220 はこの Decision の参照面であり、#1153 の GitHub comment は
`decision_ref` として扱う。**GitHub comment 単独を唯一の正本にしない。**

real pilot activation の gate は Stop hook ではなく、session 開始前の host verifier JSON
（`.claude/scripts/check_session_recording_runtime_safety.py --json --execution-profile host`）を正本とする。
hook は diagnostic / prevention layer であり、activation の証明にはしない。

明示的な real pilot 承認が存在しない限り、実装既定値は `approve_synthetic_only` とし、
real pilot activation は `blocked_until_activation` のまま維持する。

```yaml
LATITUDE_PILOT_EXCEPTION_V1:
  # decision は closed enum。明示的な real pilot 承認が無い限り approve_synthetic_only を既定とする。
  decision: approve_synthetic_only
  decision_enum:
    - reject_and_uninstall
    - approve_synthetic_only
    - approve_timeboxed_real_pilot
    - defer
  decision_source:
    canonical_surface: repo_policy_yaml
    parent_issue: "#1153"
    issue: "#1220"
    decision_ref: "#1153"      # GitHub comment は decision_ref であり唯一の正本ではない
    source_digest: required     # secret-policy.md の sha256 を host verifier が記録する

  # --- API key 境界（AC3）---
  api_key_storage_boundary: agent_local_secret_local_only   # repo tracked file / git history へ書かない
  rotation_owner: repository_human_maintainer
  revoke_procedure: >
    1. Latitude Cloud で対象 API key を即時 revoke する。
    2. 新 key を rotate し、local-only (.claude/settings.local.json 等) にのみ保持する。
    3. argv / shell history / process env への露出有無を再点検する。
    4. host verifier を再実行し credential_state: absent を再確認する。
  leakage_response: >
    credential value が public surface / log / argv / remote trace に露出した可能性があれば、
    docs/dev/session-recording-policy.md の Kill Switch を発動し、revoke から rotate、
    2 回 scan までを記録する。

  # --- argv 露出と rotation（AC8）---
  argv_exposure_state: absent_verified      # absent_verified | possible | unknown
  credential_rotation_state: not_required   # not_required | required | completed_attested
  argv_key_passing: forbidden               # real key を argv (--api-key=...) に渡す手順を禁止する
  # argv_exposure_state が possible または unknown の場合:
  #   credential_rotation_state: required とし、real pilot activation を blocked にする。

  # --- provider-side retention / 削除検証（AC7）---
  remote_cleanup_state: unknown             # machine_verified | human_attested | unknown
  remote_cleanup_rule: >
    provider-side retention / delete verification が machine_verified でない場合は
    remote_cleanup_state を unknown（または unknown_blocking）とし、PASS にしない。
    human_attested は machine_verified の代替ではなく、activation は別途 blocked のままにする。

  # --- reject_and_uninstall 時の postcondition（AC9）---
  reject_and_uninstall_postcondition:
    quiescent_two_stage_required: true
    two_stage_scan_contract: >
      1 回目 scan の後に静止（新規起動経路と active process の停止）させ、2 回目 scan を行う。
      2 回の read-only scan 結果が同一かつ対象 process 不在のときのみ contained とする。
    record_two_scans: true

  # --- approve_timeboxed_real_pilot のときだけ必須になる fields（AC2 / AC5）---
  real_pilot_required_if_approved:
    activation_window: required
    max_eligible_runs: required
    allowed_issue_labels: required
    rollback_deadline: required
    stop_thresholds: required
    distribution:
      distribution_exact_version: required        # exact semver のみ。latest/next/canary/range/^/~ は不可
      lockfile_digest: required
      npm_audit_signatures_verified: required      # `npm audit signatures` で検証
      npm_registry_signature_verified: required
      tarball_sha256: required
      entrypoint_sha256: required
      preload_sha256: required
      hook_command_sha256: required

  # --- activation 拒否条件（AC5）---
  activation_denied_if:
    - unpinned_npx                # npx -y @latitude-data/claude-code-telemetry（version 無し）
    - unknown_provenance
    - unknown_destination
    - non_https
    - latitude_debug_enabled      # LATITUDE_DEBUG=1
    - argv_exposure_possible_or_unknown
    - remote_cleanup_state_unknown
    - required_field_missing

  # 既定 activation state（approve_synthetic_only のため）
  pilot_activation_state: blocked_until_activation
  synthetic_only_allows:
    - synthetic_fixture_validation
    - policy_validation
  synthetic_only_forbids:
    - real_prompt
    - real_trace_export
    - real_cloud_pilot
```

> **注意**: 上記 `decision` を `approve_timeboxed_real_pilot` に変更する場合は、
> `real_pilot_required_if_approved` の全 field を実値で埋め、`argv_exposure_state: absent_verified`、
> `remote_cleanup_state: machine_verified`、`distribution.*` の各 digest を確定させること。
> 1 つでも欠落・unknown・不正があれば host verifier は activation を `blocked_until_activation` に固定する。

---

## VITE_ 環境変数の取り扱い

Vite は `VITE_` プレフィックスを持つ環境変数を **client bundle に静的に展開する**（公式ドキュメント: [Env Variables and Modes](https://vitejs.dev/guide/env-and-mode.html)）。

**結論: `VITE_*` に sensitive な値を設定してはならない。**

- `VITE_*` はビルド成果物（JS bundle）に平文で埋め込まれ、ブラウザの DevTools で誰でも閲覧できる。
- API key、認証トークン、パスワード等の sensitive な値を `VITE_*` に設定することは **厳禁**。
- 公開しても問題ないアプリ設定値（例: 公開 URL、feature flag）のみ `VITE_*` を使用する。
- sensitive な値が必要な場合は、サーバーサイドまたはバックエンド proxy を経由する設計にする。

---

## Secret 発生時の Decision Gate

Secret カテゴリが `current: なし` から変化する前に、以下の Decision Gate を通過すること。

```yaml
decision_gate:
  required_when:
    - introducing_github_actions_secret
    - adding_deploy_job
    - adding_app_runtime_secret
    - enabling_checkpoint_token
    - allowing_full_transcript_storage
  approval_surface:
    - linked_github_issue_comment
    - linked_pr_comment
  required_fields:
    - secret_category
    - target_service
    - storage_location
    - rotation_owner
    - leakage_response
    - session_recording_impact
  approval_marker:
    label: state/approved
    comment_keyword: LGTM
```

参考: 手順フロー

```
Secret 発生 (新規・変更)
  |
  v
[DG-1] 区分の確認
  - publish_secret / app_runtime_secret / agent_local_secret / checkpoint_token のどれか？
  - 本文書の対応セクションを更新する
  |
  v
[DG-2] VITE_ チェック (app_runtime_secret の場合)
  - VITE_* に sensitive な値を設定していないか？
  - YES → 設計変更必須（バックエンド proxy 等）
  |
  v
[DG-3] secrets_mode の更新
  - secrets_mode を none → 該当区分に更新する
  - docs/dev/session-recording-policy.md の Kill Switch 手順を確認する (#242)
  |
  v
[DG-4] CI 導入 (publish_secret の場合)
  - 人間承認を得てから GitHub Actions Secrets に登録する
  - scope を最小化（リポジトリ単位、環境ごとに分離）
  |
  v
[DG-5] 本文書の更新
  - Secret Inventory の対応セクションを更新する
  - current_secrets_mode フィールドを更新する
```

---

## taxonomy_mapping — #242 session-recording-policy との対応

#242（`session-recording-policy.md`）が定義する `secrets_mode` の 4 値と、
本文書の 5 区分の対応関係を示す。

```yaml
taxonomy_mapping:
  none:
    description: "現時点で管理中の Secret が存在しない状態"
    maps_to_secret_inventory:
      - current  # current: なし = secrets_mode: none
  publish_secret:
    description: "publish/deploy 用 Secret が存在する状態"
    maps_to_secret_inventory:
      - publish_secret
  app_secret:
    description: "アプリ実行時 Secret またはローカル Agent Secret が存在する状態"
    maps_to_secret_inventory:
      - app_runtime_secret
      - agent_local_secret
      - checkpoint_token
  unknown:
    description: "分類が確定していない、または複数区分が混在する暫定状態"
    maps_to_secret_inventory:
      - current  # 状況不明の場合は unknown に分類し本文書で確認する

inventory_to_secrets_mode:
  current:
    mode_when_secret_absent: none
    mode_when_unknown: unknown
  publish_secret:
    mode: publish_secret
    fail_closed: true
  app_runtime_secret:
    mode: app_secret
    fail_closed: true
  agent_local_secret:
    mode: app_secret
    fail_closed: true
  checkpoint_token:
    mode: app_secret
    fail_closed: true
    rationale: "session recording credential; public/full transcript must be disabled"
```

**現在の `secrets_mode`: `none`**（Secret Inventory 全区分でリアル Secret が存在しないため）

---

## Current Audit Evidence

```yaml
current_audit_evidence:
  checked_at: "2026-05-24"
  repository_ref: "25925201417c827bc55d10c30d57af484fe769b1"
  result: "current_secrets_mode = none"
  commands:
    - "git ls-files | rg '(^|/)\\.env\\.local$|(^|/)\\.env\\..*\\.local$|settings\\.local\\.json|\\.local$' || true"
    - "rg -n 'secrets\\.|BUTLER_API_KEY|CLOUDFLARE_API_TOKEN|ENTIRE_CHECKPOINT_TOKEN|VERCEL|NETLIFY|DEPLOY' .github .claude docs package.json || true"
    - "rg -n 'VITE_.*(SECRET|TOKEN|KEY|PASSWORD|PRIVATE)' src .github docs package.json vite.config.* --glob '!docs/dev/secret-policy.md' || true"
```

---

## #242 未完了時の fail-closed 条件

`docs/dev/session-recording-policy.md` が未存在、または #242 が未完了の場合、
`checkpoint_token` / `full_transcript` / `public checkpoint branch` に関わる変更は
fail-closed とし、実装を停止する。

---

## 関連文書

- `docs/dev/session-recording-policy.md` — session 記録 Kill Switch policy (#242)
- `CLAUDE.md` — プロジェクト入口
- `.claude/rules/project-constitution.md` — 運用ルールの正本
