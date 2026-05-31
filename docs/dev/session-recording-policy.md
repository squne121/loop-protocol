---
id: session-recording-policy
status: stable
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
  github_issue_comment:
    agent_session_manifest_allowed: true
    raw_transcript_allowed: false
    source_kind_prohibited:
      - transcript
      - local_file
    rationale: >
      agent_session_manifest/v1 は公開コメントへの添付を許可するが、
      raw transcript / local_file の source_kind は禁止する。
      公開 GitHub コメントには session metadata summary のみを置く。
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
```

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

### 3. remote config / pushRemote / insteadOf 確認

```bash
git remote -v
git config --get remote.origin.pushurl 2>/dev/null || echo "pushRemote: 未設定"
git config --list | grep -E "url\.|insteadOf|pushInsteadOf" || echo "insteadOf: なし"
```

### 4. GitHub comment surface 確認

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
python3 .claude/scripts/check_session_recording_policy.py docs/dev/session-recording-policy.md
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
| pilot smoke test（Kill Switch 動作確認）| 未実装 | #246 |

> **重要**: deterministic manifest producer、manifest schema validation path、
> no-push / private checkpoint / local-only verifier、Kill Switch runtime smoke test、
> Skill 手順 (#326)、pilot smoke test (#246) が完了するまで、
> full transcript を生成する session 記録ツールの pilot / 本番運用を開始しないこと。

---

## manifest producer lifecycle（Hook + CI）

manifest producer（`scripts/generate-session-manifest.mjs`）は以下の自動 lifecycle で呼び出される。

### Claude Code hook lifecycle

`.claude/settings.json` の hooks セクションで以下のイベントが wiring されている。

| イベント | hooks（順序固定） |
|---|---|
| Stop | 1. `session_recording_policy_guard.sh`（policy guard、先行評価）→ 2. `generate_session_manifest_from_hook.mjs`（producer） |
| SubagentStop | 1. `session_recording_policy_guard.sh`（policy guard、先行評価）→ 2. `generate_session_manifest_from_hook.mjs`（producer） |
| PostToolUse | `generate_session_manifest_from_hook.mjs`（matcher で対象 tool を限定） |
| SessionStart | 対象外（context 混入リスクが高いため除外） |

hook wrapper（`generate_session_manifest_from_hook.mjs`）の動作:
- stdin の hook JSON を読み取り、producer CLI 引数へ変換する（hook_event_name / session_id / tool_name / tool_use_id / agent_id を抽出）
- stdout は完全に沈黙させる（manifest JSON を stdout に出さない）
- `transcript_path` / `cwd` の絶対パスを public output に含めない
- artifact file へ atomic write（temp + rename）を行う
- 同一 stable key（`hookEventName:toolName:ledgerPhase`）の artifact が既にあれば duplicate skip する
- **best-effort artifact generation**: producer 失敗 / artifact 書き込み失敗時は `exit 0` でセッションをブロックしない（stderr にログを出力）

> **注意（#412 境界）**: artifact に Secret が混入しない保証は `#412` 完了まで **保留**。
> 現状は `secrets_mode: none` 前提で運用する。
> "private artifact channel" とは「Issue / PR comment ではない非コメント面（retention-limited GitHub Actions artifact）」を指す。
> **public repo では artifact は REST API 経由で公開アクセス可能**。manifest content は public-safe contract（絶対パスなし、token なし、transcript 本文なし）を満たすこと。

`session_recording_policy_guard.sh` は Stop / SubagentStop で producer hook より前に評価される（順序固定）。

### GitHub Actions CI lifecycle

`.github/workflows/session-manifest.yml` が `push` / `pull_request` / `merge_group` trigger で実行される。

| 設定項目 | 値 |
|---|---|
| trigger | `push` + `pull_request` + `merge_group`（`pull_request_target` は不使用） |
| permissions | `contents: read`（read-only、write 権限なし） |
| persist-credentials | `false` |
| artifact upload | `actions/upload-artifact@v4`、`retention-days: 7`、`if-no-files-found: error` |
| artifact name prefix | `agent-session-manifest` |

### required check operational contract (#432)

manifest validation gate を main の merge blocker にする場合、required check の exact context は
`agent-session-manifest / validate-generated-artifact` とする。

| 項目 | 値 |
|---|---|
| workflow name | `agent-session-manifest` |
| job_id | `validate_generated_artifact` |
| job name | `validate-generated-artifact` |
| required check context | `agent-session-manifest / validate-generated-artifact` |

`phase_instance_id` は現行 producer contract が `issue-<N>:<phase>:<seq>` を要求するため、
CI では `run_id` と `run_attempt` から導出した 3 桁 seq を使って `issue-432:impl:<seq>` を生成する。
raw の `ci:<workflow>:<run_id>:<run_attempt>` は current schema / validator では受け付けない。

required check の SSOT は **branch protection** とする（ruleset PATCH API が 404 を返すため）。
ruleset が利用可能になった場合は ruleset 側に required checks を移行し、branch protection fallback を削除する。

<!-- verification-anchor: branch protection|ruleset -->

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

<!-- verification-anchor: branch protection|ruleset -->

### admin stop condition

ruleset / branch protection の参照または更新に必要な admin 権限がない場合は、required check enforcement の実設定を進めず stop condition とする。
その場合は docs と workflow だけを更新し、`gh api .../rulesets` または
`gh api .../required_status_checks` を実行できなかった事実を Issue / PR に記録して人間へ引き継ぐ。

<!-- verification-anchor: required_status_checks|admin stop condition|stop condition admin -->

---

## artifact channel と public-safe content contract

manifest の出力先は **retention-limited GitHub Actions artifact** とする。
「private artifact channel」は「Issue / PR comment ではない非コメント面（artifact として保持期間付きで管理）」を指す。

**重要**: public repo（`squne121/loop-protocol`）では、GitHub Actions artifact は
`actions/artifacts` REST API 経由で誰でもダウンロード可能である。
「private artifact」という語は "Secret が含まれない" ことを保証するものではなく、
「公開コメント・git history ではない管理された配布面」であることを示す用語である。

### manifest content の public-safe contract

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
